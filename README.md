# Production RAG — built phase by phase

This repository is being built phase by phase. **The published foundations now include document ingestion, a deterministic query flow, an authenticated API boundary, PostgreSQL persistence, API/database integration, local embeddings, and pgvector retrieval.** Hosted generation, evaluation, deployment, and remaining production controls will be added in later phases.

## Repository history and canonical implementation

This project is built in visible phases. Phases 1–2 begin with pure in-memory ingestion and lexical querying. Phase 3 adds the authenticated HTTP boundary while retaining that fallback. Phases 4–5 add transactional PostgreSQL persistence, selected with `DATABASE_URL`. Phase 6 adds local FastEmbed embeddings and tenant-scoped pgvector cosine retrieval.

The canonical implementation for these phases is the root-level modules `rag.py`, `query.py`, `api.py`, `persistence.py`, and `embeddings.py`, supported by the ordered SQL files in [`migrations/`](migrations/), the [`migrate.py`](migrate.py) runner, and [`compose.yaml`](compose.yaml). Real environment files and credentials stay outside Git; `.env.example` documents the expected configuration.

The API now supports an explicit tenant identity binding. Set `RAG_TENANT_ID` for a deployed API instance; requests whose body tenant differs from that authenticated identity are rejected. The body field remains visible in the learning API so the data flow is easy to inspect, but production authentication should derive it from an API-key or JWT claim rather than trusting a client-selected tenant.

## What this phase does

`rag.py` turns one uploaded document into stable, inspectable chunks that later RAG stages can consume:

```text
raw bytes
  → strict UTF-8 decoding
  → document guardrail / role-label sanitization
  → SHA-256 content identity
  → whitespace normalization
  → bounded, word-aware overlapping chunks
  → typed chunk records
```

The implementation is deliberately self-contained and has no paid API, database, embedding model, LLM, or framework dependency. That makes the first contract easy to run from a laptop and easy to explain in an interview.

## Android-developer translation

Think of `RagIngestionPipeline.ingest()` as a pure repository use case. The byte array is the uploaded file, `filename` and `tenant_id` are request metadata, and `IngestResult` is the immutable result that a future backend adapter will persist. The pipeline does not own a screen, thread, HTTP client, or database connection. A later API layer can call it the way a ViewModel calls a repository method, while a background worker can call the same method for large files.

## The code, step by step

### 1. Input validation and decoding

`ingest()` rejects blank filenames and tenant IDs, then decodes the original bytes as UTF-8 with `errors="strict"`. Invalid bytes fail immediately instead of silently replacing characters and changing the document. In the future API boundary, this same stage will also enforce upload size limits, MIME allowlists, MIME sniffing, malware scanning, and parser isolation for PDF/HTML files.

### 2. Document sanitization

The document is untrusted data. `_sanitize_document()` rewrites line-leading `system:`, `assistant:`, and `developer:` labels to `[untrusted-document-label]:`. This is a small defense-in-depth measure so role-looking text is less likely to be treated as a model instruction later. It is not a complete prompt-injection defense; future phases add policy checks, isolation, and adversarial evaluation.

### 3. Content identity

The SHA-256 digest is calculated from the original bytes. The first 24 hexadecimal characters become `document_id`; the full digest is returned as `content_sha256`. Equal bytes therefore produce the same document identity, which gives later storage and idempotency work a deterministic key. Chunk IDs remain UUIDs because each chunk is an individual record; a later phase will define update and deduplication semantics explicitly.

### 4. Normalization and chunking

Whitespace is collapsed before splitting. The default target is 900 characters with 120 characters of overlap. The splitter prefers the last space before the limit, so words are kept together when possible. Overlap carries context across boundaries, which helps a future retriever answer questions whose evidence crosses two chunks. A progress guard handles a token longer than the target size without looping forever.

Chunk size is measured in characters here for clarity. A production implementation will benchmark token-based limits against the chosen embedding and generation models, preserve useful document structure, and record a chunker version so re-indexing is reproducible.

### 5. Typed output

Each immutable `Chunk` contains the text, content-based document ID, zero-based position, tenant, source filename, UUID, and UTC creation time. `IngestResult` returns the document identity, full digest, and tuple of chunks. No network side effect occurs in this phase: the caller decides whether and how to persist the result.

## Run the phase-one example

Requirements: Python 3.11+.

```bash
python rag.py
```

Expected shape of output:

```text
{'document_id': '<24 hex characters>', 'chunks_created': 1}
```

Use it from Python:

