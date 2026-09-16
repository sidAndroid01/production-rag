# API reference and local operation

[Guide home](README.md) · Previous: [Pipeline walkthrough](03-pipeline-walkthrough.md) · Next: [Quality and gaps](05-quality-and-gaps.md)

## Start the application

From the repository root, with Python 3.12+ and uv installed:

```bash
# Create the local configuration only if you do not already have it.
cp -n .env.example .env
uv sync --extra dev
uv run uvicorn rag_api.main:app --reload
```

The repository already had a local `.env` at inspection; preserve your settings. The examples below assume the development sample key. If you configured a different key, use it in your local requests.

Open `http://127.0.0.1:8000/docs` for interactive API documentation, `/redoc` for the alternative documentation, or `/openapi.json` for the schema. Requests to the two RAG endpoints need the `x-api-key` header.

Equivalent convenience commands are `make install` and `make run`. `uv run rag-api` invokes the packaged console entry point, which binds to `0.0.0.0:8000` without reload. The development Uvicorn command above uses its default loopback host.

Reloads restart the application and erase ingested data. Use one process for a predictable demonstration.

## A reproducible upload/query session

In a second terminal:

```bash
# Write the exact example bytes, without a trailing newline.
printf '%s' 'Employees receive twenty days of annual leave.' > /tmp/rag-guide-handbook.txt

curl -sS http://127.0.0.1:8000/health/live

curl -sS -X POST http://127.0.0.1:8000/v1/documents \
  -H 'x-api-key: local-development-key' \
  -F 'file=@/tmp/rag-guide-handbook.txt;filename=handbook.txt;type=text/plain'

curl -sS -X POST http://127.0.0.1:8000/v1/query \
  -H 'content-type: application/json' \
  -H 'x-api-key: local-development-key' \
  -H 'x-request-id: guide-example-001' \
  -d '{"question":"How many annual leave days?"}'
```

