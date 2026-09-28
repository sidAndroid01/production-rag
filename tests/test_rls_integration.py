"""Requires a running PostgreSQL instance and the restricted app DSN."""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")
APP_DSN = os.getenv("RAG_RLS_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not APP_DSN,
    reason="set RAG_RLS_TEST_DSN to run PostgreSQL RLS integration tests",
)


def test_non_superuser_without_tenant_context_sees_zero_rows() -> None:
    assert APP_DSN is not None
    with psycopg.connect(APP_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        is_superuser, bypasses_rls = cursor.fetchone()
        assert is_superuser is False
        assert bypasses_rls is False
        cursor.execute("SELECT count(*) FROM documents")
        assert cursor.fetchone()[0] == 0
