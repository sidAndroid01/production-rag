# RAG systems explained for an Android developer

## Purpose and scope

This guide prepares you to explain a RAG system from first principles, connect its backend design to your Android experience, walk through every component in this repository, and reason about production tradeoffs in AI interviews. Read the tutorial first, then practice the numbered questions aloud. The companion quick-revision guide uses exactly the same Q001 to Q220 identifiers.

Our current system is an offline FastAPI backend: it accepts text files, splits them into chunks, ranks chunks by lexical cosine similarity, and assembles extracted text with citations. It has no embedding model, persistent vector database, hosted LLM, or durable job queue. It includes useful engineering scaffolding and known correctness gaps. Describing that distinction accurately is part of interview preparation.

The code was re-inspected on 6 September 2026. The recorded local quality run from 5 September passed eight tests with 94.76% statement coverage, plus Ruff and strict mypy. This is software verification, not an answer-accuracy benchmark or evidence of production deployment. The advanced sections explain general or proposed capabilities and are not claims that they have been built.

## How to study

First understand one upload and one question without mentioning any frameworks. Then map each step to its source file. Next learn the retrieval and evaluation mathematics. Finally add production components one at a time, stating why they are needed and which metric or invariant they protect.

For each numbered question, the short answer is what you can say in the first fifteen seconds. The explanation supports a longer answer. The follow-up is a prompt to test your reasoning; its answer is usually developed elsewhere in the guide. No question bank can guarantee every possible interview question, so focus on transferable reasoning and accurate scope rather than memorization alone.

## Your Android knowledge already gives you a starting point

| Android concept | Backend connection | Important difference |
| --- | --- | --- |
| Retrofit interface | HTTP endpoint contract | The backend implements and enforces the contract for all clients |
| Kotlin DTO and serialization | Pydantic request and response models | Backend validation must treat client input as untrusted |
| Repository interface | ChunkRepository Protocol | A server repository must also handle shared data and authorization |
| Hilt constructor injection | RagService constructor and FastAPI Depends | Object/request lifecycle and Python structural typing differ |
| Application singleton | app.state.rag_service | One singleton exists per worker process, not across a deployment |
| In-memory cache | InMemoryChunkRepository | Restart loses the corpus; this is not durable storage |
| Room and migrations | Proposed database and schema migrations | Multiple server clients/workers write concurrently |
| suspend function | async def coroutine | Neither declaration automatically moves CPU work to another execution resource |
| WorkManager | Proposed durable ingestion jobs | Distributed workers additionally need leases, idempotency, and recovery |
| UI state with loading and errors | HTTP status and domain result states | Abstention is an intentional answer outcome, separate from a transport error |
| Logcat and tracing | Structured logs, metrics, distributed traces | Server observability must correlate many users and services safely |
| Gradle and dependency locking | pyproject.toml, uv, uv.lock | The build image must consume the tested dependency resolution |

These are bridges between concepts, not claims that the APIs or lifecycle semantics are identical.

## The story of one request

Imagine an employee uploads the sentence “Employees receive twenty days of annual leave.” Later they ask “How many annual leave days?” The backend must first have stored the evidence. The question itself does not ingest the document.

The Android client sends an HTTP request. Uvicorn accepts the connection and passes application events to FastAPI. FastAPI resolves the route, request model, and dependencies. Authentication checks the shared key and produces tenant default. The handler calls RagService, which performs the use case through the injected adapters. Pydantic serializes the result into JSON for the client.

There is no ViewModel or UI state on the server. The backend should provide a stable contract so the Android app can show loading, answer, no-evidence, authentication, validation, and retry states appropriately.

## Ingestion explained without skipping steps

1. The client sends POST /v1/documents with a multipart field named file and an x-api-key header.
2. Authentication compares the key with Settings.api_key. All accepted callers currently receive tenant default.
3. The route reads at most MAX_UPLOAD_BYTES + 1 bytes. A result larger than the configured limit is rejected with 413. This bounds the route read, not every earlier transport/parser resource.
4. The route compares the declared MIME string with text/plain, text/markdown, and application/octet-stream. It does not inspect the true file format.
5. RagService decodes bytes as strict UTF-8. Invalid sequences become a 422 response.
6. The sanitizer replaces selected line-leading role labels. It does not certify the remaining document as safe or authoritative.
7. SHA-256 is computed from the original bytes. The full digest is returned; the first 24 hexadecimal characters become document_id.
8. TextChunker flattens whitespace and uses character windows with intended overlap. Its defaults are 900 characters and 120 overlap.
9. Each Chunk receives text, document ID, index, tenant, source filename, fresh UUID, and UTC creation time.
10. The repository appends chunks to an in-memory list, skipping only IDs that existed before the add call.
11. The response is 201 with document_id, chunks_created, and content_sha256.