The expected bodies are in the [walkthrough](03-pipeline-walkthrough.md#exact-single-upload-example). Start from a fresh server and upload once for that exact result. Other stored documents can affect ranking; repeated uploads duplicate chunks.

Try a question with no shared terms to see abstention:

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/query \
  -H 'content-type: application/json' \
  -H 'x-api-key: local-development-key' \
  -d '{"question":"quasar spectroscopy"}'
```

With only the handbook indexed and the default positive threshold, this returns HTTP 200, the fixed insufficient-evidence answer, `citations: []`, and `grounded: false`.

## Endpoint reference

| Method and path | Authentication | Request | Success response |
| --- | --- | --- | --- |
| `GET /health/live` | None | None | 200, `{"status":"ok"}` |
| `GET /health/ready` | None | None | 200, `{"status":"ready"}` |
| `POST /v1/documents` | `x-api-key` | Multipart field `file` | 201, `IngestResponse` |
| `POST /v1/query` | `x-api-key` | JSON `{"question":"..."}`; optional `x-request-id` | 200, `QueryResponse` |

There are no list/get/delete document endpoints, chat history, streaming responses, query-time top-k overrides, tenant-management routes, or ingestion job endpoints.

### Authentication behavior

A missing or incorrect key produces HTTP 401 with `{"detail":"Invalid API key"}`. Successful comparison returns `default` as the tenant. An arbitrary tenant header does not change this identity. All holders of the one API key access the same corpus through HTTP.

The comparison is ordinary string equality. There is no key database, per-client identity, role authorization, expiry, rotation mechanism, or rate limiting. Setting `APP_ENV=production` does not enforce a stronger key or change the adapter selection.

### Error behavior

| Condition | Status | Response detail or behavior |
| --- | --- | --- |
| Missing/wrong API key | 401 | `Invalid API key` |
| File exceeds configured route read limit | 413 | `File is too large` |
| Declared content type not accepted | 415 | `Only text files are supported` |
| Invalid UTF-8 | 422 | `File must be UTF-8` |
| Missing multipart file | 422 | Framework validation details |
| Missing/empty `question`, wrong JSON shape, invalid JSON | 422 | Framework validation details |
| Whitespace-only question after stripping | 400 | `Query is empty or exceeds the configured length limit` |
| Stripped question too long | 400 | Same length-policy message |
| Query regex match | 400 | `Query matched a prompt-injection policy` |
| No relevant evidence | 200 | Abstention response, not an HTTP error |
| Empty/whitespace-only accepted upload | 201 | `chunks_created: 0` |
| Unexpected exception | 500 | `Internal server error`; exception logged |

These are individual cases, not a guaranteed precedence table when multiple parts of a request are invalid. Framework validation errors use a structured `detail` list; application errors generally use a string `detail`.

## Complete application settings

Settings come from [`core/config.py`](../../src/rag_api/core/config.py). Environment variables override `.env` values, with code defaults as fallback. Unrecognized settings are ignored. `get_settings()` is cached; restart the process after changing configuration.

| Environment variable | Default | Validation/use |
| --- | --- | --- |
| `APP_NAME` | `Production RAG API` | Exists in Settings, but FastAPI title is hardcoded in `main.py` |
| `APP_ENV` | `development` | Must be `development`, `test`, or `production`; no behavior switch currently consumes it |
| `LOG_LEVEL` | `INFO` | Passed to standard logging setup |
| `API_KEY` | `local-development-key` | Compared against the request header |
| `MAX_UPLOAD_BYTES` | `5000000` | Integer at least 1,024; route-level file read limit |
| `MAX_QUERY_LENGTH` | `2000` | Integer at least 32; applied after stripping question |
| `TOP_K` | `5` | Integer from 1 to 20; repository result limit |
| `MIN_RELEVANCE_SCORE` | `0.10` | Float from 0 to 1; inclusive relevance gate |

The commented `OPENAI_API_KEY` and `QDRANT_URL` examples do not connect providers. Neither is a field used by this pipeline. No API key for an LLM is needed. Chunk size and overlap are set in the `TextChunker` constructor and have no environment bindings.

The upload route calls `get_settings()` directly, while authentication receives it via FastAPI dependency injection. This matters when writing tests: overriding the authentication settings dependency alone does not change the route's direct call. The service also captures retrieval/query settings at startup.

## Quality commands

```bash
make check
```

Runs lint, formatting check, strict typing, and pytest coverage through uv. Individually:

```bash
make lint
make typecheck
make test
```

CI performs frozen dependency synchronization and enforces `--cov-fail-under=85`. `make test` reports coverage but does not itself enforce that threshold. Tests use FastAPI's in-process `TestClient`; no running HTTP server, external database, or model credentials are required. They assume the sample API key, so a different local setting can cause authentication failures in the current tests.

## Container configuration

The checked-in workflow for a local container is:

```bash
# Set API_KEY in your local .env before this command.
docker compose up --build
```

The Dockerfile installs uv into `python:3.12-slim`, copies package metadata and source, installs the project, switches to UID 10001, and launches Uvicorn on port 8000.

Compose adds a read-only root filesystem, writable `/tmp` tmpfs, dropped Linux capabilities, `no-new-privileges`, host port 8000, and a periodic liveness check. It passes `APP_ENV=production` and a required nonempty `API_KEY` into the container.

Limits of the configuration:

- It defines only the API service. There is no database, Redis, persistent volume, worker, or reverse proxy.
- Other application variables in the host `.env` are not automatically injected into the container; Compose explicitly passes only the two listed settings.
- The image installation does not use `uv.lock`; it installs from package dependency constraints. This differs from CI's frozen dependency resolution.
- No `.dockerignore` is checked in. The explicit `COPY` instructions do not copy `.env` into the image, but build-context filtering is still absent.
- Both health endpoints return constants. Readiness does not verify a repository or model provider.
- The container has no configured memory/CPU limits, TLS termination, backup policy, or deployment automation.

These are configuration observations. A Docker build or live container deployment was not run during this documentation task.

## Logging and diagnosis

`configure_logging()` sets a standard logging format and structlog processors for context, level, ISO time, and JSON output. The explicit application log event is the generic exception handler's `unhandled_exception`, with the request path.

There is no ingestion audit event, query latency event, token/cost metric, retrieval-score metric, tracing span, or request-ID middleware. Having JSON formatting configured does not mean every server/dependency log is structured or every request is correlated.

| Symptom | Likely explanation in this implementation |
| --- | --- |
| Query abstains after restart | The process-local corpus was lost |
| Different workers answer differently | Each worker has its own memory repository |
| Answer repeats text | Duplicate uploads or overlapping retrieved chunks |
| Relevant paraphrase is missed | Lexical overlap is weak; no semantic embeddings |
| Five citations but only three source passages in answer | Generator and citation builder use different hit counts |
| `grounded=true` but answer feels unrelated | Threshold is permissive; boolean only tracks admitted hits |
| A PDF upload fails or returns nonsense | There is no PDF parser; only UTF-8 decoding |
| Readiness says ready despite no documents | It is a constant response, not a corpus check |
| Long unusual text stalls ingestion | Inspect the chunk-progress defect before using such inputs |
