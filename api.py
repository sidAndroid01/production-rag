"""Authenticated HTTP boundary for the RAG flows.

The server uses only Python's standard library so the request lifecycle stays
visible: a small route table maps (method, path) to one handler method each.
Request bodies are JSON; documents arrive as text or as base64-encoded files.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import os
import re
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from auth import ApiKeyStore
from embeddings import Embedder, LocalFastEmbedder
from gateway import configured_provider
from history import ChatHistoryStore, PostgresChatHistory
from memory_store import InMemoryStore
from observability import DailyTokenBudget, Metrics, RateLimiter, configure_logging
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
    def __init__(
        self, status: HTTPStatus, message: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or {}
        self.request_id: str | None = None


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
logger = logging.getLogger("rag.api")
usage_logger = logging.getLogger("rag.usage")
audit_logger = logging.getLogger("rag.audit")

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
        max_body_bytes: int | None = None,
        persistence: PostgresPersistence | None = None,
        embedder: Embedder | None = None,
        tenant_id: str | None = None,
        provider: ModelProvider | None = None,
        reranker: CrossEncoderReranker | None = None,
        keys: ApiKeyStore | None = None,
        min_score: float | None = None,
        relative_score: float | None = None,
        run_worker: bool | None = None,
        rate_limiter: RateLimiter | None = None,
        token_budget: DailyTokenBudget | None = None,
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
        # Uploads travel as base64 JSON, so a file can be ~3/4 of this size.
        if max_body_bytes is None:
            max_body_bytes = int(float(os.getenv("RAG_MAX_BODY_MB", "25")) * 1_000_000)
        self.max_body_bytes = max_body_bytes
        self.ingestion = RagIngestionPipeline()
        self.persistence = persistence
        self.embedder = embedder or (LocalFastEmbedder() if persistence is not None else None)
        self.provider = provider or configured_provider()
        self.reranker = reranker or CrossEncoderReranker()
        self.history: ChatHistoryStore | PostgresChatHistory = (
            PostgresChatHistory(persistence) if persistence is not None else ChatHistoryStore()
        )
        self.memory = InMemoryStore()
        self.metrics = Metrics()
        # Per caller (tenant/user) request rate, and per tenant daily model tokens.
        self.rate_limiter = rate_limiter or RateLimiter(
            float(os.getenv("RAG_RATE_LIMIT_PER_MINUTE", "120"))
        )
        self.token_budget = token_budget or DailyTokenBudget(
            int(os.getenv("RAG_TENANT_DAILY_TOKEN_BUDGET", "0"))
        )
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
            ("GET", re.compile(r"/v1/me"), self._me, True),
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
        """Route one request; log it once and record its metrics, whatever happens."""
        lowered = {key.lower(): value for key, value in headers.items()}
        request_id = lowered.get("x-request-id") or str(uuid4())
        context: dict[str, Any] = {"route": "unmatched", "principal": None, "detail": None}
        started = time.perf_counter()
        status = int(HTTPStatus.INTERNAL_SERVER_ERROR)
        try:
            status, payload = self._route(method, path, headers, body, request_id, context)
            return status, payload
        except ApiError as exc:
            status = int(exc.status)
            exc.request_id = request_id
            # Record why a request was refused, so a user's report can be explained.
            context["detail"] = exc.message
            if status == HTTPStatus.UNAUTHORIZED:
                audit_logger.warning("auth.failed", extra={"request_id": request_id})
            raise
        except Exception as exc:
            logger.exception("unhandled error", extra={"request_id": request_id})
            error = ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error")
            error.request_id = request_id
            raise error from exc
        finally:
            elapsed = time.perf_counter() - started
            route = context["route"]
            principal = context["principal"]
            self.metrics.inc(
                "rag_http_requests_total",
                "HTTP requests by route, method and status.",
                route=route,
                method=method,
                status=str(status),
            )
            self.metrics.observe_latency(elapsed, route=route)
            # Orchestrator probes arrive every few seconds; keep them out of INFO logs.
            logger.log(
                logging.DEBUG if route in {"live", "ready"} else logging.INFO,
                "request",
                extra={
                    "request_id": request_id,
                    "method": method,
                    "route": route,
                    "status": status,
                    "detail": context["detail"],
                    "duration_ms": round(elapsed * 1000, 2),
                    "tenant_id": principal.tenant_id if principal else None,
                    "user_id": principal.user_id if principal else None,
                },
            )

    def _route(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        request_id: str,
        context: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
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
            # Label metrics by handler, never by raw path (IDs would explode cardinality).
            context["route"] = handler.__name__.lstrip("_")
            principal = self._authenticate(headers) if requires_auth else None
            context["principal"] = principal
            if principal is not None:
                wait = self.rate_limiter.acquire(f"{principal.tenant_id}/{principal.user_id}")
                if wait > 0:
                    self.metrics.inc(
                        "rag_rate_limited_total",
                        "Requests rejected by the rate limiter.",
                        tenant=principal.tenant_id,
                    )
                    raise ApiError(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        "rate limit exceeded",
                        {"retry-after": str(max(1, round(wait)))},
                    )
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

    def close(self) -> None:
        """Stop background work and release database connections."""
        self.worker.stop()
        if self.persistence is not None:
            self.persistence.close()

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

    def _me(self, request: Request) -> tuple[int, dict[str, Any]]:
        """Who the presented key belongs to, and which backend answers; used by the UI."""
        principal = request.caller
        return HTTPStatus.OK, {
            "tenant_id": principal.tenant_id,
            "user_id": principal.user_id,
            "groups": sorted(principal.groups),
            "storage": "postgres" if self.persistence is not None else "memory",
            # Largest file the UI can send once base64 and JSON overhead are added.
            "max_file_bytes": int(self.max_body_bytes * 0.74),
            # Name the actual models behind the gateway, e.g. "llama3 → extractive".
            "model": " → ".join(
                [p.name for p in getattr(self.provider, "providers", [])] + ["extractive"]
            )
            if hasattr(self.provider, "providers")
            else getattr(self.provider, "name", "provider"),
        }

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
        audit_logger.info(
            "document.created",
            extra={
                "request_id": request.request_id,
                "tenant_id": request.caller.tenant_id,
                "user_id": request.caller.user_id,
                "document_id": stored["document_id"],
                "version": stored["version"],
                "already_existed": stored["already_existed"],
            },
        )
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
        audit_logger.info(
            "document.deleted",
            extra={
                "request_id": request.request_id,
                "tenant_id": principal.tenant_id,
                "user_id": principal.user_id,
                "document_id": document_id,
            },
        )
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
            if self.token_budget.remaining(tenant_id) <= 0:
                raise ApiError(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    "daily token budget exhausted",
                    {"retry-after": str(self.token_budget.seconds_until_reset())},
                )
            generation = self.provider.complete(
                question, [chunk.text for chunk, _ in evidence], prior
            )
            tokens = generation.prompt_tokens + generation.completion_tokens
            self.token_budget.spend(tenant_id, tokens)
            self.metrics.inc(
                "rag_generation_tokens_total",
                "Model tokens by model.",
                tokens,
                model=generation.model,
            )
            if generation.degraded:
                self.metrics.inc(
                    "rag_generation_degraded_total", "Answers served by the fallback answerer."
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


WEB_ROOT = (Path(__file__).parent / "web").resolve()
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


class RequestHandler(BaseHTTPRequestHandler):
    # Assigned by run() or tests; building it at import would start threads
    # and load models as a side effect of importing this module.
    application: RagApiApplication | None = None
    # Drop connections that stall mid-request (slow-loris style clients).
    timeout = 30
    cors_origins: frozenset[str] = frozenset()
    metrics_token: str | None = None

    def _common_headers(self) -> None:
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("cache-control", "no-store")
        self.send_header("referrer-policy", "no-referrer")
        origin = self.headers.get("origin")
        if origin and origin in self.cors_origins:
            self.send_header("access-control-allow-origin", origin)
            self.send_header("vary", "origin")
            self.send_header("access-control-expose-headers", "x-request-id, retry-after")

    def _respond(
        self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        if "request_id" in payload:
            self.send_header("x-request-id", str(payload["request_id"]))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self._common_headers()
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/metrics":
            self._metrics()
        elif path == "/" or path.startswith("/static/"):
            self._static(path)
        else:
            self._dispatch()

    def _static(self, path: str) -> None:
        """Serve the test UI from web/. Only known files; no directory traversal."""
        name = "index.html" if path == "/" else path.removeprefix("/static/")
        file = (WEB_ROOT / name).resolve()
        content_type = STATIC_TYPES.get(file.suffix)
        if file.parent != WEB_ROOT or content_type is None or not file.is_file():
            self._respond(HTTPStatus.NOT_FOUND, {"detail": "not found"})
            return
        encoded = file.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(encoded)))
        # Same-origin scripts and API calls only; the page loads nothing external.
        self.send_header(
            "content-security-policy",
            "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'",
        )
        self._common_headers()
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        """CORS preflight for browser clients listed in RAG_CORS_ORIGINS."""
        self.send_response(HTTPStatus.NO_CONTENT)
        if self.headers.get("origin") in self.cors_origins:
            self.send_header("access-control-allow-methods", "GET, POST, DELETE, OPTIONS")
            self.send_header(
                "access-control-allow-headers", "content-type, x-api-key, x-request-id"
            )
            self.send_header("access-control-max-age", "600")
        self._common_headers()
        self.send_header("content-length", "0")
        self.end_headers()

    def _metrics(self) -> None:
        application = self.application
        assert application is not None
        expected = self.metrics_token
        if expected and not hmac.compare_digest(
            self.headers.get("authorization", ""), f"Bearer {expected}"
        ):
            self._respond(HTTPStatus.UNAUTHORIZED, {"detail": "metrics token required"})
            return
        encoded = application.metrics.render().encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "text/plain; version=0.0.4")
        self.send_header("content-length", str(len(encoded)))
        self._common_headers()
        self.end_headers()
        self.wfile.write(encoded)

    def _dispatch(self) -> None:
        application = self.application
        assert application is not None, "RequestHandler.application is not configured"
        extra_headers: dict[str, str] = {}
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
            if exc.request_id:
                payload["request_id"] = exc.request_id
            extra_headers = exc.headers
        except Exception:
            logger.exception("transport error")
            status, payload = HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": "internal server error"}
        self._respond(int(status), payload, extra_headers)

    def log_message(self, format: str, *args: object) -> None:
        return  # RagApiApplication.handle writes one structured line per request.


class GracefulHTTPServer(ThreadingHTTPServer):
    # Non-daemon request threads + block_on_close: shutdown waits for in-flight requests.
    daemon_threads = False
    block_on_close = True


def run() -> None:
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    port = int(os.getenv("PORT", "8000"))
    # Loopback by default; containers set RAG_HOST=0.0.0.0 to accept traffic.
    host = os.getenv("RAG_HOST", "127.0.0.1")
    dsn = os.getenv("DATABASE_URL")
    persistence = (
        PostgresPersistence(dsn, max_size=int(os.getenv("RAG_DB_POOL_MAX", "10"))) if dsn else None
    )
    # Schema changes are applied separately by migrate.py as the owner role;
    # the API's restricted role cannot run DDL.
    application = RagApiApplication(persistence=persistence, keys=ApiKeyStore.from_environment())
    RequestHandler.application = application
    RequestHandler.cors_origins = frozenset(
        origin.strip() for origin in os.getenv("RAG_CORS_ORIGINS", "").split(",") if origin.strip()
    )
    RequestHandler.metrics_token = os.getenv("RAG_METRICS_TOKEN") or None
    server = GracefulHTTPServer((host, port), RequestHandler)

    def stop(signum: int, frame: object) -> None:
        logger.info("shutting down", extra={"signal": signum})
        # shutdown() blocks until serve_forever returns, so call it off this thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logger.info("listening", extra={"host": host, "port": port})
    try:
        server.serve_forever()
    finally:
        server.server_close()
        application.close()


if __name__ == "__main__":
    run()
