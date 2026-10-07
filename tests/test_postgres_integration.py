"""Repository behavior against real PostgreSQL as the restricted app role.

Set RAG_RLS_TEST_DSN (CI does) after running migrate.py.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest

from embeddings import DIMENSIONS
from persistence import PostgresPersistence
from rag import RagIngestionPipeline

pytest.importorskip("psycopg")
APP_DSN = os.getenv("RAG_RLS_TEST_DSN")
pytestmark = pytest.mark.skipif(not APP_DSN, reason="set RAG_RLS_TEST_DSN")


def vectors(count: int) -> list[list[float]]:
    return [[1.0] + [0.0] * (DIMENSIONS - 1) for _ in range(count)]


@pytest.fixture
def store() -> PostgresPersistence:
    assert APP_DSN is not None
    return PostgresPersistence(APP_DSN)


@pytest.fixture
def tenant(store: PostgresPersistence) -> Iterator[str]:
    tenant_id = f"test-{uuid4().hex[:12]}"
    yield tenant_id
    for row in store.list_documents(tenant_id, ["hr"]):
        store.delete_document(tenant_id, row["document_id"], ["hr"])


def ingest(store: PostgresPersistence, tenant_id: str, text: str, name: str = "a.txt") -> str:
    result = RagIngestionPipeline().ingest(text.encode(), name, tenant_id)
    return store.persist(result, vectors(len(result.chunks))).document_id


def test_document_lifecycle_is_tenant_scoped(store: PostgresPersistence, tenant: str) -> None:
    document_id = ingest(store, tenant, "Refunds take thirty days.")
    assert [row["document_id"] for row in store.list_documents(tenant)] == [document_id]
    assert store.get_document(tenant, document_id) is not None
    other = f"{tenant}-other"
    assert store.list_documents(other) == []
    assert store.get_document(other, document_id) is None
    assert store.delete_document(other, document_id) is False
    assert store.delete_document(tenant, document_id) is True
    assert store.list_chunks(tenant) == []


def test_identical_upload_is_idempotent(store: PostgresPersistence, tenant: str) -> None:
    first = RagIngestionPipeline().ingest(b"Same bytes.", "a.txt", tenant)
    assert store.persist(first, vectors(1)).already_existed is False
    again = store.persist(first, vectors(1))
    assert again.already_existed is True and again.chunks_written == 0
    assert len(store.list_chunks(tenant)) == 1


def test_hybrid_search_returns_only_the_tenants_rows(
    store: PostgresPersistence, tenant: str
) -> None:
    ingest(store, tenant, "The refund window is thirty days.")
    other = f"{tenant}-other"
    try:
        ingest(store, other, "The refund window is ninety days.")
        rows = store.search_hybrid(tenant, "refund window", vectors(1)[0], limit=5)
        assert rows and all(row["tenant_id"] == tenant for row in rows)
    finally:
        for row in store.list_documents(other):
            store.delete_document(other, row["document_id"])


def test_acl_is_enforced_in_sql_before_top_k(store: PostgresPersistence, tenant: str) -> None:
    public = RagIngestionPipeline().ingest(b"Public refund policy text.", "public.txt", tenant)
    store.persist(public, vectors(1))
    for index in range(3):
        secret = RagIngestionPipeline().ingest(
            f"Restricted refund memo {index}.".encode(), f"hr-{index}.txt", tenant
        )
        store.persist(secret, vectors(1), allowed_groups=["hr"])
    # limit=1: restricted rows must not take the only slot from the caller.
    outsider = store.search_hybrid(tenant, "refund", vectors(1)[0], limit=1, candidate_limit=1)
    assert [row["source"] for row in outsider] == ["public.txt"]
    insider = store.search_hybrid(tenant, "refund", vectors(1)[0], limit=4, groups=["hr"])
    assert len(insider) == 4
    assert {row["source"] for row in store.list_documents(tenant)} == {"public.txt"}
    hr_id = next(
        row["document_id"] for row in store.list_documents(tenant, ["hr"]) if row["allowed_groups"]
    )
    assert store.get_document(tenant, hr_id) is None
    assert store.delete_document(tenant, hr_id) is False
    assert store.delete_document(tenant, hr_id, ["hr"]) is True


def test_reupload_supersedes_and_hides_the_old_version(
    store: PostgresPersistence, tenant: str
) -> None:
    first = RagIngestionPipeline().ingest(b"Doors open at nine.", "hours.txt", tenant)
    second = RagIngestionPipeline().ingest(b"Doors open at ten.", "hours.txt", tenant)
    v1 = store.persist(first, vectors(1))
    v2 = store.persist(second, vectors(1))
    assert (v1.version, v2.version, v2.superseded) == (1, 2, (v1.document_id,))
    rows = store.search_hybrid(tenant, "doors open", vectors(1)[0], limit=5)
    assert [row["document_id"] for row in rows] == [v2.document_id]
    restored = store.persist(first, vectors(1))
    assert restored.already_existed and restored.version == 3
    assert restored.superseded == (v2.document_id,)
    assert len(store.list_documents(tenant, include_superseded=True)) == 2


def test_job_queue_processes_retries_and_isolates_tenants(
    store: PostgresPersistence, tenant: str
) -> None:
    from workers import PostgresJobQueue

    attempts: list[str] = []

    def handler(payload: dict[str, object]) -> dict[str, object]:
        attempts.append(str(payload["kind"]))
        if payload["kind"] == "bad-input":
            raise ValueError("document has no text to index")
        if payload["kind"] == "flaky" and attempts.count("flaky") == 1:
            raise RuntimeError("embedding service hiccup")
        return {"ok": payload["kind"]}

    queue = PostgresJobQueue(store, handler, start=False)
    good = queue.submit(tenant, {"kind": "good", "content_base64": "eA=="})
    bad = queue.submit(tenant, {"kind": "bad-input"})
    flaky = queue.submit(tenant, {"kind": "flaky"})
    while queue.run_once():
        pass
    assert queue.get(tenant, good.id).as_dict()["result"] == {"ok": "good"}  # type: ignore[union-attr]
    failed = queue.get(tenant, bad.id)
    assert failed is not None and failed.status == "failed" and "no text" in (failed.error or "")
    retrying = queue.get(tenant, flaky.id)
    assert retrying is not None and retrying.status == "queued"  # backing off
    assert queue.get(f"{tenant}-other", good.id) is None
    assert queue.get(tenant, "not-a-uuid") is None

    with store._connect() as connection, connection.cursor() as cursor:
        store._set_tenant(cursor, tenant)
        cursor.execute(
            "SELECT payload ? 'content_base64' FROM ingestion_jobs WHERE id = %s", (good.id,)
        )
        assert cursor.fetchone()[0] is False  # uploaded bytes dropped after completion
        # Simulate the backoff elapsing, then a crash mid-run on a second job.
        cursor.execute("UPDATE ingestion_jobs SET run_after = now() WHERE id = %s", (flaky.id,))
    assert queue.run_once()
    assert queue.get(tenant, flaky.id).status == "completed"  # type: ignore[union-attr]

    stuck = queue.submit(tenant, {"kind": "good"})
    with store._connect() as connection, connection.cursor() as cursor:
        store._set_tenant(cursor, tenant)
        cursor.execute(
            "UPDATE ingestion_jobs SET status = 'running', locked_at = now() - interval '1 hour' "
            "WHERE id = %s",
            (stuck.id,),
        )
    assert queue.run_once()
    assert queue.get(tenant, stuck.id).status == "completed"  # type: ignore[union-attr]


def test_source_identity_acl_inheritance_and_updates(
    store: PostgresPersistence, tenant: str
) -> None:
    from persistence import SourceRef

    def ingest_as(text: str, name: str, source_id: str, **options: object) -> object:
        result = RagIngestionPipeline().ingest_segments(
            text.encode(), [(None, text)], name, tenant, identity=f"upload/{source_id}"
        )
        return store.persist(
            result,
            vectors(len(result.chunks)),
            source=SourceRef(id=source_id),
            groups=["hr"],  # uploading as an HR member, as the API would
            **options,
        )

    v1 = ingest_as("Leave is 15 days.", "handbook.txt", "handbook", allowed_groups=["hr"])
    v2 = ingest_as("Leave is 20 days.", "handbook-2026.txt", "handbook", allowed_groups=None)
    assert v2.version == 2 and v2.superseded == (v1.document_id,)  # type: ignore[attr-defined]
    current = store.current_version(tenant, SourceRef(id="handbook"), ["hr"])
    assert current is not None and current["source"] == "handbook-2026.txt"
    assert current["allowed_groups"] == ["hr"]  # inherited, not reset
    assert store.current_version(tenant, SourceRef(id="handbook")) is None  # hidden

    # Same bytes as v2 under another identity is a separate document.
    copy = ingest_as("Leave is 20 days.", "copy.txt", "elsewhere/handbook", allowed_groups=[])
    assert copy.document_id != v2.document_id  # type: ignore[attr-defined]

    found, row = store.update_document(
        tenant,
        v1.document_id,
        ["hr"],
        allowed_groups=[],
        display_name="Handbook",  # type: ignore[attr-defined]
    )
    assert found and row is not None and row["source"] == "Handbook"
    everyone = store.list_documents(tenant, include_superseded=True)
    assert {d["document_id"] for d in everyone} >= {v1.document_id, v2.document_id}  # type: ignore[attr-defined]
    assert store.update_document(tenant, "missing", allowed_groups=[]) == (False, None)
    found, row = store.update_document(tenant, v2.document_id, allowed_groups=["finance"])  # type: ignore[attr-defined]
    assert found and row is None  # caller removed their own access


def test_migration_backfilled_source_ids_for_old_documents(store: PostgresPersistence) -> None:
    with store._connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_name = 'documents' "
            "AND column_name IN ('source_system', 'source_id', 'source_version', "
            "'source_modified_at', 'last_checked_at')"
        )
        assert cursor.fetchone()[0] == 5
