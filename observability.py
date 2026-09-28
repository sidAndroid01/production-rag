"""Structured logs, Prometheus metrics, and in-process rate limits.

Everything here is standard library. Limits and metrics are per process: with
several replicas, put the limiter in Redis and let Prometheus sum the series.
"""

from __future__ import annotations

import json
import logging
import math
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

# Attributes every LogRecord has; anything else came from ``extra=``.
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update(
            (key, value) for key, value in vars(record).items() if key not in _STANDARD_ATTRS
        )
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
Labels = tuple[tuple[str, str], ...]


class Metrics:
    """A minimal Prometheus registry: counters and one latency histogram."""

    def __init__(self) -> None:
        self._counters: dict[str, dict[Labels, float]] = defaultdict(lambda: defaultdict(float))
        self._help: dict[str, str] = {}
        self._histogram: dict[Labels, list[float]] = {}
        self._lock = threading.Lock()

    def inc(self, name: str, help_text: str, value: float = 1.0, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._help[name] = help_text
            self._counters[name][key] += value

    def observe_latency(self, seconds: float, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            # Per-bucket counts, then sum and count.
            series = self._histogram.setdefault(key, [0.0] * (len(LATENCY_BUCKETS) + 2))
            for index, bound in enumerate(LATENCY_BUCKETS):
                if seconds <= bound:
                    series[index] += 1
            series[-2] += seconds
            series[-1] += 1

    @staticmethod
    def _labels(pairs: Iterable[tuple[str, str]]) -> str:
        escaped = [
            f'{key}="{value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
            for key, value in pairs
        ]
        return "{" + ",".join(escaped) + "}" if escaped else ""

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name in sorted(self._counters):
                lines += [f"# HELP {name} {self._help[name]}", f"# TYPE {name} counter"]
                for key, value in sorted(self._counters[name].items()):
                    lines.append(f"{name}{self._labels(key)} {value:g}")
            name = "rag_http_request_duration_seconds"
            lines += [
                f"# HELP {name} HTTP request latency by route.",
                f"# TYPE {name} histogram",
            ]
            for key, series in sorted(self._histogram.items()):
                for index, bound in enumerate(LATENCY_BUCKETS):
                    labels = self._labels((*key, ("le", f"{bound:g}")))
                    lines.append(f"{name}_bucket{labels} {series[index]:g}")
                lines.append(f"{name}_bucket{self._labels((*key, ('le', '+Inf')))} {series[-1]:g}")
                lines.append(f"{name}_sum{self._labels(key)} {series[-2]:.6f}")
                lines.append(f"{name}_count{self._labels(key)} {series[-1]:g}")
        return "\n".join(lines) + "\n"


class RateLimiter:
    """Token bucket per key: ``rate_per_minute`` sustained, bursts up to ``burst``."""

    def __init__(
        self,
        rate_per_minute: float,
        burst: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate = rate_per_minute / 60.0
        self.capacity = burst if burst is not None else max(1.0, rate_per_minute)
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str) -> float:
        """Take one token; return 0 on success or the seconds to wait."""
        if self.rate <= 0:
            return 0.0
        now = self._clock()
        with self._lock:
            tokens, updated = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - updated) * self.rate)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                return 0.0
            self._buckets[key] = (tokens, now)
            return (1 - tokens) / self.rate


class DailyTokenBudget:
    """Per-tenant cap on model tokens per UTC day (0 disables the cap)."""

    def __init__(self, limit: int, today: Callable[[], str] | None = None) -> None:
        self.limit = limit
        self._today = today or (lambda: datetime.now(UTC).date().isoformat())
        self._used: dict[tuple[str, str], int] = defaultdict(int)
        self._lock = threading.Lock()

    def remaining(self, tenant_id: str) -> float:
        if self.limit <= 0:
            return math.inf
        with self._lock:
            return self.limit - self._used[(tenant_id, self._today())]

    def spend(self, tenant_id: str, tokens: int) -> None:
        if self.limit > 0:
            with self._lock:
                self._used[(tenant_id, self._today())] += tokens

    def seconds_until_reset(self) -> int:
        now = datetime.now(UTC)
        return 86_400 - (now.hour * 3600 + now.minute * 60 + now.second)
