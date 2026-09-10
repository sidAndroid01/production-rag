"""Phase 3: small authenticated HTTP boundary for the standalone RAG flows.

The server uses only Python's standard library so the API contract can be
understood and exercised before introducing FastAPI, a database, or a cloud
runtime. Request bodies are JSON; uploaded document content is a UTF-8 string.
"""

from __future__ import annotations

import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

from query import RagQueryPipeline, UnsafeQueryError
from rag import RagIngestionPipeline


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class RagApiApplication:
    """Own request policy and in-memory state for the phase-three API."""

    def __init__(self, api_key: str | None = None, max_body_bytes: int = 5_000_000) -> None:
        self.api_key = api_key or os.getenv("RAG_API_KEY", "local-development-key")
        self.max_body_bytes = max_body_bytes
        self.ingestion = RagIngestionPipeline()
        self._chunks: list[Any] = []
        self._lock = threading.Lock()

    def _authenticate(self, headers: dict[str, str]) -> None:
        normalized = {key.lower(): value for key, value in headers.items()}
        if normalized.get("x-api-key") != self.api_key:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "invalid or missing API key")

    @staticmethod
    def _json_body(body: bytes) -> dict[str, Any]:
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
        return value

    def handle(self, method: str, path: str, headers: dict[str, str], body: bytes = b"") -> tuple[int, dict[str, Any]]:
        request_id = headers.get("x-request-id") or str(uuid4())
        if path == "/health/live" and method == "GET":
            return HTTPStatus.OK, {"status": "ok", "request_id": request_id}
        if path == "/health/ready" and method == "GET":
            return HTTPStatus.OK, {"status": "ready", "request_id": request_id}
        if method != "POST" or path not in {"/v1/documents", "/v1/query"}:
            raise ApiError(HTTPStatus.NOT_FOUND, "route not found")

        self._authenticate(headers)
        if len(body) > self.max_body_bytes:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large")
        payload = self._json_body(body)

        if path == "/v1/documents":
            filename = payload.get("filename")
            tenant_id = payload.get("tenant_id")
            content = payload.get("content")
            if not all(isinstance(value, str) for value in (filename, tenant_id, content)):
                raise ApiError(HTTPStatus.BAD_REQUEST, "filename, tenant_id, and content are required strings")
            try:
                result = self.ingestion.ingest(content.encode("utf-8"), filename, tenant_id)
            except (UnicodeError, ValueError) as exc:
                raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
            with self._lock:
                self._chunks.extend(result.chunks)
            return HTTPStatus.CREATED, {
                "document_id": result.document_id,
                "content_sha256": result.content_sha256,
                "chunks_created": len(result.chunks),
                "request_id": request_id,
            }

        question = payload.get("question")
        tenant_id = payload.get("tenant_id")
        if not isinstance(question, str) or not isinstance(tenant_id, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "question and tenant_id are required strings")
        with self._lock:
            query = RagQueryPipeline(tuple(self._chunks))
        try:
            result = query.query(question, tenant_id, request_id=request_id)
        except UnsafeQueryError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        return HTTPStatus.OK, {
                "answer": result.answer,
                "grounded": result.grounded,
                "request_id": result.request_id,
                "citations": [
                    {
                        "document_id": citation.document_id,
                        "source": citation.source,
                        "chunk_index": citation.chunk_index,
                        "score": citation.score,
                        "excerpt": citation.excerpt,
                    }
                    for citation in result.citations
                ],
        }


class RequestHandler(BaseHTTPRequestHandler):
    application = RagApiApplication()

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(min(length, self.application.max_body_bytes + 1))
            status, payload = self.application.handle(self.command, self.path, dict(self.headers), body)
        except ApiError as exc:
            status, payload = exc.status, {"detail": exc.message}
        except Exception:
            status, payload = HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": "internal server error"}
        self._respond(int(status), payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def run() -> None:
    port = int(os.getenv("PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), RequestHandler)
    print(f"RAG API listening on http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
