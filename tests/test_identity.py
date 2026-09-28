from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from api import ApiError, RagApiApplication
from auth import DEVELOPMENT_KEY, ApiKeyStore, hash_key
from permissions import Principal

KEYS = ApiKeyStore(
    {
        hash_key("alice-key"): Principal("acme", "alice", frozenset({"hr"})),
        hash_key("bob-key"): Principal("acme", "bob"),
        hash_key("eve-key"): Principal("globex", "eve", frozenset({"hr"})),
    }
)
HANDBOOK = {"filename": "handbook.txt", "content": "The office opens at nine every weekday."}
SALARIES = {
    "filename": "salaries.txt",
    "content": "The salary review happens every March for all staff.",
    "allowed_groups": ["hr"],
}


def call(
    app: RagApiApplication, key: str, method: str, path: str, payload: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    body = b"" if payload is None else json.dumps(payload).encode()
    try:
        return app.handle(method, path, {"x-api-key": key}, body)
    except ApiError as exc:
        return int(exc.status), {"detail": exc.message}


@pytest.fixture
def app() -> RagApiApplication:
    app = RagApiApplication(keys=KEYS)
    assert call(app, "alice-key", "POST", "/v1/documents", HANDBOOK)[0] == 201
    assert call(app, "alice-key", "POST", "/v1/documents", SALARIES)[0] == 201
    return app


def sources(app: RagApiApplication, key: str) -> set[str]:
    _, listing = call(app, key, "GET", "/v1/documents")
    return {row["source"] for row in listing.get("documents", [])}


def test_tenant_comes_from_the_key_not_the_body(app: RagApiApplication) -> None:
    status, body = call(app, "eve-key", "GET", "/v1/documents", {"tenant_id": "acme"})
    assert status == 403
    assert body["detail"] == "tenant_id does not match authenticated identity"
    assert sources(app, "eve-key") == set()


def test_group_acl_limits_listing_and_answers(app: RagApiApplication) -> None:
    assert sources(app, "alice-key") == {"handbook.txt", "salaries.txt"}
    assert sources(app, "bob-key") == {"handbook.txt"}
    question = {"question": "When is the salary review?"}
    _, alice = call(app, "alice-key", "POST", "/v1/query", question)
    assert alice["grounded"] and alice["citations"][0]["source"] == "salaries.txt"
    # Claiming a group in the body has no effect.
    _, bob = call(app, "bob-key", "POST", "/v1/query", {**question, "groups": ["hr"]})
    assert all(c["source"] != "salaries.txt" for c in bob["citations"])


def test_restricted_document_is_hidden_from_get_and_delete(app: RagApiApplication) -> None:
    _, listing = call(app, "alice-key", "GET", "/v1/documents")
    salaries_id = next(r["document_id"] for r in listing["documents"] if r["allowed_groups"])
    assert call(app, "bob-key", "GET", f"/v1/documents/{salaries_id}")[0] == 404
    assert call(app, "bob-key", "DELETE", f"/v1/documents/{salaries_id}")[0] == 404
    assert call(app, "alice-key", "DELETE", f"/v1/documents/{salaries_id}")[0] == 200


def test_invalid_acl_is_rejected(app: RagApiApplication) -> None:
    status, _ = call(
        app, "alice-key", "POST", "/v1/documents", {**HANDBOOK, "allowed_groups": "hr"}
    )
    assert status == 422


def test_unknown_key_is_rejected(app: RagApiApplication) -> None:
    assert call(app, "wrong", "GET", "/v1/documents")[0] == 401


def test_key_file_is_loaded_and_validated(tmp_path: Path) -> None:
    path = tmp_path / "keys.json"
    entry = {"key_sha256": hash_key("k1"), "tenant_id": "t", "user_id": "u", "groups": ["g"]}
    path.write_text(json.dumps([entry]))
    store = ApiKeyStore.from_environment({"RAG_API_KEYS_FILE": str(path)})
    assert store.authenticate("k1") == Principal("t", "u", frozenset({"g"}))
    assert store.authenticate("k2") is None
    with pytest.raises(ValueError, match="hex digest"):
        ApiKeyStore.from_entries([{**entry, "key_sha256": "plain-text-key"}])
    with pytest.raises(ValueError, match="duplicate"):
        ApiKeyStore.from_entries([entry, entry])


def test_production_refuses_the_development_key() -> None:
    with pytest.raises(RuntimeError):
        ApiKeyStore.from_environment({"APP_ENV": "production"})
    store = ApiKeyStore.from_environment({"RAG_API_KEY": "real", "RAG_TENANT_ID": "acme"})
    assert store.authenticate("real") == Principal("acme", "api")
    assert ApiKeyStore.from_environment({}).authenticate(DEVELOPMENT_KEY) is not None
