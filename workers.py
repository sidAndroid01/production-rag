"""Minimal background ingestion worker; Phase 17 adds a durable queue."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

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
