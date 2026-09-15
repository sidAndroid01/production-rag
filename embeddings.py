"""Local, no-API-cost text embeddings for ingestion and query workloads."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any, Protocol

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384


class Embedder(Protocol):
    model_name: str
    dimensions: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class LocalFastEmbedder:
    """CPU-friendly local ONNX embeddings; weights download once and are cached."""

    def __init__(
        self,
        model_name: str = MODEL_NAME,
        cache_dir: str | None = None,
        batch_size: int = 32,
        threads: int | None = None,
    ) -> None:
        if model_name != MODEL_NAME:
            raise ValueError(f"this schema is configured for {MODEL_NAME}")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.model_name = model_name
        self.dimensions = DIMENSIONS
        self.batch_size = batch_size
        self._cache_dir = cache_dir
        self._threads = threads
        self._model: Any | None = None

    def _load_model(self) -> Any:
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:
                raise RuntimeError(
                    "install the local embedding dependencies with "
                    '`pip install "fastembed>=0.7,<1"`'
                ) from exc
            self._model = TextEmbedding(
                model_name=self.model_name,
                cache_dir=self._cache_dir,
                threads=self._threads,
            )
        return self._model

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: Iterable[Any] = self._load_model().embed(list(texts), batch_size=self.batch_size)
        return [self._validate(vector) for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        vectors = self._load_model().query_embed(text)
        try:
            vector = next(iter(vectors))
        except StopIteration as exc:
            raise RuntimeError("embedding model returned no query vector") from exc
        return self._validate(vector)

    @staticmethod
    def _validate(vector: Iterable[Any]) -> list[float]:
        values = [float(value) for value in vector]
        if len(values) != DIMENSIONS:
            raise ValueError(f"expected {DIMENSIONS}-dimension embedding; got {len(values)}")
        if not all(math.isfinite(value) for value in values):
            raise ValueError("embedding contains a non-finite value")
        return values
