"""Minimal background ingestion worker; replace with a durable queue later."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class IngestionJob:
    id: str
    payload: dict[str, str]


class IngestionWorker:
    def __init__(self, handler: Callable[[dict[str, str]], None]) -> None:
        self._handler = handler
        self._queue: queue.Queue[IngestionJob | None] = queue.Queue()
        self._status: dict[str, str] = {}
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True, name="rag-ingestion")
        self._thread.start()

    def submit(self, payload: dict[str, str]) -> IngestionJob:
        job = IngestionJob(str(uuid4()), payload)
        with self._lock:
            self._status[job.id] = "queued"
        self._queue.put(job)
        return job

    def status(self, job_id: str) -> str | None:
        with self._lock:
            return self._status.get(job_id)

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            with self._lock:
                self._status[job.id] = "running"
            try:
                self._handler(job.payload)
            except Exception:
                with self._lock:
                    self._status[job.id] = "failed"
            else:
                with self._lock:
                    self._status[job.id] = "completed"
