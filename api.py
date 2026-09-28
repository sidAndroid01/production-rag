"""Authenticated HTTP boundary for the RAG flows.

The server uses only Python's standard library so the request lifecycle stays
visible: a small route table maps (method, path) to one handler method each.
Request bodies are JSON; documents arrive as text or as base64-encoded files.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from auth import ApiKeyStore
from embeddings import Embedder, LocalFastEmbedder
from gateway import configured_provider
from history import ChatHistoryStore
from memory_store import InMemoryStore
from parsers import parse
from permissions import Principal, can_read, normalize_groups
from persistence import PostgresPersistence
from providers import (
    ABSTENTION,
    ChatTurn,
    Generation,
    ModelProvider,
    cited_sources,
    strip_invalid_citations,
)
from query import RagQueryPipeline, UnsafeQueryError
from rag import RagIngestionPipeline
from reranker import CrossEncoderReranker
from workers import IngestionWorker, PostgresJobQueue


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
    query: dict[str, str] = field(default_factory=dict)

    @property
    def caller(self) -> Principal:
        assert self.principal is not None, "route requires authentication"
        return self.principal


Handler = Callable[[Request], tuple[int, dict[str, Any]]]
usage_logger = logging.getLogger("rag.usage")

# Calibrated on evals/datasets/golden-v2.json (see README, Phase 15).
LEXICAL_MIN_SCORE = 0.15
HYBRID_MIN_SCORE = 0.40
RELATIVE_SCORE = 0.8


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
        min_score: float | None = None,
        relative_score: float | None = None,
        run_worker: bool | None = None,
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
        # Evidence gate. Lexical and hybrid scores live on different scales, so
        # each storage mode has its own default, calibrated by evals/evaluate.py.
        if min_score is None:
            configured = os.getenv("RAG_MIN_SCORE")
            default = HYBRID_MIN_SCORE if persistence is not None else LEXICAL_MIN_SCORE
            min_score = float(configured) if configured else default
        self.min_score = min_score
        if relative_score is None:
            relative_score = float(os.getenv("RAG_RELATIVE_SCORE", RELATIVE_SCORE))
        self.relative_score = relative_score
        self.max_body_bytes = max_body_bytes
        self.ingestion = RagIngestionPipeline()
        self.persistence = persistence
        self.embedder = embedder or (LocalFastEmbedder() if persistence is not None else None)
        self.provider = provider or configured_provider()
        self.reranker = reranker or CrossEncoderReranker()
        self.history = ChatHistoryStore()
        self.memory = InMemoryStore()
        # Durable queue with a database; in-process worker for the demo.
        # RAG_RUN_WORKER=0 serves the API without consuming jobs.
        if run_worker is None:
            run_worker = os.getenv("RAG_RUN_WORKER", "1") != "0"
        self.worker: IngestionWorker | PostgresJobQueue = (
            PostgresJobQueue(persistence, self._ingest_document, start=run_worker)
            if persistence is not None
            else IngestionWorker(self._ingest_document)
        )
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
        url = urlsplit(path)
        route_path = url.path.rstrip("/") or "/"
        query = {key: values[-1] for key, values in parse_qs(url.query).items()}
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
                query,
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

    def _document_job(self, request: Request) -> dict[str, Any]:
        """Validate an upload and bind it to the caller's identity.

        Send text as ``content``, or any supported file as ``content_base64``
        with an optional ``content_type``.
        """
        payload = request.payload
        filename = payload.get("filename")
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("filename is required")
        content, encoded = payload.get("content"), payload.get("content_base64")
        if isinstance(content, str) and encoded is None:
            encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        elif not isinstance(encoded, str) or content is not None:
            raise ValueError("send exactly one of content (text) or content_base64 (file)")
        else:
            try:
                base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("content_base64 is not valid base64") from exc
        content_type = payload.get("content_type")
        if content_type is not None and not isinstance(content_type, str):
            raise ValueError("content_type must be a string")
        principal = request.caller
        return {
            "tenant_id": self._tenant(request),
            "filename": filename,
            "content_base64": encoded,
            "content_type": content_type,
            "allowed_groups": list(normalize_groups(payload.get("allowed_groups"))),
            # The uploader's groups decide which older versions it may supersede.
            "groups": sorted(principal.groups),
        }

    def _ingest_document(self, job: dict[str, Any]) -> dict[str, Any]:
        """Parse, chunk, embed and store one validated job; runs inline or on a worker."""
        data = base64.b64decode(job["content_base64"])
        parsed = parse(data, job["filename"], job.get("content_type"))
        result = self.ingestion.ingest_segments(
            data, parsed.segments, job["filename"], job["tenant_id"]
        )
        if not result.chunks:
            raise ValueError("document has no text to index")
        options = {
            "groups": job.get("groups", []),
            "content_type": parsed.content_type,
        }
        if self.persistence is not None:
            assert self.embedder is not None
            embeddings = self.embedder.embed_documents([chunk.text for chunk in result.chunks])
            stored = self.persistence.persist(
                result, embeddings, self.embedder.model_name, job["allowed_groups"], **options
            )
        else:
            stored = self.memory.persist(result, job["allowed_groups"], **options)
        return {
            "document_id": stored.document_id,
            "content_sha256": result.content_sha256,
            "content_type": parsed.content_type,
            "version": stored.version,
            "superseded": list(stored.superseded),
            "chunks_created": stored.chunks_written,
            "already_existed": stored.already_existed,
        }

    def _create_document(self, request: Request) -> tuple[int, dict[str, Any]]:
        try:
            stored = self._ingest_document(self._document_job(request))
        except (UnicodeError, ValueError) as exc:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
        status = HTTPStatus.OK if stored["already_existed"] else HTTPStatus.CREATED
        return status, stored

    @property
    def store(self) -> PostgresPersistence | InMemoryStore:
        return self.persistence if self.persistence is not None else self.memory

    def _list_documents(self, request: Request) -> tuple[int, dict[str, Any]]:
        principal = request.caller
        self._tenant(request)
        include_superseded = request.query.get("include_superseded") == "true"
        rows = self.store.list_documents(
            principal.tenant_id, sorted(principal.groups), include_superseded=include_superseded
        )
        documents = [{key: _iso(value) for key, value in row.items()} for row in rows]
        return HTTPStatus.OK, {"documents": documents}

    def _get_document(self, request: Request) -> tuple[int, dict[str, Any]]:
        principal = request.caller
        self._tenant(request)
        row = self.store.get_document(
            principal.tenant_id, request.params["document_id"], sorted(principal.groups)
        )
        if row is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "document not found")
        return HTTPStatus.OK, {key: _iso(value) for key, value in row.items()}

    def _delete_document(self, request: Request) -> tuple[int, dict[str, Any]]:
        principal = request.caller
        document_id = request.params["document_id"]
        deleted = self.store.delete_document(
            self._tenant(request), document_id, sorted(principal.groups)
        )
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

    def retrieve(
        self, principal: Principal, question: str, *, gated: bool = True
    ) -> list[tuple[Any, float]]:
        """Return evidence the principal may read, best first.

        ``gated=False`` skips both evidence gates; evaluation uses it to see the
        full ranking and calibrate the thresholds.
        """
        tenant_id = principal.tenant_id
        gate = self.min_score if gated else 0.0
        relative = self.relative_score if gated else 0.0
        if self.persistence is None:
            chunks = self.memory.readable_chunks(principal)
            return RagQueryPipeline(chunks, min_score=gate, relative_score=relative).evidence(
                question, tenant_id
            )
        assert self.embedder is not None
        query = RagQueryPipeline((), min_score=gate, relative_score=relative)
        # Validate before any embedding or database work is spent on the query.
        safe_question = query.validate_query(question, tenant_id)
        rows = self.persistence.search_hybrid(
            tenant_id,
            safe_question,
            self.embedder.embed_query(safe_question),
            limit=20,
            model_name=self.embedder.model_name,
            groups=sorted(principal.groups),
        )
        # SQL already applied the ACL; re-check before rows become evidence.
        rows = self.reranker.rerank(safe_question, [r for r in rows if can_read(r, principal)])
        ranked = [
            (
                SimpleNamespace(
                    id=row["id"],
                    tenant_id=row["tenant_id"],
                    document_id=row["document_id"],
                    index=row["chunk_index"],
                    text=row["text"],
                    source=row["source"],
                    page=row.get("page"),
                ),
                float(row["score"]),
            )
            for row in rows
        ]
        return query.evidence(safe_question, tenant_id, ranked)

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
        prior: tuple[ChatTurn, ...] = ()
        if is_chat:
            assert isinstance(session_id, str)
            prior = self.history.get(tenant_id, principal.user_id, session_id)
        try:
            question = RagQueryPipeline(()).validate_query(question, tenant_id)
            # Follow-ups such as "and for contractors?" are rewritten into a
            # standalone query for retrieval; the model still sees the original.
            search_query = self.provider.rewrite_query(question, prior) if prior else question
            evidence = self.retrieve(principal, search_query)
        except UnsafeQueryError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc

        answer, used = ABSTENTION, []
        generation: Generation | None = None
        if evidence:
            generation = self.provider.complete(
                question, [chunk.text for chunk, _ in evidence], prior
            )
            candidate = strip_invalid_citations(generation.text, len(evidence))
            used = cited_sources(candidate, len(evidence))
            # An answer that cites nothing is not grounded; return the abstention.
            if used:
                answer = candidate
        citations = []
        for number in used:
            chunk, score = evidence[number - 1]
            citations.append(
                {
                    "source_number": number,
                    "document_id": chunk.document_id,
                    "source": chunk.source,
                    "chunk_index": chunk.index,
                    "page": getattr(chunk, "page", None),
                    "score": round(score, 4),
                    "excerpt": chunk.text[:240],
                }
            )
        response: dict[str, Any] = {
            "answer": answer,
            "grounded": bool(used),
            "citations": citations,
        }
        if generation is not None:
            usage_logger.info(
                "generation",
                extra={
                    "request_id": request.request_id,
                    "tenant_id": tenant_id,
                    "model": generation.model,
                    "prompt_tokens": generation.prompt_tokens,
                    "completion_tokens": generation.completion_tokens,
                    "cost_usd": generation.cost_usd,
                    "degraded": generation.degraded,
                },
            )
            response["generation"] = {
                "model": generation.model,
                "degraded": generation.degraded,
                "prompt_tokens": generation.prompt_tokens,
                "completion_tokens": generation.completion_tokens,
                "cost_usd": generation.cost_usd,
            }
        if is_chat:
            assert isinstance(session_id, str)
            self.history.append(
                tenant_id,
                principal.user_id,
                session_id,
                ChatTurn("user", question),
                ChatTurn("assistant", answer),
            )
            response["session_id"] = session_id
            response["history_messages"] = len(
                self.history.get(tenant_id, principal.user_id, session_id)
            )
            if search_query != question:
                response["search_query"] = search_query
        return HTTPStatus.OK, response


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
