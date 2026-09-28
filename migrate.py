"""Apply numbered SQL migrations as the database owner.

Each file in ``migrations/`` runs once, in filename order, inside its own
transaction, and is recorded in ``applied_migrations``. The application role
never runs DDL; this script is a deliberate, separate deployment step.

    RAG_OWNER_DATABASE_URL=postgresql://rag_owner:...@host/rag python migrate.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
APP_ROLE = "rag_app"


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    files = sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
    numbers = [path.name[:3] for path in files]
    if len(numbers) != len(set(numbers)):
        raise RuntimeError("migration numbers must be unique")
    return files


def apply_migrations(connection: Any, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations on an open psycopg connection; return their names."""
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS applied_migrations (
                name TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        # Serialize concurrent deploys; the lock is released at commit.
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('rag_migrations'))")
        cursor.execute("SELECT name FROM applied_migrations")
        applied = {row[0] for row in cursor.fetchall()}
    newly_applied: list[str] = []
    for path in migration_files(directory):
        if path.name in applied:
            continue
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(path.read_text(encoding="utf-8"))
            cursor.execute("INSERT INTO applied_migrations (name) VALUES (%s)", (path.name,))
        newly_applied.append(path.name)
    return newly_applied


def set_app_password(connection: Any, password: str) -> None:
    from psycopg import sql

    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(APP_ROLE), sql.Literal(password)
            )
        )


def main() -> int:
    dsn = os.getenv("RAG_OWNER_DATABASE_URL")
    if not dsn:
        print("RAG_OWNER_DATABASE_URL is required", file=sys.stderr)
        return 2
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        applied = apply_migrations(connection)
        password = os.getenv("RAG_APP_DB_PASSWORD")
        if password:
            set_app_password(connection, password)
    print(f"applied {len(applied)} migration(s): {', '.join(applied) or 'none pending'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
