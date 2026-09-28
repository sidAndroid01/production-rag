"""Authenticated HTTP boundary for the RAG flows.

The server uses only Python's standard library so the request lifecycle stays
visible: a small route table maps (method, path) to one handler method each.
Request bodies are JSON; uploaded document content is a UTF-8 string.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from auth import ApiKeyStore
from embeddings import Embedder, LocalFastEmbedder
from history import ChatHistoryStore
from permissions import Principal, can_read, can_read_groups, normalize_groups
from persistence import PostgresPersistence
from providers import ChatTurn, ModelProvider, configured_provider
from query import RagQueryPipeline, UnsafeQueryError
from rag import RagIngestionPipeline
from reranker import CrossEncoderReranker
from workers import IngestionWorker


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    params: dict[str, str]
    payload: dict[str, Any]
    request_id: str
    principal: Principal | None = None

    @property
    def caller(self) -> Principal:
        assert self.principal is not None, "route requires authentication"
        return self.principal


Handler = Callable[[Request], tuple[int, dict[str, Any]]]


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


class RagApiApplication:
    """Own request policy and state for the API."""

    def __init__(
        self,
        api_key: str | None = None,
        max_body_bytes: int = 5_000_000,
        persistence: PostgresPersistence | None = None,
        embedder: Embedder | None = None,
        tenant_id: str | None = None,
        provider: ModelProvider | None = None,
        reranker: CrossEncoderReranker | None = None,
        keys: ApiKeyStore | None = None,
    ) -> None:
        # Identity is server-side: each key maps to one tenant, user and groups.
        # api_key/tenant_id are a shorthand for a single-key deployment.
        if keys is None:
            keys = (
                ApiKeyStore.single(api_key, (tenant_id or "default").strip())
                if api_key
                else ApiKeyStore.from_environment()
            )
        self.keys = keys
        self.max_body_bytes = max_body_bytes
        self.ingestion = RagIngestionPipeline()
        self.persistence = persistence
        self.embedder = embedder or (LocalFastEmbedder() if persistence is not None else None)
        self.provider = provider or configured_provider()
        self.reranker = reranker or CrossEncoderReranker()
        self.history = ChatHistoryStore()
        # In-memory fallback: chunks plus one metadata record per document.
        self._chunks: list[Any] = []
        self._documents: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.worker = IngestionWorker(self._ingest_document)
        # (method, path pattern, handler, requires authentication)
        self.routes: list[tuple[str, re.Pattern[str], Handler, bool]] = [
            ("GET", re.compile(r"/health/live"), self._live, False),
            ("GET", re.compile(r"/health/ready"), self._ready, False),
            ("GET", re.compile(r"/v1/documents"), self._list_documents, True),
            ("POST", re.compile(r"/v1/documents"), self._create_document, True),
            ("GET", re.compile(r"/v1/documents/(?P<document_id>[^/]+)"), self._get_document, True),
            (
                "DELETE",
                re.compile(r"/v1/documents/(?P<document_id>[^/]+)"),
                self._delete_document,
                True,
            ),
            ("POST", re.compile(r"/v1/query"), self._query, True),
            ("POST", re.compile(r"/v1/chat"), self._chat, True),
            ("POST", re.compile(r"/v1/ingestion/jobs"), self._submit_job, True),
            ("GET", re.compile(r"/v1/ingestion/jobs/(?P<job_id>[^/]+)"), self._get_job, True),
        ]

    # -- request plumbing -------------------------------------------------

    def _authenticate(self, headers: dict[str, str]) -> Principal:
        normalized = {key.lower(): value for key, value in headers.items()}
        principal = self.keys.authenticate(normalized.get("x-api-key"))
        if principal is None:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "invalid or missing API key")
        return principal

    @staticmethod
    def _tenant(request: Request) -> str:
        """The caller's tenant. A body tenant_id is optional and must agree."""
        claimed = request.payload.get("tenant_id")
        if claimed is not None and (
            not isinstance(claimed, str) or claimed.strip() != request.caller.tenant_id
        ):
            raise ApiError(HTTPStatus.FORBIDDEN, "tenant_id does not match authenticated identity")
        return request.caller.tenant_id

    @staticmethod
    def _json_body(body: bytes) -> dict[str, Any]:
        if not body.strip():
            return {}
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
        lowered = {key.lower(): value for key, value in headers.items()}
        request_id = lowered.get("x-request-id") or str(uuid4())
        route_path = urlsplit(path).path.rstrip("/") or "/"
        allowed: list[str] = []
        for route_method, pattern, handler, requires_auth in self.routes:
            match = pattern.fullmatch(route_path)
            if match is None:
                continue
            if route_method != method:
                allowed.append(route_method)
                continue
            principal = self._authenticate(headers) if requires_auth else None
            if len(body) > self.max_body_bytes:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large")
            request = Request(
                method,
                route_path,
                match.groupdict(),
                self._json_body(body),
                request_id,
                principal,
            )
            status, payload = handler(request)
            return status, {**payload, "request_id": request_id}
        if allowed:
            raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
        raise ApiError(HTTPStatus.NOT_FOUND, "route not found")

    # -- health -----------------------------------------------------------

    def _live(self, request: Request) -> tuple[int, dict[str, Any]]:
        return HTTPStatus.OK, {"status": "ok"}

    def _ready(self, request: Request) -> tuple[int, dict[str, Any]]:
        if self.persistence is not None:
            try:
                self.persistence.check_connection()
            except Exception as exc:
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "database is unavailable") from exc
        return HTTPStatus.OK, {"status": "ready"}

    # -- documents --------------------------------------------------------

    @staticmethod
    def _document_job(request: Request) -> dict[str, Any]:
        """Validate an upload and bind it to the caller's tenant."""
        payload = request.payload
        filename = payload.get("filename")
        content = payload.get("content")
        if not isinstance(filename, str) or not isinstance(content, str):
            raise ValueError("filename and content are required strings")
        return {
            "tenant_id": RagApiApplication._tenant(request),
            "filename": filename,
            "content": content,
            "allowed_groups": list(normalize_groups(payload.get("allowed_groups"))),
        }

    def _ingest_document(self, job: dict[str, Any]) -> dict[str, Any]:
        """Ingest a validated job from _document_job; runs inline or on the worker."""
        tenant_id = job["tenant_id"]
        allowed_groups = tuple(job["allowed_groups"])
        result = self.ingestion.ingest(job["content"].encode("utf-8"), job["filename"], tenant_id)
        if not result.chunks:
            raise ValueError("document has no text to index")
        if self.persistence is not None:
            assert self.embedder is not None
            embeddings = self.embedder.embed_documents([chunk.text for chunk in result.chunks])
            stored = self.persistence.persist(
                result, embeddings, self.embedder.model_name, allowed_groups
            )
            document_id = stored.document_id
            chunks_created = stored.chunks_written
            already_existed = stored.already_existed
        else:
            key = (tenant_id, result.document_id)
            with self._lock:
                already_existed = key in self._documents
                if not already_existed:
                    now = datetime.now(UTC)
                    self._documents[key] = {
                        "document_id": result.document_id,
                        "source": result.chunks[0].source,
                        "content_sha256": result.content_sha256,
                        "status": "ready",
                        "chunk_count": len(result.chunks),
                        "allowed_groups": list(allowed_groups),
                        "created_at": now,
                        "updated_at": now,
                    }
                    self._chunks.extend(result.chunks)
            document_id = result.document_id
            chunks_created = 0 if already_existed else len(result.chunks)
        return {
            "document_id": document_id,
            "content_sha256": result.content_sha256,
            "chunks_created": chunks_created,
            "already_existed": already_existed,
        }

    def _create_document(self, request: Request) -> tuple[int, dict[str, Any]]:
        try:
            stored = self._ingest_document(self._document_job(request))
        except (UnicodeError, ValueError) as exc:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
        status = HTTPStatus.OK if stored["already_existed"] else HTTPStatus.CREATED
        return status, stored

    def _visible_documents(self, principal: Principal) -> list[dict[str, Any]]:
        """In-memory documents this principal may read (caller holds no lock)."""
        with self._lock:
            return [
                dict(meta)
                for (owner, _), meta in self._documents.items()
                if owner == principal.tenant_id
                and can_read_groups(meta["allowed_groups"], principal)
            ]

    def _list_documents(self, request: Request) -> tuple[int, dict[str, Any]]:
        principal = request.caller
        self._tenant(request)
        if self.persistence is not None:
            rows = self.persistence.list_documents(principal.tenant_id, sorted(principal.groups))
        else:
            rows = self._visible_documents(principal)
            rows.sort(key=lambda row: (row["created_at"], row["document_id"]), reverse=True)
        documents = [{key: _iso(value) for key, value in row.items()} for row in rows]
        return HTTPStatus.OK, {"documents": documents}

    def _get_document(self, request: Request) -> tuple[int, dict[str, Any]]:
        principal = request.caller
        self._tenant(request)
        document_id = request.params["document_id"]
        if self.persistence is not None:
            row = self.persistence.get_document(
                principal.tenant_id, document_id, sorted(principal.groups)
            )
        else:
            row = next(
                (r for r in self._visible_documents(principal) if r["document_id"] == document_id),
                None,
            )
        if row is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "document not found")
        return HTTPStatus.OK, {key: _iso(value) for key, value in row.items()}

    def _delete_document(self, request: Request) -> tuple[int, dict[str, Any]]:
        principal = request.caller
        tenant_id = self._tenant(request)
        document_id = request.params["document_id"]
        if self.persistence is not None:
            deleted = self.persistence.delete_document(
                tenant_id, document_id, sorted(principal.groups)
            )
        else:
            with self._lock:
                meta = self._documents.get((tenant_id, document_id))
                deleted = meta is not None and can_read_groups(meta["allowed_groups"], principal)
                if not deleted:
                    raise ApiError(HTTPStatus.NOT_FOUND, "document not found")
                del self._documents[(tenant_id, document_id)]
                self._chunks = [
                    chunk
                    for chunk in self._chunks
                    if not (chunk.tenant_id == tenant_id and chunk.document_id == document_id)
                ]
        if not deleted:
            raise ApiError(HTTPStatus.NOT_FOUND, "document not found")
        return HTTPStatus.OK, {"document_id": document_id, "deleted": True}

    # -- ingestion jobs ---------------------------------------------------

    def _submit_job(self, request: Request) -> tuple[int, dict[str, Any]]:
        try:
            document_job = self._document_job(request)
        except ValueError as exc:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
        job = self.worker.submit(document_job["tenant_id"], document_job)
        return HTTPStatus.ACCEPTED, job.as_dict()

    def _get_job(self, request: Request) -> tuple[int, dict[str, Any]]:
        job = self.worker.get(self._tenant(request), request.params["job_id"])
        if job is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "job not found")
        return HTTPStatus.OK, job.as_dict()

    # -- query and chat ---------------------------------------------------

    def _query(self, request: Request) -> tuple[int, dict[str, Any]]:
        return self._answer(request, is_chat=False)

    def _chat(self, request: Request) -> tuple[int, dict[str, Any]]:
        return self._answer(request, is_chat=True)

    def _answer(self, request: Request, *, is_chat: bool) -> tuple[int, dict[str, Any]]:
        payload = request.payload
        question = payload.get("question")
        if not isinstance(question, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "question is required as a string")
        principal = request.caller
        tenant_id = self._tenant(request)
        session_id = payload.get("session_id")
        if is_chat and (not isinstance(session_id, str) or not session_id.strip()):
            raise ApiError(HTTPStatus.BAD_REQUEST, "session_id is required for chat")
        try:
            if self.persistence is not None:
                assert self.embedder is not None
                query = RagQueryPipeline(())
                safe_question = query.validate_query(question, tenant_id)
                vector = self.embedder.embed_query(safe_question)
                rows = self.persistence.search_hybrid(
                    tenant_id,
                    safe_question,
                    vector,
                    limit=5,
                    model_name=self.embedder.model_name,
                    groups=sorted(principal.groups),
                )
                # SQL already applied the ACL; re-check before evidence is used.
                rows = [row for row in rows if can_read(row, principal)]
                rows = self.reranker.rerank(safe_question, rows)
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
                result = query.query_ranked(
                    safe_question, tenant_id, ranked, request_id=request.request_id
                )
            else:
                readable = {row["document_id"] for row in self._visible_documents(principal)}
                with self._lock:
                    chunks = tuple(c for c in self._chunks if c.document_id in readable)
                query = RagQueryPipeline(chunks)
                result = query.query(question, tenant_id, request_id=request.request_id)
        except UnsafeQueryError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        answer = result.answer
        extra: dict[str, Any] = {}
        if is_chat:
            assert isinstance(session_id, str)
            user_id = principal.user_id
            prior = self.history.get(tenant_id, user_id, session_id)
            if result.grounded:
                answer = self.provider.generate(
                    question, [citation.excerpt for citation in result.citations], prior
                )
            self.history.append(
                tenant_id,
                user_id,
                session_id,
                ChatTurn("user", question),
                ChatTurn("assistant", answer),
            )
            extra = {
                "session_id": session_id,
                "history_messages": len(self.history.get(tenant_id, user_id, session_id)),
            }
        return HTTPStatus.OK, {
            "answer": answer,
            "grounded": result.grounded,
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
            **extra,
        }


