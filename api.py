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
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from embeddings import Embedder, LocalFastEmbedder
from persistence import PostgresPersistence
from query import RagQueryPipeline, UnsafeQueryError
from rag import RagIngestionPipeline


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class RagApiApplication:
    """Own request policy and in-memory state for the phase-three API."""

    def __init__(
        self,
        api_key: str | None = None,
        max_body_bytes: int = 5_000_000,
        persistence: PostgresPersistence | None = None,
        embedder: Embedder | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("RAG_API_KEY", "local-development-key")
        self.tenant_id = tenant_id.strip() if tenant_id and tenant_id.strip() else None
        self.max_body_bytes = max_body_bytes
        self.ingestion = RagIngestionPipeline()
        self.persistence = persistence
        self.embedder = embedder or (LocalFastEmbedder() if persistence is not None else None)
        self._chunks: list[Any] = []
        self._lock = threading.Lock()

    def _authenticate(self, headers: dict[str, str]) -> None:
        normalized = {key.lower(): value for key, value in headers.items()}
        if normalized.get("x-api-key") != self.api_key:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "invalid or missing API key")

    def _tenant_from_payload(self, payload: dict[str, Any]) -> str:
        tenant_id = payload.get("tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "tenant_id is required as a non-empty string")
        normalized = tenant_id.strip()
        if self.tenant_id is not None and normalized != self.tenant_id:
            raise ApiError(HTTPStatus.FORBIDDEN, "tenant_id does not match authenticated identity")
        return normalized

    @staticmethod
    def _json_body(body: bytes) -> dict[str, Any]:
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
        return value

    def handle(
        self, method: str, path: str, headers: dict[str, str], body: bytes = b""
    ) -> tuple[int, dict[str, Any]]:
        request_id = headers.get("x-request-id") or str(uuid4())
        if path == "/health/live" and method == "GET":
            return HTTPStatus.OK, {"status": "ok", "request_id": request_id}
        if path == "/health/ready" and method == "GET":
            if self.persistence is not None:
                try:
                    self.persistence.check_connection()
                except Exception as exc:
                    raise ApiError(
                        HTTPStatus.SERVICE_UNAVAILABLE, "database is unavailable"
                    ) from exc
            return HTTPStatus.OK, {"status": "ready", "request_id": request_id}
        document_id_to_delete = None
        if method == "DELETE" and path.startswith("/v1/documents/"):
            document_id_to_delete = path.removeprefix("/v1/documents/").strip()
            if not document_id_to_delete:
                raise ApiError(HTTPStatus.NOT_FOUND, "document route not found")
        elif method != "POST" or path not in {"/v1/documents", "/v1/query"}:
            raise ApiError(HTTPStatus.NOT_FOUND, "route not found")

        self._authenticate(headers)
        if len(body) > self.max_body_bytes:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large")
        payload = self._json_body(body)

        if document_id_to_delete is not None:
            tenant_id = self._tenant_from_payload(payload)
            if self.persistence is not None:
                deleted = self.persistence.delete_document(tenant_id, document_id_to_delete)
            else:
                with self._lock:
                    before = len(self._chunks)
                    self._chunks = [
                        chunk for chunk in self._chunks
                        if not (
                            chunk.tenant_id == tenant_id
                            and chunk.document_id == document_id_to_delete
                        )
                    ]
                    deleted = len(self._chunks) != before
            if not deleted:
                raise ApiError(HTTPStatus.NOT_FOUND, "document not found")
            return HTTPStatus.OK, {
                "document_id": document_id_to_delete,
                "deleted": True,
                "request_id": request_id,
            }

        if path == "/v1/documents":
            filename = payload.get("filename")
            content = payload.get("content")
            if not isinstance(filename, str) or not isinstance(content, str):
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "filename and content are required strings",
                )
            tenant_id = self._tenant_from_payload(payload)
            try:
                result = self.ingestion.ingest(content.encode("utf-8"), filename, tenant_id)
            except (UnicodeError, ValueError) as exc:
                raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
            if self.persistence is not None:
                if not result.chunks:
                    raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "document has no text to index")
                assert self.embedder is not None
                embeddings = self.embedder.embed_documents([chunk.text for chunk in result.chunks])
                stored = self.persistence.persist(result, embeddings, self.embedder.model_name)
                chunks_created = stored.chunks_written
            else:
                with self._lock:
                    self._chunks.extend(result.chunks)
                chunks_created = len(result.chunks)
            return HTTPStatus.CREATED, {
                "document_id": result.document_id,
                "content_sha256": result.content_sha256,
                "chunks_created": chunks_created,
                "request_id": request_id,
            }

        question = payload.get("question")
        if not isinstance(question, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "question is required as a string")
        tenant_id = self._tenant_from_payload(payload)
        if self.persistence is not None:
            assert self.embedder is not None
            query = RagQueryPipeline(())
            try:
                safe_question = query.validate_query(question, tenant_id)
                vector = self.embedder.embed_query(safe_question)
                rows = self.persistence.search_hybrid(
                    tenant_id, safe_question, vector, limit=5, model_name=self.embedder.model_name
                )
                ranked = [
                    (
                        SimpleNamespace(
                            id=row["id"],
                            tenant_id=row["tenant_id"],
                            document_id=row["document_id"],
                            index=row["chunk_index"],
                            text=row["text"],
                            created_at=row["created_at"],
                            source=row["source"],
                        ),
                        float(row["score"]),
                    )
                    for row in rows
                ]
            except UnsafeQueryError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        else:
            with self._lock:
                query = RagQueryPipeline(tuple(self._chunks))
        try:
            if self.persistence is not None:
                result = query.query_ranked(safe_question, tenant_id, ranked, request_id=request_id)
            else:
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

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(min(length, self.application.max_body_bytes + 1))
            status, payload = self.application.handle(
                self.command, self.path, dict(self.headers), body
            )
        except ApiError as exc:
            status, payload = exc.status, {"detail": exc.message}
        except Exception:
            status, payload = HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": "internal server error"}
        self._respond(int(status), payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def run() -> None:
    port = int(os.getenv("PORT", "8000"))
    dsn = os.getenv("DATABASE_URL")
    persistence = PostgresPersistence(dsn) if dsn else None
    if persistence is not None:
        configured_tenant = os.getenv("RAG_TENANT_ID")
        if not configured_tenant or not configured_tenant.strip():
            raise RuntimeError("RAG_TENANT_ID is required when DATABASE_URL is configured")
        persistence.initialize()
    else:
        configured_tenant = os.getenv("RAG_TENANT_ID")
    RequestHandler.application = RagApiApplication(
        persistence=persistence,
        tenant_id=configured_tenant,
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), RequestHandler)
    print(f"RAG API listening on http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
