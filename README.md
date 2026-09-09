# Production RAG — Phase 1 and Phase 2 foundations

This repository is being built phase by phase. **The first two standalone foundations are now published: document ingestion and a deterministic query flow.** Embeddings, vector storage, hosted generation, evaluation, deployment, and the remaining production controls will be added in later phases after these boundaries are understood and tested.

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

## Phase plan

The repository will grow in this order:

1. **Phase 1 — ingestion foundation:** decode, sanitize, identify, normalize, and chunk.
2. **Phase 2 — query foundation (this commit):** validate, retrieve, gate, answer, and cite.
3. **Phase 3 — persistence and indexing:** durable metadata, idempotency, embeddings, and vector/keyword indexes.
4. **Phase 4 — retrieval quality:** hybrid search, reranking, query transformation, and metadata filters.
5. **Phase 5 — generation and safety:** model gateway, grounded prompts, verification, abstention, and policy controls.
6. **Phase 6 — evaluation and operations:** golden datasets, retrieval/answer metrics, tracing, cost and latency budgets, retries, and deployment.

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

## License

This learning and portfolio project is provided for personal educational use.