The file bytes are not retained in object storage. A duplicate upload receives new chunk UUIDs and is stored again. An empty or whitespace-only upload returns 201 with zero chunks. Restarting the app loses everything in the list. A pathological word-boundary case can stop the splitter from advancing; the implementation still needs correction.

## Querying explained without skipping steps

1. The client sends POST /v1/query with JSON question and its key, optionally adding x-request-id.
2. Pydantic checks the request schema; the service then strips the question and applies length and regex policy checks.
3. The repository tokenizes the question into lowercase ASCII alphanumeric counts.
4. It considers only chunks with the supplied tenant ID, which is default through the current HTTP API.
5. For each eligible chunk, it recomputes token counts and cosine similarity.
6. It sorts every eligible hit by descending score and returns at most TOP_K, default five.
7. RagService admits hits whose raw score is at least MIN_RELEVANCE_SCORE, default 0.10.
8. ExtractiveGenerator joins the first three admitted chunk texts. It ignores the question; no LLM is called.
9. If no hits were admitted, it returns the fixed insufficient-evidence text.
10. The service creates a citation for every admitted hit, with a 240-character prefix excerpt and score rounded to four decimals.
11. It returns answer, citations, request_id, and grounded. The last field means only that the admitted-hit list is nonempty.

This simple sequence is the baseline against which you can later evaluate embeddings, reranking, and model generation.

## The current mind map

![Current RAG implementation mind map](../guide/assets/current-system-mind-map.png)

The map depicts the inspected baseline. The numerical settings are defaults, not universal RAG recommendations.

## Every source component and how to explain it

| Component | Source file | Responsibility and interview detail |
| --- | --- | --- |
| FastAPI app and lifespan | src/rag_api/main.py | Composition root; constructs one service/repository per application lifespan; registers routes and a generic 500 handler |
| Uvicorn entry point | src/rag_api/main.py run | Starts the ASGI application on port 8000; rag-api console script calls it |
| HTTP routes | src/rag_api/api/routes.py | Four routes; upload checks; policy/decoding exception translation; response schemas |
| Request dependencies | src/rag_api/api/dependencies.py | Reads app.state service; checks shared key; returns default tenant; provides Annotated dependency aliases |
| Settings | src/rag_api/core/config.py | Pydantic environment/dotenv settings with bounds and cached construction |
| Logging setup | src/rag_api/core/logging.py | Configures JSON structlog processors; does not itself add request traces or useful business events |
| Internal data | src/rag_api/domain/models.py | Frozen Chunk and SearchHit dataclasses; chunk UUID and UTC timestamp factories |
| Public contracts | src/rag_api/domain/models.py | QueryRequest, QueryResponse, IngestResponse, and Citation Pydantic models |
| Replaceable ports | src/rag_api/domain/ports.py | Structural repository and generator protocols; no deletion, filters, streaming, or usage-result contract yet |
| Application use cases | src/rag_api/services/rag.py | Ingest/query sequencing, original-byte hashing, score gate, citation building, request ID, grounded flag |
| Text splitter | src/rag_api/services/chunking.py | Whitespace normalization and character overlap; known forward-progress defect |
| Input policy | src/rag_api/services/guardrails.py | Three query regex patterns, stripped length validation, role-label sanitation, UnsafeInputError |
| Lexical utilities | src/rag_api/adapters/memory.py | ASCII regex, Counter representations, cosine with zero-vector handling |
| Memory adapter | src/rag_api/adapters/memory.py | Process-local list, UUID-only add deduplication, tenant filter, full ranking and top-k |
| Extractive adapter | src/rag_api/adapters/memory.py | Fixed abstention or first-three-chunk concatenation; no learned generation |
| Package markers | src/rag_api/__init__.py and py.typed | Package description and marker exposing typing information |

## Data contracts to know by heart

| Type | Fields | What it does not prove |
| --- | --- | --- |
| Chunk | text, document_id, index, tenant_id, source, id, created_at | Durability, source authority, content deduplication, or effective policy date |
| SearchHit | chunk, score | Probability of a correct answer |
| QueryRequest | question with minimum schema length one | Maximum length is a later service policy; whitespace can still reach it |
| Citation | document_id, source, chunk_index, score, excerpt | Claim-level support or a clickable original source |
| IngestResponse | document_id, chunks_created, content_sha256 | Durable database inserts or idempotency |
| QueryResponse | answer, citations, request_id, grounded | Verified factual accuracy or complete request tracing |

## Settings reference

