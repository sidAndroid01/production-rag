# Production RAG — Phase 1 through Phase 6 foundations

This repository is being built phase by phase. **The published foundations now include document ingestion, a deterministic query flow, an authenticated API boundary, PostgreSQL persistence, API/database integration, local embeddings, and pgvector retrieval.** Hosted generation, evaluation, deployment, and remaining production controls will be added in later phases.

## Repository history and canonical implementation

This project is built in visible phases. Phases 1–2 begin with pure in-memory ingestion and lexical querying. Phase 3 adds the authenticated HTTP boundary while retaining that fallback. Phases 4–5 add transactional PostgreSQL persistence, selected with `DATABASE_URL`. Phase 6 adds local FastEmbed embeddings and tenant-scoped pgvector cosine retrieval.

The canonical implementation for these phases is the root-level modules `rag.py`, `query.py`, `api.py`, `persistence.py`, and `embeddings.py`, supported by `schema.sql` and `docker-compose.persistence.yml`. Real environment files and credentials stay outside Git; `.env.example` documents the expected configuration.

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
export DATABASE_URL='postgresql://rag:rag-local-password@localhost:5432/rag'
export RAG_TENANT_ID='default'
pip install "psycopg[binary]"
python3 api.py
```

At startup the API initializes the schema. Document ingestion writes the document and all chunks through `PostgresPersistence.persist()`. The original integration loaded only the requested tenant's chunks into the lexical query pipeline; Phase 6 replaces this with direct pgvector similarity search. The readiness endpoint checks the database connection when `DATABASE_URL` is configured. Without `DATABASE_URL`, the API remains an intentionally ephemeral in-memory demo.

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

The schema is in [`schema.sql`](schema.sql), and [`docker-compose.persistence.yml`](docker-compose.persistence.yml) runs a local PostgreSQL 17 instance with the pgvector image and a named volume. Docker is only the repeatable local runtime; the volume keeps database files when the container restarts.

Install the Python driver and start the database:

```bash
pip install -r requirements-embeddings.txt
docker compose -f docker-compose.persistence.yml up -d
```

Use the repository:

```python
from embeddings import LocalFastEmbedder
from persistence import PostgresPersistence
from rag import RagIngestionPipeline

store = PostgresPersistence("postgresql://rag:rag-local-password@localhost:5432/rag")
store.initialize()  # safe to run repeatedly
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
pip install -r requirements-embeddings.txt
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

## Phase plan

The repository will grow in this order:

1. **Phase 1 — ingestion foundation:** decode, sanitize, identify, normalize, and chunk.
2. **Phase 2 — query foundation:** validate, retrieve, gate, answer, and cite.
3. **Phase 3 — API boundary:** authenticated JSON routes, validation, health probes, and error mapping.
4. **Phase 4 — PostgreSQL persistence:** durable metadata, transactional chunks, idempotency, and pgvector readiness.
5. **Phase 5 — API/database integration (this commit):** database-backed ingestion, tenant-scoped reads, and readiness checks.
6. **Phase 6 — local embeddings and pgvector retrieval:** local model adapter, stored vectors, HNSW cosine index, and tenant-scoped SQL vector search.
7. **Phase 7 — hybrid retrieval and deterministic reranking (this phase):** PostgreSQL full-text search, GIN index, rank fusion, and transparent second-stage scoring.
8. **Phase 8 — retrieval hardening:** migrations, row-level security, metadata filters, deduplication, model-aware chunk limits, learned reranking, and query transformation.
9. **Phase 9 — generation and safety:** model gateway, grounded prompts, citation entailment, PII controls, and policy enforcement.
10. **Phase 10 — evaluation and operations:** golden datasets, retrieval/answer metrics, tracing, cost and latency budgets, retries, rate limiting, and deployment.

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
