"""Pluggable generation providers for the chat path."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from urllib import request


@dataclass(frozen=True, slots=True)
class ChatTurn:
    role: str
    content: str


class ModelProvider:
    def generate(self, question: str, context: Sequence[str], history: Sequence[ChatTurn]) -> str:
        raise NotImplementedError


class ExtractiveProvider(ModelProvider):
    """Free fallback that makes the local demo work without a model API."""

    def generate(self, question: str, context: Sequence[str], history: Sequence[ChatTurn]) -> str:
        del question, history
        return "Based on the indexed sources: " + " ".join(context[:3])


class OpenAICompatibleProvider(ModelProvider):
    """Works with OpenAI, Ollama, vLLM, LM Studio, and compatible gateways."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def generate(self, question: str, context: Sequence[str], history: Sequence[ChatTurn]) -> str:
        sources = "\n\n".join(f"[{index}] {text}" for index, text in enumerate(context, 1))
        messages = [
            {"role": "system", "content": (
                "Answer only from the numbered sources. Cite factual claims as [n]. "
                "If the sources do not contain the answer, say you do not know.\n\n"
                f"Sources:\n{sources}"
            )}
        ]
        messages.extend({"role": turn.role, "content": turn.content} for turn in history)
        messages.append({"role": "user", "content": question})
        payload = json.dumps({"model": self.model, "messages": messages, "temperature": 0}).encode()
        req = request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=self.timeout) as response:
            body = json.loads(response.read())
        return str(body["choices"][0]["message"]["content"])


def configured_provider() -> ModelProvider:
    base_url = os.getenv("RAG_MODEL_BASE_URL")
    api_key = os.getenv("RAG_MODEL_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = os.getenv("RAG_MODEL_NAME")
    if base_url and api_key and model:
        return OpenAICompatibleProvider(base_url, api_key, model)
    return ExtractiveProvider()
