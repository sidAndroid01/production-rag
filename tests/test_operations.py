from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from typing import Any
from urllib import error, request
from uuid import uuid4

import pytest

from api import ApiError, RagApiApplication, RequestHandler
from observability import DailyTokenBudget, JsonFormatter, Metrics, RateLimiter
from providers import ChatTurn, Generation, ModelProvider

HEADERS = {"x-api-key": "k"}
DOC = json.dumps({"filename": "a.txt", "content": "Refunds take thirty days."}).encode()


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class CountingProvider(ModelProvider):
    name = "counting"

    def complete(self, question: str, context: Any, history: Any) -> Generation:
        return Generation("Thirty days [1].", "counting", prompt_tokens=60, completion_tokens=40)


def status_of(app: RagApiApplication, method: str, path: str, body: bytes = b"") -> int:
    try:
        return app.handle(method, path, HEADERS, body)[0]
    except ApiError as exc:
        return int(exc.status)


def test_rate_limiter_refills_over_time() -> None:
    clock = Clock()
    limiter = RateLimiter(rate_per_minute=60, burst=2, clock=clock)
    assert limiter.acquire("a") == 0 and limiter.acquire("a") == 0
    assert limiter.acquire("a") == pytest.approx(1.0)
    assert limiter.acquire("b") == 0  # buckets are per key
    clock.now += 1
    assert limiter.acquire("a") == 0


def test_api_returns_429_with_retry_after() -> None:
    clock = Clock()
    app = RagApiApplication(api_key="k", rate_limiter=RateLimiter(60, burst=1, clock=clock))
    assert status_of(app, "GET", "/v1/documents") == 200
    with pytest.raises(ApiError) as caught:
        app.handle("GET", "/v1/documents", HEADERS)
    assert caught.value.status == 429 and caught.value.headers["retry-after"] == "1"
    assert status_of(app, "GET", "/health/live") == 200  # probes are never limited


def test_daily_token_budget_blocks_generation() -> None:
    app = RagApiApplication(
        api_key="k", provider=CountingProvider(), token_budget=DailyTokenBudget(150)
    )
    app.handle("POST", "/v1/documents", HEADERS, DOC)
    question = b'{"question": "How long do refunds take?"}'
    assert status_of(app, "POST", "/v1/query", question) == 200  # spends 100
    assert status_of(app, "POST", "/v1/query", question) == 200  # spends 100 more
    assert status_of(app, "POST", "/v1/query", question) == 429
    assert app.token_budget.remaining("default") == -50


def test_metrics_render_prometheus_text() -> None:
    app = RagApiApplication(api_key="k")
    app.handle("GET", "/health/live", {})
    status_of(app, "GET", "/v1/documents/unknown")
    text = app.metrics.render()
    assert 'rag_http_requests_total{method="GET",route="live",status="200"} 1' in text
    assert 'route="get_document",status="404"' in text
    assert 'rag_http_request_duration_seconds_bucket{route="live",le="+Inf"} 1' in text
    metrics = Metrics()
    metrics.inc("x_total", "help", source='a"b')
    assert 'x_total{source="a\\"b"} 1' in metrics.render()


def test_every_request_is_logged_once_with_its_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = RagApiApplication(api_key="k")
    with caplog.at_level(logging.INFO, logger="rag"), pytest.raises(ApiError) as caught:
        app.handle("GET", "/v1/documents", {"x-request-id": "req-1"})
    assert caught.value.request_id == "req-1"
    lines = [r for r in caplog.records if r.getMessage() == "request"]
    assert len(lines) == 1 and lines[0].__dict__["status"] == 401
    assert any(r.getMessage() == "auth.failed" for r in caplog.records)
    formatted = json.loads(JsonFormatter().format(lines[0]))
    assert formatted["request_id"] == "req-1" and formatted["route"] == "list_documents"


def test_unexpected_errors_become_500_with_request_id() -> None:
    class Broken(ModelProvider):
        def complete(self, question: str, context: Any, history: Any) -> Generation:
            raise RuntimeError("boom")

    app = RagApiApplication(api_key="k", provider=Broken())
    app.handle("POST", "/v1/documents", HEADERS, DOC)
    with pytest.raises(ApiError) as caught:
        app.handle("POST", "/v1/query", HEADERS, b'{"question": "refunds thirty days"}')
    assert caught.value.status == 500 and caught.value.request_id


