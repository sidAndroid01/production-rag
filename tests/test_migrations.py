from __future__ import annotations

import os
from pathlib import Path

import pytest

from migrate import apply_migrations, migration_files


def test_migrations_are_ordered_and_unique() -> None:
    names = [path.name for path in migration_files()]
    assert names == sorted(names) and names[0].startswith("001_")


def test_duplicate_numbers_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "001_a.sql").write_text("")
    (tmp_path / "001_b.sql").write_text("")
    with pytest.raises(RuntimeError, match="unique"):
        migration_files(tmp_path)


@pytest.mark.skipif(not os.getenv("RAG_OWNER_DATABASE_URL"), reason="needs owner DSN")
def test_migrations_are_idempotent() -> None:
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(os.environ["RAG_OWNER_DATABASE_URL"], autocommit=True) as connection:
        apply_migrations(connection)
        assert apply_migrations(connection) == []
        with connection.cursor() as cursor:
            cursor.execute("SELECT name FROM applied_migrations ORDER BY name")
            applied = [row[0] for row in cursor.fetchall()]
    assert applied == [path.name for path in migration_files()]