```python
from rag import RagIngestionPipeline

pipeline = RagIngestionPipeline(chunk_size=900, overlap=120)
result = pipeline.ingest(
    data=open("handbook.txt", "rb").read(),
    filename="handbook.txt",
    tenant_id="default",
)

for chunk in result.chunks:
    print(chunk.index, chunk.document_id, chunk.text)
```

## Current contract and deliberate boundaries

| Concern | Phase-one behavior | Deferred to a later phase |
| --- | --- | --- |
| Input | bytes expected to be valid UTF-8 | size, MIME sniffing, parsers, malware scan |
| Identity | SHA-256 of original bytes; 24-char ID | idempotent upserts and document versioning |
| Chunking | normalized character windows with overlap | token-aware, structure-aware chunking |
| Storage | returns records in memory | object storage, SQL metadata, vector database |
| Retrieval | not included | BM25, embeddings, hybrid search, reranking, filters |
| Generation | not included | model gateway, grounded prompt, citations, abstention |
| Security | basic role-label sanitization | authn/authz, tenant isolation, PII and threat controls |
| Operations | synchronous function | queues, retries, cancellation, metrics, tracing |

## Phase 2: standalone query flow

`query.py` consumes the immutable chunks returned by `rag.py` and runs the complete query-side contract:

```text
question + tenant ID
  → validate length and injection policy
  → filter chunks by tenant before scoring
  → tokenize and score lexical cosine similarity
  → select top-k candidates
  → reject weak evidence with a minimum-score gate
  → build a deterministic extractive answer
  → return citations, grounding status, and request ID
```

The query component is intentionally self-contained. It has no HTTP server, database, embedding model, or LLM dependency. `ChunkLike` is a small structural interface, so `rag.Chunk` can be passed directly while a future database adapter can return the same fields.

### Query example

```python
from query import RagQueryPipeline
from rag import RagIngestionPipeline

ingested = RagIngestionPipeline().ingest(
    b"The refund window is thirty days.", "policy.txt", "default"
)
response = RagQueryPipeline(ingested.chunks).query(
    "What is the refund window?", tenant_id="default"
)
print(response.answer)
print(response.citations[0].source)
```

The answer is grounded only when at least one tenant-scoped chunk reaches `min_score`. Otherwise the component abstains with an explicit message and returns no citations. This is a deterministic baseline for learning and benchmarking; lexical overlap is not semantic understanding. Later phases replace `_retrieve()` with hybrid BM25/vector search and replace extractive generation with a model gateway plus citation verification.

### Query contract and boundaries

| Concern | Phase-two behavior | Deferred to a later phase |
| --- | --- | --- |
| Query input | trim, length limit, injection patterns | richer policy engine and moderation |
| Security | tenant filter happens before ranking | authenticated identity and storage-enforced authorization |
| Retrieval | term-frequency cosine similarity, top-k | BM25, embeddings, hybrid search, reranking, metadata filters |
| Evidence gate | configurable `min_score` | calibrated threshold and evaluation-driven policy |
| Answer | deterministic extractive concatenation | LLM gateway, token budgets, grounded generation |
| Citations | source, chunk index, score, excerpt | entailment verification and richer provenance |
| Storage | chunks supplied in memory | persistent SQL/vector/search indexes |

## Phase 3: standalone API boundary

`api.py` exposes the first two components through a small JSON HTTP contract. It uses Python's standard library so the request lifecycle is visible without hiding it behind a framework.

```text
HTTP request
  → route and method check
  → API-key authentication (except health probes)
  → bounded JSON body
  → ingestion or query pipeline
  → consistent JSON response and request ID
```

Run it with:

```bash
python3 api.py
```

The phase-three API supports `GET /health/live`, `GET /health/ready`, `POST /v1/documents`, and `POST /v1/query`. Document requests use `{filename, tenant_id, content}` JSON, and query requests use `{question, tenant_id}` JSON. Send `x-api-key: local-development-key` for the two protected routes; set `RAG_API_KEY` to change it. The document content is held in memory and is lost when the process stops.

Example requests:

```bash
curl http://127.0.0.1:8000/health/live

curl -X POST http://127.0.0.1:8000/v1/documents \
  -H 'content-type: application/json' \
  -H 'x-api-key: local-development-key' \
  -d '{"filename":"policy.txt","tenant_id":"default","content":"Refunds are available within thirty days."}'

curl -X POST http://127.0.0.1:8000/v1/query \
  -H 'content-type: application/json' \
  -H 'x-api-key: local-development-key' \
  -d '{"question":"How long is the refund window?","tenant_id":"default"}'
```

