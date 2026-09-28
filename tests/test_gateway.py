from __future__ import annotations

import json
from collections.abc import Sequence
from email.message import Message
from urllib import error

from api import RagApiApplication
from gateway import Budget, CircuitBreaker, GatewayProvider, Pricing, configured_provider
from providers import ChatTurn, ExtractiveProvider, Generation, ModelProvider


class FlakyProvider(ModelProvider):
    def __init__(self, name: str, failures: list[BaseException]) -> None:
        self.name = name
        self.failures = list(failures)
        self.calls = 0
        self.contexts: list[list[str]] = []

    def complete(
        self, question: str, context: Sequence[str], history: Sequence[ChatTurn]
    ) -> Generation:
        self.calls += 1
        self.contexts.append(list(context))
        if self.failures:
            raise self.failures.pop(0)
        return Generation(f"answer from {self.name} [1]", self.name, 100, 20)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def http_error(code: int, retry_after: str | None = None) -> error.HTTPError:
    headers = Message()
    if retry_after:
        headers["retry-after"] = retry_after
    return error.HTTPError("http://model", code, "error", headers, None)


def gateway(*providers: ModelProvider, clock: FakeClock | None = None) -> GatewayProvider:
    clock = clock or FakeClock()
    return GatewayProvider(providers, sleep=clock.sleep, clock=clock, base_delay=0.1)


def test_transient_errors_are_retried_with_backoff() -> None:
    clock = FakeClock()
    primary = FlakyProvider("primary", [error.URLError("reset"), http_error(503)])
    result = gateway(primary, clock=clock).complete("q", ["ctx"], ())
    assert result.text == "answer from primary [1]" and not result.degraded
    assert primary.calls == 3 and len(clock.sleeps) == 2


def test_retry_after_header_is_honored() -> None:
    clock = FakeClock()
    primary = FlakyProvider("primary", [http_error(429, retry_after="2")])
    gateway(primary, clock=clock).complete("q", ["ctx"], ())
    assert clock.sleeps == [2.0]


def test_client_errors_skip_to_the_fallback_model() -> None:
    primary = FlakyProvider("primary", [http_error(400)])
    secondary = FlakyProvider("secondary", [])
    result = gateway(primary, secondary).complete("q", ["ctx"], ())
    assert primary.calls == 1 and result.model == "secondary"


def test_everything_down_degrades_to_extractive() -> None:
    primary = FlakyProvider("primary", [http_error(500)] * 3)
    result = gateway(primary).complete("q", ["the evidence"], ())
    assert result.degraded and result.model == "extractive" and "[1]" in result.text


def test_deadline_stops_retrying() -> None:
    clock = FakeClock()
    primary = FlakyProvider("primary", [http_error(429, retry_after="60")] * 3)
    result = gateway(primary, clock=clock).complete("q", ["ctx"], ())
    assert primary.calls == 1 and result.degraded and clock.sleeps == []


def test_circuit_breaker_opens_and_recovers() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, reset_after=30, clock=clock)
    breaker.record_failure()
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state == "open" and not breaker.allow()
    clock.now += 30
    assert breaker.allow() and not breaker.allow()  # one half-open trial
    breaker.record_success()
    assert breaker.state == "closed"


def test_open_breaker_skips_provider_without_calling_it() -> None:
    clock = FakeClock()
    primary = FlakyProvider("primary", [])
    breaker = CircuitBreaker(failure_threshold=1, clock=clock)
    breaker.record_failure()
    gw = GatewayProvider([primary], breakers={"primary": breaker}, clock=clock)
    assert gw.complete("q", ["ctx"], ()).degraded and primary.calls == 0
    assert gw.rewrite_query("How often?", [ChatTurn("user", "Pay?")]) == "Pay? How often?"


def test_budget_packs_context_and_trims_history() -> None:
    budget = Budget(max_context_tokens=10, max_history_tokens=5)
    assert budget.pack_context(["a" * 20, "b" * 20, "c" * 20]) == ["a" * 20, "b" * 20]
    assert budget.pack_context(["x" * 400]) == ["x" * 40]
    history = [ChatTurn("user", "old " * 10), ChatTurn("user", "new")]
    assert budget.trim_history(history) == [ChatTurn("user", "new")]


def test_pricing_and_configuration() -> None:
    primary = FlakyProvider("m", [])
    gw = GatewayProvider([primary], pricing={"*": Pricing(1.0, 2.0)})
    assert gw.complete("q", ["c"], ()).cost_usd == round((100 * 1 + 20 * 2) / 1e6, 6)
    assert isinstance(configured_provider({}), ExtractiveProvider)
    env = {
        "RAG_MODEL_BASE_URL": "http://a",
        "RAG_MODEL_API_KEY": "k",
        "RAG_MODEL_NAME": "m",
        "RAG_FALLBACK_MODEL_BASE_URL": "http://b",
        "RAG_FALLBACK_MODEL_API_KEY": "k",
        "RAG_FALLBACK_MODEL_NAME": "n",
    }
    configured = configured_provider(env)
    assert isinstance(configured, GatewayProvider)
    assert [p.name for p in configured.providers] == ["m", "n"]


def test_api_reports_generation_metadata() -> None:
    down = FlakyProvider("primary", [http_error(500)] * 3)
    app = RagApiApplication(api_key="k", provider=gateway(down))
    headers = {"x-api-key": "k"}
    doc = {"filename": "a.txt", "content": "Refunds take thirty days."}
    app.handle("POST", "/v1/documents", headers, json.dumps(doc).encode())
    body = app.handle("POST", "/v1/query", headers, b'{"question": "refunds thirty days"}')[1]
    assert body["grounded"] and body["generation"]["degraded"] is True
