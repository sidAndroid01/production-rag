"""Model gateway: budgets, retries, circuit breakers, fallback, and usage.

A request tries each configured provider in order. Transient failures (timeouts,
connection errors, HTTP 429 and 5xx) are retried with jittered exponential
backoff; other failures move straight to the next provider. A provider that
keeps failing is skipped by its circuit breaker until a cool-down passes. The
free extractive answerer is always last, so the API degrades to a cited
extract instead of returning 500 when every model is down.
"""

from __future__ import annotations

import logging
import os
import random
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from urllib import error

from providers import (
    ChatTurn,
    ExtractiveProvider,
    Generation,
    ModelProvider,
    OpenAICompatibleProvider,
    estimate_tokens,
    heuristic_rewrite,
)

logger = logging.getLogger("rag.gateway")


class CircuitBreaker:
    """Closed → open after N consecutive failures → half-open after a cool-down."""

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_after: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.reset_after = reset_after
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_in_flight = False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state()

    def _state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self.reset_after:
            return "half-open"
        return "open"

    def allow(self) -> bool:
        with self._lock:
            state = self._state()
            if state == "closed":
                return True
            if state == "half-open" and not self._trial_in_flight:
                # Let exactly one request test whether the provider recovered.
                self._trial_in_flight = True
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._trial_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._trial_in_flight = False
            if self._opened_at is not None or self._failures >= self.failure_threshold:
                self._opened_at = self._clock()


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, error.URLError | TimeoutError | socket.timeout | ConnectionError)


def retry_after_seconds(exc: BaseException) -> float | None:
    if isinstance(exc, error.HTTPError) and exc.headers is not None:
        value = exc.headers.get("retry-after")
        if value and value.strip().isdigit():
            return float(value)
    return None


@dataclass(frozen=True, slots=True)
class Budget:
    """Token limits applied before a provider is called."""

    max_context_tokens: int = 3_000
    max_history_tokens: int = 1_000

    def pack_context(self, context: Sequence[str]) -> list[str]:
        """Keep sources in rank order while they fit; always keep the best one."""
        packed: list[str] = []
        used = 0
        for text in context:
            cost = estimate_tokens(text)
            if packed and used + cost > self.max_context_tokens:
                break
            if not packed and cost > self.max_context_tokens:
                # Truncate an oversized best source instead of dropping all evidence.
                text = text[: self.max_context_tokens * 4]
                cost = self.max_context_tokens
            packed.append(text)
            used += cost
        return packed

    def trim_history(self, history: Sequence[ChatTurn]) -> list[ChatTurn]:
        """Keep the most recent turns that fit the history budget."""
        kept: list[ChatTurn] = []
        used = 0
        for turn in reversed(history):
            cost = estimate_tokens(turn.content)
            if used + cost > self.max_history_tokens:
                break
            kept.append(turn)
            used += cost
        return list(reversed(kept))


@dataclass(frozen=True, slots=True)
class Pricing:
    input_per_million: float = 0.0
    output_per_million: float = 0.0

    def cost(self, generation: Generation) -> float:
        return (
            generation.prompt_tokens * self.input_per_million
            + generation.completion_tokens * self.output_per_million
        ) / 1_000_000


