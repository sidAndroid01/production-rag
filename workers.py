"""Background ingestion: an in-process worker for the in-memory demo and a
durable PostgreSQL queue for deployments."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger("rag.worker")


@dataclass(frozen=True, slots=True)
class IngestionJob:
    id: str
    tenant_id: str
    status: str = "queued"
    error: str | None = None
    result: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "status": self.status,
            "error": self.error,
            "result": self.result,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class IngestionWorker:
    def __init__(self, handler: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._handler = handler
        self._queue: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue()
        self._jobs: dict[str, IngestionJob] = {}
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True, name="rag-ingestion")
        self._thread.start()

    def submit(self, tenant_id: str, payload: dict[str, Any]) -> IngestionJob:
        job = IngestionJob(str(uuid4()), tenant_id)
        with self._lock:
            self._jobs[job.id] = job
        self._queue.put((job.id, payload))
        return job

    def get(self, tenant_id: str, job_id: str) -> IngestionJob | None:
        """Return the job only to the tenant that submitted it."""
        with self._lock:
            job = self._jobs.get(job_id)
        return job if job is not None and job.tenant_id == tenant_id else None

    def status(self, job_id: str) -> str | None:
        with self._lock:
            job = self._jobs.get(job_id)
        return job.status if job else None

    def stop(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=5)

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            self._jobs[job_id] = replace(
                self._jobs[job_id], updated_at=datetime.now(UTC), **changes
            )

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            job_id, payload = item
            self._update(job_id, status="running")
            try:
                result = self._handler(payload)
            except (UnicodeError, ValueError) as exc:
                logger.warning("ingestion job %s rejected: %s", job_id, exc)
                self._update(job_id, status="failed", error=str(exc))
            except Exception:
                # Unexpected failures are logged in full but not echoed to the
                # caller, which may expose internals.
                logger.exception("ingestion job %s failed", job_id)
                self._update(job_id, status="failed", error="internal error during ingestion")
            else:
                self._update(job_id, status="completed", result=result)


class PostgresJobQueue:
    """Durable ingestion queue in PostgreSQL; survives restarts, safe across replicas.

    Workers claim jobs through the owner-defined ``claim_ingestion_job()`` (FOR
    UPDATE SKIP LOCKED), then read and write each job under its tenant's RLS
    context. Input errors fail immediately; other errors retry with backoff
    until ``max_attempts``. A job abandoned by a crashed worker is reclaimed.
    """

    COLUMNS = "id, tenant_id, status, error, result, created_at, updated_at"

    def __init__(
        self,
        persistence: Any,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        poll_interval: float = 1.0,
        start: bool = True,
    ) -> None:
        self._persistence = persistence
        self._handler = handler
        self._poll_interval = poll_interval
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="rag-ingestion-pg")
        if start:
            self._thread.start()

    @staticmethod
    def _job(row: Sequence[Any]) -> IngestionJob:
        job_id, tenant_id, status, error_text, result, created_at, updated_at = row
        return IngestionJob(
            str(job_id), tenant_id, status, error_text, result, created_at, updated_at
        )

    def submit(self, tenant_id: str, payload: dict[str, Any]) -> IngestionJob:
        from psycopg.types.json import Jsonb

        with self._persistence._connect() as connection, connection.cursor() as cursor:
            self._persistence._set_tenant(cursor, tenant_id)
            cursor.execute(
                f"INSERT INTO ingestion_jobs (id, tenant_id, payload) VALUES (%s, %s, %s) "
                f"RETURNING {self.COLUMNS}",
                (str(uuid4()), tenant_id, Jsonb(payload)),
            )
            return self._job(cursor.fetchone())

    def get(self, tenant_id: str, job_id: str) -> IngestionJob | None:
        try:
            UUID(job_id)
        except ValueError:
            return None
        with self._persistence._connect() as connection, connection.cursor() as cursor:
            self._persistence._set_tenant(cursor, tenant_id)
            cursor.execute(
                f"SELECT {self.COLUMNS} FROM ingestion_jobs WHERE tenant_id = %s AND id = %s",
                (tenant_id, job_id),
            )
            row = cursor.fetchone()
        return self._job(row) if row else None

    def stop(self) -> None:
        self._stopping.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def run_once(self) -> bool:
        """Claim and process one job; return False when the queue is empty."""
        with self._persistence._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM claim_ingestion_job()")
            claimed = cursor.fetchone()
        if claimed is None:
            return False
        job_id, tenant_id, payload, attempts, max_attempts = claimed
        try:
            result = self._handler(payload)
        except (UnicodeError, ValueError) as exc:
            logger.warning("ingestion job %s rejected: %s", job_id, exc)
            self._finish(tenant_id, job_id, "failed", error_text=str(exc))
        except Exception:
            logger.exception("ingestion job %s attempt %d failed", job_id, attempts)
            if attempts < max_attempts:
                self._finish(
                    tenant_id,
                    job_id,
                    "queued",
                    error_text="retrying after an internal error",
                    retry_in=2.0**attempts,
                )
            else:
                self._finish(
                    tenant_id, job_id, "failed", error_text="internal error during ingestion"
                )
        else:
            self._finish(tenant_id, job_id, "completed", result=result)
        return True

    def _finish(
        self,
        tenant_id: str,
        job_id: Any,
        status: str,
        *,
        error_text: str | None = None,
        result: dict[str, Any] | None = None,
        retry_in: float = 0.0,
    ) -> None:
        from psycopg.types.json import Jsonb

        done = status in {"completed", "failed"}
        with self._persistence._connect() as connection, connection.cursor() as cursor:
            self._persistence._set_tenant(cursor, tenant_id)
            cursor.execute(
                """
                UPDATE ingestion_jobs
                SET status = %s, error = %s, result = %s, locked_at = NULL,
                    run_after = now() + make_interval(secs => %s), updated_at = now(),
                    -- Drop the uploaded bytes once the job can no longer run.
                    payload = CASE WHEN %s THEN payload - 'content_base64' ELSE payload END
                WHERE tenant_id = %s AND id = %s
                """,
                (
                    status,
                    error_text,
                    Jsonb(result) if result is not None else None,
                    retry_in,
                    done,
                    tenant_id,
                    job_id,
                ),
            )

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                busy = self.run_once()
            except Exception:
                logger.exception("ingestion worker could not reach the queue")
                busy = False
            if not busy:
                self._stopping.wait(self._poll_interval)
