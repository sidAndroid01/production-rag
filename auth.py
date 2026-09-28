"""Map API keys to principals on the server.

Clients present only a key. The tenant, user, and groups come from server-side
configuration, so a caller cannot choose another tenant or claim a group by
editing the request body. Keys are stored as SHA-256 digests, never in plain
text.

Configure with ``RAG_API_KEYS_FILE`` pointing at a JSON list::

    [{"name": "alice-laptop", "key_sha256": "<hex>", "tenant_id": "acme",
      "user_id": "alice", "groups": ["hr"]}]

Create entries with ``python scripts/create_api_key.py``. Without a key file,
development mode accepts a single ``RAG_API_KEY`` bound to ``RAG_TENANT_ID``.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from permissions import Principal

DEVELOPMENT_KEY = "local-development-key"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class ApiKeyStore:
    def __init__(self, principals_by_hash: Mapping[str, Principal]) -> None:
        if not principals_by_hash:
            raise ValueError("at least one API key must be configured")
        self._principals = dict(principals_by_hash)

    @classmethod
    def single(
        cls, key: str, tenant_id: str, user_id: str = "api", groups: frozenset[str] = frozenset()
    ) -> ApiKeyStore:
        return cls({hash_key(key): Principal(tenant_id, user_id, groups)})

    @classmethod
    def from_entries(cls, entries: Any) -> ApiKeyStore:
        if not isinstance(entries, list):
            raise ValueError("API key configuration must be a JSON list")
        principals: dict[str, Principal] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("each API key entry must be an object")
            digest = str(entry.get("key_sha256", "")).lower()
            tenant_id = str(entry.get("tenant_id", "")).strip()
            user_id = str(entry.get("user_id", "")).strip()
            groups = entry.get("groups", [])
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("key_sha256 must be a 64-character hex digest")
            if not tenant_id or not user_id:
                raise ValueError("tenant_id and user_id are required for every key")
            if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
                raise ValueError("groups must be a list of strings")
            if digest in principals:
                raise ValueError("duplicate API key digest")
            principals[digest] = Principal(tenant_id, user_id, frozenset(groups))
        return cls(principals)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] = os.environ) -> ApiKeyStore:
        production = environ.get("APP_ENV", "development") == "production"
        path = environ.get("RAG_API_KEYS_FILE")
        if path:
            return cls.from_entries(json.loads(Path(path).read_text(encoding="utf-8")))
        key = environ.get("RAG_API_KEY") or DEVELOPMENT_KEY
        if production and key == DEVELOPMENT_KEY:
            raise RuntimeError("set RAG_API_KEYS_FILE or a non-default RAG_API_KEY in production")
        tenant_id = (environ.get("RAG_TENANT_ID") or "default").strip()
        return cls.single(key, tenant_id)

    def authenticate(self, presented: str | None) -> Principal | None:
        if not presented:
            return None
        # Lookup by digest: the raw key is never compared or stored.
        return self._principals.get(hash_key(presented))
