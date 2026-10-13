from __future__ import annotations

import json
import logging
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest

import byok
from api import ApiError, RagApiApplication
from byok import LlmSettingsError, check_public_url, parse_llm_settings
from observability import DailyTokenBudget
from providers import Generation, ModelProvider

USER_KEY = "sk-user-secret-0123456789"
DOC = json.dumps({"filename": "policy.txt", "content": "Refunds take thirty days."}).encode()


class FakeProvider(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint whose behaviour each test chooses."""

    mode: ClassVar[str] = "ok"
    seen_keys: ClassVar[list[str]] = []
    seen_agents: ClassVar[list[str]] = []

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["content-length"]))
        FakeProvider.seen_keys.append(self.headers["authorization"])
        FakeProvider.seen_agents.append(self.headers["user-agent"])
        if FakeProvider.mode == "reject":
            self.send_response(401)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        if FakeProvider.mode == "redirect":
            self.send_response(302)
            self.send_header("location", "http://169.254.169.254/latest/meta-data")
            self.send_header("content-length", "0")
            self.end_headers()
            return
        body = json.dumps(
            {
                "model": "user-model",
                "choices": [{"message": {"content": "Refunds take thirty days [1]."}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10},
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def provider_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    # The fake provider runs on localhost, which the public-address check rightly
    # blocks; disable only that check here (it has its own tests below).
    monkeypatch.setattr(byok, "check_public_url", lambda url: None)
    FakeProvider.mode, FakeProvider.seen_keys = "ok", []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


def llm(url: str) -> dict[str, str]:
    return {"provider": "custom", "base_url": url, "model": "user-model", "api_key": USER_KEY}


def ask(app: RagApiApplication, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return app.handle("POST", "/v1/query", {"x-api-key": "k"}, json.dumps(payload).encode())[1]
    except ApiError as exc:
        return {"status": int(exc.status), "detail": exc.message}


@pytest.fixture
def app() -> RagApiApplication:
    app = RagApiApplication(api_key="k", token_budget=DailyTokenBudget(1))
    app.handle("POST", "/v1/documents", {"x-api-key": "k"}, DOC)
    return app


def test_questions_use_the_callers_own_key(app: RagApiApplication, provider_url: str) -> None:
    body = ask(app, {"question": "How long do refunds take?", "llm": llm(provider_url)})
    assert body["answer"] == "Refunds take thirty days [1]."
    assert body["generation"]["own_key"] is True and body["generation"]["model"] == "user-model"
    assert FakeProvider.seen_keys == [f"Bearer {USER_KEY}"]
    # Python's default agent is blocked by some providers' CDNs (Groq: "error 1010").
    assert FakeProvider.seen_agents[0].startswith("production-rag/")


def test_own_keys_do_not_use_the_servers_token_budget(provider_url: str) -> None:
    class ServerModel(ModelProvider):
        name = "server-model"

        def complete(self, question: str, context: Any, history: Any) -> Generation:
            return Generation("Thirty days [1].", "server-model", 60, 40)

    app = RagApiApplication(api_key="k", provider=ServerModel(), token_budget=DailyTokenBudget(150))
    app.handle("POST", "/v1/documents", {"x-api-key": "k"}, DOC)
    question = {"question": "How long do refunds take?"}
    for _ in range(3):  # the user's own key never touches the server's budget
        assert ask(app, {**question, "llm": llm(provider_url)})["grounded"]
    assert app.token_budget.remaining("default") == 150
    assert ask(app, question)["grounded"]  # server model: 100 tokens
    assert ask(app, question)["grounded"]  # 200 used; budget now exhausted
    assert ask(app, question)["status"] == 429
    assert ask(app, {**question, "llm": llm(provider_url)})["grounded"]  # own key still works


def test_rejected_key_explains_itself(app: RagApiApplication, provider_url: str) -> None:
    FakeProvider.mode = "reject"
    body = ask(app, {"question": "How long do refunds take?", "llm": llm(provider_url)})
    generation = body["generation"]
    assert generation["degraded"] and "rejected the API key" in generation["note"]
    assert body["grounded"]  # the extractive fallback still answers with a citation


def test_provider_redirects_are_refused(app: RagApiApplication, provider_url: str) -> None:
    FakeProvider.mode = "redirect"
    body = ask(app, {"question": "How long do refunds take?", "llm": llm(provider_url)})
    assert body["generation"]["degraded"] and "HTTP 302" in body["generation"]["note"]


def test_connection_test_endpoint(app: RagApiApplication, provider_url: str) -> None:
    headers = {"x-api-key": "k"}
    ok = app.handle(
        "POST", "/v1/llm/test", headers, json.dumps({"llm": llm(provider_url)}).encode()
    )
    assert ok[1]["ok"] is True and ok[1]["model"] == "user-model"
    FakeProvider.mode = "reject"
    bad = app.handle(
        "POST", "/v1/llm/test", headers, json.dumps({"llm": llm(provider_url)}).encode()
    )
    assert bad[1]["ok"] is False and "rejected" in bad[1]["error"]


def test_the_key_never_reaches_the_logs(
    app: RagApiApplication, provider_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        ask(app, {"question": "How long do refunds take?", "llm": llm(provider_url)})
        FakeProvider.mode = "reject"
        ask(app, {"question": "How long do refunds take?", "llm": llm(provider_url)})
        ask(app, {"question": "x", "llm": {**llm(provider_url), "model": "bad model!"}})
    assert caplog.records
    for record in caplog.records:
        assert USER_KEY not in json.dumps(vars(record), default=str)


def test_invalid_settings_are_rejected(app: RagApiApplication) -> None:
    base = {"provider": "groq", "model": "llama-3.1-8b-instant", "api_key": USER_KEY}
    for bad in (
        {**base, "provider": "nope"},
        {**base, "model": "has spaces"},
        {**base, "api_key": "short"},
        "not-an-object",
    ):
        assert ask(app, {"question": "refunds?", "llm": bad})["status"] == 422


def test_presets_and_custom_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = parse_llm_settings(
        {"provider": "groq", "model": "llama-3.1-8b-instant", "api_key": USER_KEY},
        allow_custom=False,
    )
    assert settings is not None and settings.base_url == "https://api.groq.com/openai/v1"
    assert USER_KEY not in repr(settings)
    with pytest.raises(LlmSettingsError, match="disabled"):
        parse_llm_settings(
            {
                "provider": "custom",
                "base_url": "https://x.example",
                "model": "m",
                "api_key": USER_KEY,
            },
            allow_custom=False,
        )
    _, me = RagApiApplication(api_key="k").handle("GET", "/v1/me", {"x-api-key": "k"})
    assert {p["id"] for p in me["llm"]["providers"]} >= {"groq", "openai", "gemini"}


@pytest.mark.parametrize(
    ("url", "address", "message"),
    [
        ("http://api.example.com/v1", "93.184.216.34", "https"),
        ("https://user:pw@api.example.com/v1", "93.184.216.34", "credentials"),
        ("https://metadata.example/v1", "169.254.169.254", "private or internal"),
        ("https://intranet.example/v1", "10.0.0.5", "private or internal"),
        ("https://localhost/v1", "127.0.0.1", "private or internal"),
        ("https://v6.example/v1", "::1", "private or internal"),
    ],
)
def test_custom_urls_must_be_public_https(
    monkeypatch: pytest.MonkeyPatch, url: str, address: str, message: str
) -> None:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(family, socket.SOCK_STREAM, 6, "", (address, 443))]
    )
    with pytest.raises(LlmSettingsError, match=message):
        check_public_url(url)


def test_public_https_url_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    check_public_url("https://ollama.example.com/v1")