The API layer handles transport concerns only. It does not add durable storage, semantic retrieval, or LLM generation. Those remain explicit future phases.

## Phase 5: API backed by PostgreSQL

Set `DATABASE_URL` to switch the API from its in-memory fallback to PostgreSQL:

```bash
export DATABASE_URL='postgresql://rag_app:<RAG_APP_DB_PASSWORD>@localhost:5432/rag'
export RAG_TENANT_ID='default'
uv sync --extra embeddings
uv run python api.py
```

`migrate.py` applies the schema as `rag_owner` and creates the restricted `rag_app` role (Phase 11). The API connects as `rag_app`; it never runs owner-only DDL. Document ingestion writes the document and all chunks through `PostgresPersistence.persist()`. The readiness endpoint checks the database connection when `DATABASE_URL` is configured. Without `DATABASE_URL`, the API remains an intentionally ephemeral in-memory demo.

This phase proves the application boundary survives process restarts and separates transport from storage. With the next phase enabled, it also uses local vector retrieval; without the database it keeps the lexical in-memory demo.

## Phase 4: PostgreSQL + pgvector persistence

`persistence.py` is the durable repository boundary. It stores document metadata and chunks in PostgreSQL and enables the `vector` extension. The initial schema left `embedding` untyped; Phase 6 migrates it to the selected model's `vector(384)` type. The write path is transactional: a document and all of its chunks are committed together.

```text
IngestResult
  → transaction begins
  → document upsert keyed by tenant + content hash
  → duplicate upload returns safely without new chunks
  → chunks inserted with ordering and foreign keys
  → indexes support tenant/document reads
  → transaction commits
```

The schema now lives in [`migrations/001_schema.sql`](migrations/001_schema.sql), and [`compose.yaml`](compose.yaml) runs a local PostgreSQL 17 instance with the pgvector image and a named volume (Phase 11 consolidated both). Docker is only the repeatable local runtime; the volume keeps database files when the container restarts.

Install the Python driver and start the database:

```bash
uv sync --extra embeddings
docker compose up -d postgres migrate
```

Use the repository:

```python
from embeddings import LocalFastEmbedder
from persistence import PostgresPersistence
from rag import RagIngestionPipeline

store = PostgresPersistence("postgresql://rag_app:<RAG_APP_DB_PASSWORD>@localhost:5432/rag")
result = RagIngestionPipeline().ingest(
    b"Refunds are available within thirty days.", "policy.txt", "default"
)
embedder = LocalFastEmbedder()
vectors = embedder.embed_documents([chunk.text for chunk in result.chunks])
print(store.persist(result, vectors, embedder.model_name))
print(store.list_chunks("default"))
```

The vector column is now constrained to the selected model's 384 dimensions, with a cosine HNSW index. See Phase 6 below for the embedding and retrieval path.

### Persistence contract and boundaries

| Concern | Phase-four behavior | Deferred to a later phase |
| --- | --- | --- |
| Database | PostgreSQL with pgvector extension | managed hosting and high availability |
| Atomicity | document and chunks in one transaction | job/outbox coordination for external embedding calls |
| Idempotency | tenant + original content SHA-256 | explicit document versions and deletion workflows |
| Tenant safety | tenant columns and scoped reads | database row-level security and application identity |
| Vector field | `vector(384)` for BGE-small | alternative model migration and versioned backfills |
| Files | chunk text in PostgreSQL | object storage for original uploads |
| Recovery | local named Docker volume | backups, point-in-time restore, replication, disaster recovery |

## Phase 6: local embeddings and pgvector retrieval

`embeddings.py` wraps FastEmbed and the open BGE-small English model (`BAAI/bge-small-en-v1.5`). It runs on the application machine's CPU, sends no text to a paid embedding API, returns 384-dimensional vectors, and batches document chunks. Model files download once from their distribution source and are cached locally; inference thereafter is local. There is no per-token API charge, though the initial download and local CPU/RAM use are real requirements.

Install the local runtime dependencies:

```bash
uv sync --extra embeddings
```

With `DATABASE_URL` configured, ingestion is now:

```text
document → chunks → local passage embeddings → one PostgreSQL transaction
```

Query is now:

```text
question → validate → local query embedding
  → tenant/model-filtered SQL vector search
  → cosine similarity score → evidence gate → answer + citations
```

The schema constrains `chunks.embedding` to `vector(384)` and creates a partial HNSW cosine index for embedded rows. SQL filters by tenant and embedding model before returning candidates. The API passes those scored rows to the shared query response builder for thresholding, abstention, and citation formatting. The no-database demo still uses the lexical in-memory retriever.

