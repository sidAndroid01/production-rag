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