| Setting | Default | Constraint and actual use |
| --- | --- | --- |
| APP_NAME | Production RAG API | Exists but app title is hardcoded separately |
| APP_ENV | development | development, test, or production; no production adapter switch |
| LOG_LEVEL | INFO | Configures logging setup |
| API_KEY | local-development-key | One accepted shared key; unsuitable as real multi-user identity |
| MAX_UPLOAD_BYTES | 5000000 | At least 1024; bounded read from parsed upload |
| MAX_QUERY_LENGTH | 2000 | At least 32; service checks stripped characters |
| TOP_K | 5 | Between 1 and 20; limits repository results |
| MIN_RELEVANCE_SCORE | 0.10 | Between 0 and 1; inclusive similarity gate |
| TextChunker chunk_size | 900 | Constructor parameter, not environment configuration |
| TextChunker overlap | 120 | Constructor parameter; only overlap >= size is explicitly rejected |

Commented provider keys in .env.example do not connect a provider. Changing a local environment file does not automatically rebuild cached settings or the service constructed at startup.

## Tooling and operations reference

| File or dependency | Purpose | Current limitation |
| --- | --- | --- |
| pyproject.toml | Package metadata, runtime/dev dependencies, tool settings, Hatchling build, console entry point | Dependency version ranges alone are not a reproducible build |
| uv.lock | Resolved dependencies | Dockerfile does not consume it |
| FastAPI | Routing, dependency resolution, schema-driven HTTP API | Does not provide durable storage or production identity automatically |
| Uvicorn standard extras | ASGI server and runtime extras | Multiple processes would not share the memory list |
| pydantic-settings | Load and validate configuration | app_env does not enforce a production policy |
| python-multipart | Parse multipart upload requests | Application MIME check is still only a client claim |
| HTTPX | HTTP client dependency used through test tooling | No outbound model call exists in app code |
| structlog | Structured event formatting | Minimal actual application events today |
| pytest and pytest-cov | Assertions and coverage measurement | No golden RAG evaluation dataset or runner |
| Ruff and mypy | Lint, formatting, strict static typing | Cannot prove chunk-loop termination or retrieval quality |
| pytest-asyncio | Async test support available | Existing tests are mostly synchronous TestClient/unit tests |
| pre-commit | Dev dependency available | No pre-commit hook configuration checked in |
| Makefile | install, run, lint, typecheck, test, check | Local test reports coverage without the CI fail threshold |
| .github/workflows/ci.yml | Locked install, lint, format, types, tests, 85% coverage gate | No load, eval-quality, deployment, or remote run evidence in this task |
| Dockerfile | Python slim image, package install, non-root UID 10001 | No lockfile install, database, persistence, or TLS |
| compose.yaml | One restricted API container and liveness healthcheck | Only APP_ENV and API_KEY explicitly injected; no persistent services |
| .env and .gitignore | Local configuration excluded from Git | Not a secret-manager integration; never share local secret values |
| evals/datasets and scripts | Scaffolding directories in the inspected baseline | No evaluation data or runner was implemented there |

## Worked similarity example

For the exact document bytes without a trailing newline:

```text
Employees receive twenty days of annual leave.
```

The document has seven tokens: employees, receive, twenty, days, of, annual, leave. The question “How many annual leave days?” has five: how, many, annual, leave, days. Three coordinates overlap, each with count one.

```text
Dot product = 3
Question norm = sqrt(5)
Document norm = sqrt(7)
Cosine = 3 / sqrt(35) = 0.50709255...
Citation score = 0.5071
Gate = score >= 0.10, so this hit passes
```

The document ID is 8e2fb9ad438a79dd4d4b82e3 and full SHA-256 is 8e2fb9ad438a79dd4d4b82e31ee02beed2875c245c36a9f55485bf7e65a90288. A newline changes the bytes and therefore the hash. A repeated upload of identical bytes preserves that hash but produces more chunks with new UUIDs.

## Worked retrieval metric example

Suppose the relevant evidence set is A, C, and F. The top five returned items are B, A, D, C, E.

```text
Relevant returned items = A and C = 2
Precision@5 = 2/5 = 0.40
Recall@5 = 2/3 ≈ 0.667
First relevant rank = 2
Reciprocal rank = 1/2 = 0.50
```

MRR averages reciprocal rank over questions; one question’s reciprocal rank is not an entire benchmark. To show nDCG with graded relevance, suppose three results have grades 0, 3, 2 and use gain 2^grade − 1 with discount log2(rank + 1). DCG is 0 + 7/log2(3) + 3/log2(4), about 5.9165. The ideal order 3, 2, 0 gives about 8.8928, so nDCG@3 is about 0.6653. Conventions differ; write down the one you use. These are toy calculations, not project results.