The selected model is English-focused and supports up to 512 input tokens. Our 900-character chunks can exceed that token limit in some cases; before calling this production-ready, we should add model-aware token limits/truncation checks and a backfill command for chunks stored before embeddings existed. See [FastEmbed's supported model list](https://qdrant.github.io/fastembed/examples/Supported_Models/) and [pgvector](https://github.com/pgvector/pgvector).

## Phase 7: hybrid retrieval and deterministic reranking

The persistent query path now retrieves candidates from two signals:

```text
question
  → semantic candidates from pgvector/HNSW
  → keyword candidates from PostgreSQL full-text search + GIN
  → reciprocal-rank fusion
  → exact-term overlap tie-breaker
  → final top-k evidence gate and citations
```

`search_hybrid()` keeps cosine similarity and `ts_rank_cd` on separate scales, fuses their ranks, and then applies a transparent second-stage score. This is a local deterministic reranker, useful as a production baseline and easy to test. It is not a learned cross-encoder; a later adapter can rerank the fused top 20–50 candidates with a BGE/Cohere/cross-encoder model while preserving the same response contract. The keyword side uses a generated `tsvector` column and a GIN index; the semantic side continues to use the HNSW cosine index.

## Phase 8: tenant database security and document lifecycle

PostgreSQL now enables and forces row-level security on both `documents` and `chunks`. Every repository transaction sets the trusted `app.tenant_id` session value before reading or writing, and policies reject rows belonging to another tenant even if an application query is accidentally broadened. Phase 11 replaces the original version marker with named migration files tracked in `applied_migrations`. `DELETE /v1/documents/{document_id}` removes a tenant-owned document and relies on the foreign-key cascade to remove its chunks.

## Phase 9: token-aware ingestion and retrieval evaluation

The chunker now targets token-like whitespace units instead of character counts, prefers sentence boundaries, preserves heading and paragraph text, and still guarantees progress for oversized tokens. The offline evaluator in [`evals/evaluate.py`](evals/evaluate.py) ran the first versioned golden set (replaced by `golden-v2.json` in Phase 15) and reports Recall@1/3/5, MRR, and nDCG@5. These metrics provide a regression gate for chunking and retrieval changes before we tune embeddings or reranker weights.

## Phase 10: pluggable chat, workers, permissions, and learned reranking

The chat path is now available at `POST /v1/chat`. It keeps bounded history by tenant, user, and session, then sends the retrieved evidence and previous turns to a model provider. By default the repository uses a free extractive fallback. To use a personal OpenAI-compatible provider, set `RAG_MODEL_BASE_URL`, `RAG_MODEL_API_KEY`, and `RAG_MODEL_NAME`; this also works with Ollama, vLLM, and LM Studio endpoints. The API never stores the key in the repository.

`POST /v1/ingestion/jobs` demonstrates asynchronous ingestion through a background worker. It is intentionally an in-process queue for learning; a durable queue such as Redis, SQS, or Kafka is still required for multi-instance production deployments. `permissions.py` defines the principal and group-ACL boundary before evidence reaches generation (enforced from Phase 13). `reranker.py` can load a local Sentence Transformers cross-encoder when the `reranking` extra is installed (`uv sync --extra reranking`), and falls back to the deterministic reranker when it is unavailable.

## Phase 11: one runnable, verified application

Before adding features, the repository needed to prove that what it ships is what it tests. Three things were out of step: an uncommitted FastAPI scaffold had grown next to the phase modules and was what the local Docker and CI configuration built; the local `pyproject.toml` only put that scaffold on the import path, so the committed phase tests could not even be collected; and the schema existed twice (in `schema.sql` and as a string in `persistence.py`) with no way to apply later changes.

```text
migrations/NNN_*.sql            ordered, reviewed schema changes
  → migrate.py (owner role)     applies each pending file once, in its own transaction
  → applied_migrations          records what ran; an advisory lock serializes deploys
  → rag_app password from env   no database password is committed
api.py (rag_app role)           never runs DDL; subject to row-level security
```

Run the whole stack:

```bash
cp .env.example .env        # set the three passwords/keys
docker compose up --build   # postgres → migrate (one-shot) → api
curl localhost:8000/health/ready
```

What changed:

- The scaffold moved to [`legacy/`](legacy/) unchanged, with the guide that described it. It is outside the build, lint, type check, and tests.
- `pyproject.toml` now describes these modules: `psycopg` is a core dependency, and `embeddings`, `reranking`, and `dev` are extras locked in `uv.lock`. `requirements-*.txt` are replaced by the extras.
- [`migrate.py`](migrate.py) applies [`migrations/`](migrations/) as the owner. `002_create_app_role.sql` creates `rag_app` without a password; `RAG_APP_DB_PASSWORD` sets it at deploy time.
- The [`Dockerfile`](Dockerfile) installs from the lockfile, bakes the embedding model into the image so the read-only container never downloads at request time, and runs `api.py` as a non-root user. `RAG_HOST` controls the bind address (loopback by default, `0.0.0.0` in the container).
- [CI](.github/workflows/ci.yml) starts a pgvector service, runs the migrations, and executes the whole suite, including the row-level-security integration test as the restricted role. It also builds the image.

**Android-developer translation:** this is the equivalent of making sure the APK you upload is built from the module your unit tests cover, and moving Room schema changes from "recreate the database" to numbered `Migration(n, n+1)` objects that run once per install.

## Phase 12: API correctness

Phase 10 grew the API faster than its dispatch code. The HTTP handler only implemented `do_GET` and `do_POST`, so `DELETE /v1/documents/{id}` worked in unit tests that called `handle()` directly but returned `501` to every real client. Jobs could be submitted but their status could not be read, a failing job recorded only `"failed"`, and the handler object was created at import time, which started a worker thread whenever any module imported `api.py`.

`api.py` now has a small route table instead of a chain of `if` statements:

```text
(method, path pattern) → handler method
  path matches, method does not → 405
  nothing matches               → 404
  protected route               → API key checked before the body is parsed
```

| Route | Purpose |
| --- | --- |
| `GET /v1/documents` | list the tenant's documents |
| `GET /v1/documents/{id}` | one document's metadata |
| `DELETE /v1/documents/{id}` | delete a document and its chunks (now reachable over HTTP) |
| `GET /v1/ingestion/jobs/{id}` | job status, result, and failure reason; visible only to the submitting tenant |

Other corrections:

- Identical uploads return `200` with `already_existed: true` in memory as well as in PostgreSQL, so re-uploading no longer duplicates evidence in answers. A new upload returns `201`.
- Empty documents are rejected with `422` in both storage modes.
- Job input is validated before it is queued; a job that fails later records why (input errors verbatim, unexpected errors as a generic message with the full traceback in the log).
- The request ID is echoed in an `x-request-id` response header; the body length is checked against the limit before reading.
- A new PostgreSQL integration suite found that `list_chunks()` had always failed on a real database (`created_at` was ambiguous across the join). It is fixed and now covered.

**Android-developer translation:** the route table is a navigation graph: each destination is declared once, and an unknown deep link has a defined result instead of falling through to whatever handler happens to be last.

## Phase 13: server-side identity and document ACLs

Until now the client chose its own identity. `tenant_id`, `user_id`, and `groups` all came from the JSON body; one shared API key admitted everyone; and a deployment could only serve the single tenant pinned by `RAG_TENANT_ID`. The group ACL in `permissions.py` read an `allowed_groups` field that did not exist in the schema, so it allowed every row, and a caller could have claimed any group anyway.

```text
x-api-key
  → SHA-256 digest → server-side key table → Principal(tenant_id, user_id, groups)
  → body tenant_id, if present, must equal the principal's (else 403)
  → SQL: tenant RLS + (no ACL OR ACL ∩ groups) before LIMIT
  → Python: can_read() re-checks each row before it becomes evidence
```

- [`auth.py`](auth.py) maps keys to principals. Keys are stored only as SHA-256 digests in the file named by `RAG_API_KEYS_FILE`; [`scripts/create_api_key.py`](scripts/create_api_key.py) generates a key and its entry. Without a key file, a single `RAG_API_KEY` is bound to `RAG_TENANT_ID` for local use, and `APP_ENV=production` refuses to start with the built-in development key.
- One process now serves any number of tenants: each request runs as its key's tenant.
- [`migrations/003_document_acl.sql`](migrations/003_document_acl.sql) adds `documents.allowed_groups` (empty means tenant-wide). Uploads accept `"allowed_groups": ["hr"]`.
- The ACL is applied in SQL for vector search, keyword search, listing, get, and delete, before `LIMIT`. Filtering only after retrieval would let restricted rows occupy the top-k slots and leave an authorized user with no evidence. A restricted document is indistinguishable from a missing one (`404`) to users outside its groups.
- Body `user_id` and `groups` are ignored. Chat history is keyed by the authenticated user.

```bash
python scripts/create_api_key.py --tenant acme --user alice --groups hr
# add the printed entry to secrets/api-keys.json, then set RAG_API_KEYS_FILE
```

Deduplication is per tenant and content: re-uploading identical bytes with a different ACL returns the existing document unchanged. Changing a document's ACL is a delete and re-upload for now.

**Android-developer translation:** this is the difference between trusting a `userId` extra in an `Intent` and reading the signed-in account from the server session. The client may say who it is; only the credential decides.

## Phase 14: grounded generation

Phase 10 connected a model, but the answer contract around it was loose:

| Problem | Effect |
| --- | --- |
| Retrieved chunks were placed in the **system** message | injected document text gained the highest instruction priority |
| The model received each chunk's 240-character citation excerpt | answers were generated from truncated evidence |
| Every retrieved hit was returned as a citation | the response claimed support the answer never used |
| Retrieval used only the latest message | "How often?" after "How are contractors paid?" searched for "How often?" |
| Cross-encoder logits (about −10 to +10) met a 0.10 threshold meant for cosine | relevant chunks with negative logits were dropped, irrelevant positive ones kept |
| `/v1/query` never used the configured model | only chat benefited from generation |

The answer path is now the same for query and chat:

```text
question → validate
  → (chat) rewrite follow-up into a standalone search query
  → hybrid retrieval → ACL → rerank (scores in [0, 1]) → evidence gate
  → model: system rules | history | user: <sources>[1..n] full text</sources> + question
  → drop citation markers that point at no source
  → keep only the sources the answer cites; no valid citation → abstain
```

- [`providers.py`](providers.py) owns the grounded-answer contract: the system prompt holds only rules; sources travel as delimited data in the user turn, and delimiter look-alikes inside documents are neutralized. `cited_sources()` and `strip_invalid_citations()` turn `[n]` markers into the response's `citations`, each with a `source_number` that matches the marker in the text.
- An answer with no valid citation is replaced by the standard abstention and reported as `grounded: false`. A fluent but uncited answer is treated as unsupported.
- `rewrite_query()` resolves follow-ups before retrieval. The OpenAI-compatible provider asks the model; the extractive provider (and the model path, if that call fails) prepends the previous user question. Chat responses include `search_query` when it differs from the question.
- [`reranker.py`](reranker.py) passes cross-encoder logits through a sigmoid and keeps the fallback score within [0, 1], so one threshold means the same thing on both paths. A missing `sentence-transformers` is detected once instead of on every query.

This is citation *alignment*, not entailment: the response now cites exactly what the answer claims to use, but whether each cited sentence supports its claim is measured in Phase 15's evaluation rather than verified per request.

**Android-developer translation:** the system prompt is like your app's manifest permissions and the sources are like content from a `ContentProvider` you do not own: you render it, but it never gets to declare permissions.

## Phase 15: evaluation that counts

Phase 9's golden set had four pre-chunked snippets and scored only the in-memory lexical retriever; it could not see the pgvector path, the evidence gate, generation, or citations. [`golden-v2.json`](evals/datasets/golden-v2.json) is a 15-document policy corpus with 50 questions: 34 literal, 6 paraphrased with little shared vocabulary ("Can I work from home?" against "Remote work is allowed…"), and 10 unanswerable questions that must abstain.

[`evals/evaluate.py`](evals/evaluate.py) now drives the real `RagApiApplication`: documents go through `POST /v1/documents` and questions through `POST /v1/query`, so ingestion, identity, ACLs, hybrid retrieval, both evidence gates, generation, and citation alignment are measured together.

| Group | Metrics |
| --- | --- |
| Retrieval (answerable) | Recall@1/3/5, MRR, nDCG@5, paraphrase Recall@5 |
| Answers | answer rate, abstention accuracy on unanswerable, citation precision, expected-term recall |
| Calibration | the configured gate and the threshold that best separates answerable from unanswerable top scores |
| Operations | `/v1/query` latency p50/p95 |

```bash
make eval                                                   # memory backend, with --check
RAG_EVAL_DSN=postgresql://rag_app:...@localhost:5432/rag \
  uv run python -m evals.evaluate --backend postgres --check
uv run python -m evals.evaluate --provider configured       # your RAG_MODEL_* model (costs tokens)
```

The first run exposed real defects, which this phase fixes:

| Finding | Fix | Hybrid before → after |
| --- | --- | --- |
| The 0.10 gate was designed for lexical scores; every hybrid score is above 0.3, so the API **never** abstained | per-backend default gates, calibrated from the score distributions: `0.40` hybrid, `0.15` lexical (`RAG_MIN_SCORE` overrides) | abstention 0.0 → 0.9 |
| Two of every three citations were off-topic runners-up | a relative gate keeps only chunks within 80% of the best hit (`RAG_RELATIVE_SCORE`) | citation precision 0.34 → 0.94 |
| Lexical scoring counted "the", "is", "do", so unrelated questions cleared the gate | stopwords are ignored by the lexical scorer | lexical abstention 0.4 → 0.8, paraphrase recall 0.67 → 1.0 |

Current results with the free extractive answerer:

| Metric | Memory (lexical) | PostgreSQL (hybrid) |
| --- | --- | --- |
| Recall@1 | 0.825 | 1.0 |
| Paraphrase Recall@5 | 1.0 | 1.0 |
| Answer rate | 0.85 | 1.0 |
| Abstention accuracy | 0.8 | 0.9 |
| Citation precision | 0.91 | 0.94 |
| Latency p95 | < 1 ms | ≈ 17 ms |

CI runs both backends with `--check` against [`evals/thresholds.json`](evals/thresholds.json) and fails the build on regression. Two honest limits: the corpus is small enough that each document is one chunk, so these numbers say little about long-document chunking; and with an LLM provider, citation precision measures alignment, not entailment. Grow the dataset (and recalibrate) before trusting the gates on a different corpus.

**Android-developer translation:** this is a macrobenchmark plus screenshot tests for answers. Unit tests prove a function returns; the evaluation proves the user-visible result stayed good, and CI blocks the merge when it does not.

## Phase 16: model gateway

A hosted model is a remote dependency that times out, rate-limits, and has outages. Until now one failed call surfaced as `500 internal server error`, nothing bounded how much context or history was sent, and nothing recorded tokens or cost.

[`gateway.py`](gateway.py) sits between the API and the providers:

```text
evidence + history
  → budget: pack sources in rank order into RAG_MAX_CONTEXT_TOKENS; keep recent history
  → for each provider (primary, then RAG_FALLBACK_MODEL_*):
       circuit open? skip it
       call → transient failure (timeout, connection, 429, 5xx)?
                retry with full-jitter exponential backoff, honoring Retry-After,
                within an overall deadline
            → other failure? next provider
  → all failed: extractive answer marked degraded (still cited, never a 500)
  → usage: prompt/completion tokens, cost from configured prices, logged per request
```

| Concern | Behavior |
| --- | --- |
| Retries | up to 3 attempts; only transient errors; delay from `Retry-After` or `uniform(0, min(8s, 0.5s·2^n))` |
| Circuit breaker | opens after 5 consecutive failures; one trial request after 30 s; success closes it |
| Deadline | no retry is started that would end after `RAG_GENERATION_DEADLINE_SECONDS` |
| Budgets | `RAG_MAX_CONTEXT_TOKENS`, `RAG_MAX_HISTORY_TOKENS`, `RAG_MAX_OUTPUT_TOKENS` (sent as `max_tokens`) |
| Accounting | response `generation: {model, degraded, prompt_tokens, completion_tokens, cost_usd}`; `rag.usage` log event |

Packing keeps sources in rank order and never renumbers them, so `[n]` citations still map to the right evidence when lower-ranked sources are cut. Query rewriting uses the first provider whose circuit is closed and falls back to the heuristic rewrite. Token counts come from the provider's `usage` field when present, otherwise a four-characters-per-token estimate.

**Android-developer translation:** this is an OkHttp interceptor chain for model calls: a retry interceptor with backoff, a circuit breaker in front of a flaky backend, and a cached/offline response when the network is down, so the screen shows something useful instead of an error state.

## Phase plan

The repository will grow in this order:

1. **Phase 1 — ingestion foundation:** decode, sanitize, identify, normalize, and chunk.
2. **Phase 2 — query foundation:** validate, retrieve, gate, answer, and cite.
3. **Phase 3 — API boundary:** authenticated JSON routes, validation, health probes, and error mapping.
4. **Phase 4 — PostgreSQL persistence:** durable metadata, transactional chunks, idempotency, and pgvector readiness.
5. **Phase 5 — API/database integration (this commit):** database-backed ingestion, tenant-scoped reads, and readiness checks.
6. **Phase 6 — local embeddings and pgvector retrieval:** local model adapter, stored vectors, HNSW cosine index, and tenant-scoped SQL vector search.
7. **Phase 7 — hybrid retrieval and deterministic reranking:** PostgreSQL full-text search, GIN index, rank fusion, and transparent second-stage scoring.
8. **Phase 8 — tenant security and document lifecycle:** forced PostgreSQL row-level security, tenant session context, migration marker, and tenant-scoped document deletion.
9. **Phase 9 — ingestion and evaluation:** token-aware sentence-bounded chunks, a versioned golden set, and retrieval metrics.
10. **Phase 10 — pluggable chat and platform adapters:** model provider configuration, bounded multi-turn history, worker queue, ACL boundary, and optional learned reranking.
11. **Phase 11 — runnable and verified:** one canonical app, migration runner, restricted DB role without committed passwords, full-stack Compose, container image, and CI against real PostgreSQL.
12. **Phase 12 — API correctness:** a route table, working DELETE, document listing, job status, and recorded job failures.
13. **Phase 13 — server-side identity:** API keys map to tenant, user, and groups on the server; persistent document ACLs filtered in SQL.
14. **Phase 14 — grounded generation:** documents kept out of the system prompt, citations limited to what the answer cites, conversational query rewriting, and calibrated reranker scores.
15. **Phase 15 — evaluation that counts:** a larger golden set with unanswerable questions, answer-level metrics, the real hybrid pipeline, and a CI regression gate.
16. **Phase 16 — model gateway (this phase):** retries with backoff, circuit breaker, fallback, token budgets, and usage accounting.
17. **Phase 17 — document lifecycle:** versioned re-uploads, sandboxed PDF/HTML parsing, page citations, and a durable job queue.
18. **Phase 18 — operations:** request logs, metrics, rate limits, a connection pool, durable chat history, and graceful shutdown.

Each phase adds a focused contract, tests, observability, and an updated README section when it is implemented.

## Interview questions this phase should answer

- Why decode with strict UTF-8? To fail visibly rather than corrupting source evidence.
- Why hash original bytes instead of normalized text? Identity should represent the uploaded artifact; normalization can change as the chunker evolves.
- Why overlap chunks? To preserve context at boundaries; too much overlap increases storage and retrieval duplication.
- Why prefer a word boundary? It improves semantic coherence, while the progress guard handles oversized tokens.
- Why keep tenant ID on every chunk? Future retrieval authorization must filter before scoring and before returning evidence.
- Why does this code not call an LLM? Ingestion should be deterministic, retryable, and independently testable; model calls belong behind later adapters.
- Why filter by tenant before scoring? Authorization must constrain the candidate set before relevance ranking or evidence construction.
- Why abstain? A fluent answer without sufficiently relevant evidence is a retrieval failure, so the contract makes uncertainty visible.
- Why keep retrieval and generation separate? It lets us measure retrieval quality independently and swap a lexical baseline for embeddings or a model gateway.
- Why keep the API layer thin? Transport validation and authentication should be testable separately from ingestion and retrieval logic.
- Why are health endpoints unauthenticated? Orchestrators need liveness and readiness probes before routing protected application traffic.
- Why is the phase-three store in memory? It keeps the API contract runnable; persistence and restart behavior are deliberately deferred.
- Why use PostgreSQL before a dedicated vector database? Documents, chunks, tenants, and jobs need relational constraints and transactions; pgvector lets us add similarity search without operating a second system.
- Why is the embedding dimension fixed now? The chosen BGE-small model emits 384 values; the database constraint catches accidental model/dimension mismatches.
- Why enforce uniqueness on tenant plus content hash? It makes retries and identical uploads idempotent within a tenant.
- Why use a Docker volume? Containers are replaceable processes; the volume keeps database files across restarts and container recreation.
- How do we avoid embedding API costs? FastEmbed runs an open model locally on CPU; initial model download and local compute are required, but no text is sent to a hosted embedding API.
- Why must query and document embeddings use the same model? Their vectors must inhabit the same learned coordinate space; mismatched models can return plausible-looking but meaningless distances.
- Why use cosine distance here? The query contract uses cosine similarity, and pgvector's `<=>` operator returns cosine distance; `1 - distance` converts it into a similarity score for the evidence threshold.
- What remains to make vector retrieval production-ready? Model-aware chunk token limits, old-chunk backfill, score calibration, ANN recall tests, query/index tuning for tenant filters, and operational monitoring.
- Why keep an in-memory fallback? It makes the transport contract runnable without a database, while `DATABASE_URL` selects durable behavior explicitly.
- What should readiness mean? Liveness means the process is running; readiness means required dependencies such as PostgreSQL can serve requests.

## License

This learning and portfolio project is provided for personal educational use.
