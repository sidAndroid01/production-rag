from __future__ import annotations

import json
from typing import Any

import pytest

from api import ApiError, RagApiApplication
from auth import ApiKeyStore, hash_key
from permissions import Principal

KEYS = ApiKeyStore(
    {
        hash_key("alice"): Principal("acme", "alice", frozenset({"hr"})),
        hash_key("bob"): Principal("acme", "bob"),
    }
)


def call(
    app: RagApiApplication,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    key: str = "alice",
) -> tuple[int, dict[str, Any]]:
    body = b"" if payload is None else json.dumps(payload).encode()
    try:
        return app.handle(method, path, {"x-api-key": key}, body)
    except ApiError as exc:
        return int(exc.status), {"detail": exc.message}


def upload(app: RagApiApplication, key: str = "alice", **payload: Any) -> dict[str, Any]:
    status, body = call(app, "POST", "/v1/documents", payload, key=key)
    assert status in {200, 201}, body
    return body


def listing(app: RagApiApplication, key: str = "alice", old: bool = False) -> list[dict[str, Any]]:
    query = "?include_superseded=true" if old else ""
    return call(app, "GET", f"/v1/documents{query}", key=key)[1]["documents"]


@pytest.fixture
def app() -> RagApiApplication:
    return RagApiApplication(keys=KEYS)


# -- A: stable source identity -----------------------------------------------


def test_rename_with_the_same_source_id_is_a_new_version_not_a_new_document(
    app: RagApiApplication,
) -> None:
    v1 = upload(app, filename="handbook.txt", source_id="handbook", content="Leave is 15 days.")
    v2 = upload(
        app, filename="handbook-2026.txt", source_id="handbook", content="Leave is 20 days."
    )
    assert (v1["version"], v2["version"], v2["superseded"]) == (1, 2, [v1["document_id"]])
    current = listing(app)
    assert [(d["source"], d["source_id"], d["version"]) for d in current] == [
        ("handbook-2026.txt", "handbook", 2)
    ]
    _, answer = call(app, "POST", "/v1/query", {"question": "How many days of leave?"})
    assert "20" in answer["answer"] and "15" not in answer["answer"]
    assert answer["citations"][0]["source"] == "handbook-2026.txt"


def test_filename_is_the_default_identity(app: RagApiApplication) -> None:
    first = upload(app, filename="notes.txt", content="Version one.")
    second = upload(app, filename="notes.txt", content="Version two.")
    assert first["source_id"] == second["source_id"] == "notes.txt"
    assert second["version"] == 2


def test_unchanged_source_version_skips_all_work(app: RagApiApplication) -> None:
    upload(app, filename="a.txt", source_id="doc-1", source_version="etag-1", content="Alpha.")
    again = upload(
        app, filename="a.txt", source_id="doc-1", source_version="etag-1", content="Beta."
    )
    # The source says nothing changed, so the bytes are not even parsed.
    assert again["unchanged"] is True and again["version"] == 1
    newer = upload(
        app, filename="a.txt", source_id="doc-1", source_version="etag-2", content="Beta."
    )
    assert newer["unchanged"] is False and newer["version"] == 2


def test_identical_bytes_refresh_freshness_and_display_name(app: RagApiApplication) -> None:
    first = upload(app, filename="faq.txt", source_id="faq", content="Shipping is free.")
    checked = listing(app)[0]["last_checked_at"]
    again = upload(app, filename="faq-renamed.txt", source_id="faq", content="Shipping is free.")
    assert again["unchanged"] and again["document_id"] == first["document_id"]
    document = listing(app)[0]
    assert document["source"] == "faq-renamed.txt" and document["last_checked_at"] >= checked


def test_same_bytes_under_two_identities_are_two_documents(app: RagApiApplication) -> None:
    one = upload(app, filename="a.txt", source_id="folder-a/policy", content="Same text.")
    two = upload(app, filename="a.txt", source_id="folder-b/policy", content="Same text.")
    assert one["document_id"] != two["document_id"]
    assert len(listing(app)) == 2


def test_source_fields_are_validated(app: RagApiApplication) -> None:
    for bad in (
        {"source_system": "Google Drive"},
        {"source_id": ""},
        {"source_version": 7},
        {"source_modified_at": "yesterday"},
    ):
        status, _ = call(app, "POST", "/v1/documents", {"filename": "x.txt", "content": "x", **bad})
        assert status == 422, bad
    created = upload(
        app,
        filename="x.txt",
        content="x",
        source_system="gdrive",
        source_id="1AbC",
        source_modified_at="2026-10-01T09:00:00+00:00",
    )
    document = listing(app)[0]
    assert (created["source_system"], document["source_modified_at"]) == (
        "gdrive",
        "2026-10-01T09:00:00+00:00",
    )


# -- C: permission updates -----------------------------------------------------


def test_new_versions_inherit_the_acl_unless_told_otherwise(app: RagApiApplication) -> None:
    upload(app, filename="pay.txt", content="Raises in March.", allowed_groups=["hr"])
    upload(app, filename="pay.txt", content="Raises in April.")
    assert listing(app)[0]["allowed_groups"] == ["hr"]
    assert listing(app, key="bob") == []
    upload(app, filename="pay.txt", content="Raises in May.", allowed_groups=[])
    assert [d["version"] for d in listing(app, key="bob")] == [3]


def test_patch_changes_acl_on_every_version_without_reupload(app: RagApiApplication) -> None:
    upload(app, filename="plan.txt", content="Plan one.", allowed_groups=["hr"])
    v2 = upload(app, filename="plan.txt", content="Plan two.")
    assert listing(app, key="bob") == []
    status, body = call(app, "PATCH", f"/v1/documents/{v2['document_id']}", {"allowed_groups": []})
    assert status == 200 and body["allowed_groups"] == [] and body["visible"]
    assert [d["version"] for d in listing(app, key="bob", old=True)] == [2, 1]
    _, answer = call(app, "POST", "/v1/query", {"question": "What is the plan?"}, key="bob")
    assert answer["grounded"]


def test_patch_can_restrict_and_rename(app: RagApiApplication) -> None:
    created = upload(app, filename="memo.txt", content="Office closes at five.")
    path = f"/v1/documents/{created['document_id']}"
    status, body = call(app, "PATCH", path, {"allowed_groups": ["hr"], "filename": "memo-v2.txt"})
    assert status == 200 and body["source"] == "memo-v2.txt"
    assert listing(app, key="bob") == []
    _, answer = call(app, "POST", "/v1/query", {"question": "When does the office close?"})
    assert answer["citations"][0]["source"] == "memo-v2.txt"


def test_patch_rules(app: RagApiApplication) -> None:
    hidden = upload(app, filename="hr.txt", content="Confidential.", allowed_groups=["hr"])
    path = f"/v1/documents/{hidden['document_id']}"
    assert call(app, "PATCH", path, {"allowed_groups": []}, key="bob")[0] == 404
    assert call(app, "PATCH", path, {})[0] == 422
    assert call(app, "PATCH", path, {"allowed_groups": "hr"})[0] == 422
    assert call(app, "PATCH", path, {"filename": "  "})[0] == 422
    assert call(app, "PATCH", "/v1/documents/missing", {"allowed_groups": []})[0] == 404
    # Restricting to a group the caller is not in succeeds but hides the document.
    status, body = call(app, "PATCH", path, {"allowed_groups": ["finance"]})
    assert status == 200 and body == {**body, "updated": True, "visible": False}
    assert listing(app) == []