## Worked sizing example

Assume one million chunks, each with a 768-dimensional float32 embedding. Raw vector values require 1,000,000 × 768 × 4 bytes = 3,072,000,000 bytes, or 3.072 decimal GB, about 2.86 GiB. This excludes database row overhead, index structure, text, metadata, replicas, backups, and rebuild headroom.

Document count alone does not determine chunk count. For an ideal fixed-window splitter with text length L, size S, and overlap O where 0 <= O < S, a useful long-text planning approximation is 1 + ceil((L − S)/(S − O)), floored to at least one for nonempty text. With L=100000, S=900, O=120, that yields 129 chunks. Our actual splitter adjusts word boundaries and has a progress defect, so this is not an exact predicted output of the implementation.

If average arrival rate is 20 queries/second and average time inside the measured system is 2 seconds under steady conditions, average work in flight is roughly 40 queries. This does not establish that the current worker can sustain that throughput. CPU utilization, memory, database/provider capacity, bursts, and queueing must be measured.

## Worked cost and latency example

Use hypothetical rates rather than memorize changing provider prices. If a model charged $1 per million input tokens and $4 per million output tokens, a request with 3000 input tokens and 300 output tokens would cost $0.003 + $0.0012 = $0.0042 for generation. Query embedding, reranking, ingestion, infrastructure, retries, and evaluation are additional costs. This is arithmetic practice, not a current provider quote.

A latency budget might allocate time to authentication, question embedding, parallel lexical/vector search, reranking, context packing, generation, and verification. Sequential durations accumulate; parallel branches generally wait for the slowest required branch. Do not add p95 values of stages and label that the end-to-end p95. Measure end-to-end distributions directly.

## A reproducible local demonstration

Run from the repository root with a configured development environment:

```bash
uv sync --extra dev
uv run uvicorn rag_api.main:app --reload
```

In another terminal, using the sample development key only if it matches your local configuration:

```bash
printf '%s' 'Employees receive twenty days of annual leave.' > /tmp/rag-interview-handbook.txt

curl -sS -X POST http://127.0.0.1:8000/v1/documents \
  -H 'x-api-key: local-development-key' \
  -F 'file=@/tmp/rag-interview-handbook.txt;filename=handbook.txt;type=text/plain'

curl -sS -X POST http://127.0.0.1:8000/v1/query \
  -H 'content-type: application/json' \
  -H 'x-api-key: local-development-key' \
  -H 'x-request-id: interview-demo-001' \
  -d '{"question":"How many annual leave days?"}'
```

For a fresh application with just one upload, the answer is “Based on the indexed sources: Employees receive twenty days of annual leave.” There is one citation with score 0.5071, request_id interview-demo-001, and grounded true. Querying “quasar spectroscopy” produces abstention with the default positive threshold. Restart and query again to demonstrate volatility. Upload the same file twice to demonstrate the current duplicate behavior. Do not run a known non-terminating chunk input in the live server as a demonstration.

## The planned production mind map

![Planned production RAG layers](../guide/assets/planned-system-mind-map.png)

These are proposed layers. A sensible progression is correctness and identity first, then durable data and evaluation, then semantic retrieval and generation, with observability and failure controls added alongside each dependency.

## Interview answering framework

When asked about a component, answer in this order: purpose, inputs and outputs, current implementation, tradeoff, failure case, and how you would test or measure a change. For a system-design question, first clarify corpus, users, permissions, freshness, scale, latency, answer quality, and cost. State assumptions before drawing infrastructure.

A strong ownership statement is: “I built an inspectable offline RAG baseline and can explain its mechanics and limitations. Persistent storage, real tenant identity, embeddings, model generation, and an evaluation benchmark are the next phases.” Do not claim production traffic, benchmark gains, or fixes that you have not actually implemented and verified.

## Suggested study sequence

1. Learn Q001 to Q050 and walk through every local module.
2. Learn Q051 to Q070 and calculate lexical similarity by hand.
3. Learn Q071 to Q100 to compare retrieval alternatives.
4. Learn Q101 to Q130 for grounded generation, evaluation, and security.
5. Learn Q131 to Q170 for persistence, reliability, deployment, and debugging.
6. Learn Q171 to Q200 and practice whiteboard scenarios and coding explanations.
7. Use Q201 to Q220 for backend and model follow-ups, revisiting weaker fundamentals first.

Study time varies with your starting knowledge. Use the revision guide for recall checks and return to this document whenever you cannot explain a one-liner with an example and a tradeoff.

## Interview questions and detailed answers