class GatewayProvider(ModelProvider):
    name = "gateway"

    def __init__(
        self,
        providers: Sequence[ModelProvider],
        *,
        budget: Budget | None = None,
        pricing: Mapping[str, Pricing] | None = None,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        max_delay: float = 8.0,
        deadline: float = 45.0,
        breakers: Mapping[str, CircuitBreaker] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.providers = list(providers)
        self.fallback = ExtractiveProvider()
        self.budget = budget or Budget()
        self.pricing = dict(pricing or {})
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.deadline = deadline
        self.breakers = dict(breakers or {})
        for provider in self.providers:
            self.breakers.setdefault(provider.name, CircuitBreaker(clock=clock))
        self._sleep = sleep
        self._clock = clock

    def generate(self, question: str, context: Sequence[str], history: Sequence[ChatTurn]) -> str:
        return self.complete(question, context, history).text

    def complete(
        self, question: str, context: Sequence[str], history: Sequence[ChatTurn]
    ) -> Generation:
        packed = self.budget.pack_context(context)
        trimmed = self.budget.trim_history(history)
        deadline = self._clock() + self.deadline
        for provider in self.providers:
            breaker = self.breakers[provider.name]
            if not breaker.allow():
                logger.warning("skipping %s: circuit %s", provider.name, breaker.state)
                continue
            generation = self._attempt(provider, breaker, question, packed, trimmed, deadline)
            if generation is not None:
                return self._priced(generation)
        logger.error("all model providers failed; answering extractively")
        return replace(self.fallback.complete(question, packed, trimmed), degraded=True)

    def _attempt(
        self,
        provider: ModelProvider,
        breaker: CircuitBreaker,
        question: str,
        context: list[str],
        history: list[ChatTurn],
        deadline: float,
    ) -> Generation | None:
        for attempt in range(1, self.max_attempts + 1):
            try:
                generation = provider.complete(question, context, history)
            except Exception as exc:
                breaker.record_failure()
                retryable = is_retryable(exc)
                logger.warning(
                    "provider %s attempt %d failed (%s, retryable=%s)",
                    provider.name,
                    attempt,
                    type(exc).__name__,
                    retryable,
                )
                if not retryable or attempt == self.max_attempts:
                    return None
                delay = retry_after_seconds(exc)
                if delay is None:
                    # Full jitter keeps many clients from retrying in lockstep.
                    delay = random.uniform(0, min(self.max_delay, self.base_delay * 2**attempt))
                if self._clock() + delay >= deadline or not breaker.allow():
                    return None
                self._sleep(delay)
            else:
                breaker.record_success()
                return generation
        return None

    def _priced(self, generation: Generation) -> Generation:
        pricing = self.pricing.get(generation.model) or self.pricing.get("*")
        if pricing is None:
            return generation
        return replace(generation, cost_usd=round(pricing.cost(generation), 6))

    def rewrite_query(self, question: str, history: Sequence[ChatTurn]) -> str:
        trimmed = self.budget.trim_history(history)
        for provider in self.providers:
            if self.breakers[provider.name].state == "closed":
                return provider.rewrite_query(question, trimmed)
        return heuristic_rewrite(question, trimmed)


def _model_from_env(prefix: str, environ: Mapping[str, str]) -> OpenAICompatibleProvider | None:
    base_url = environ.get(f"{prefix}_BASE_URL")
    api_key = environ.get(f"{prefix}_API_KEY") or (
        environ.get("OPENAI_API_KEY") if prefix == "RAG_MODEL" else None
    )
    model = environ.get(f"{prefix}_NAME")
    if not (base_url and api_key and model):
        return None
    return OpenAICompatibleProvider(
        base_url,
        api_key,
        model,
        timeout=float(environ.get("RAG_MODEL_TIMEOUT_SECONDS", "30")),
        max_output_tokens=int(environ.get("RAG_MAX_OUTPUT_TOKENS", "512")),
    )


def configured_provider(environ: Mapping[str, str] = os.environ) -> ModelProvider:
    """Build the gateway from RAG_MODEL_* and optional RAG_FALLBACK_MODEL_* settings.

    With no model configured, the free extractive answerer is used directly.
    """
    providers = [
        provider
        for provider in (
            _model_from_env("RAG_MODEL", environ),
            _model_from_env("RAG_FALLBACK_MODEL", environ),
        )
        if provider is not None
    ]
    if not providers:
        return ExtractiveProvider()
    price = Pricing(
        float(environ.get("RAG_MODEL_INPUT_PRICE_PER_MTOK", "0")),
        float(environ.get("RAG_MODEL_OUTPUT_PRICE_PER_MTOK", "0")),
    )
    return GatewayProvider(
        providers,
        budget=Budget(
            max_context_tokens=int(environ.get("RAG_MAX_CONTEXT_TOKENS", "3000")),
            max_history_tokens=int(environ.get("RAG_MAX_HISTORY_TOKENS", "1000")),
        ),
        pricing={"*": price},
        deadline=float(environ.get("RAG_GENERATION_DEADLINE_SECONDS", "45")),
    )
