# Quality, current limitations, and next steps

[Guide home](README.md) · Previous: [API and operations](04-api-and-operations.md)

## What verification actually shows

On 5 September 2026, the installed local environment ran the repository's existing checks successfully:

| Check | Observed result |
| --- | --- |
| `.venv/bin/ruff check .` | Passed |
| `.venv/bin/ruff format --check .` | Passed; 18 files already formatted |
| `.venv/bin/mypy` | Passed; no issues in 17 source files |
| `.venv/bin/pytest --cov=rag_api --cov-report=term-missing --cov-fail-under=85` | 8 passed; 94.76% statement coverage |

The local run used Python 3.13.5; the CI workflow configures Python 3.12. TestClient dependencies emitted deprecation warnings, but no test failed. This verifies local checks, not a remotely executed CI job or deployment.

High statement coverage is useful evidence that code paths execute. It does not establish retrieval quality, correct security boundaries, robust chunking for every input, or performance at production scale.

## What the eight tests assert

| Test | What it checks | What it does not establish |
| --- | --- | --- |
| `test_health_is_public` | Public liveness returns the expected JSON | Readiness dependencies or failure health |
| `test_query_requires_authentication` | Missing key on query gets 401 | Wrong-key handling, upload authentication, multiple tenants |
| `test_ingest_then_grounded_query` | Upload succeeds; query succeeds, is grounded, and cites the filename | Exact answer, score correctness, citation entailment, deduplication |
| `test_refuses_unsupported_file_type` | `image/png` gets 415 | Content sniffing, upload-size enforcement, parser safety |
| `test_chunks_overlap_and_preserve_content_shape` | More than one chunk; every chunk within configured size | Actual overlap, full content preservation, loop progress |
| `test_rejects_invalid_overlap` | Equal overlap and size raises `ValueError` | Negative values or other invalid configurations |
| `test_blocks_prompt_injection` | One known query pattern raises | Broad adversarial coverage or all regex variants |
| `test_neutralizes_roles_in_documents` | A `SYSTEM:` example gets the replacement prefix | Comprehensive document safety |

Source: [`tests/test_api.py`](../../tests/test_api.py), [`tests/test_chunking.py`](../../tests/test_chunking.py), [`tests/test_guardrails.py`](../../tests/test_guardrails.py).

Additional in-process experiments during documentation confirmed:

- Querying an empty corpus returns abstention and no citations.
- Reuploading identical bytes yields the same document ID but duplicates evidence and citations.
- Whitespace-only questions and the tested injection phrase return 400.
- An empty text file is accepted with zero chunks.
- Invalid UTF-8 produces 422.
- Direct repository search for another tenant does not return a stored tenant's chunk.

These were exploratory checks, not additions to the committed regression suite. A bounded trace of the chunking arithmetic also demonstrated the progress defect below without running an unbounded ingestion.

## Important implementation gaps

### 1. Chunking can fail to make progress

**Evidence:** [`TextChunker.split`](../../src/rag_api/services/chunking.py) moves the next start to `end - overlap` after selecting the last space in a window. There is no requirement that this value exceed the previous start.

With default settings and normalized input `'a ' + 'x' * 1000`, a bounded trace of the exact boundary calculations produces:

| Iteration | `start` | Chosen `end` | Next `start` |
| --- | --- | --- | --- |
| 0 | 0 | 1 | -119 |
| 1 | -119 | -1 | -121 |
| 2 | -121 | -1 | -121 |
| 3 | -121 | -1 | -121 |

The loop has reached a repeated state without reaching the document end. It can keep appending slices, consuming CPU and memory. Because chunking runs synchronously within the request task, it can block the event loop in that worker. An async function declaration does not prevent this.

**Needed:** require forward progress, validate positive sizes/nonnegative overlap, and cover long tokens, leading short words, whitespace, and boundary positions with regression tests. This should precede exposure to untrusted uploads. No source fix is included in this documentation task.