@pytest.fixture
def server() -> Iterator[str]:
    RequestHandler.application = RagApiApplication(api_key="k")
    RequestHandler.cors_origins = frozenset({"https://app.example"})
    RequestHandler.metrics_token = "secret"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        RequestHandler.application = None
        RequestHandler.cors_origins = frozenset()
        RequestHandler.metrics_token = None


def fetch(url: str, method: str = "GET", **headers: str) -> tuple[int, dict[str, str], bytes]:
    req = request.Request(url, method=method, headers=headers)
    try:
        with request.urlopen(req) as response:
            return response.status, dict(response.headers), response.read()
    except error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_transport_headers_cors_and_metrics(server: str) -> None:
    status, headers, _ = fetch(server + "/health/live", Origin="https://app.example")
    assert status == 200
    assert headers["access-control-allow-origin"] == "https://app.example"
    assert headers["x-content-type-options"] == "nosniff"
    _, other, _ = fetch(server + "/health/live", Origin="https://evil.example")
    assert "access-control-allow-origin" not in {key.lower() for key in other}
    status, preflight, _ = fetch(server + "/v1/query", "OPTIONS", Origin="https://app.example")
    assert status == 204 and "x-api-key" in preflight["access-control-allow-headers"]
    assert fetch(server + "/metrics")[0] == 401
    status, _, body = fetch(server + "/metrics", Authorization="Bearer secret")
    assert status == 200 and b"rag_http_requests_total" in body
    status, _, body = fetch(server + "/v1/documents")
    assert status == 401 and json.loads(body)["request_id"]


@pytest.mark.skipif(not os.getenv("RAG_RLS_TEST_DSN"), reason="set RAG_RLS_TEST_DSN")
def test_chat_history_survives_a_new_process() -> None:
    from history import PostgresChatHistory
    from persistence import PostgresPersistence

    dsn = os.environ["RAG_RLS_TEST_DSN"]
    tenant, session = f"test-{uuid4().hex[:8]}", "s1"
    first = PostgresChatHistory(PostgresPersistence(dsn), max_turns=1)
    first.append(tenant, "u", session, ChatTurn("user", "q1"), ChatTurn("assistant", "a1"))
    first.append(tenant, "u", session, ChatTurn("user", "q2"), ChatTurn("assistant", "a2"))
    # A different persistence object stands in for a restarted replica.
    second = PostgresChatHistory(PostgresPersistence(dsn), max_turns=1)
    assert second.get(tenant, "u", session) == (ChatTurn("user", "q2"), ChatTurn("assistant", "a2"))
    assert second.get(f"{tenant}-other", "u", session) == ()
    second.clear(tenant, "u", session)
    assert second.get(tenant, "u", session) == ()


def test_ui_is_served_with_a_strict_content_security_policy(server: str) -> None:
    status, headers, body = fetch(server + "/")
    assert status == 200 and b"RAG Console" in body
    assert "default-src 'self'" in headers["content-security-policy"]
    for asset, kind in (("app.js", "javascript"), ("styles.css", "css"), ("favicon.svg", "svg")):
        status, headers, _ = fetch(f"{server}/static/{asset}")
        assert status == 200 and kind in headers["content-type"]
    for probe in ("/static/../api.py", "/static/%2e%2e/api.py", "/static/nope.js", "/static/"):
        assert fetch(server + probe)[0] == 404


def test_me_reports_the_callers_identity() -> None:
    app = RagApiApplication(api_key="k", tenant_id="acme")
    _, me = app.handle("GET", "/v1/me", HEADERS)
    assert (me["tenant_id"], me["storage"]) == ("acme", "memory")


def test_upload_limit_is_configurable_and_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_MAX_BODY_MB", "2")
    app = RagApiApplication(api_key="k")
    assert app.max_body_bytes == 2_000_000
    _, me = app.handle("GET", "/v1/me", HEADERS)
    assert me["max_file_bytes"] == 1_480_000
    monkeypatch.delenv("RAG_MAX_BODY_MB")
    assert RagApiApplication(api_key="k").max_body_bytes == 25_000_000
