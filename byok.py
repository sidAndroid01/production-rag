"""Bring your own LLM: per-request model settings supplied by the user.

A user's provider key arrives with each question, is used for that request
only, and is never stored or logged. Presets cover common OpenAI-compatible
providers; a custom base URL (for example a self-hosted Ollama behind HTTPS)
is allowed only to public HTTPS hosts, because the server makes the request
and must not be steerable towards its own network.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from gateway import Budget, GatewayProvider
from providers import OpenAICompatibleProvider

PRESETS: dict[str, dict[str, Any]] = {
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "models": ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"],
        "key_url": "https://console.groq.com/keys",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o"],
        "key_url": "https://platform.openai.com/api-keys",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["meta-llama/llama-3.1-8b-instruct"],
        "key_url": "https://openrouter.ai/keys",
    },
    "gemini": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": ["gemini-2.0-flash"],
        "key_url": "https://aistudio.google.com/apikey",
    },
    "together": {
        "label": "Together AI",
        "base_url": "https://api.together.xyz/v1",
        "models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo"],
        "key_url": "https://api.together.ai/settings/api-keys",
    },
    "mistral": {
        "label": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "models": ["mistral-small-latest"],
        "key_url": "https://console.mistral.ai/api-keys",
    },
}
CUSTOM = "custom"
MODEL_RE = re.compile(r"[A-Za-z0-9._:/@+-]{1,128}")


class LlmSettingsError(ValueError):
    """Invalid user LLM settings; the message never contains the key."""


@dataclass(frozen=True, slots=True)
class LlmSettings:
    provider: str
    base_url: str
    model: str
    api_key: str

    def __repr__(self) -> str:  # never print the key, even in a traceback
        return f"LlmSettings(provider={self.provider!r}, model={self.model!r})"


def custom_urls_allowed(environ: Mapping[str, str] = os.environ) -> bool:
    return environ.get("RAG_ALLOW_CUSTOM_LLM_URL", "1") != "0"


def check_public_url(url: str) -> None:
    """Allow only https URLs whose host resolves exclusively to public addresses.

    Called before every request, not just when settings are saved, so a DNS
    record changed after validation is checked again (a short race remains;
    see the README).
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise LlmSettingsError("custom LLM URLs must use https")
    if parts.username or parts.password:
        raise LlmSettingsError("custom LLM URLs must not contain credentials")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise LlmSettingsError(f"cannot resolve {parts.hostname}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise LlmSettingsError(
                f"{parts.hostname} resolves to a private or internal address, which is not allowed"
            )


def parse_llm_settings(value: Any, *, allow_custom: bool) -> LlmSettings | None:
    """Validate the ``llm`` object from a request body; None means "not provided"."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise LlmSettingsError("llm must be an object")
    provider = value.get("provider")
    model = value.get("model")
    api_key = value.get("api_key")
    if provider == CUSTOM:
        if not allow_custom:
            raise LlmSettingsError("custom LLM URLs are disabled on this server")
        base_url = value.get("base_url")
        if not isinstance(base_url, str) or len(base_url) > 300:
            raise LlmSettingsError("base_url is required for a custom provider")
        base_url = base_url.strip().rstrip("/")
        check_public_url(base_url)
    elif isinstance(provider, str) and provider in PRESETS:
        base_url = PRESETS[provider]["base_url"]
    else:
        raise LlmSettingsError(f"provider must be one of: {', '.join([*PRESETS, CUSTOM])}")
    if not isinstance(model, str) or not MODEL_RE.fullmatch(model.strip()):
        raise LlmSettingsError("model must be a model ID such as llama-3.1-8b-instant")
    if not isinstance(api_key, str) or not 8 <= len(api_key.strip()) <= 512:
        raise LlmSettingsError("api_key is missing or malformed")
    return LlmSettings(provider, base_url, model.strip(), api_key.strip())


def build_user_provider(
    settings: LlmSettings, environ: Mapping[str, str] = os.environ
) -> GatewayProvider:
    """A one-request gateway around the user's provider.

    It gets its own circuit breaker, so one user's failing provider never
    affects another user, and it falls back to the extractive answerer with a
    note explaining what went wrong.
    """
    guard = check_public_url if settings.provider == CUSTOM else None
    provider = OpenAICompatibleProvider(
        settings.base_url,
        settings.api_key,
        settings.model,
        timeout=float(environ.get("RAG_MODEL_TIMEOUT_SECONDS", "30")),
        max_output_tokens=int(environ.get("RAG_MAX_OUTPUT_TOKENS", "512")),
        url_guard=guard,
    )
    return GatewayProvider(
        [provider],
        budget=Budget(
            max_context_tokens=int(environ.get("RAG_MAX_CONTEXT_TOKENS", "3000")),
            max_history_tokens=int(environ.get("RAG_MAX_HISTORY_TOKENS", "1000")),
        ),
        max_attempts=2,
        deadline=float(environ.get("RAG_GENERATION_DEADLINE_SECONDS", "45")),
    )


def presets_for_ui(environ: Mapping[str, str] = os.environ) -> dict[str, Any]:
    return {
        "providers": [
            {
                "id": key,
                "label": value["label"],
                "models": value["models"],
                "key_url": value["key_url"],
            }
            for key, value in PRESETS.items()
        ],
        "custom_allowed": custom_urls_allowed(environ),
    }