### 2. Tenant plumbing exists, but HTTP identity is shared

**Evidence:** the repository filters by `tenant_id`; `authenticate()` always returns `"default"` for the one accepted API key.

**Consequence:** repository-level separation is possible when called with different tenant IDs, but current API callers all share a tenant and its documents. “Tenant-isolated retrieval” in the original overview should be read with this qualification.

**Needed:** map authenticated credentials to a trusted tenant identity, enforce authorization at persistent storage, and test isolation through HTTP and the database. Client-supplied tenant labels alone would not establish authorization.

### 3. Content-derived IDs do not provide idempotency

**Evidence:** ingestion hashes original bytes for the document ID but constructs fresh chunk UUIDs. Repository deduplication compares those UUIDs.

**Consequence:** repeated uploads inflate memory, repeat answer text, and occupy top-k slots. The same bytes under a new filename can create additional chunks with another source label. Content IDs do not include tenant identity.

**Needed:** define tenant-scoped document identity/versioning and duplicate-upload semantics, then enforce appropriate uniqueness. Decide whether parser/chunker versions belong in identity before adding transformations and reindexing.

### 4. Storage is volatile and unbounded

There is no persistent store, original-file retention, delete operation, total corpus limit, or shared state across processes. Restarting loses data. Scaling replicas creates inconsistent corpora. Searching re-tokenizes and sorts candidates on every request.

**Needed:** persistence and migrations, bounded resource policy, deletion/lifecycle operations, appropriate indexes, and shared storage before multi-worker deployment. A database adapter must also define transactional behavior and failure handling.

### 5. Input controls are partial

The route bounds its read of the parsed upload and checks a caller-declared MIME value. It does not enforce the complete incoming HTTP body size, sniff file contents, scan malware, isolate parsing, or cap total stored content. Empty documents succeed with zero chunks. Query JSON body size is not capped at transport level.

The query regexes and document label replacement are narrow heuristics. There is no PII redaction, moderation, policy engine, or adversarial dataset. An extractive generator avoids model instruction-following behavior but can still return harmful or sensitive document content verbatim.

**Needed:** decide accepted document formats and empty-document behavior, add layered size/resource controls, and use isolated parsing when richer formats are introduced. Add stronger data handling policies according to the intended corpus and users.

### 6. Grounding and citations are evidence metadata, not verification

`grounded=true` means at least one hit passed a threshold. The score measures token overlap. The generator ignores the question, returns up to three chunks, and emits no claim-level citations; citation construction includes all admitted hits. A zero threshold permits irrelevant zero-score hits.

**Needed:** benchmark the retrieval gate, align citations with context actually used, and introduce answer/citation verification when generation becomes more capable. Avoid treating a similarity value as confidence or accuracy.

### 7. Operations are scaffolded

JSON formatting and a generic error event exist. Request tracing, request-ID correlation, latency/cost counters, audit events, meaningful readiness checks, and dependency health checks do not. The shared development key is not rejected in production mode.

The container has useful runtime restrictions but no persistent services, deployment automation, resource limits, or tested recovery process. Its dependency installation does not consume the lockfile. No Docker execution was verified in this task.

**Needed:** establish operational signals and failure behavior for each external dependency as it is introduced, align container dependency resolution with CI, and add resource/deployment controls appropriate to the runtime.

### 8. A quality benchmark has not been built

`evals/datasets/` contains no files, and no evaluation runner exists. The tests validate selected software behaviors, not answer quality across questions. There is no measured basis yet to call future embeddings or reranking an improvement.

**Needed:** version a representative dataset with questions, relevant documents/chunks, expected answers or criteria, and unanswerable/adversarial examples. Record the current baseline before changing retrieval.

## What the roadmap means in concrete terms

The [roadmap](../../ROADMAP.md) groups work into foundation, retrieval, generation/safety, quality, and portfolio milestones. Its checked foundation items describe available scaffolding; the qualifications above remain relevant.