class RequestHandler(BaseHTTPRequestHandler):
    # Assigned by run() or tests; building it at import would start threads
    # and load models as a side effect of importing this module.
    application: RagApiApplication | None = None

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        if "request_id" in payload:
            self.send_header("x-request-id", str(payload["request_id"]))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        application = self.application
        assert application is not None, "RequestHandler.application is not configured"
        try:
            try:
                length = int(self.headers.get("content-length", "0"))
            except ValueError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid content-length") from exc
            if length < 0:
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid content-length")
            if length > application.max_body_bytes:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large")
            body = self.rfile.read(length)
            status, payload = application.handle(self.command, self.path, dict(self.headers), body)
        except ApiError as exc:
            status, payload = exc.status, {"detail": exc.message}
        except Exception:
            status, payload = HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": "internal server error"}
        self._respond(int(status), payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def run() -> None:
    port = int(os.getenv("PORT", "8000"))
    # Loopback by default; containers set RAG_HOST=0.0.0.0 to accept traffic.
    host = os.getenv("RAG_HOST", "127.0.0.1")
    dsn = os.getenv("DATABASE_URL")
    persistence = PostgresPersistence(dsn) if dsn else None
    # Schema changes are applied separately by migrate.py as the owner role;
    # the API's restricted role cannot run DDL.
    RequestHandler.application = RagApiApplication(
        persistence=persistence, keys=ApiKeyStore.from_environment()
    )
    server = ThreadingHTTPServer((host, port), RequestHandler)
    print(f"RAG API listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