| Planned capability | What must be added | Why it matters |
| --- | --- | --- |
| Postgres + pgvector | Schema, migrations, tenant policies, repository adapter, connection lifecycle | Shared durable data and vector search |
| Embeddings | Provider adapter, batching, retries, vector version/dimension tracking, cost accounting | Semantic similarity beyond word overlap |
| Hybrid retrieval | Lexical/vector candidate combination and calibrated ranking | Match exact terms and paraphrases |
| Reranking/deduplication | Candidate reranker and duplicate/overlap handling | Improve context precision |
| Background ingestion | Job queue, status, retries, object storage, idempotency | Keep slow parsing/embedding out of HTTP request lifetimes |
| Rich document parsing | PDF/HTML extraction, MIME sniffing, sandboxing, source-location metadata | Support real document collections safely |
| Model gateway | Provider calls, timeouts, fallback, circuit breaker, token budgets | Reliable generative answers |
| Structured citations | Response contracts, used-evidence tracking, entailment checks | Assess whether answer claims follow from sources |
| Policy controls | PII handling, moderation, configurable rules | Apply intended data-use policy |
| Redis limits/cache | Tenant-aware rate limits and properly scoped cache keys | Control abuse, latency, and recurring cost |
| Evaluations | Dataset, runner, metrics, regression gates | Measure changes rather than infer improvement |
| Telemetry | Traces, metrics, dashboards and correlation | Diagnose latency, failures, and spending |
| UI/deployment | Chat/document UI, IaC, autoscaling, backups, threat model | Operate and demonstrate the complete product |

These entries are planned work; no provider SDK or Compose service secretly implements them today. The commented Qdrant setting is a future hint, while the roadmap names Postgres/pgvector; neither is connected and no final database choice is implemented.

## Suggested build order

This is an implementation sequence inferred from the inspected gaps, not a change to the roadmap or a completed set of tasks.

1. **Make baseline behavior reliable.** Fix chunk progress; define duplicate/empty-upload behavior; add targeted regression tests. Resolve shared API tenant identity before supporting distinct customers.
2. **Measure the current baseline.** Add a small versioned golden set, including unanswerable questions. Capture retrieval and answer results so later changes can be compared.
3. **Make storage durable.** Add migrations, tenant authorization, repository persistence, lifecycle operations, and restart/isolation tests.
4. **Separate slow ingestion.** Add original-file storage and jobs with status/retry/idempotency as parsing and embedding make ingestion slower.
5. **Improve retrieval against the benchmark.** Add embeddings, hybrid candidates, and reranking incrementally. Recalibrate score thresholds after each ranking change.
6. **Add generation and verification.** Introduce a model gateway with budgets and failure policy; align and verify citations; retain reliable abstention behavior.
7. **Complete operations and delivery.** Add telemetry, rate limits, load tests, dependency-aware health, deployment, backups, and UI. Develop operational instrumentation alongside each new dependency rather than deferring it all to the end.

Useful future metrics include Recall@k (how much relevant evidence appears in the first k results), MRR (how early the first relevant result appears), and nDCG (ranking quality with graded relevance). Answer checks should separately measure faithfulness to evidence, relevance to the question, and citation precision. Latency percentiles and per-query cost address operating behavior. None of these metrics currently has an implemented runner.

## An accurate way to describe the project today

> I built a modular FastAPI RAG baseline with UTF-8 document ingestion, overlapping character chunks, in-memory lexical cosine retrieval, a relevance gate, extractive answers, and source metadata. It has a shared API-key boundary, tenant-aware repository contracts, input checks, tests, strict typing, CI configuration, and a restricted container configuration. Persistent storage, distinct authenticated tenants, embeddings, a model gateway, evaluations, and production operations remain next steps. Inspection also identified a chunk-progress defect and duplicate-ingestion behavior that need correction.

That description makes both the implemented engineering and the remaining work clear.
