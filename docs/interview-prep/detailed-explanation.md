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

## 1 Backend foundations through Android concepts

A backend is a long-running application serving many clients. Android already gives you useful concepts: Retrofit consumes an API contract, Hilt constructs dependencies, repositories hide data access, and coroutines coordinate asynchronous work. The backend implements the other side of these contracts, while owning shared data, authorization, availability, and operational limits.

### Q001 What is a backend and what does ours do?

**Short answer:** Our backend accepts text documents and questions over HTTP, stores searchable chunks in memory, and returns extracted evidence with citations.

An Android app renders screens and sends network requests. A backend receives those requests on a server process and decides what data or operation the caller is allowed to access. Our FastAPI application exposes upload and query operations. It runs independently of any one Android screen or user session. Unlike a ViewModel, its service object can serve many callers during one process lifetime. Shared state, malicious inputs, simultaneous requests, and process restarts therefore become central design concerns.

**Self-test follow-up:** Explain what happens when two Android users call the same endpoint.

### Q002 What are HTTP requests and responses?

**Short answer:** A request has a method, URL, headers, and optional body; a response has a status, headers, and optional body.

With Retrofit you declare a request and deserialize a response. On the backend, FastAPI routes incoming requests to Python functions. POST /v1/query carries a JSON question and x-api-key header; the reply includes answer, citations, request_id, and grounded. Headers carry metadata, while the body carries the main payload. A network timeout is different from an HTTP error response: the former may leave the client unsure whether the server completed the operation.

**Self-test follow-up:** Why can retrying a timed-out upload create duplicates?

### Q003 How do GET and POST differ in this project?

**Short answer:** GET reads health state; POST carries document uploads or query bodies, and POST is not inherently idempotent.

GET is intended for safe retrieval. POST asks the target resource to process its representation and is not guaranteed idempotent by HTTP semantics. Our query POST is read-like at the business level even though it uses POST to accept a structured body; document POST changes stored data. Idempotency means repeating a request has the same intended effect as doing it once, not necessarily byte-identical responses. API designers must define retry behavior explicitly. Reference: [HTTP semantics](https://www.rfc-editor.org/rfc/rfc9110.html).

**Self-test follow-up:** Could a query endpoint use GET, and what would query strings expose?

### Q004 What are JSON, serialization, and multipart uploads?

**Short answer:** JSON represents structured request/response data; multipart separates uploaded files and fields into parts within one request.

Serialization converts an in-memory object into transferable data, while deserialization reconstructs an object from that data. Pydantic models describe our JSON contract, similar to Kotlin DTOs used with kotlinx.serialization or Moshi. A file upload uses multipart/form-data instead of embedding raw file bytes in JSON. The part named file has its own filename, declared content type, and content. That content type is a client claim, not proof about the bytes.

**Self-test follow-up:** Why is base64-in-JSON usually less efficient than a binary upload?

### Q005 What are FastAPI, Uvicorn, and ASGI?

**Short answer:** Uvicorn serves network traffic, ASGI is the application-server interface, and FastAPI maps requests to validated Python handlers.

Think of Uvicorn as the running HTTP server and FastAPI as the application routing and validation framework. ASGI specifies how asynchronous Python servers and applications communicate, including request events and application lifecycle events. The command uvicorn rag_api.main:app loads the app object from main.py. FastAPI does not independently make the process durable, globally scalable, or connected to a database; those are architectural and deployment responsibilities.

**Self-test follow-up:** Which component receives the network connection and which owns business logic?

### Q006 What are a process, thread, coroutine, and event loop?

**Short answer:** A process owns memory, threads execute within it, coroutines can suspend, and an event loop schedules asynchronous tasks.

Separate Uvicorn worker processes do not share our Python list. Within an event loop, a coroutine makes progress until it suspends or finishes. Another task can then run while I/O completes. This resembles Kotlin suspension conceptually, but Python asyncio has its own runtime rules; there is no automatic Android-style dispatcher switch. A synchronous CPU-heavy loop executed inside an async handler still monopolizes that event-loop thread. Multiple threads share process memory but require appropriate coordination.

**Self-test follow-up:** Why would adding workers make our corpus inconsistent?

### Q007 Does async or await make our retrieval parallel?

**Short answer:** No; our CPU-bound tokenization and sorting run synchronously despite async method signatures.

RagService awaits repository.search, but the in-memory implementation contains no awaited I/O. It counts tokens and sorts on the calling event loop. Awaiting a coroutine does not necessarily yield useful scheduling time if that coroutine never suspends. This is similar to putting a long calculation inside a Kotlin suspend function without moving or partitioning the work. Use appropriate worker processes, optimized native execution, or external indexed services for heavy CPU work. Framework-called synchronous handlers and directly called helpers have different scheduling behavior. Reference: [FastAPI concurrency](https://fastapi.tiangolo.com/async/).

**Self-test follow-up:** Would a timeout reliably interrupt a Python loop that never yields?

### Q008 What does stateless mean and is our API stateless?

**Short answer:** A stateless API keeps durable application state outside individual workers; our current corpus is process-local state.

Stateless does not mean there is no data anywhere. It means a request can reach any equivalent API replica because shared durable state lives in a database or another external service. Our authentication does not require a server-side login session, but our document storage still resides inside the worker. Therefore the whole service is not ready for interchangeable replicas. An Android analogy is relying on an in-memory singleton instead of Room for data that must survive process death.

**Self-test follow-up:** Would sticky sessions solve durability or only hide some routing problems?

### Q009 What are ports, localhost, DNS, and TLS?

**Short answer:** A hostname resolves to a server address, a port selects a service, localhost points to the caller itself, and TLS protects transport.

Port 8000 is where our development server listens. An Android device calling localhost targets the device, not your laptop. A production hostname needs routing and a reachable service, commonly behind a load balancer or reverse proxy. TLS encrypts and authenticates the transport connection; it does not decide which tenant may read a document. Our Compose file exposes an HTTP port but provides no TLS termination or public DNS configuration.

**Self-test follow-up:** Why can an endpoint work with laptop curl but fail on an Android device?

### Q010 How should an Android client handle backend outcomes?

**Short answer:** Represent loading, success, abstention, validation errors, authentication errors, and transport failures as distinct UI states.

A 200 response with grounded=false is an intentional no-evidence result, not necessarily a network failure. A 401 should lead to credential/session handling; 400 or 422 should guide input correction; a transient 5xx may be retryable under a bounded policy. Use lifecycle-aware requests and cancellation for obsolete questions, but do not assume client cancellation reverses completed server work. Display citations as source evidence and never embed a privileged provider secret in the APK.

**Self-test follow-up:** How would you preserve a chat message while showing that its answer request failed?

## 2 HTTP contract authentication and configuration

These questions map directly to api/routes.py, api/dependencies.py, and core/config.py. Keep authentication, request-shape validation, business policy, and error translation separate in your explanation.

### Q011 Which endpoints have we built?

**Short answer:** GET /health/live, GET /health/ready, POST /v1/documents, and POST /v1/query are the four application routes.

Both health routes are public and return constant JSON. Document ingestion requires an API key and a multipart file, then returns 201 with document_id, chunks_created, and content_sha256. Query requires an API key and JSON question, then returns 200 with answer, citations, request_id, and grounded. FastAPI also supplies documentation and schema routes. We have not built document listing, deletion, version history, streaming, chat sessions, or ingestion-job status endpoints.

**Self-test follow-up:** How would you add asynchronous job status without breaking existing clients?

### Q012 How does API key authentication currently work?

**Short answer:** The x-api-key header is compared with one configured key, and a successful match returns tenant default.

FastAPI resolves TenantDep through authenticate. Missing or different keys raise HTTPException with 401 and Invalid API key. The comparison uses ordinary string equality, and the settings default is the sample development key. There is no credential-to-organization mapping, key rotation mechanism, user identity, expiration, or roles. Every caller possessing that key shares the same HTTP-visible corpus. Describe this as a baseline authentication boundary, not complete enterprise identity management.

**Self-test follow-up:** What changes would support separate customers securely?

### Q013 How are authentication and authorization different?

**Short answer:** Authentication establishes identity; authorization decides whether that identity may access a specific resource or action.

An API key check asks whether the credential is accepted. Authorization must then constrain which documents, tenants, operations, and possibly fields the caller can access. Our repository can filter by a tenant argument, but the API always supplies default. A future service must derive tenant identity from verified credentials rather than trust a caller-provided tenant_id. Android hiding a button is UX, not server-side authorization.

**Self-test follow-up:** Why must a source-document download enforce authorization again?

### Q014 What does FastAPI dependency injection do here?

**Short answer:** Depends resolves Settings, RagService, and authenticated tenant inputs before invoking the endpoint.

ServiceDep reads the application-scoped service from request.app.state. TenantDep runs authentication, which itself depends on get_settings. Think of the concept as related to Hilt, but FastAPI resolves dependencies in the request handling system rather than generating an Android component graph. RagService also receives collaborators through its constructor, which makes its logic easier to test with fakes. The current get_settings direct call in the upload route differs from an overridable Depends use. Reference: [FastAPI dependencies](https://fastapi.tiangolo.com/tutorial/dependencies/).

**Self-test follow-up:** Which dependency would you replace to test a provider failure?

### Q015 What do 200, 201, 400, 401, 413, 415, 422, and 500 mean here?

**Short answer:** They represent successful query, successful upload, policy rejection, failed authentication, oversized file, unsupported MIME, invalid input, and unexpected failure.

The route maps excessive upload bytes to 413, disallowed declared MIME to 415, and invalid UTF-8 to 422. Missing fields or schema failures also produce framework 422 responses. The service raises UnsafeInputError for blank/long questions or regex matches, mapped to 400. Empty evidence still returns a successful 200 abstention. Unexpected exceptions reach the generic 500 handler. A future 429 would represent rate limiting, but no such limiter exists today.

**Self-test follow-up:** Why should clients not blindly retry all 4xx responses?

### Q016 Where are schema validation and policy validation applied?

**Short answer:** Pydantic validates the JSON shape first; RagService applies stripped length and query policy checks afterward.

QueryRequest requires question with minimum length one. An empty string fails schema validation with 422, but a whitespace-only string can satisfy that schema and then fail the stripped query policy with 400. MAX_QUERY_LENGTH is enforced in the service, not expressed as a Pydantic max_length in the request model. Type annotations alone are not equivalent to runtime validation everywhere: Pydantic models and internal dataclasses have different roles.

**Self-test follow-up:** How would you keep API error behavior consistent across similar input failures?

### Q017 What settings exist and what are their defaults?

**Short answer:** Defaults are 5,000,000 upload bytes, 2,000 query characters, top_k 5, minimum score 0.10, and the development API key.

Settings also includes app_name, app_env, and log_level. Bounds require upload limit at least 1,024; query length at least 32; top_k between 1 and 20; score between 0 and 1. APP_ENV accepts development, test, or production, but no adapter-selection branch uses it. APP_NAME is not used to set the hardcoded app title. Chunk size 900 and overlap 120 come from TextChunker defaults, not environment fields.

**Self-test follow-up:** Which setting can accidentally make irrelevant hits count as grounded?

### Q018 How do environment settings and caching work?

**Short answer:** Settings loads environment and .env values, while lru_cache reuses the Settings object until the process or cache is reset.

Environment values take precedence over dotenv values in the normal settings flow, with defaults used when values are absent. Our Settings ignores extra fields. get_settings caches its return value, and RagService captures selected values at startup, so editing .env does not live-update those collaborators. This resembles configuration injected once into a singleton. The upload route directly reads get_settings while authentication uses dependency injection, which can complicate test overrides. Reference: [Pydantic settings](https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings/).

**Self-test follow-up:** How would tests avoid inheriting a developer’s local API key?

### Q019 What are request IDs and what have we implemented?

**Short answer:** The query echoes a truthy x-request-id or generates a UUID, but it does not propagate that ID through logs and traces.

A request ID lets an Android error report be correlated with backend processing. Our field is included only in QueryResponse; no middleware binds it to structured logs, no tracing span uses it, and no response header is set by this code. A production implementation would validate length and format, generate trusted identifiers as needed, and correlate dependencies without treating client text as trusted log metadata.

**Self-test follow-up:** How would you find one slow request across API, retrieval, and model calls?

### Q020 What are CORS and API versioning concerns?

**Short answer:** CORS is a browser cross-origin policy, while /v1 is an API naming convention whose compatibility must still be maintained.

A native Android HTTP client is not governed by browser CORS enforcement in the same way as JavaScript running in a webpage. Adding CORS does not authenticate users or prevent direct API requests. Our code has no CORS middleware because no browser frontend is built. The /v1 prefix leaves room for future versions, but safe evolution still requires decisions about field defaults, renamed fields, enum changes, error contracts, and old-client support.

**Self-test follow-up:** Would adding a required request field break already installed Android clients?

## 3 Architecture models and startup lifecycle

Read main.py, domain/models.py, domain/ports.py, and services/rag.py together. This is where you can draw on Android clean architecture, repository interfaces, immutable data, and application-scoped objects without pretending the runtimes are identical.

### Q021 How are the code layers organized?

**Short answer:** Routes handle HTTP, RagService orchestrates use cases, domain models and ports define contracts, and adapters implement infrastructure.

api/routes.py should not need to know how cosine similarity is calculated. RagService decides the sequence of ingestion and query operations, using ChunkRepository and Generator interfaces. adapters/memory.py owns the current implementations. core contains configuration and logging. main.py wires concrete objects together. This resembles separation between UI/network boundary, use cases, domain contracts, and data-layer implementations in Android. It reduces coupling but does not itself guarantee correctness or scalability.

**Self-test follow-up:** Where should an SDK-specific timeout exception be translated?

### Q022 What happens during application startup?

**Short answer:** The lifespan function loads settings, configures logs, constructs adapters and RagService, and stores the service in app.state.

Uvicorn imports the module-level app, then FastAPI runs lifespan before serving normal requests. InMemoryChunkRepository starts with an empty list. TextChunker and InputGuardrail are instantiated, and configured relevance/query limits are passed to the service. The async context manager yields for the application lifetime. No cleanup appears afterward because there are no external resources today. Future database pools and HTTP clients should have explicit initialization and shutdown ownership. Reference: [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/).

**Self-test follow-up:** Should you create a new database pool for every request?

### Q023 What is a Python Protocol and how does it relate to a Kotlin interface?

**Short answer:** A Protocol defines a structural contract: an object can satisfy it by exposing the required methods without explicit inheritance.

ChunkRepository requires async add and search methods. Generator requires async generate. A Kotlin interface typically uses explicit implementation syntax, whereas Python structural typing can accept compatible objects without declaring that inheritance. Mypy checks compatibility statically. Runtime protocol behavior and data validation are separate concerns. Constructor injection allows a fake repository or generator to be supplied to RagService, avoiding network calls in focused tests.

**Self-test follow-up:** Does satisfying the repository signature prove tenant safety?

### Q024 What does RagService own?

**Short answer:** RagService owns ingestion/query sequencing, document identity, relevance filtering, citation construction, and response assembly.

For ingestion it decodes bytes, sanitizes text, hashes original bytes, chunks the sanitized text, creates Chunk objects, and calls repository.add. For queries it validates the question, searches with tenant and top_k, filters hits by score, calls the generator, builds citations, and chooses the request ID. It does not parse HTTP multipart boundaries or implement cosine scoring. Keeping orchestration here gives a clear place to define use-case behavior and failure policy.

**Self-test follow-up:** Would reranking belong in an HTTP route or a query use case?

### Q025 What fields does Chunk store?

**Short answer:** Chunk stores text, document_id, zero-based index, tenant_id, source filename, random chunk UUID, and UTC creation time.

Chunk is the searchable storage unit. It is a frozen, slotted dataclass, which discourages mutation and avoids a regular instance attribute dictionary. Each ingestion constructs new chunk IDs even when document bytes match an earlier upload. There is no embedding, page number, character offset, original-file location, access-control list, or parser/chunker version. Those missing fields matter when designing accurate citations, updates, migrations, and access checks.

**Self-test follow-up:** Which metadata would you add for a versioned PDF knowledge base?

### Q026 How do Chunk, SearchHit, Citation, and response DTOs differ?

**Short answer:** Chunk is stored evidence, SearchHit adds internal ranking, Citation exposes provenance, and response DTOs define the public API.

SearchHit pairs a Chunk with a floating-point score. Citation selects document ID, source, index, rounded score, and a prefix excerpt for the caller. QueryResponse adds the answer, request ID, and grounded flag. IngestResponse describes this upload’s generated pieces and content hash. This is analogous to separating Room entities, domain results, and API DTOs rather than exposing one database object everywhere. Public schemas deserve compatibility and information-exposure review.

**Self-test follow-up:** Why might returning every internal field create security or compatibility problems?

### Q027 What do frozen dataclasses, slots, UUIDs, and UTC timestamps buy us?

**Short answer:** They provide stable object fields, compact instance structure, generated chunk identity, and timezone-aware creation metadata.

Frozen does not mean arbitrary nested objects become deeply immutable, although our Chunk fields are mostly immutable values. Slots limits normal per-instance attributes, not persistence. UUID4 supplies a fresh random identifier, not deterministic content identity. A UTC timestamp avoids ambiguity about local timezone at construction, but it is not a document effective date or database commit timestamp. None of these mechanisms implements transactions, versioning, or deduplication.

**Self-test follow-up:** Why should a policy’s effective date be separate from ingestion time?

### Q028 How would we replace the memory repository or extractive generator?

**Short answer:** Implement the existing port, wire the adapter in main.py, and preserve semantic contracts with tests.

A persistent repository would store chunks and return SearchHit values for authorized searches. A model generator would consume question/context and return text. However, the tiny interfaces do not currently express streaming, token usage, model citations, filters, deletion, or job status. Those requirements may justify contract changes rather than forcing everything into a string. Also recalibrate the relevance gate when replacing cosine scores with a different retrieval scoring system.

**Self-test follow-up:** How would you support structured answer and usage metadata without leaking a provider SDK type?

### Q029 Why start without a RAG framework?

**Short answer:** A small explicit pipeline makes behavior, errors, tests, and tradeoffs inspectable before adding abstraction.

Our code directly exposes decoding, splitting, scoring, gating, and answer generation. That helps an interviewer assess whether you understand the mechanics. A framework can later save integration effort through loaders, retrievers, or orchestration, but its defaults still require evaluation and debugging. Avoid claiming frameworks are inherently bad or that hand-written code is inherently production-ready. The useful criterion is whether the abstraction makes the required behavior easier to implement, observe, and change.

**Self-test follow-up:** What would you inspect if a framework silently changed chunking defaults?

### Q030 What are the limits of our clean architecture?

**Short answer:** The boundaries are useful, but persistence semantics, tenant identity, observability, and richer provider failures are still incomplete.

Interfaces decouple implementations but cannot substitute for operational contracts. add returns no persisted insert count, search exposes no filters or retrieval diagnostics, and generate returns only text. Settings access mixes direct calls and dependencies. There is no database lifecycle, unit of work, job orchestration, or domain-specific timeout policy. In an interview, present these as known evolution points rather than asserting that ports alone make the system production-ready.

**Self-test follow-up:** Which interface would you evolve first when introducing paid model calls?

## 4 Document ingestion and identity

Ingestion transforms uploaded bytes into retrievable evidence. The key interview distinction is between validating transport, parsing content, assigning identity, transforming text, persisting output, and reporting completion. Our implementation performs these in a single request.

### Q031 Walk through an upload end to end.

**Short answer:** Authenticate, read at most limit plus one byte, check declared MIME, decode UTF-8, sanitize, hash original bytes, chunk, store, and return 201.

The route reads UploadFile and enforces its configured byte limit before delegating to RagService. The service strictly decodes, replaces role labels, hashes the original data, splits transformed text, constructs chunks with tenant/source metadata, and awaits repository.add. Returning 201 means this local call completed; it does not prove durable storage across restart. No background queue, object storage upload, embedding call, or document parser runs in this path.

**Self-test follow-up:** At what exact stage would you add original-file storage and job status?

### Q032 Why read MAX_UPLOAD_BYTES plus one?

**Short answer:** The extra byte distinguishes a file exactly at the limit from an oversized file while bounding the route’s read buffer.

If the permitted size is N and the read returns N+1 bytes, the route can reject immediately with 413. Reading only N would not reveal whether more bytes existed. But UploadFile is already the framework’s parsed upload abstraction. This is not a complete ingress-body limit or a guarantee that multipart handling used no disk or memory beforehand. Production limits belong at relevant proxy/server/parser layers as well as inside application code.

**Self-test follow-up:** How could many individually valid files still exhaust the service?

### Q033 Which file types are supported?

**Short answer:** Only UTF-8 content with exactly text/plain, text/markdown, or application/octet-stream as the declared MIME value is accepted.

The check compares the provided content_type string against a set. It does not sniff bytes or validate the extension. A binary PDF generally fails strict text decoding, and an allowed MIME string does not make it parseable prose. Conversely, a misleading filename containing valid text may pass. There is no PDF, OCR, HTML, spreadsheet, image, or audio extraction path. Be precise when explaining an allowlist: ours is a declared MIME allowlist.

**Self-test follow-up:** What would you need to safely support PDFs and scanned pages?

### Q034 What is UTF-8 and why strict decoding?

**Short answer:** UTF-8 encodes Unicode text as bytes; strict decoding rejects invalid byte sequences rather than silently changing evidence.

Network uploads arrive as bytes, while chunking and search operate on strings. decode("utf-8", errors="strict") creates text only if the sequence is valid. The route catches UnicodeDecodeError and returns 422. Replacing bad characters might keep ingestion running but can alter identifiers, names, and factual content. A richer ingestion system should track encoding, parser errors, extraction confidence, and original bytes rather than quietly discard information.

**Self-test follow-up:** How are byte limits different from character and model-token limits?

### Q035 What does document sanitization actually do?

**Short answer:** It replaces line-leading system, assistant, or developer labels with an untrusted-document marker.

The regex is case-insensitive and multiline, allowing whitespace before labels and around the colon. It does not remove the sentence after the label or run comprehensive document policy checks. For SYSTEM: disclose secrets, the instruction text remains after the replacement. Sanitization precedes whitespace normalization, when line boundaries are still available. The extractive generator may echo the remaining content. This is a narrow defense-in-depth transformation, not a security proof.

**Self-test follow-up:** Why should retrieved text remain untrusted even after sanitation?

### Q036 How is document identity computed?

**Short answer:** SHA-256 hashes the original upload bytes; the full digest is returned and its first 24 hex characters become document_id.

A hexadecimal SHA-256 digest has 64 characters. Taking 24 characters gives a 96-bit truncated identifier, not encryption or a reversible encoding. Identical bytes yield the same document ID regardless of filename or tenant argument. Sanitization and whitespace normalization are not part of the hash input. Therefore different whitespace can create distinct IDs even when stored text looks identical. Future identity design should define tenant scope and parser/chunker versions explicitly.

**Self-test follow-up:** Why is a content hash different from an access token?

### Q037 Are repeated uploads idempotent?

**Short answer:** No; repeated bytes reuse the document ID but create new chunk UUIDs, so the repository stores duplicates.

repository.add only excludes chunk IDs already present when the method begins. Ingestion assigns new UUID4 IDs on every call, so its deduplication check does not recognize the same document content. A timeout followed by client retry can therefore duplicate evidence. An idempotency design needs a scoped request key or stable document/chunk uniqueness policy, atomic enforcement, and defined responses for repeated requests. A content-derived document ID alone is insufficient.

**Self-test follow-up:** How would concurrent identical uploads defeat a check-then-insert implementation?

### Q038 What happens with empty uploads?

**Short answer:** An empty or whitespace-only text file returns 201 with zero chunks.

Normalization strips whitespace and TextChunker returns an empty list. The repository accepts that list, and IngestResponse still contains a content-derived document ID with chunks_created equal to zero. Nothing searchable has been added. Decide whether a production product should reject empty evidence, mark a document as extraction_failed, or allow a metadata-only record. The current behavior should be explained and tested rather than assumed to be a parsing error.

**Self-test follow-up:** How would the UI tell users that a scanned PDF contained no extracted text?

### Q039 What source and version metadata should production ingestion preserve?

**Short answer:** Preserve original location, tenant/ACL, document version, effective dates, parser/chunker versions, and page or character spans.

A citation needs a stable route back to the relevant source passage. Our filename and chunk index cannot identify a PDF page, highlight original text, or distinguish later document revisions. A production record should separate original identity from transformed artifacts and record lineage between them. Effective dates can differ from ingestion dates. Include checksums and state transitions so reprocessing is auditable and stale evidence can be retired predictably.

**Self-test follow-up:** How would you answer a policy question as of a date in the past?

### Q040 How do you handle updates and deletion?

**Short answer:** Create versioned ingestion output, atomically publish the active version, and invalidate retrieval and caches when data changes.

Updating chunks in place piecemeal can expose a mix of old and new versions. A safer design prepares a new version, validates it, then changes the active pointer in a transaction or equivalent controlled publication step. Deletion must cover searchable chunks, original files, derived summaries, embeddings, and cache entries under a defined retention policy. Our project has no update/delete endpoints or such workflow yet.

**Self-test follow-up:** What prevents a revoked document from being returned by a cached answer?

## 5 Chunking choices and failure cases

A chunk is the retrieval unit, not merely a string length setting. It determines what evidence can be found, how much irrelevant material enters context, how citations work, and how much indexing costs. Start from our exact implementation before discussing alternatives.

### Q041 Why split documents into chunks?

**Short answer:** Chunks make long documents searchable at useful granularity and let queries retrieve focused evidence.

Embedding or returning an entire long document can dilute a narrow answer inside unrelated material. Smaller chunks can improve precision but lose definitions, conditions, tables, or neighboring explanations. Chunking also affects the number of vectors, index size, retrieval redundancy, and source citations. In our system it bounds normal text pieces for lexical search and extraction. There is no model context limit forcing today’s 900-character choice, since no model is called.

**Self-test follow-up:** Why can a one-sentence chunk still be too small?

### Q042 What is our exact chunking algorithm?

**Short answer:** Collapse whitespace, take a 900-character window, prefer its last space, append the slice, and restart 120 characters before its end.

The algorithm normalizes all whitespace to single spaces and strips leading/trailing spaces. It uses a fixed character window, not token counting. A non-final window backs up to its last space when that boundary is greater than start. It then sets the next start to end minus overlap. For normal text this gives overlap, but the implementation does not ensure forward progress and can loop on unusual long tokens. Explain that defect rather than presenting the splitter as fully robust.

**Self-test follow-up:** Which loop invariant is missing?

### Q043 Why are characters, bytes, words, and tokens different?

**Short answer:** Bytes encode text, characters are string units, words depend on language rules, and tokens depend on a model tokenizer.

Our upload limit measures bytes, query limit measures Python string length, and chunk size measures normalized string length. An embedding or LLM provider usually enforces tokenizer-specific limits. A multibyte script can consume many bytes per character, and code or rare identifiers may split into many model tokens. A fixed conversion such as four characters per token is only a rough planning heuristic for some text, never a safe enforcement mechanism.

**Self-test follow-up:** How would you prevent a code snippet from exceeding an embedding model’s input limit?

### Q044 What is the chunk-size tradeoff?

**Short answer:** Smaller chunks improve focus but may lose context; larger chunks preserve context but add noise and consume more retrieval/generation budget.

For an HR policy, a short chunk containing an exception without the main rule can be misleading. A large chunk containing ten policies may match an unrelated word and overwhelm the answer. Test sizes against the real corpus and questions, considering answer span length, headings, tables, language, tokenizer limits, and retrieval model. Do not memorize one universal size. Our 900 characters are a baseline default with no quality benchmark behind the choice.

**Self-test follow-up:** How would you compare two chunk sizes without changing the embedding model?

### Q045 Why use overlap and what does it cost?

**Short answer:** Overlap preserves boundary context but increases stored text, duplicate hits, and downstream token cost.

A key sentence can straddle two windows. Repeating a portion helps at least one chunk retain relevant neighboring context. However, overlap also makes multiple chunks contain similar evidence, wasting top-k slots and causing our extractive answer to repeat text. For long text with ideal fixed windows, the approximate number of chunks depends on the stride chunk_size minus overlap; our word-boundary adjustments make actual counts vary. Deduplication or parent expansion can sometimes replace large overlap.

**Self-test follow-up:** How would you measure whether overlap improves recall enough to justify its cost?

### Q046 What is the known chunk-progress defect?

**Short answer:** A boundary too close to start makes end minus overlap fail to advance, potentially causing an infinite loop.

With default settings and text consisting of "a " followed by 1,000 x characters, the first chosen end is 1. The next start becomes -119; later boundary calculations reach start=-121 and end=-1 repeatedly. The loop never reaches the text end and keeps appending slices. Because this is synchronous work inside the request task, it can block the worker and consume memory. The existing tests do not catch it. A bounded arithmetic trace confirmed the issue without running an unbounded request.

**Self-test follow-up:** How would you prove termination after fixing the algorithm?

### Q047 How would you fix and test chunking?

**Short answer:** Validate sizes and enforce 0 ≤ start < next_start ≤ text_length for every non-final iteration, with boundary and long-token tests.

Require chunk_size to be positive and overlap to be nonnegative and smaller than size. Choose a word boundary only when the resulting progress is safe; otherwise use a full window or another explicit fallback that preserves coverage. Verify no skipped content outside intentional normalization, no empty accidental chunks, bounded lengths, intended overlap where feasible, and termination. Property-based tests can exercise arbitrary strings, while deterministic cases cover long tokens and short prefixes. This is a proposed fix, not a source change already made.

**Self-test follow-up:** Why is merely asserting that each chunk is short insufficient?

### Q048 What are recursive, sentence-aware, and semantic chunking?

**Short answer:** They choose boundaries using structural separators, sentence boundaries, or semantic changes instead of only a fixed character window.

A recursive splitter tries progressively smaller separators such as headings, paragraphs, sentences, then characters. Sentence-aware splitting groups complete sentences within a budget. Semantic splitting estimates topic shifts, often using embeddings, and introduces additional compute and tuning. These methods can preserve meaning better but still need language, table, code, and size safeguards. None is implemented here. Compare them through retrieval quality and source fidelity, not because a name sounds more advanced.

**Self-test follow-up:** When could semantic chunking split a definition from its exception?

### Q049 What is parent-child or small-to-big retrieval?

**Short answer:** Retrieve small precise child chunks, then provide authorized surrounding parent context to the generator.

A narrow child can match a specific question while its parent section supplies scope and caveats. Store parent IDs or source offsets so expansion is deterministic. Deduplicate parents and enforce a context budget, otherwise retrieving several children may repeatedly include the same large section. Authorization must apply to expanded content too. Our current Chunk does not store parent relationships, so this would require metadata and retrieval changes.

**Self-test follow-up:** How do you avoid expanding a permitted paragraph into a restricted parent document?

### Q050 How should tables, code, scanned PDFs, and languages affect chunking?

**Short answer:** Preserve their structure and provenance; generic whitespace flattening can destroy the relationships needed for correct answers.

Tables need row/column association and repeated headers, code needs functions/classes and indentation, and scanned PDFs need OCR with extraction-quality checks. Multilingual text needs appropriate segmentation and tokenization. Our normalizer flattens Markdown and code layout, and the retriever recognizes only ASCII alphanumeric tokens. A production parser should preserve logical blocks and source coordinates before selecting chunks. An unreadable extraction should not silently become trusted evidence.

**Self-test follow-up:** How would you cite one cell from a policy table with the correct row and column labels?

## 6 Lexical retrieval and the current answer path

Retrieval selects evidence; generation uses it. Our implementation makes this distinction particularly visible because its retriever is lexical and its generator simply concatenates text. A correct interview explanation must not call these learned embeddings or an LLM.

### Q051 How does our tokenizer work?

**Short answer:** It lowercases text and counts matches of the ASCII pattern [a-zA-Z0-9]+.

Counter turns the matched tokens into word-frequency counts. "Leave, leave in 2026!" becomes leave:2, in:1, 2026:1. Punctuation separates tokens, case is discarded, and there is no stemming, stop-word removal, synonym expansion, or inverse-document-frequency weighting. Non-Latin writing can produce no tokens and accented words can be fragmented. The function is used on both the question and every eligible chunk each time search runs.

**Self-test follow-up:** Will vacation necessarily retrieve a document containing only annual leave?

### Q052 What is a term-frequency vector?

**Short answer:** It is a sparse mapping from each token to the number of times that token occurs.

Imagine each distinct word as a coordinate axis. A document with two occurrences of leave has value two on the leave axis. A Counter stores only nonzero coordinates rather than allocating a huge dense vocabulary array. These counts are numeric vectors, but they are not learned semantic embeddings. Their geometry expresses lexical overlap. Common words can still influence the score because our implementation has no IDF weighting.

**Self-test follow-up:** Why is a vector representation not automatically semantic search?

### Q053 How is cosine similarity computed?

**Short answer:** Cosine is the dot product divided by the product of vector lengths; zero-norm inputs return zero in our implementation.

For counters q and d, sum q[t]×d[t] over shared tokens, then divide by sqrt(sum q[t]^2)×sqrt(sum d[t]^2). Cosine measures directional alignment. With our nonnegative counts it lies between zero and one; general signed dense vectors can have negative cosine values. It is not the probability that a chunk answers the question. Normalization reduces raw length effects but does not understand negation or relevance.

**Self-test follow-up:** Why can two opposite statements still have high lexical cosine similarity?

### Q054 Work out the handbook similarity by hand.

**Short answer:** The question and handbook share three tokens, so cosine is 3 divided by sqrt(5×7), approximately 0.5071.

The question "How many annual leave days?" has five distinct tokens. "Employees receive twenty days of annual leave." has seven. The shared tokens are annual, leave, and days, each with count one. Thus the dot product is three, and vector norms are sqrt(5) and sqrt(7). The unrounded result is about 0.50709255. It passes 0.10, and Citation rounds it to four decimals. This example assumes exactly that single chunk.

**Self-test follow-up:** What changes if leave appears twice in the document?

### Q055 In what order do tenant filtering, top-k, and thresholding happen?

**Short answer:** Search scores only matching-tenant chunks, sorts and takes top-k, then RagService applies the minimum score.

The tenant check is inside the repository comprehension before hit construction. The repository still includes zero-score eligible chunks among candidates, sorts descending, and slices to the limit. The service keeps hit.score >= min_score. Since the same monotonic score is used, filtering earlier would produce the same admitted top-k set here; more complex approximate/filtering systems can behave differently. Equal scores preserve list insertion order because Python sorting is stable.

**Self-test follow-up:** Why is top-k alone insufficient to determine whether evidence is relevant?

### Q056 What does MIN_RELEVANCE_SCORE mean?

**Short answer:** It is an inclusive lexical similarity cutoff, not a calibrated confidence or universal threshold.

With the default 0.10, a chunk scoring exactly 0.10 passes. If set to zero, unrelated zero-score chunks can pass and grounded becomes true when the corpus has eligible entries. Increasing the threshold can reduce irrelevant evidence but increase abstention. The right choice depends on the corpus, query distribution, and score function. Reranker, BM25, cosine-distance, and rank-fusion scores do not share this same interpretation.

**Self-test follow-up:** Why must you retune the gate after replacing the retriever?

### Q057 How does ExtractiveGenerator produce an answer?

**Short answer:** It ignores the question and joins the text of the first three admitted hits after a fixed prefix.

The generate method explicitly deletes its question parameter. For a nonempty context list it strips and joins up to three chunk texts, then prefixes "Based on the indexed sources:". This is deterministic extractive composition for the given hit order. It does not synthesize a concise answer, reconcile contradictions, translate, or call a model. A repeated upload can make the same sentence appear multiple times. Describe the component as an offline generator adapter, not an LLM.

**Self-test follow-up:** Why would twenty days appear as a copied sentence rather than a reasoned numeric answer?

### Q058 How do abstention and grounded work?

**Short answer:** No admitted hits produce a fixed insufficient-evidence answer; grounded is simply whether the admitted-hit list is nonempty.

RagService always invokes the generator with the filtered list. The generator returns the fixed abstention text when that list is empty, and the response contains no citations and grounded=false. When any hit exists, grounded=true even if the passage is misleading or irrelevant to the actual question. This boolean is an implementation signal, not an independent faithfulness assessment. A production confidence policy should separate retrieval sufficiency, authorization, and answer verification.

**Self-test follow-up:** Can an answer be factually true but ungrounded in the supplied sources?

### Q059 What do our citations prove and where can they mislead?

**Short answer:** They identify admitted chunks and show prefix excerpts; they do not establish that each answer claim is supported.

Each citation contains document_id, source filename, zero-based chunk index, score rounded to four decimals, and the first 240 characters. All admitted hits get citations, while only the first three contribute answer text. Therefore five citations can accompany an answer based on three chunks. A prefix excerpt can omit the relevant sentence, and filename metadata may be client-chosen. There are no claim-level inline markers, source URLs, or entailment checks.

**Self-test follow-up:** How would you ensure only actually used evidence appears in citations?

### Q060 What is the complexity and scalability of current search?

**Short answer:** It scans and tokenizes all matching-tenant chunks, then fully sorts their scores, with no search index or cached vectors.

Let N be eligible chunks and T their total text/token-processing size. Tokenization and scoring are broadly linear in T plus per-vector operations; sorting N hits costs O(N log N). Returning five results does not avoid scoring the others. The list grows without a total corpus cap, and duplicates multiply work. A small baseline is fine for inspection, but large corpora need indexing, persistence, and measured concurrency/resource behavior.

**Self-test follow-up:** Would replacing full sorting with a bounded heap remove the dominant tokenization cost?

## 7 RAG and language model fundamentals

A modern RAG system combines a retriever with a generative model, but retrieval quality, source authority, and access control remain separate problems. Our offline pipeline demonstrates the retrieval and response boundaries without a learned generator.

### Q061 What is RAG?

**Short answer:** RAG retrieves relevant external evidence and conditions answer generation on that evidence at query time.

The broad architecture separates a knowledge corpus from model parameters. Ingested evidence can be updated without retraining the language model, while the query path finds useful context for an answer. The original RAG research combined parametric language generation with nonparametric retrieval; practical systems vary widely in implementation. Our project is a simplified retrieval-plus-extractive-response baseline. Reference: [Lewis and colleagues on RAG](https://arxiv.org/abs/2005.11401).

**Self-test follow-up:** Which problems does retrieval solve that a pretrained model alone does not?

### Q062 Why use RAG for enterprise knowledge?

**Short answer:** It can supply current, private, attributable information, provided ingestion freshness and authorization are correctly implemented.

A model may not know an organization’s internal leave policy or its latest version. A retriever can select that authorized policy and attach provenance. However, RAG inherits corpus errors and can fail when parsing, ranking, or generation is wrong. It does not automatically make information current, private, or correct. Those properties depend on update pipelines, access checks, and answer verification. Our baseline has provenance metadata but no durable update or fine-grained identity system.

**Self-test follow-up:** What happens if the most relevant document is outdated?

### Q063 How do RAG and fine-tuning differ?

**Short answer:** RAG supplies external evidence at inference time; fine-tuning changes model behavior or parameters through additional training.

Use retrieval for changing facts, private knowledge, and source attribution. Fine-tuning can adapt style, task behavior, tool-use patterns, or domain performance, but it is not a reliable substitute for a versioned factual database. They can be combined: a tuned model can answer from retrieved evidence. Compare maintenance, data requirements, privacy, evaluation, and serving cost. Avoid claiming fine-tuning never adds knowledge or that RAG removes all need for adaptation.

**Self-test follow-up:** Would you fine-tune every time a company policy changes?

### Q064 How do RAG, search, and long-context prompting differ?

**Short answer:** Search returns evidence, RAG uses evidence to answer, and long-context prompting supplies a larger body of text directly.

A search UI may be sufficient if users need documents rather than synthesized answers. A long-context model can process more text, but relevant evidence still needs to fit the budget, be authorized, and be used correctly. RAG can reduce context size and cost, but retrieval may miss evidence. Compare actual corpus size, query types, latency, update frequency, and accuracy rather than assume one approach always wins.

**Self-test follow-up:** For a single short document, when would retrieval be unnecessary?

### Q065 What are an LLM and next-token prediction?

**Short answer:** An LLM estimates likely next tokens from context and generates a sequence; fluency does not establish factual correctness.

Tokens are pieces of text from a tokenizer. During generation, the model repeatedly uses its context to predict another token. Training can yield useful language, reasoning, and task behaviors, but plausible continuation can still contain unsupported claims. Providing retrieved evidence changes the context; it does not transform the model into a database query engine. Our current ExtractiveGenerator has none of this prediction machinery, so discuss LLM behavior as a future component.

**Self-test follow-up:** Why can a confident-sounding answer still be unsupported?

### Q066 What is a context window and token budget?

**Short answer:** The context window limits tokens processed in a request, so instructions, history, evidence, and output allowance must be budgeted.

A practical prompt budget reserves space for instructions, the current question, selected history, retrieved chunks, and an answer margin according to the provider’s accounting. Large chunks or many duplicates can exhaust the useful evidence budget. Count with the actual model tokenizer where possible, and enforce truncation policies that preserve source identity. Do not use the project’s character limits as if they were model-token limits.

**Self-test follow-up:** Which content would you discard first when the evidence budget is exceeded?

### Q067 What are hallucination, faithfulness, and factual correctness?

**Short answer:** Hallucination is unsupported or fabricated content; faithfulness checks support in evidence, while correctness checks truth under the task.

An answer can faithfully repeat an outdated policy and still be incorrect for today. It can also state a true fact from model memory that is not supported by the provided context. Evaluate these dimensions separately. In our baseline the text is extracted, but retrieving the wrong document, omitting a caveat, or echoing misleading content still harms answer quality. grounded=true measures neither dimension independently.

**Self-test follow-up:** How would you evaluate a true answer with a fabricated citation?

### Q068 Does temperature zero guarantee correct deterministic answers?

**Short answer:** No; low temperature reduces sampling variation but does not guarantee truth or identical behavior across all serving conditions.

Generation depends on model version, prompt, decoding implementation, retrieval ordering, and infrastructure as well as sampling settings. A low-temperature model can confidently repeat the same wrong answer. Our current generator is deterministic string assembly for fixed ordered hits, but UUID request IDs and chunk IDs differ between calls. For reproducibility record model, prompt, retrieval configuration, corpus version, and relevant seeds/settings where supported.

**Self-test follow-up:** Which variables would you freeze when comparing two retrieval changes?

### Q069 What are parametric and nonparametric knowledge?

**Short answer:** Parametric knowledge is encoded in model weights; nonparametric knowledge is stored externally and retrieved when needed.

A language model’s parameters can encode patterns and factual associations learned in training. Our uploaded chunks are external records that can be inspected and searched. In a full RAG system, the generated answer combines model behavior with this retrieved context. The external store can be updated separately, but only if ingestion, publication, and cache invalidation actually expose the new version. Our current corpus disappears on restart rather than acting as durable external memory.

**Self-test follow-up:** Why is changing an embedding index different from training the generator?

### Q070 When should you not use RAG?

**Short answer:** Avoid adding RAG when the task needs no external evidence, exact structured computation is better, or retrieval adds cost without quality benefit.

For a fixed transformation such as rewriting supplied text, retrieval may be unnecessary. For exact totals over invoices, an authorized database query and calculation can be more suitable than similarity over prose. For a tiny document already in the request, direct context may suffice. State requirements first: evidence source, freshness, scale, uncertainty, and required exactness. RAG is an architecture choice, not a mandatory ingredient in every AI feature.

**Self-test follow-up:** How would you answer “total revenue last quarter” reliably?

## 8 Embeddings and representation choices

This entire section describes future or general RAG components. The checked-in implementation has no embedding model. An embedding adapter introduces model versioning, input limits, batching, latency, provider errors, and a new evaluation surface.

### Q071 What is a text embedding?

**Short answer:** A text embedding is a learned numeric representation used to compare texts in a model-defined vector space.

An encoder maps a question or passage to a fixed-length vector. A compatible similarity function ranks passages near the question representation. Training determines which relationships the geometry captures, so closeness is not universal meaning or certainty. Unlike our Counter vectors, dense embeddings can match paraphrases without exact words. They can still confuse negation, identifiers, domain jargon, and languages they handle poorly.

**Self-test follow-up:** Why might lexical search outperform embeddings for an error code?

### Q072 How are embeddings different from LLM generation?

**Short answer:** An embedding model returns a vector for retrieval; a generative model returns tokens for an answer.

Some model families can support several tasks, but the serving contracts differ. In a typical RAG pipeline, documents are embedded during ingestion, the question is embedded during querying, and a separate generator consumes selected text. The vector itself is not an answer and normally cannot be displayed as meaningful prose. Our repository interface currently accepts a text query and hides retrieval details, so embedding ownership would need an explicit adapter design.

**Self-test follow-up:** Why should a provider embedding call not happen for every stored chunk on every question?

### Q073 Must query and document embeddings use compatible models?

**Short answer:** Yes; query and document representations must be in compatible spaces with the expected preprocessing and scoring rules.

Matching dimensionality is necessary for many indexes but not sufficient for semantic compatibility. Two unrelated models can both output 768 numbers whose coordinate systems differ. Some retrieval models also require query/document prefixes or separate compatible encoders. Store model identity, version, dimensions, normalization, and preprocessing metadata. A model migration usually requires re-embedding documents and switching query encoding coherently.

**Self-test follow-up:** What symptom would appear after changing only the query embedding model?

### Q074 How do you choose an embedding model?

**Short answer:** Evaluate representative domain and language queries under quality, latency, input-length, storage, privacy, and cost constraints.

Create a held-out evaluation set with paraphrases, identifiers, unanswerable questions, and relevant document labels. Compare retrieval recall/ranking with the same chunking and corpus before changing multiple variables. Consider provider availability, batching limits, deployment options, and model version lifecycle. A public benchmark score is useful evidence but not proof on your corpus. Avoid claiming a universally best model or using vector dimension as a quality proxy.

**Self-test follow-up:** How would you compare multilingual quality without averaging away a weak language?

### Q075 What is embedding dimension and how does it affect storage?

**Short answer:** Dimension is the number of vector coordinates; more coordinates increase raw storage and distance-computation work.

For N float32 vectors of dimension d, raw values occupy N×d×4 bytes before row/index overhead, metadata, graph edges, replicas, and backups. One million vectors at 768 dimensions need about 3.072 GB of raw float values. Higher dimensions do not guarantee better task accuracy. Some models support smaller representations through specified mechanisms, but arbitrary truncation can damage compatibility unless the model supports it.

**Self-test follow-up:** What additional memory does an approximate index require beyond raw vectors?

### Q076 How do cosine, dot product, and Euclidean distance relate?

**Short answer:** For unit-normalized vectors cosine equals dot product, and squared Euclidean distance equals 2 minus 2 times cosine.

Cosine compares angle, dot product includes vector magnitude unless normalized, and Euclidean distance measures straight-line distance. Ranking equivalence requires relevant assumptions, especially normalization. A database may return cosine distance, where lower is better, while our service expects similarity, where higher is better. Mixing these semantics can reverse filtering. Choose the model’s recommended metric and document the adapter’s score transformation.

**Self-test follow-up:** Why is applying score >= 0.10 to a distance potentially wrong?

### Q077 How should embedding requests be batched and retried?

**Short answer:** Batch within token/item limits, bound concurrency, retry transient failures with backoff and jitter, and track per-item completion.

Batching amortizes request overhead but can increase the impact of one failure and exceed provider limits. Preserve the mapping from returned vectors to chunk IDs; never assume missing or reordered responses are harmless. Retry transient errors within a total deadline and budget, not invalid inputs forever. Store completed artifacts or use idempotent processing so a job retry does not repeatedly pay for the same work.

**Self-test follow-up:** How would you recover when 30 of 100 chunks embed successfully before failure?

### Q078 How do you migrate an embedding model safely?

**Short answer:** Build a versioned new index, evaluate it, switch query encoding and index selection together, and retain a rollback path.

Mixing embeddings from incompatible models in one similarity space produces unreliable rankings. A staged migration can dual-write new ingestions while backfilling historical data, track completeness, run offline comparisons, and switch a version pointer after validation. Account for documents updated during backfill and for cache keys containing the embedding/index version. If rollback is required, preserve the old index until the new path is stable.

**Self-test follow-up:** How do you prevent an updated document from being overwritten by an older backfill result?

### Q079 How do multilingual and domain-specific retrieval change embedding strategy?

**Short answer:** Evaluate each language and domain separately, preserving original text and testing cross-language or specialized terminology explicitly.

A model that works on general English may miss company abbreviations, medical phrasing, code identifiers, or Hindi-English mixed questions. Options include a better-suited encoder, lexical hybrid retrieval, query normalization, a controlled glossary, or retrieval-model adaptation with good labels. Translation may help but can alter entity names and meaning. Keep citations tied to original evidence and measure quality on the actual language distribution.

**Self-test follow-up:** How would you detect a model that performs well overall but poorly on Hindi questions?

### Q080 Are embeddings anonymized or safe to share freely?

**Short answer:** No; embeddings are derived from source data and should remain subject to access control and privacy policy.

Vectors can reveal similarity and membership information, and their privacy properties depend on the model and threat model. Do not treat an embedding as encryption or assume deleting source text removes all derived information. Protect vector storage, provider requests, metadata, logs, and backups consistently with the corpus policy. Our project currently has no embedding storage, so these are design requirements for that future layer.

**Self-test follow-up:** Does deleting the original document automatically delete its vectors and cached answers?

## 9 Vector storage indexes and filtering

A vector database is more than a list of numbers: persistence, metadata, access control, indexing, updates, replication, and operational behavior matter. The vector-search algorithm is only one part of that system.

### Q081 What does a vector store provide?

**Short answer:** It stores vectors with identifiers and metadata and retrieves nearby candidates under a chosen distance function.

Some products also provide persistence, filtering, hybrid search, replication, and lifecycle tools. Those capabilities differ and must be evaluated against requirements. Our current repository provides none of the learned-vector functions; it is a Python list with lexical scoring. A relational database plus an extension can be sufficient for some applications, while a dedicated search service may suit others. Decide using workload, filtering, consistency, operations, and benchmark evidence.

**Self-test follow-up:** When would you keep documents and vectors in the same relational system?

### Q082 How do exact and approximate nearest-neighbor search differ?

**Short answer:** Exact search finds the true nearest vectors for the metric; ANN trades some retrieval recall for lower search cost.

Brute-force comparison can work well for small datasets and offers a useful evaluation baseline. Approximate methods prune the search so they may miss some true neighbors. Distinguish ANN recall against exact neighbors from task relevance recall against human-labeled useful evidence: they measure different things. An exact geometric neighbor can still be semantically irrelevant to the question.

**Self-test follow-up:** How would you tell whether a miss comes from the embedding model or the ANN index?

### Q083 What is HNSW?

**Short answer:** HNSW is a hierarchical proximity-graph index that navigates through vector neighbors to find approximate matches.

Upper layers help traverse the space coarsely, with lower layers refining nearby candidates. It often provides a useful latency/recall tradeoff, with memory and build/update costs to consider. It is not a blanket worst-case logarithmic guarantee. Compare it with exact search on your distribution, dimensions, filter patterns, and update rate. Reference: [HNSW paper](https://arxiv.org/abs/1603.09320).

**Self-test follow-up:** Why can increasing search effort improve recall but worsen latency?

### Q084 What are common HNSW tuning concepts?

**Short answer:** Graph connectivity and construction effort affect index quality and memory, while query search effort affects recall and latency.

Parameters commonly called M, ef_construction, and ef_search control these aspects in HNSW implementations. Exact names, limits, and effects are implementation-specific. Higher values are not free: they can increase memory, build time, or query work. Benchmark filtered recall as well as unfiltered neighbors, and include concurrency and tail latency. Preserve the configuration with the evaluation result so a claimed improvement can be reproduced.

**Self-test follow-up:** What would you tune first if index recall is poor but latency budget remains?

### Q085 What is IVFFlat and how does it differ from HNSW?

**Short answer:** IVFFlat partitions vectors into coarse lists and probes selected lists; HNSW navigates a graph of neighbors.

IVFFlat depends on representative data for its partitioning and a suitable probe count. Probing more lists generally spends more work to improve recall. HNSW has different memory/build characteristics and does not use that same clustering structure. In pgvector both are available alongside exact search; choose by measured behavior rather than declaring one always superior. Reference: [pgvector indexing](https://github.com/pgvector/pgvector).

**Self-test follow-up:** How can a changing data distribution affect a partition-based index?

### Q086 What are quantization and compression tradeoffs?

**Short answer:** Quantization reduces vector precision or encodes subspaces to save memory and work, potentially reducing retrieval accuracy.

Scalar quantization uses fewer bits per coordinate, while product quantization represents groups of dimensions using codebooks. Compression affects similarity approximation and may require rescoring a larger candidate set with higher-precision vectors. Measure end-to-end relevant-evidence recall, not just bytes saved. Also include storage for original vectors if they remain needed for reranking or migration. No compression mechanism exists in our repository.

**Self-test follow-up:** Could a smaller index be slower if it requires much more candidate rescoring?

### Q087 Why are metadata filtering and ANN interaction tricky?

**Short answer:** Approximate candidates may be filtered after search, leaving too few permitted results even when relevant permitted data exists.

Tenant or ACL constraints are mandatory, not optional relevance hints. In pgvector, approximate index scans can lose results after filters; documented remedies include suitable indexes, partitioning, or iterative scans. Never remove authorization filtering just to recover recall. Compare filtered queries with exact authorized search, monitor returned-candidate counts, and tune within resource budgets. Reference: [pgvector filtering](https://github.com/pgvector/pgvector#filtering).

**Self-test follow-up:** Why is retrieving across all tenants then sending results to an external reranker unsafe?

### Q088 How do prefiltering and postfiltering differ?

**Short answer:** Prefiltering restricts eligible data before candidate search; postfiltering removes ineligible candidates afterward.

The exact execution depends on the engine and index. Prefiltering can preserve authorization and improve selective queries but may constrain efficient graph traversal. Postfiltering can return fewer than k results and waste candidate budget. Authorization must be enforced before evidence reaches the generator, external services, or user-visible output, regardless of execution strategy. Benchmark highly selective tenants and ACLs rather than only a broad public corpus.

**Self-test follow-up:** How would you guarantee no unauthorized content enters the prompt?

### Q089 How would Postgres row-level security help?

**Short answer:** RLS can enforce row access policies in the database, but runtime roles, tenant context, and bypass behavior must be configured correctly.

Application filters can be accidentally omitted. Database policies provide another enforcement layer. Table owners and roles with bypass privileges can evade ordinary policies, so do not connect the application with an overly privileged role. Propagate trusted tenant context safely through pooled connections and transactions. Test cross-tenant reads and writes, not just application query builders. RLS is planned, not implemented in our baseline. Reference: [PostgreSQL row security](https://www.postgresql.org/docs/17/ddl-rowsecurity.html).

**Self-test follow-up:** How could connection-pool reuse leak a previous request’s tenant context?

### Q090 What should you evaluate before choosing a vector database?

**Short answer:** Measure authorized retrieval quality, latency, update/deletion behavior, durability, scale, operational burden, and total cost.

Include real metadata selectivity, document churn, corpus size, embedding dimensions, and concurrent queries. Check backup/restore, migrations, observability, access controls, isolation, SDK stability, and portability. Compare exact and approximate results. Do not choose solely because a demo returns five vectors quickly. Our ROADMAP mentions Postgres/pgvector, and .env.example contains a commented Qdrant hint; neither is integrated or a finished database-selection decision.

**Self-test follow-up:** Which requirement would justify a dedicated vector service over a database extension?

## 10 Hybrid retrieval reranking and query transformations

Retrieval improvements should solve observed failure modes. Exact identifiers, paraphrases, ambiguous questions, duplicate evidence, and multi-hop questions need different techniques; adding every technique usually increases cost and debugging difficulty.

### Q091 What is BM25 and how does it differ from our cosine baseline?

**Short answer:** BM25 is a lexical relevance function with term-frequency saturation, inverse-document frequency, and document-length normalization.

Our scorer uses raw term counts normalized by vector length. BM25 gives rarer terms more discriminative weight and limits the effect of repeated occurrences through saturation. It remains lexical, so paraphrases may still need other techniques. Parameters such as k1 and b control aspects of the function, but should be tuned and evaluated rather than memorized as universally optimal. Reference: [Elastic similarity documentation](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/index-modules-similarity.html).

**Self-test follow-up:** Why might BM25 help with an internal error code?

### Q092 What is hybrid search?

**Short answer:** Hybrid search combines complementary retrieval signals, often lexical matching and dense semantic similarity.

A phrase such as annual leave may be captured semantically from vacation, while a product ID benefits from exact token matching. Retrieve candidates through both paths, enforce authorization, and combine them using a defined fusion method. Hybrid search is not automatically better: poor fusion or incompatible candidate budgets can add noise. Compare query slices for identifiers, paraphrases, multilingual text, and ambiguous questions. Reference: [Elastic hybrid search](https://www.elastic.co/docs/solutions/search/hybrid-search).

**Self-test follow-up:** When might lexical-only retrieval outperform your hybrid configuration?

### Q093 Why not just add BM25 and cosine scores?

**Short answer:** Their scales and distributions differ, so raw addition can let one signal dominate for accidental numerical reasons.

A BM25 score can vary with corpus statistics and query terms, while cosine has a different range and meaning. Weighted fusion requires calibrated or normalized scores and validation across query types. Rank-based fusion avoids direct scale comparison but loses some magnitude information. Our fixed 0.10 gate must not be copied onto the resulting fusion scores. Track which scorer produced each value in logs and response internals.

**Self-test follow-up:** How would you detect a scale mismatch after changing one retriever?

### Q094 What is reciprocal rank fusion?

**Short answer:** RRF sums contributions such as 1 divided by c plus rank from each result list, combining ranks rather than raw scores.

A document appearing near the top of several lists accumulates evidence from each. c is a smoothing constant, not the number of returned results, and absent documents contribute nothing for that list. Choose candidate windows and tie handling explicitly. RRF can combine lexical and vector rankings without score calibration, but it does not independently judge whether the results answer the question. Reference: [Elastic RRF](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion).

**Self-test follow-up:** Why is an RRF score not a probability?

### Q095 What does a reranker do?

**Short answer:** A reranker reorders a bounded candidate set with a more expensive relevance assessment.

The first retriever must search many records cheaply. A cross-encoder reranker jointly processes each query-passage pair, enabling richer interaction than separately computed embeddings, but doing that across an entire large corpus is expensive. Reranking can improve precision only among retrieved candidates; it cannot rescue evidence absent from the candidate set. Its scores may be logits or model-specific values, not universally calibrated probabilities. Reference: [Sentence Transformers retrieve and rerank](https://www.sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html).

**Self-test follow-up:** Why might you retrieve 50 candidates but send only 5 to generation?

### Q096 How do a bi-encoder and cross-encoder differ?

**Short answer:** A bi-encoder embeds texts separately for efficient retrieval; a cross-encoder scores the query and passage together.

Separately encoded documents can be precomputed and indexed. Joint scoring lets query tokens interact directly with passage tokens but usually requires inference for each pair at query time. This produces the common retrieve-then-rerank architecture. Keep latency, candidate count, batching, and domain quality under evaluation. Neither architecture is implemented by our raw Counter retriever. Reference: [Sentence Transformers retrieval architecture](https://www.sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html).

**Self-test follow-up:** Can a cross-encoder usually reuse one fixed document vector for every query?

### Q097 What is MMR and why diversify results?

**Short answer:** Maximal marginal relevance balances relevance to the question with dissimilarity from already selected results.

A common selection objective rewards query relevance and penalizes similarity to selected evidence, with a parameter controlling the balance. This can reduce repeated chunks from overlap or duplicate sources, leaving room for complementary evidence. Diversity is useful only when the additional content remains relevant and authorized. A query needing one exact policy should not receive unrelated documents merely to make the result list varied.

**Self-test follow-up:** How would you distinguish duplicate evidence from independently corroborating evidence?

### Q098 What are query rewriting and multi-query retrieval?

**Short answer:** They reformulate a question or search several formulations to improve matching, while risking semantic drift and extra cost.

A conversational follow-up may need rewriting into a standalone question using history. Multi-query retrieval can capture synonyms or alternative phrasings and merge the candidate sets. Preserve entities, dates, negation, and user intent; a rewrite that changes one of these can retrieve confidently wrong evidence. Keep the original question for answering and auditing. Limit expansion count and compare against the unmodified baseline.

**Self-test follow-up:** How would you evaluate a rewrite that changes “not eligible” to “eligible”?

### Q099 What is HyDE?

**Short answer:** HyDE generates a hypothetical answer-like document, embeds it, and uses that representation to retrieve real corpus evidence.

The hypothetical text is a search aid, not a trusted source, and can contain invented details. Its purpose is to bridge a short question to passage-like language in embedding space. It adds a generation call and can steer retrieval incorrectly. Final answers must still rely on real authorized documents. Test it for the query types where the baseline struggles. Reference: [HyDE paper](https://arxiv.org/abs/2212.10496).

**Self-test follow-up:** Why must the hypothetical document never become its own citation?

### Q100 What are multi-hop retrieval and contextual compression?

**Short answer:** Multi-hop retrieval gathers linked evidence across steps, while compression reduces selected evidence to the parts useful for the question.

A question connecting a person, project, and policy may require retrieving one fact to form the next search. Bound the number of steps and preserve provenance for every link. Compression can remove irrelevant text or extract sentences, but an abstractive compressor may introduce errors or drop exceptions. Keep access checks and original spans, and compare against simply using fewer well-ranked chunks. Neither technique exists in the current service.

**Self-test follow-up:** How would you detect that compression removed the exception determining the answer?

## 11 Generation context and answer verification

Generation is a future adapter in this project. The design challenge is to convert useful evidence into an answer while respecting budgets, source authority, uncertainty, and provider failures. Prompt instructions alone cannot establish those properties.

### Q101 What should a grounded generation prompt contain?

**Short answer:** It should contain the task, trust boundaries, labeled evidence, question, output requirements, and an explicit insufficient-evidence policy.

Give each evidence passage a stable source ID and delimit it from instructions. Ask for answers supported by the provided evidence and a defined abstention when evidence is inadequate. Include only authorized, relevant context. A prompt is one control, not a substitute for access checks or verification. Our current generator uses no prompt template or LLM, so this is a proposed contract for a model adapter.

**Self-test follow-up:** How would you prevent the model from inventing a source ID?

### Q102 How should context be selected and ordered?

**Short answer:** Choose authorized, relevant, nonredundant evidence within a token budget and evaluate how ordering affects answer quality.

Top-k is a candidate limit, not a full context-packing policy. Account for passage length, duplicates, source authority, recency, and multi-part questions. Preserve labels when truncating or combining chunks. Some experiments have found weaker use of information placed in the middle of long contexts, but the effect depends on model and task; it is a reason to test, not a universal positioning law. Reference: [Lost in the Middle](https://arxiv.org/abs/2307.03172).

**Self-test follow-up:** Why might adding more retrieved context make an answer worse?

### Q103 How should a model gateway be designed?

**Short answer:** A gateway should hide provider specifics while enforcing deadlines, budgets, retries, fallbacks, structured results, and observability.

Instead of letting RagService depend directly on a vendor SDK, translate internal request types into provider calls and translate outcomes back. Include model/version, token usage, finish reason, and structured errors where needed. Our Generator port currently returns only a string, so richer behavior requires evolving the contract. A fallback must preserve tenant/privacy requirements and cannot silently send confidential context to an unapproved provider.

**Self-test follow-up:** What should happen if the provider finishes because of a token limit?

### Q104 When should the system abstain or ask for clarification?

**Short answer:** Abstain when authorized evidence is insufficient; clarify when ambiguity can be resolved by a useful user answer.

Low retrieval scores, conflicting sources, missing dates, or incomplete multi-hop evidence can signal uncertainty, but no single score universally decides sufficiency. A clarification such as which policy year can be more useful than a vague refusal. Distinguish no evidence from provider outage and permission denial in internal diagnostics, without disclosing restricted content. Our baseline only abstains on an empty admitted-hit list.

**Self-test follow-up:** Would you expose that a restricted document exists when a user lacks access?

### Q105 How do you verify citations?

**Short answer:** Check that cited IDs exist, were authorized and supplied, and support the associated claims under a defined evidence rubric.

Structural validation catches fabricated IDs or malformed output. Semantic validation asks whether the cited text actually entails the claim, including conditions and dates. A citation pointing to a real but irrelevant document fails this second check. Human review or a calibrated verifier can assess support, with uncertainty and evaluator error tracked. Our current citations are constructed by the service from hits, but there is no claim-level mapping or entailment verification.

**Self-test follow-up:** How would you score an answer with three claims but support for only two?

### Q106 How do you handle conflicting or stale evidence?

**Short answer:** Use version and authority metadata, state unresolved conflicts, and avoid silently merging incompatible claims.

A newer document is not always the authoritative one; effective date, jurisdiction, product version, and source owner may matter. Store these fields and route queries to the correct scope. If two valid sources disagree, the answer should surface the conflict or follow a documented authority rule. Our filenames and creation timestamps are insufficient to resolve this reliably. Retrieval relevance and factual authority are separate dimensions.

**Self-test follow-up:** What if the latest upload contains an older policy revision?

### Q107 What are structured outputs and their limits?

**Short answer:** A schema constrains output shape, but valid JSON can still contain unsupported or incorrect claims.

A useful answer schema could contain answer text, source IDs, abstention reason, and confidence-related diagnostics. Validate types, allowed IDs, and bounded sizes. Keep a clear failure policy when the model cannot satisfy the schema, rather than parsing arbitrary text optimistically. Schema compliance does not prove entailment, correct calculations, or safe actions. Our QueryResponse is already structured at the API layer, but its answer is plain extractive text.

**Self-test follow-up:** What would you do with a valid answer object containing a nonexistent citation ID?

### Q108 How should conversational history be used?

**Short answer:** Use scoped history to resolve the current question, but retrieve fresh evidence and enforce tenant/user isolation on every turn.

A follow-up such as “Does that include contractors?” needs the previous topic, yet copying all history can waste tokens and propagate earlier errors or instructions. Store sessions with ownership, versioning, retention, and clear cancellation semantics. Rewrite to a standalone query when appropriate, preserve the original, and never treat a previous model answer as authoritative evidence. The current API is stateless per question with no chat-history model.

**Self-test follow-up:** How would you prevent one user from guessing another conversation ID?

### Q109 How do streaming answers change the system?

**Short answer:** Streaming improves time to first visible output but adds event contracts, cancellation, partial-failure, and verification tradeoffs.

SSE can deliver server-to-client events; WebSockets support bidirectional interaction when needed. Define events for text, citations, completion, and errors, and decide what the Android client should do after disconnect. Verifying the full answer before showing it reduces risk but delays streaming; displaying tokens immediately can expose content before final checks. A partial stream cannot be treated as a complete verified answer. No streaming endpoint exists today.

**Self-test follow-up:** How should the client display a stream that fails after half the answer?

### Q110 How do you handle numbers and precise computations?

**Short answer:** Retrieve relevant records and definitions, then use deterministic calculation or authorized structured queries for exact arithmetic.

An LLM can miscalculate or confuse units and time windows even when evidence is present. For totals, use a database or constrained computation path with explicit filters and auditability. Cite the underlying records or query scope and explain missing data. Our extractive generator does not perform arithmetic; it simply returns passages. A tool-using future implementation needs validation and least-privilege execution, not arbitrary model-generated commands.

**Self-test follow-up:** How would you answer a financial total without summing only the top five retrieved rows?

## 12 Evaluation datasets and metrics

Evaluation separates whether software executes from whether the system retrieves and answers well. Define the question population, labels, metric denominators, acceptable errors, and evaluation versions before claiming an improvement.

### Q111 Why is test coverage not RAG quality?

**Short answer:** Coverage measures executed code statements; RAG quality measures retrieval usefulness, supported answers, and task outcomes.

Our eight tests achieve 94.76% statement coverage in the recorded run while missing important behaviors such as chunk termination and duplicate ingestion. A single successful handbook query can execute most orchestration lines without proving good retrieval across paraphrases, languages, or unanswerable questions. Use software tests for invariants and failure behavior, and an evaluation dataset for relevance and answer quality. Report both with their scope.

**Self-test follow-up:** How can 100% coverage coexist with a broken ranking algorithm?

### Q112 What belongs in a golden evaluation dataset?

**Short answer:** Include representative questions, relevant evidence labels, answer criteria, tenant/version context, and unanswerable or adversarial cases.

Cover common queries and difficult slices: identifiers, paraphrases, dates, negation, tables, long documents, multilingual questions, and multi-hop reasoning. Preserve corpus and label versions so results are reproducible. Reference answers need not be one exact string if several formulations are valid; use claims and evidence criteria. Our evals/datasets directory is empty, and there is no runner, so any benchmark described here is proposed.

**Self-test follow-up:** How would you label a question for which two different passages both support a correct answer?

### Q113 What are precision at k and recall at k?

**Short answer:** Precision@k is relevant retrieved items divided by k; Recall@k is retrieved relevant items divided by all labeled relevant items.

Choose the retrieval unit and denominator explicitly. If three relevant chunks exist and the top five contain two, Precision@5 is 2/5 and Recall@5 is 2/3. When fewer than k results are returned, document whether precision uses k or returned count; this guide uses fixed k. Questions with no relevant evidence need a separate abstention evaluation rather than undefined recall. These definitions do not assess answer fluency or correctness.

**Self-test follow-up:** Can a system have high precision but low recall?

### Q114 What are MRR and nDCG?

**Short answer:** MRR rewards the rank of the first relevant result; nDCG rewards an entire graded ranking with position discounting.

Reciprocal rank is 1/r for the first relevant rank r, or zero if none appears within the chosen cutoff; MRR averages across queries. nDCG divides a discounted gain score by the ideal score for the same labels, so highly relevant evidence near the top is rewarded. Define gain conventions and zero-ideal cases consistently. Reference: [Stanford ranked retrieval evaluation](https://nlp.stanford.edu/IR-book/html/htmledition/evaluation-of-ranked-retrieval-results-1.html).

**Self-test follow-up:** Why might MRR miss poor ordering after the first relevant result?

### Q115 What are answer faithfulness, relevance, correctness, and citation precision?

**Short answer:** They respectively assess evidence support, responsiveness to the question, truth under the task, and support from cited sources.

Use separate rubrics rather than combine everything into one opaque score. Faithfulness can be estimated as supported claims over checkable claims; citation precision can be measured over cited claim-source pairs under a stated definition. Answer relevance asks whether the response addresses the question. Correctness may require authoritative ground truth and dates. A verbose response can score poorly despite containing one correct sentence. Define treatment of abstentions and partially supported claims.

**Self-test follow-up:** Which metric fails when a correct answer cites the wrong document?

### Q116 How do you evaluate abstention?

**Short answer:** Measure both unsupported answers on unanswerable cases and unnecessary refusals on answerable cases.

A system that always abstains can avoid hallucinations but is useless for answerable questions. Build answerable and unanswerable sets, choose a decision threshold on a validation split, and measure the tradeoff between coverage and error among answered questions. Separate missing evidence, unauthorized evidence, ambiguous questions, and provider failures. Our current grounded flag is a threshold artifact, so it should not be used as its own ground-truth label.

**Self-test follow-up:** How would a stricter relevance threshold affect user usefulness and unsupported-answer rate?

### Q117 What are the risks of LLM-as-a-judge?

**Short answer:** A model judge can scale evaluation but has biases, nondeterminism, prompt sensitivity, and imperfect agreement with humans.

Use explicit rubrics and evidence, blind the judge to experiment identity, randomize answer order in pairwise comparisons, and calibrate against human-labeled examples. Track disagreements and avoid treating a single model score as objective truth. Do not let evaluated content instruct the judge. Separate schema checks, deterministic metric calculations, and semantic judgments. Report model/prompt versions and uncertainty.

**Self-test follow-up:** What would you do if a judge consistently favors longer answers?

### Q118 How do you prevent evaluation leakage?

**Short answer:** Keep a held-out test set and avoid tuning prompts, thresholds, or models repeatedly on its answers.

Use training or development data for adaptation, validation data for choices, and a final test set for reporting. Split related documents or query families appropriately so near-duplicates do not inflate results. Version the corpus and inspect synthetic questions for answer leakage. User feedback can become future labeled data under a privacy policy, but it should not silently contaminate an untouched benchmark.

**Self-test follow-up:** Why can a random query split be misleading when many questions share the same source paragraph?

### Q119 How do you run a fair RAG experiment?

**Short answer:** Freeze unrelated variables, change one component, evaluate paired queries, inspect slices, and report quality plus latency/cost uncertainty.

If you change chunking, embedding, and prompt together, you may see an improvement without knowing why. An ablation isolates contributions. Use repeated measurements where nondeterminism matters, paired bootstrap or suitable statistics for uncertainty, and error analysis rather than only averages. Include query types that regress. A faster average does not compensate automatically for unacceptable p95 latency or cross-tenant leakage.

**Self-test follow-up:** How would you decide whether a one-point score improvement justifies doubled cost?

### Q120 How do offline evaluation and online monitoring complement each other?

**Short answer:** Offline sets make changes comparable; online signals reveal real traffic, drift, latency, and user outcomes.

A benchmark can miss new terminology, changed documents, or actual query distributions. Online monitoring can track abstention, source usage, feedback, error rates, and timing, but thumbs-up rates are noisy and not direct truth labels. Canary or A/B releases need controlled cohorts and safety guardrails. Monitor corpus/embedding changes as well as model changes. Preserve privacy in logs and samples used for review.

**Self-test follow-up:** How would you detect quality degradation after a large document import?

## 13 Security privacy and tenant boundaries

Treat the client, uploaded documents, retrieved content, and generated output as distinct trust boundaries. The baseline has a shared key and small input heuristics; it does not have production identity, database policies, or comprehensive prompt-injection resistance.

### Q121 What is direct versus indirect prompt injection?

**Short answer:** Direct injection appears in the user request; indirect injection arrives through external content such as retrieved documents.

An attacker may ask a model to ignore its task directly or place that request inside a document the system later retrieves. A RAG prompt combines instructions and data, so the system must maintain their separation and restrict downstream capabilities. Our query regexes screen a few direct phrases, and document labels are replaced, but neither technique comprehensively handles indirect attacks. Reference: [OWASP prompt injection guidance](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html).

**Self-test follow-up:** Why can a harmless user question trigger malicious instructions from a retrieved page?

### Q122 What do our three query guardrail patterns cover?

**Short answer:** They match selected ignore-previous-instructions, reveal-system/developer-prompt, and print-secret-style phrases.

The patterns are case-insensitive. They do not cover all paraphrases, obfuscations, languages, encoded payloads, or context-sensitive attacks. They can also reject legitimate questions about prompt security. validate_query strips the question and checks emptiness and maximum length before applying these patterns. The route converts UnsafeInputError to 400. These are inspectable heuristics, not a trained classifier or complete security boundary.

**Self-test follow-up:** How would you measure false positives as well as missed attacks?

### Q123 What makes tenant isolation an end-to-end property?

**Short answer:** Every storage, retrieval, reranking, generation, source-access, cache, and logging path must respect trusted tenant and user permissions.

It is insufficient to hide unauthorized citations after their text has already reached an external reranker or LLM. Apply checks at data selection and downstream boundaries, and avoid cross-tenant cache reuse. Propagate identity explicitly through jobs and storage records. Our repository carries tenant IDs, but HTTP authentication returns only default. A meaningful test should upload different data through distinct identities and prove no cross-access across all relevant endpoints.

**Self-test follow-up:** Where could a cache bypass otherwise correct database authorization?

### Q124 How should API keys and provider secrets be handled?

**Short answer:** Keep privileged secrets server-side, rotate and scope them, and never ship a provider credential in an Android APK.

APK contents and client traffic can be inspected, so an embedded shared secret is not a durable identity boundary. A production mobile app usually authenticates a user or device through an appropriate identity flow, then calls the backend with scoped credentials. The backend owns provider credentials under its deployment secret policy. Our .env is ignored by Git, but the code still has a default development key and no secret-manager integration.

**Self-test follow-up:** What does app attestation add, and why does it not replace authorization?

### Q125 What are PII redaction and data-minimization concerns?

**Short answer:** Only send, store, log, and retain the data required for the authorized task, with explicit handling of sensitive fields.

Consider original files, chunks, embeddings, prompts, outputs, traces, backups, and human evaluation samples. Redaction can reduce exposure but may also remove necessary meaning, so preserve controlled mappings only where justified and protected. Tenant separation and encryption do not decide retention or provider data-use policy. Our baseline has no PII detector, redaction stage, retention policy, or moderation pipeline.

**Self-test follow-up:** Could logging the full prompt leak more data than the user-facing answer?

### Q126 How do ingestion attacks differ from prompt injection?

**Short answer:** Uploads can exhaust resources or exploit parsers even without an LLM, while prompt injection targets model behavior.

Oversized multipart bodies, decompression bombs, pathological text, malware, and parser vulnerabilities are conventional ingestion risks. Our known non-advancing chunk loop is a resource-exhaustion defect reachable with valid text. Future PDF/HTML/OCR support needs isolated parsing, resource limits, and controlled external access. A MIME string and extension are not proof of safe content. Keep these concerns separate from model prompt policy.

**Self-test follow-up:** Why is the chunking bug security-relevant even though generation is offline?

### Q127 What is retrieval poisoning?

**Short answer:** An attacker adds or modifies corpus content to make malicious or misleading evidence rank for legitimate questions.

A document can repeat target keywords, imitate an authoritative policy, or include instruction-like content. Source authentication, ingestion permissions, provenance, review workflows, and retrieval anomaly checks help address this. A relevance score does not certify source trustworthiness. Our content hashes identify bytes but do not prove who authored them or whether they are approved. Reference: [OWASP RAG security](https://cheatsheetseries.owasp.org/cheatsheets/RAG_Security_Cheat_Sheet.html).

**Self-test follow-up:** Can a higher similarity score make an untrusted document authoritative?

### Q128 What if we add URL ingestion or model tools?

**Short answer:** Add network and action boundaries such as SSRF protection, least-privilege tools, validated arguments, and explicit write policies.

A URL fetcher can be abused to reach internal services unless destinations, redirects, address resolution, and response limits are controlled. A model-generated tool call is untrusted input and must pass authorization and schema validation independently of the prompt. Restrict commands and data access to the task. Our app currently has neither URL ingestion nor model tools, so these risks belong to future design, not existing features.

**Self-test follow-up:** Why is checking the initial URL insufficient if redirects are allowed?

### Q129 How should generated content be rendered safely?

**Short answer:** Treat model output and source labels as untrusted content, escaping HTML and validating links or actions before display.

A future web frontend can introduce script injection if it renders arbitrary HTML from evidence or generated text. Android rendering also needs careful handling of clickable links, intents, and WebView content. Do not automatically execute commands or navigate privileged deep links because a model suggested them. Our current backend returns JSON strings, but client interpretation can introduce new risks. Source filenames are client-provided labels, not trusted URLs.

**Self-test follow-up:** How would you display Markdown citations without allowing arbitrary executable content?

### Q130 What should a practical threat model cover?

**Short answer:** Identify assets, actors, trust boundaries, abuse paths, controls, and residual risks across ingestion, retrieval, generation, and operations.

Assets include tenant documents, credentials, prompts, outputs, and service availability. Actors include ordinary users, malicious uploaders, compromised providers, and overly privileged operators. Map where data crosses systems and where checks occur. Include resource exhaustion, poisoning, permission changes, cache leakage, and supply-chain/deployment risks. Link each important control to a test or operational signal. Our repository has a short security note, not a completed threat-model artifact.

**Self-test follow-up:** Which three risks would you prioritize before exposing this baseline publicly?

## 14 Persistence jobs and distributed correctness

Android process death is a useful starting analogy for backend durability, but distributed services add concurrent writers, retries, replication, and partial failures. A successful response must have a clearly defined durability and consistency contract.

### Q131 Why is a Python list not a production database?

**Short answer:** It lacks durable storage, shared multi-worker state, transactional constraints, query indexes, backups, and lifecycle management.

Our list exists only in one repository instance. Restarting removes all documents, and different replicas have different lists. There is no total capacity limit, document deletion, durable uniqueness, or recovery mechanism. A database is not merely a larger list: it provides concurrency and persistence semantics that application code must use correctly. Room provides a closer Android durability analogy than an in-memory singleton, though a server database is shared across clients.

**Self-test follow-up:** What guarantee should a successful ingestion response make about a subsequent restart?

### Q132 What schema would you propose for durable RAG storage?

**Short answer:** Use tenant-scoped documents, versions, chunks, embeddings, and ingestion jobs with explicit keys, states, and lineage.

A document record captures logical identity and source. A version records checksum, parser/chunker configuration, effective dates, and publication status. Chunks reference that version and preserve positions; embeddings reference chunk and model version. Jobs track processing attempts and failures. Add uniqueness and foreign keys appropriate to the chosen duplicate policy. Do not mix a logical document ID with a particular embedding artifact’s identity.

**Self-test follow-up:** How would you represent one document embedded by two models during migration?

### Q133 What are transactions and why do they matter?

**Short answer:** A transaction groups database changes under defined atomicity and isolation so readers do not observe invalid partial state.

For ingestion, inserting a version, its chunks, and an active-version pointer separately can expose incomplete evidence. A transaction can publish related database state consistently. External object storage and model API calls do not automatically participate in the same database transaction, so design staged states and recovery. Avoid holding database transactions open during long network calls. Choose isolation according to invariants and test concurrent operations.

**Self-test follow-up:** What can go wrong between storing the file and committing its metadata?

### Q134 How should uniqueness and upserts support idempotency?

**Short answer:** Enforce scoped uniqueness in the database and define repeat-request behavior atomically instead of relying on a preliminary read.

Two workers can both check that a document is absent and then both insert it. A unique key such as tenant plus source/version/content identity, chosen for the product semantics, resolves that race at storage. An upsert can update or return the existing record, but its behavior must match expectations. Distinguish request-idempotency keys from permanent document identity, and reject reusing a request key with a different payload.

**Self-test follow-up:** Should identical content under two different source permissions share the same public record?

### Q135 Why use background ingestion jobs?

**Short answer:** Slow parsing, embedding, and indexing should run in bounded workers with durable status rather than hold HTTP requests open indefinitely.

The API can validate and store the upload, create a job, and return 202 with a job ID. A worker progresses through queued, processing, ready, or failed states. Clients poll or subscribe to status and only query published documents. This resembles WorkManager’s deferred-work intent conceptually, but server jobs need distributed ownership, retries, monitoring, and isolation. Our current ingestion completes synchronously and returns 201.

**Self-test follow-up:** What should happen if the worker dies after embedding but before publishing?

### Q136 What are at-least-once delivery and exactly-once effects?

**Short answer:** A job may be delivered again after failure, so idempotent processing is needed to make repeated execution safe.

Distributed queues often favor retryable delivery over losing work. If a worker commits data but crashes before acknowledging the message, another worker may receive it again. Exactly-once behavior across a queue, database, object store, and external model provider is not obtained by one flag. Define transactional boundaries, deduplication keys, and recovery states so the intended database effect occurs once even when processing repeats.

**Self-test follow-up:** How would you prevent a retried job from duplicating chunks or double-charging a downstream operation?

### Q137 What is the transactional outbox pattern?

**Short answer:** Write the business change and an event record in one database transaction, then publish the event asynchronously with deduplication.

Without an outbox, committing a document job and then failing to enqueue it can leave work stranded; enqueuing before a failed commit can produce a job referencing absent state. An outbox makes the database decision durable alongside a pending event. A publisher can retry delivery, and consumers still need idempotency because publication can repeat. It is a proposed option when our future ingestion spans a database and queue.

**Self-test follow-up:** Why does the outbox not eliminate the need for idempotent consumers?

### Q138 What are eventual consistency and read-your-writes?

**Short answer:** Eventual consistency allows propagation delay; read-your-writes means a client can observe its completed write under the promised contract.

A document may be stored in a database before its search index becomes queryable. Returning ready too early misleads the client. Define whether ingestion acknowledgment means accepted, durable, or searchable, and expose distinct states. For migrations or replica reads, track version visibility and synchronization lag. Our single-process list is immediately visible to later requests in that instance, but this does not provide durability or cross-replica consistency.

**Self-test follow-up:** Should a 202 response promise that the document can already answer questions?

### Q139 How do locks, optimistic concurrency, and job leases help?

**Short answer:** They coordinate competing updates through exclusive ownership, version checks, or time-bounded processing claims.

A version column can reject an update based on stale state. A job lease lets another worker recover abandoned work after expiry, but the old worker may still run, so fencing or idempotent publication matters. Database constraints should enforce core invariants even when application coordination fails. Do not infer thread or distributed safety merely from Python’s execution model. Our list adapter defines none of these distributed mechanisms.

**Self-test follow-up:** How do you prevent an expired worker from overwriting the result of its replacement?

### Q140 What do backups, recovery, and deletion guarantees require?

**Short answer:** Define acceptable data loss and recovery time, test restoration, and account for originals, indexes, metadata, and retained backups.

RPO is the tolerated recovery point gap; RTO is the tolerated restoration duration. Search indexes may be rebuildable, but rebuilding millions of embeddings can be slow and costly if artifacts were not preserved. Deletion policy must explain active removal and backup retention, with access revoked immediately where required by the product. Our container has no volume, database, backup job, or restore test.

**Self-test follow-up:** Which artifacts would you preserve so a vector-index rebuild does not require paying for every embedding again?

## 15 Latency cost caching and resilience

Production RAG involves several latency and cost stages. Optimizing only the language model can miss slow retrieval, queueing, connection pools, parsing, or retry amplification. Use explicit budgets and workload assumptions.

### Q141 How do you break down query latency?

**Short answer:** Measure authentication, query processing, embedding, retrieval, reranking, context assembly, generation, and verification separately.

End-to-end latency includes network and queueing as well as execution. Time to first token differs from full completion time. Parallel independent lexical/vector retrieval may contribute roughly the slower branch plus coordination, not their sum, while sequential stages accumulate. Percentiles of stage times cannot generally be added to obtain the end-to-end percentile. Our current pipeline has no embedding or model latency, so do not project its local test timing onto full RAG.

**Self-test follow-up:** Which spans would reveal whether a slow answer is caused by retrieval or model output length?

### Q142 Why monitor p50, p95, and p99 rather than only averages?

**Short answer:** Percentiles expose typical and tail latency, while averages can hide a small but painful set of slow requests.

If most requests are fast but a few wait on provider retries or overloaded pools, the average may look acceptable while users experience stalls. Report the sample size, measurement window, load, and percentile definition. Correlate tails with request length, tenant, corpus size, and cache state without leaking sensitive data. A system that meets latency at idle may fail under realistic concurrent traffic.

**Self-test follow-up:** What could cause p99 to rise while p50 stays stable?

### Q143 How do you estimate RAG cost?

**Short answer:** Include ingestion embeddings, query embeddings, reranking, input/output generation tokens, storage, compute, retries, and evaluation.

For one query, model cost is input_tokens×input_rate plus output_tokens×output_rate using consistent billing units, plus other paid stages. Cached or batch pricing can differ by provider and should be verified when budgeting. Use symbolic or explicitly hypothetical rates in interview estimates rather than invent current prices. Our offline adapters make no paid provider calls, but compute and operating costs still exist. Duplicate chunks increase future indexing and prompt costs.

**Self-test follow-up:** Which costs remain even if answer generation is cached?

### Q144 What can be cached and what belongs in cache keys?

**Short answer:** Cache embeddings, authorized retrieval, or answers using keys that include tenant/ACL scope, corpus version, and relevant model/prompt settings.

A query-text-only cache can leak another tenant’s answer or serve an obsolete policy. Embedding caches need compatible model/preprocessing identity; retrieval caches need index/filter context; answer caches need evidence/prompt/generator versions and permission scope. Set freshness and invalidation rules rather than relying only on a long TTL. Our service has no semantic or answer cache; lru_cache only caches Settings.

**Self-test follow-up:** What should invalidate a cached answer after a user’s permissions change?

### Q145 What is semantic caching and what are its risks?

**Short answer:** Semantic caching reuses answers for similar questions, but similarity does not prove identical intent, permissions, or required freshness.

“Can contractors take leave?” and “Can contractors not take leave?” may be close in embedding space yet require different interpretation. Dates, entities, tenant context, and authorization must be checked. A conservative semantic cache may require structured matching and an evidence-validity policy in addition to a similarity threshold. Evaluate false reuse separately from hit rate. Start with exact safe caching when semantics are hard to establish.

**Self-test follow-up:** Why is maximizing semantic-cache hit rate not the right objective by itself?

### Q146 How do timeouts, retries, backoff, and jitter work together?

**Short answer:** Use per-stage and total deadlines, retry only suitable transient failures, and add bounded exponential backoff with jitter.

Retries consume latency and cost and can amplify an outage. A request with three stages should not allow each stage to use the full end-to-end budget independently. Respect provider guidance such as retry-after where applicable, propagate cancellation, and avoid retrying invalid input or authorization errors. Idempotency matters for side effects. Our adapters currently contain no provider retry policy, and an event-loop-blocking loop cannot be reliably rescued by cooperative timeout alone.

**Self-test follow-up:** How would you stop retries from exhausting the remaining user deadline?

### Q147 What are circuit breakers and bulkheads?

**Short answer:** Circuit breakers stop repeated calls to a failing dependency; bulkheads isolate resource pools so one workload cannot consume everything.

A breaker typically opens after a defined failure condition, waits, then permits controlled recovery probes. A bulkhead might separate ingestion workers from query workers or cap one tenant’s model concurrency. These controls complement timeouts and rate limits but require measured thresholds and observability. Fallback behavior should preserve correctness and confidentiality rather than fabricate an answer. None is implemented in our current service.

**Self-test follow-up:** How should queries behave when the model provider is unavailable but retrieval still works?

### Q148 What are rate limiting and backpressure?

**Short answer:** Rate limiting controls admitted demand; backpressure prevents producers from overwhelming downstream capacity.

Limit by appropriate identity and resource cost, not just IP when many users share a network. Token-heavy questions and large ingestion jobs can cost much more than small requests. Bounded queues, concurrency semaphores, quotas, and rejection policies help protect capacity. Return clear retryable overload behavior where appropriate. Redis-backed per-tenant limits are planned, but the current API has no rate limiter or total corpus quota.

**Self-test follow-up:** Why is an unbounded background queue not a complete scaling strategy?

### Q149 How do you estimate concurrency and capacity?

**Short answer:** Under stable conditions, average in-flight work is roughly arrival rate times average time in the system.

Using Little’s law for a consistently defined boundary, 20 requests per second at 2 seconds average time implies about 40 requests in flight. This is a planning relationship, not proof that one worker can handle them. CPU, provider quotas, database pools, memory, and burstiness constrain actual throughput. Measure saturation and include headroom. Our CPU-heavy in-memory retrieval makes workload size particularly relevant.

**Self-test follow-up:** What changes if service time grows as the queue gets longer?

### Q150 How should you optimize a slow or expensive RAG system?

**Short answer:** Profile first, then remove avoidable work while checking quality: duplicates, excess candidates, oversized context, repeated embeddings, and retry storms.

Tune the dominant measured stage instead of guessing. Options include better indexes, bounded reranking candidates, shorter useful context, caching with correct keys, batching ingestion, and model routing under an evaluation gate. Smaller models or fewer chunks may lower cost but lose essential evidence. Record the before/after quality, latency, and cost on the same workload. Our baseline should first fix termination and duplicates before scale optimization.

**Self-test follow-up:** How would you prove a cheaper configuration did not worsen unanswerable-question handling?

## 16 Deployment observability and development tooling

A container and a CI file are useful foundations, but they are not proof of an operating production deployment. Know exactly what the repository configures, what the local checks verify, and what remains to be built.

### Q151 What do Docker and Compose provide here?

**Short answer:** Docker packages the Python API runtime; Compose configures one local API container with port and runtime restrictions.

The Dockerfile uses python:3.12-slim, installs uv and the package, creates appuser with UID 10001, and launches Uvicorn on 0.0.0.0:8000. Compose exposes port 8000, passes APP_ENV and API_KEY, makes the filesystem read-only, provides /tmp tmpfs, drops capabilities, and forbids privilege escalation. There is no database, Redis, worker, volume, TLS proxy, or cloud deployment in Compose. Container configuration was inspected, not deployed during this documentation work.

**Self-test follow-up:** Why does a non-root container not fix an application authorization bug?

### Q152 Why does reproducible dependency installation matter?

**Short answer:** A lockfile keeps resolved dependencies consistent, but the installer must actually use it.

CI runs uv sync --extra dev --frozen against uv.lock, while the Dockerfile installs the project from pyproject constraints without copying or consuming the lockfile. Those paths can resolve different dependency versions. The repository requires Python 3.12+, CI uses 3.12, and the recorded local verification used 3.13.5. Align environments and retain tested versions before interpreting differences as application bugs. Reference: [uv locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/).

**Self-test follow-up:** How would you ensure the image runs the same dependency set that passed CI?

### Q153 What are liveness, readiness, and startup probes?

**Short answer:** Liveness detects a stuck process, readiness controls whether it receives traffic, and startup probes allow initialization time.

Our two health handlers return constants: ok and ready. They do not test storage or a provider. A future readiness policy should reflect whether the instance can serve its promised function, while liveness should avoid restarting healthy processes merely because an external provider is down. Kubernetes treats these probes differently, and startup success can gate the others. Reference: [Kubernetes probes](https://kubernetes.io/docs/concepts/workloads/pods/probes/).

**Self-test follow-up:** Why can checking a failing external dependency in liveness create a restart storm?

### Q154 What logging have we built?

**Short answer:** We configure structlog JSON output and log unhandled exceptions with the request path; request-level observability is still sparse.

The processors merge context variables, add log level and ISO timestamp, then render JSON. Standard logging is also configured. But no middleware binds request or tenant IDs, no ingestion audit event is emitted, and no query latency/retrieval/model usage event is implemented. JSON formatting makes events easier to parse, but the useful events and fields must still be designed. Avoid logging full documents, secrets, or prompts by default.

**Self-test follow-up:** What minimum fields would make an ingestion failure diagnosable?

### Q155 How do logs, metrics, and traces differ?

**Short answer:** Logs describe events, metrics aggregate measurements, and traces connect timed operations across a request path.

A metric can show rising p95 latency; a trace can identify a slow reranker call; a log can explain a validation failure. OpenTelemetry provides conventions and collection/export mechanisms, not a guarantee that your business signals are instrumented. Choose spans and attributes for ingestion, embedding, retrieval, generation, and publication. Keep sensitive text out and avoid unbounded metric labels. Reference: [OpenTelemetry signals](https://opentelemetry.io/docs/concepts/signals/).

**Self-test follow-up:** Why should request_id usually not be a Prometheus metric label?

### Q156 What are SLI, SLO, and error budget?

**Short answer:** An SLI measures service behavior, an SLO sets its target, and an error budget is the permitted miss rate over a window.

Examples include successful authorized-query fraction, latency within a threshold, and document publication delay. Define which requests count, how abstention is classified, and whether provider failures are included. Product-quality targets can complement availability targets. An error budget can inform rollout and reliability work. Our repository has no declared SLO or production measurements, so any numerical target in an interview is an assumption to negotiate.

**Self-test follow-up:** Does a fast but unsupported answer satisfy the product’s quality objective?

### Q157 What does CI currently enforce?

**Short answer:** The workflow installs locked dev dependencies, runs Ruff checks, strict mypy, and pytest with an 85% coverage threshold.

It runs on pushes and pull requests with read-only repository permissions on an Ubuntu runner. There is no evaluation-quality gate, load test, container smoke test, dependency vulnerability scan, or deployment stage in the workflow. Make check runs local lint, typing, and tests, but make test only reports coverage rather than enforcing the CI threshold. State recorded local pass results separately from an observed remote workflow run.

**Self-test follow-up:** Which additional gate would detect retrieval regressions that unit tests miss?

### Q158 What roles do pyproject, uv.lock, Ruff, mypy, and pytest play?

**Short answer:** They define package/tool settings, resolved dependencies, lint/format rules, static type checks, and executable tests.

pyproject.toml specifies dependencies, Python requirement, Hatchling packaging, and the rag-api console entry point. uv.lock records the resolution. Ruff catches selected code issues and formats; mypy checks annotated type consistency; pytest runs assertions. pytest-cov reports statement coverage. These tools are analogous in purpose to Gradle dependency configuration, Kotlin checking, formatting/static analysis, and unit tests, though the Python mechanisms differ. Pre-commit is installed as a dev extra but has no configured hook file.

**Self-test follow-up:** Which tool can detect a wrong type, and which can detect a semantically wrong score?

### Q159 How would you deploy safely with multiple replicas?

**Short answer:** Externalize durable state, use compatible versioned contracts, drain requests during rollout, and verify readiness and rollback behavior.

Multiple current workers would each have their own corpus, so increasing replicas first is incorrect. After persistence, manage database pools per process, cap aggregate connections, and ensure migrations are backward-compatible during rolling deployment. Handle long-running streams and jobs deliberately. A rollback must remain compatible with data written by the new version. Canary traffic can detect regressions before broad release.

**Self-test follow-up:** Why can a destructive schema migration make application rollback impossible?

### Q160 What are the first production-readiness priorities for this repository?

**Short answer:** Fix chunk termination, define idempotency, establish real tenant identity, add durable storage, and build representative evaluations.

The order depends on exposure and product requirements, but known correctness defects should not be hidden behind a model integration. Add resource controls, meaningful observability, lifecycle operations, and failure tests as external services arrive. Then improve retrieval and generation under measured quality gates. Do not claim the baseline is production-ready because it has a Dockerfile and high statement coverage.

**Self-test follow-up:** What would you refuse to scale before correcting its behavior?

## 17 Testing and practical debugging

In interviews, move from symptoms to hypotheses to discriminating evidence. Separate parser, retrieval, generation, authorization, and infrastructure failures rather than attributing every poor answer to the model.

### Q161 What do the existing eight tests actually verify?

**Short answer:** They cover public liveness, missing query auth, one grounded upload/query, disallowed MIME, basic chunk bounds, overlap validation, and two guardrail examples.

Four API tests use TestClient, two chunking tests check a simple split and overlap equal to size, and two guardrail tests check one query phrase and one document label. The grounded API test asserts success, grounded=true, and a filename; it does not assert exact text or score. The chunk test’s name suggests preservation, but its assertions only check multiple chunks and maximum length. Coverage therefore overstates behavioral completeness if read without test inspection.

**Self-test follow-up:** Which assertion would you add to catch repeated source sentences after reupload?

### Q162 How do unit, integration, end-to-end, load, and evaluation tests differ?

**Short answer:** They validate isolated logic, component interactions, user flows, performance under traffic, and retrieval/answer quality respectively.

A cosine unit test can check known vectors. A repository integration test can verify database transactions and tenant policies. An end-to-end test can upload and query through HTTP. A load test observes saturation and tails. A RAG evaluation set compares evidence relevance and answer support. Keep these purposes distinct so a fast unit suite does not falsely imply durable deployment or quality on real questions.

**Self-test follow-up:** Which test would prove data survives an API process restart?

### Q163 How would you test tenant isolation?

**Short answer:** Use distinct verified identities, ingest unique evidence for each, and assert no cross-access through query, source retrieval, jobs, or cache.

Test both allowed and denied paths, including missing filters, forged tenant labels, permission changes, and concurrent pooled connections. Database tests should use the same constrained role and policies as the application. Our direct repository filter experiment confirms one local filter case, but the API cannot yet express separate tenants. Until identity mapping exists, a real multi-tenant HTTP isolation test cannot pass meaningfully.

**Self-test follow-up:** How would you test that an external reranker never receives another tenant’s chunks?

### Q164 How do you debug no answer despite an uploaded document?

**Short answer:** Trace whether the document was accepted, parsed into chunks, stored in this worker, authorized, retrieved, and admitted by the gate.

Check chunks_created, process restarts, worker routing, tenant identity, tokenization, query terms, top-k competition, and threshold. A paraphrase may share no words with the source. A non-Latin document may yield no searchable tokens. An empty upload may have succeeded with zero chunks. If evidence reaches the generator but the answer remains wrong, then inspect generation. This staged approach avoids changing the model for a storage or retrieval problem.

**Self-test follow-up:** Which check distinguishes lost memory after restart from a relevance-threshold miss?

### Q165 How do you debug a wrong answer with apparently good retrieval?

**Short answer:** Inspect exact passages, source versions, missing caveats, context selection, prompt behavior, and claim-to-citation support.

A high similarity score may come from the wrong policy year or a negated statement. The relevant sentence may be outside the citation prefix or dropped during compression. Context can contain contradictions. Our extractor can concatenate incompatible chunks without reasoning about them. In a future model path, compare an oracle-context run against retrieved context to separate generation failure from selection failure.

**Self-test follow-up:** What does it mean if the model fails even when given the correct passage alone?

### Q166 How do you debug duplicate answers or citations?

**Short answer:** Check repeated document ingestion, overlap, duplicate corpus sources, and whether result diversification or deduplication is applied.

Our specific duplicate-upload bug comes from fresh chunk UUIDs combined with UUID-only repository deduplication. Overlapping chunks can repeat text even without reupload. Distinguish those causes before applying response-level string removal, which can hide a storage problem and damage legitimate repetition. Add stable tenant/version identity, storage constraints, and context deduplication where appropriate. Verify top-k is not monopolized by repeated copies.

**Self-test follow-up:** Why is removing duplicate sentences only at rendering time an incomplete fix?

### Q167 How do you debug slow requests?

**Short answer:** Break latency into stages, check queueing and resource saturation, then reproduce the slow input under controlled conditions.

For this baseline, corpus scan/sort and the chunk loop are key suspects; there is no model provider to blame. For a full pipeline, inspect embedding, database, reranker, token generation, retries, and connection-pool waits. Compare cache hits/misses and large/small inputs. Capture safe metadata and bounded samples. A timeout symptom alone does not identify whether work is blocked, overloaded, or waiting externally.

**Self-test follow-up:** How would event-loop lag differ from a slow asynchronous provider call?

### Q168 How would you test failure handling with fake adapters?

**Short answer:** Inject repositories or generators that return controlled hits, delay, or raise errors, then assert the service’s intended response and cleanup.

Ports make fakes easy to pass into RagService. Test empty hits, exact threshold edges, more than three admitted hits, duplicate evidence, and provider exceptions. The current generic handler maps unexpected errors to 500; richer retry and fallback behavior must first be designed before tests can validate it. Keep tests focused on externally meaningful behavior rather than duplicating every implementation line.

**Self-test follow-up:** How would you prove a failed generator does not accidentally return stale cached data?

### Q169 What is an oracle-context test?

**Short answer:** Give the generator known-correct evidence to determine whether answer failure persists when retrieval is ideal.

If the generator answers well with oracle context but poorly with actual retrieval, inspect candidate recall, ranking, chunking, and packing. If it fails with oracle evidence, inspect prompt, model capability, output parsing, or the answer rubric. This is an ablation, not a production retrieval strategy. Ensure oracle context does not leak the final answer into a way that makes the experiment unrealistic for the intended task.

**Self-test follow-up:** What would an oracle-retriever upper bound tell you about investing in a better index?

### Q170 Which regression tests should be added first?

**Short answer:** Prioritize chunk termination, duplicate-ingestion semantics, tenant boundaries, abstention edges, upload limits, and exact citation behavior.

These correspond to concrete current gaps, not speculative complexity. Add invalid UTF-8, empty/whitespace uploads, wrong API keys, threshold equality and zero behavior, request IDs, and settings isolation. For a future database add restart durability and concurrency tests. For a model adapter add bounded retry, timeout, structured output, and token-budget cases. The guide proposes these tests; application source and test files remain unchanged by the documentation task.

**Self-test follow-up:** Which test protects availability rather than only answer correctness?

## 18 Advanced RAG patterns and their tradeoffs

These are awareness topics for follow-up interviews, not implemented project features. Explain when a technique addresses a measured problem and what complexity it adds. Avoid listing advanced names without a workload or evaluation argument.

### Q171 What is agentic RAG?

**Short answer:** Agentic RAG lets a controller choose retrieval steps or tools dynamically rather than always execute one fixed search.

The controller may decide to rewrite a query, search again, consult structured data, or ask for clarification. This can help complex tasks but adds latency, branching, cost, and failure modes. Bound steps and budgets, enforce tool authorization outside the model, and log decisions. A deterministic pipeline is easier to reason about for straightforward questions. Our RagService follows a fixed sequence and is not an agent.

**Self-test follow-up:** What stopping condition prevents an agent from searching indefinitely?

### Q172 What is GraphRAG?

**Short answer:** GraphRAG uses entity/relationship structure and sometimes community summaries to retrieve evidence for connected or corpus-wide questions.

Microsoft’s implementation includes local search combining graph-related information with text chunks and global search over community reports. It can help questions requiring entity relationships or broad themes, but building and maintaining the graph adds extraction errors, cost, and update complexity. A graph does not automatically make facts correct or current. Keep provenance back to original passages. Reference: [GraphRAG query overview](https://microsoft.github.io/graphrag/query/overview/).

**Self-test follow-up:** When would a simple vector query be cheaper and sufficient?

### Q173 What is RAPTOR?

**Short answer:** RAPTOR recursively clusters and summarizes text into a retrieval tree spanning different levels of abstraction.

Leaf passages retain detailed evidence, while higher summaries support broader questions. This can address the limits of retrieving only short contiguous chunks. Summaries add generation cost and may omit or distort details, so preserve source lineage and evaluate both broad and precise questions. Updating source text can require refreshing derived summaries. Reference: [RAPTOR paper](https://arxiv.org/abs/2401.18059).

**Self-test follow-up:** How would you cite an answer derived from a generated summary?

### Q174 What are adaptive and self-correcting retrieval pipelines?

**Short answer:** They use feedback signals to decide whether to retrieve again, change strategy, verify evidence, or abstain.

For example, a system might detect low evidence coverage and try a lexical query after dense retrieval. The assessment can be heuristic or model-based and is itself fallible. Bound the loop, track reasons, and compare against a simpler fixed baseline under the same cost budget. A second retrieval does not guarantee the first error is corrected; it may reinforce an incorrect assumption.

**Self-test follow-up:** How would you distinguish useful recovery from a costly loop that never improves evidence?

### Q175 What is late-interaction retrieval?

**Short answer:** It keeps multiple token-level representations and combines fine-grained query-document interactions instead of one vector per passage.

This can retain details lost in a single pooled embedding and often occupies a middle ground between simple bi-encoder retrieval and full cross-encoder scoring. It adds vector storage and specialized indexing/scoring complexity. Evaluate whether the quality improvement justifies those costs for the corpus and latency budget. Our repository stores neither dense vectors nor token-level representations.

**Self-test follow-up:** Why can multi-vector representation increase memory even if each vector is small?

### Q176 How does multimodal RAG differ?

**Short answer:** It retrieves evidence from modalities such as images, tables, audio, or video while preserving modality-specific meaning and citations.

A scanned diagram may require image understanding rather than OCR text alone. Audio needs timestamps, video may need frame and transcript alignment, and tables need cell context. You may use separate encoders and retrieval paths with a grounded generation model that understands the selected modality. Evaluation must check cross-modal source fidelity. Our text-only parser and ASCII lexical tokenizer do not support this.

**Self-test follow-up:** How would you cite the exact moment in a video supporting an answer?

### Q177 How do SQL and knowledge graphs complement text retrieval?

**Short answer:** Structured queries answer exact filters and aggregations, while text retrieval supplies narrative evidence; routing should match the task.

A question about all invoices above a threshold is not a nearest-neighbor problem. A graph traversal may answer relationship questions explicitly. A hybrid system can combine authorized structured results with passages, but generated queries need validation, least privilege, and cost controls. Show calculations and query scope when relevant. Do not treat top-k text retrieval as exhaustive database scanning.

**Self-test follow-up:** How would you prevent a generated SQL query from reading another tenant’s rows?

### Q178 What is retrieval-model fine-tuning and what data does it need?

**Short answer:** It adapts a retriever using relevance supervision such as query-positive-negative examples, with held-out evaluation.

Useful negatives resemble relevant passages but do not answer the question, teaching discrimination beyond easy unrelated text. Poor labels and leaked duplicates can harm generalization. Domain adaptation may improve specialized language but can regress other slices. Re-embedding and index migration may be necessary after the model changes. This is separate from fine-tuning the answer generator and is not part of our current baseline.

**Self-test follow-up:** Why are hard negatives useful and how can a mislabeled negative hurt training?

### Q179 What are freshness-aware and time-aware retrieval?

**Short answer:** They use version, effective-date, and publication metadata to retrieve evidence valid for the requested time and scope.

Newest-by-ingestion is not always newest-by-policy, and “as of last year” should not automatically use today’s document. Store temporal metadata separately, define authority rules, and ensure caches include relevant version/time context. Questions may need explicit clarification about the date. Our Chunk created_at only records object creation, not business validity, so it cannot alone implement time-aware answers.

**Self-test follow-up:** How would you handle a policy announced today but effective next month?

### Q180 How do you choose whether an advanced pattern is worth adding?

**Short answer:** Tie it to a specific observed error class, run a controlled evaluation, and include complexity, latency, cost, and operability.

If paraphrase recall is poor, dense or hybrid retrieval may help; if candidate precision is poor, reranking may help; if questions require broad synthesis, hierarchical summaries may help. An agent or graph is not automatically the next step after basic RAG. Prefer the least complex change that meets the measured requirement. Keep rollback and monitoring for every new component.

**Self-test follow-up:** What evidence would convince you to remove an advanced stage you previously added?

## 19 System design interview scenarios

Use a repeatable sequence: clarify requirements, state assumptions, sketch data paths, define contracts and storage, estimate scale, discuss failure/security, then specify evaluation and rollout. The numerical values here are practice assumptions, not measured project performance.

### Q181 How would you design a company knowledge assistant?

**Short answer:** Separate authorized ingestion from querying, persist versioned evidence, retrieve and rerank within scope, generate cited answers, and evaluate continuously.

Clarify users, document types, update delay, permissions, latency, language, and answer-risk tolerance. Propose an API with trusted identity, object storage plus metadata/chunks, queued parsing/embedding, a searchable index, context packing, and a model gateway. Add quotas, deletion, observability, and a golden set. State what is synchronous versus asynchronous and when a document becomes searchable. Contrast this design with our baseline explicitly.

**Self-test follow-up:** What would you build first for a small internal pilot?

### Q182 How would you scale from one thousand to one million documents?

**Short answer:** Estimate chunk count and workload, introduce durable indexed storage and workers, then scale measured bottlenecks with tenant-aware limits.

Document count alone is insufficient: one thousand huge PDFs may exceed one million short notes. Estimate average chunks, vector dimensions, update rate, query rate, and ACL selectivity. Use backpressure for ingestion and independent query capacity. Include index rebuild and backup costs. Moving to many API replicas before replacing our memory repository would create inconsistent data, not useful scale.

**Self-test follow-up:** Which workload numbers must you ask for before choosing an index?

### Q183 How would you meet a two-second response target?

**Short answer:** Define whether the target is first token or completion, measure stage budgets, and trade off model/output size, reranking, retrieval, and caching.

A two-second p95 full answer is very different from a two-second first-token target. State assumed prompt and answer lengths, provider latency, and query complexity. Parallelize independent retrieval branches, cap reranking, and use safe caching where justified. If the target is infeasible under constraints, explain the tradeoff rather than invent capacity. Define degraded behavior for overload and provider failure.

**Self-test follow-up:** What changes if answers must be fully verified before any token is shown?

### Q184 How would you design for frequently changing policies?

**Short answer:** Use versioned ingestion with explicit publication, effective-date metadata, and cache invalidation tied to document and permission changes.

Preserve the previous active version while a new one is parsed and indexed. Switch visibility after validation so users do not see partial updates. Track deletion and ACL changes with high priority, and expose indexing lag. Evaluate temporal questions and conflicts. A content hash helps identify unchanged bytes but does not select the authoritative policy or invalidate old answers automatically.

**Self-test follow-up:** What do users see if new ingestion fails halfway through?

### Q185 How would you handle confidential multi-tenant data?

**Short answer:** Derive trusted identity, enforce tenant/ACL constraints before external processing, scope every artifact/cache, and test revocation and isolation.

Choose database policies and service roles intentionally. Ensure ingestion jobs retain tenant identity and source permissions. Do not log raw documents or send unauthorized candidates to rerankers. Provider selection, retention, encryption, and deletion behavior must match requirements. Consider dedicated storage or indexes where isolation and workload economics justify them. Our shared API key is not a finished multi-tenant design.

**Self-test follow-up:** What tradeoffs arise between shared indexes and tenant-specific indexes?

### Q186 How would you work under a very small model budget?

**Short answer:** Measure quality per unit cost, reduce avoidable context and calls, batch ingestion, cache safely, and use simpler retrieval/answers when sufficient.

The existing offline baseline is useful for learning and deterministic tests but cannot handle broad semantic questions like a full model. For a budgeted product, use lexical retrieval for exact queries, restrict unnecessary rewrites, rerank selectively, and cap output length. Track cost per successful supported answer, not only cost per call. A cheaper system that answers incorrectly can be a worse product.

**Self-test follow-up:** Which query types could be answered extractively without a generative call?

### Q187 How would you build an Android chat client for this backend?

**Short answer:** Use Retrofit DTOs and a repository/use-case layer, model abstention and errors explicitly, and keep privileged model access on the backend.

Separate transport DTOs from UI state, attach user credentials and a correlation ID, and display source excerpts with clear labels. Handle process recreation, retries, and upload progress. For future streaming define incremental events and completion/error states. A retry should not duplicate uploads once the backend supports idempotency. The current server is question-only, so conversation persistence and turn context need new API contracts rather than client assumptions.

**Self-test follow-up:** How would you distinguish a local optimistic message from a completed server answer?

### Q188 How would you handle a model-provider outage?

**Short answer:** Apply bounded timeouts and circuit breaking, preserve retrieval where possible, and return a clear degraded result or an approved fallback.

A fallback could provide source snippets without synthesis if the product permits it, similar to our extractive adapter. Another provider is only acceptable under the same data policy and evaluated behavior. Avoid endlessly retrying, lying about successful generation, or charging repeatedly without visibility. Keep provider failures distinct from no-evidence abstention in diagnostics and possibly the public schema.

**Self-test follow-up:** How would you prevent one failing provider from consuming all request capacity?

### Q189 How would you investigate declining answer quality after deployment?

**Short answer:** Compare versioned evaluation and traffic slices, inspect source freshness and retrieval traces, and isolate model, index, prompt, and corpus changes.

Start with what changed and when. Check ingestion failures, permission filters, embedding compatibility, candidate counts, threshold calibration, model version, and cache staleness. Use oracle-context tests and replayable safe examples. Roll back a suspect component if the evidence supports it, but avoid blaming the LLM by default. Monitor quality alongside latency and error rate because a service can remain technically available while becoming unhelpful.

**Self-test follow-up:** What if overall quality is stable but one tenant’s answers have deteriorated?

### Q190 How would you answer an interviewer asking for your architecture in five minutes?

**Short answer:** State requirements and current scope, walk ingestion and query paths, explain key tradeoffs, then discuss correctness gaps and the production plan.

Use a simple diagram and name components by responsibility. Explain why the baseline is lexical and extractive, how ports allow evolution, what authentication actually provides, and what tests prove. Show one numeric retrieval example and one failure case. Finish with measured next steps: fix chunking/idempotency, add identity/persistence/evals, then embeddings and generation. Do not use “we deployed” or claim benchmark gains without evidence.

**Self-test follow-up:** Which implementation detail best demonstrates that you inspected the code rather than memorized a diagram?

## 20 Coding exercises behavioral answers and common traps

Practice explaining decisions aloud and implementing small core functions. Strong interviews reward correct invariants, explicit assumptions, and honest ownership more than a list of tools. The answers below are preparation prompts, not claims of unbuilt features.

### Q191 How would you implement cosine similarity in a coding round?

**Short answer:** Tokenize into counters, compute shared-token dot product and norms, handle zero vectors, and test known scores.

Start with the metric definition and decide tokenization semantics before coding. Test identical vectors, disjoint vectors, repeated counts, empty inputs, and the handbook example. General dense vectors can be signed, unlike our nonnegative counts. Avoid dividing by zero and avoid interpreting the result as confidence. For performance, discuss precomputed document representations and norms before premature micro-optimization.

**Self-test follow-up:** What expected score should a nonzero vector have against itself?

### Q192 How would you implement safe overlapping chunking?

**Short answer:** Maintain a strict forward-progress invariant, enforce valid parameters, preserve normalized coverage, and use bounded boundary selection.

A safe solution must handle long words and short prefixes without negative or repeated starts. Define whether overlap is exact or best-effort when respecting boundaries, and make that choice testable. Prove every iteration either finishes or increases start, bounded by text length. Then test empty input, all whitespace, exact-size strings, no spaces, multibyte text, and overlap near size. The existing algorithm violates progress, so do not copy it unquestioningly.

**Self-test follow-up:** What maximum iteration bound follows from your chosen minimum stride?

### Q193 How would you implement idempotent ingestion?

**Short answer:** Choose a tenant-scoped idempotency key and payload hash, enforce uniqueness atomically, and return the stored result for a valid retry.

Record processing state so concurrent repeats do not start independent expensive work. Reusing the key with different content should have a defined conflict response. Store or derive stable chunk identities consistent with document/version policy. Distinguish retry idempotency from deduplicating identical files under different source permissions. A database constraint is the final arbiter, not an application-only exists check.

**Self-test follow-up:** How should a retry behave while the original request is still processing?

### Q194 How would you implement a retrieval evaluation loop?

**Short answer:** Load versioned questions and relevance labels, retrieve deterministically, calculate metrics, save per-query results, and compare configurations.

Record corpus, chunker, encoder, index, reranker, threshold, and code versions. Compute recall/ranking metrics with clear cutoffs and zero-relevance rules. Preserve per-query misses for inspection rather than only one average. Evaluate answers and abstention separately. Add latency/cost measurement under controlled conditions and do not tune repeatedly on the final test set. Our project has no evaluation runner yet.

**Self-test follow-up:** What data would you save to explain a regression six weeks later?

### Q195 What is an honest ninety-second project pitch?

**Short answer:** I built an inspectable FastAPI RAG baseline with modular ingestion, lexical retrieval, extractive answers, citations, tests, and explicit production gaps.

Expand with the actual flow: shared-key auth, bounded UTF-8 uploads, content hashing, overlapping character chunks, in-memory cosine ranking, a relevance gate, and deterministic extraction. Mention ports for future storage/model adapters. State that all API callers currently share default, reuploads duplicate chunks, and chunking needs a progress fix. Cite the recorded test scope without claiming retrieval accuracy. Then explain the planned identity, persistence, evaluation, embedding, and model work.

**Self-test follow-up:** How do you discuss limitations confidently without making unsupported claims of production maturity?

### Q196 How should you answer “Why did you choose this design?”

**Short answer:** Explain the goal of a cheap reproducible baseline, the benefits of explicit boundaries, and the tradeoffs you intentionally accepted.

Say that local lexical retrieval and extraction remove provider dependencies while making behavior testable and visible. The cost is weak paraphrase support, no synthesis, and no durable storage. Do not describe discovered defects as intentional choices: duplicate ingestion and non-advancing chunking need correction. Separate deliberate simplification from missing implementation and bugs. Then show how you would evaluate the next change.

**Self-test follow-up:** Which choices were educational simplifications and which issues are correctness defects?

### Q197 How should you answer “What was the hardest bug?”

**Short answer:** Use the observed chunk-progress defect to explain symptom, root cause, invariant, proposed fix, and verification without claiming an unperformed fix.

A precise answer is that inspection found boundary selection could move start backward and eventually repeat it. The existing bounds-only test missed termination. A bounded trace demonstrated the repeated state. The intended fix is strict forward progress and parameter validation with edge-case regressions. As of this guide, the source remains unchanged. If you later implement it, update the story with actual tests and outcomes rather than presenting the proposal as completed work.

**Self-test follow-up:** What evidence separates an observed defect from a speculative risk?

### Q198 Which claims should you avoid in interviews about this project?

**Short answer:** Do not claim embeddings, a vector database, an LLM, durable multi-tenancy, verified citations, or production deployment are already implemented.

Also avoid saying that 94.76% code coverage means 94.76% answer accuracy, that the regexes solve injection, that async means parallel CPU execution, that document hashing deduplicates uploads, or that Docker proves production readiness. The strength of the project is its inspectable baseline and your reasoning about improvement. Accurate scope makes deeper technical follow-ups easier to answer.

**Self-test follow-up:** How would you correct yourself if you accidentally called the lexical vectors embeddings?

### Q199 How do you answer an unfamiliar RAG design question?

**Short answer:** Clarify the required behavior, state assumptions, reason from data and failure boundaries, and propose a measurable experiment.

If you do not know an index parameter or model behavior, say so and explain how you would verify it in official documentation and a benchmark. Use fundamentals: where evidence comes from, who can access it, how it is selected, how the answer is supported, and what happens on failure. Avoid inventing a universal best chunk size or latency number. Tradeoff reasoning is more transferable than memorizing vendor names.

**Self-test follow-up:** What information would you ask before estimating capacity or choosing a model?

### Q200 What should you be able to demonstrate before the interview?

**Short answer:** Explain the code, run upload/query and abstention examples, calculate similarity and metrics, diagnose failures, and sketch a measured production design.

Practice a five-minute architecture walkthrough, a two-minute current-scope pitch, and coding drills for cosine, chunk progress, and idempotency. Be ready to point to routes, RagService, ports, adapters, settings, tests, and deployment files. Explain why the next steps are ordered as they are. Use the matching question IDs in the revision and detailed guides to revisit weak topics; no question bank can guarantee every possible interviewer prompt.

**Self-test follow-up:** Can you explain the full pipeline aloud without using a framework name as the explanation?

## 21 Additional backend interview depth

These topics often appear when an AI interview turns into a backend design discussion. They are not all implemented here, but understanding them helps explain how a mobile-facing RAG API becomes a reliable shared service.

### Q201 How do JWT and OAuth differ?

**Short answer:** JWT is a claims/token representation; OAuth is an authorization framework, and a signed JWT is not necessarily encrypted.

A backend validating a JWT must verify signature, allowed algorithm, issuer, audience, expiration, and relevant claims under its identity design. Decoding the payload alone establishes no trust. OAuth describes delegated access flows and roles; its access tokens need not be JWTs. Our shared API key implements neither. For a mobile product, use a suitable identity system and current platform/security guidance rather than inventing an authentication protocol. References: [JWT](https://www.rfc-editor.org/rfc/rfc7519) and [OAuth framework](https://www.rfc-editor.org/rfc/rfc6749).

**Self-test follow-up:** Why does a validly signed token for another API not authorize access to ours?

### Q202 What is a connection pool?

**Short answer:** A connection pool reuses a bounded set of database or HTTP connections instead of creating one for each operation.

Connections cost setup time and server resources. Size pools with total replicas and worker processes in mind: ten workers each allowing twenty database connections can demand two hundred. Long-held connections or transactions can queue requests. Acquire for the needed scope, release reliably, set deadlines, and reset tenant/session state safely. Create process-owned pools during lifespan and close them at shutdown. Our memory adapter has no connection pool.

**Self-test follow-up:** Why can simply increasing pool size overload the database?

### Q203 What do database indexes and transaction isolation add?

**Short answer:** Indexes accelerate selected access patterns; isolation defines what concurrent transactions can observe and modify safely.

A B-tree index on tenant and document identity serves a different purpose from a vector ANN index. Indexes consume storage and write work, so choose them from actual queries. Isolation affects races such as concurrent updates or publishing conflicting active versions; constraints and version checks enforce important invariants. Do not assume all databases or default isolation levels provide the same behavior. Our current list has no database transaction semantics.

**Self-test follow-up:** Which index would help a highly selective tenant filter before vector scoring?

### Q204 What are ORMs, migrations, and SQL injection?

**Short answer:** An ORM maps code to database operations, migrations version schema changes, and parameterized queries help prevent SQL injection.

An ORM can simplify access but does not remove the need to understand queries, indexes, transactions, and authorization. Migrations should be reviewed and compatible with rollout/rollback requirements. Never concatenate user text into SQL commands; use bound parameters and least-privilege roles. A future text-to-SQL model adds another untrusted query source requiring validation and restricted execution. No ORM or migration system is currently installed as an application component.

**Self-test follow-up:** Can an ORM query still accidentally omit a tenant filter?

### Q205 Are FastAPI BackgroundTasks a durable job queue?

**Short answer:** No; post-response in-process tasks do not provide durable distributed job ownership or recovery by themselves.

They can be useful for limited work after sending a response, but a worker crash can lose unfinished work. Expensive parsing and embedding across many documents needs explicit persistence, job state, retries, and bounded workers. The current project does not use BackgroundTasks either. Use the right mechanism for the completion guarantee, not merely an API that makes the response return earlier. Reference: [FastAPI background tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/).

**Self-test follow-up:** What happens to an in-process task during container replacement?

### Q206 How should cancellation and structured concurrency work?

**Short answer:** Tie related tasks to a request or job scope, propagate cancellation deliberately, and ensure cleanup without assuming completed side effects are undone.

If an Android user navigates away, cancelling the client request may stop some server work, but an already committed upload remains. For independent concurrent retrieval branches, manage child-task lifetimes and exceptions together. Release resources in cleanup paths and decide whether accepted ingestion jobs outlive the HTTP connection. CPU loops without suspension require a different execution strategy. Reference: [Python coroutines and tasks](https://docs.python.org/3/library/asyncio-task.html).

**Self-test follow-up:** Which work should continue after a client disconnects, and why?

### Q207 What should you know about Python CPU work, the GIL, and GPUs?

**Short answer:** Async is for cooperative concurrency; CPU/GPU execution requires deliberate runtime and worker design, not an async keyword.

Traditional GIL-enabled CPython limits concurrent Python bytecode execution in one process, while native libraries can release the GIL and Python builds differ. Avoid universal claims about all Python versions. CPU-bound Python work often benefits from separate processes; model inference may use native/GPU execution and batching. GPU memory, model residency, batch size, and queueing determine serving capacity. Our current scorer is ordinary Python CPU work.

**Self-test follow-up:** Why might a thread pool help blocking I/O more than pure Python token-count computation?

### Q208 How should pagination and payload limits be designed?

**Short answer:** Bound list sizes and request/response sizes, and use stable pagination contracts for growing document collections.

Future document listing should not return the entire corpus. Cursor pagination can avoid some shifting-page problems under updates, but the ordering and snapshot semantics must be defined. Query result counts, citation excerpts, filenames, request IDs, and generated answer length also deserve bounds. Our API has upload/query limits and a top_k cap, but no document listing or full transport-body policy.

**Self-test follow-up:** What happens to offset pagination when documents are inserted between requests?

### Q209 How should exceptions be translated across layers?

**Short answer:** Adapters expose meaningful internal failures, services apply business policy, and routes translate outcomes into stable public errors.

Do not leak vendor stack traces, SQL strings, or secrets to clients. Distinguish invalid input, missing authorization, unavailable dependencies, timeouts, and internal bugs. Our route catches selected decoding and policy exceptions; everything else reaches a generic 500 response. A full model gateway needs richer internal categories and observable causes so the client can retry appropriately without learning confidential internals.

**Self-test follow-up:** Should a provider rate-limit failure always be presented as the user exceeding their own quota?

### Q210 How do network partitions affect consistency and availability?

**Short answer:** When systems cannot communicate, you must define which operations fail, wait, or serve possibly stale data while preserving required invariants.

Do not reduce distributed design to “pick any two” without specifying the partition and operation. A tenant permission revocation may require refusing stale cached access, whereas an explicitly stale public answer may be acceptable. Replication lag, index lag, and queue delay are different consistency problems. State the product’s guarantees, failure policy, and recovery process. Our single-process baseline has no distributed replication or partition-handling design.

**Self-test follow-up:** Would you serve cached confidential evidence if current authorization cannot be verified?

## 22 Additional model and evaluation interview depth

These questions connect RAG engineering to the model concepts an AI interviewer may probe. They supplement the system design; implementing a transformer or fine-tuning a model is not part of the current repository.

### Q211 What is attention in a transformer?

**Short answer:** Attention computes context-dependent combinations of token representations so tokens can use information from other permitted positions.

The familiar scaled dot-product form uses query/key similarities to weight value vectors, with masking where required. These attention queries are model-internal vectors, not the same object as our user’s HTTP question or a database query. Transformer architectures differ, but this mechanism helps explain why a model can relate parts of a context and why context length has compute implications. Reference: [Attention Is All You Need](https://arxiv.org/abs/1706.03762).

**Self-test follow-up:** How does causal masking differ from an encoder reading a whole passage?

### Q212 How are retrieval embeddings trained with contrastive learning?

**Short answer:** Training rewards relevant query-passage pairs relative to negative passages so compatible encoders learn useful retrieval geometry.

A common objective increases the positive pair’s similarity compared with negatives in a batch or mined candidate set. Negative quality, labeling errors, and domain coverage affect what the model learns. The resulting space is task-specific, not a universal semantic truth map. This motivates held-out evaluation and careful handling of hard negatives. Our term-frequency vectors are manually computed counts and have no training objective.

**Self-test follow-up:** Why can in-batch negatives accidentally include another relevant passage?

### Q213 How do TF-IDF, BM25, sparse learned retrieval, and dense retrieval differ?

**Short answer:** They use lexical weights, saturated probabilistic-style lexical scoring, learned sparse term weights, and learned dense vectors respectively.

TF-IDF weights terms by local frequency and corpus rarity, while BM25 adds its own saturation and length normalization. Learned sparse models can expand or weight vocabulary terms with a neural model. Dense models represent text in a compact continuous space. Each has strengths for exact terms, paraphrases, storage, and scoring infrastructure. Our baseline uses raw TF cosine, so it is neither TF-IDF nor BM25 nor a learned sparse model.

**Self-test follow-up:** Why should you identify the exact baseline instead of calling every lexical method BM25?

### Q214 What is SPLADE?

**Short answer:** SPLADE learns sparse vocabulary-based representations with term expansion for first-stage retrieval.

It uses a neural model to assign sparse term weights, including useful terms beyond literal input matches. This can bridge lexical and semantic behavior while using sparse retrieval infrastructure. Learned expansion can still introduce noise and requires model/version and index management. It is an alternative retrieval component to evaluate, not something our Counter implements. Reference: [SPLADE paper](https://arxiv.org/abs/2107.05720).

**Self-test follow-up:** How is learned sparse expansion different from manually adding synonyms?

### Q215 What is Dense Passage Retrieval or DPR?

**Short answer:** DPR uses compatible question and passage encoders to retrieve passages through dense-vector similarity.

The question and passage sides can be trained jointly with relevance supervision. Document representations can then be indexed, while a new question is encoded at query time. This illustrates why retrieval embeddings are trained for matching and why compatibility matters. DPR is a research architecture, not a vector database or a generator. Reference: [Dense Passage Retrieval paper](https://arxiv.org/abs/2004.04906).

**Self-test follow-up:** Where does DPR end and answer generation begin?

### Q216 What is ColBERT?

**Short answer:** ColBERT uses contextual token representations and late interaction to score fine-grained query-passage matches efficiently.

Rather than collapsing a passage to a single vector, it retains token-level information and combines token matches at scoring time. This can improve discrimination while adding storage and retrieval-system complexity. Compare the actual variant and implementation against single-vector and reranking baselines under the same constraints. Reference: [ColBERT paper](https://arxiv.org/abs/2004.12832).

**Self-test follow-up:** Why is ColBERT not simply a cross-encoder run on every document?

### Q217 What is LoRA and does it replace RAG?

**Short answer:** LoRA trains low-rank parameter updates for adaptation; it does not replace retrieval of current authorized evidence.

LoRA is a parameter-efficient fine-tuning approach that can reduce trainable parameters compared with full adaptation. It changes model behavior through training, while RAG changes available context at inference. A system may use both if evaluation justifies it. Neither LoRA nor model fine-tuning exists in our repository. Reference: [LoRA paper](https://arxiv.org/abs/2106.09685).

**Self-test follow-up:** Would you use LoRA to implement document deletion from a changing knowledge base?

### Q218 How do candidate recall, context precision, and answer quality connect?

**Short answer:** Retrieval must find useful evidence, packing must retain the right parts, and generation must use them correctly; failure at any stage can dominate.

A high-recall candidate set can still become a poor prompt if duplicates or long irrelevant passages crowd out the answer. A perfect prompt can still yield an unsupported claim from the generator. Measure stages separately and run ablations such as oracle context or no-reranker comparisons. Do not optimize a single retrieval metric without checking supported-answer outcomes and resource cost.

**Self-test follow-up:** Why can increasing candidate count improve recall but reduce final answer quality?

### Q219 What does calibrated confidence mean?

**Short answer:** A calibrated confidence estimate matches observed correctness frequencies on a defined population; similarity scores are not automatically calibrated.

If responses assigned 0.8 confidence are correct about 80% of the time under a stable rubric and distribution, the estimate is calibrated in that sense. Build and validate such estimates on held-out data and monitor drift. Retrieval similarity, model token probability, and self-reported confidence measure different things and may correlate poorly with task correctness. Our grounded boolean and citation score should not be presented as calibrated confidence.

**Self-test follow-up:** How would you test calibration separately from ranking quality?

### Q220 How do annotation quality and uncertainty affect evaluation?

**Short answer:** Clear relevance rubrics, multiple reviewers where needed, disagreement review, and uncertainty reporting make metric conclusions more trustworthy.

Two passages may support different valid interpretations, and a source may be outdated or partially relevant. Define graded labels, query scope, source versions, and abstention criteria. Review disagreements instead of silently treating one label as unquestionable truth. Small test sets can make apparent improvements unstable, so include confidence intervals or paired error inspection. Good evaluation is a maintained data product, not just a script that prints a score.

**Self-test follow-up:** How would you revise an ambiguous question without biasing the evaluation toward one system?

## Primary references

The implementation claims come from the repository files listed in the tutorial. The following official documentation and research papers support the general backend and RAG concepts; links also appear beside the relevant explanations. Design proposals and numerical practice assumptions are labeled separately.

- [HTTP semantics](https://www.rfc-editor.org/rfc/rfc9110.html)

- [FastAPI concurrency](https://fastapi.tiangolo.com/async/)

- [FastAPI dependencies](https://fastapi.tiangolo.com/tutorial/dependencies/)

- [Pydantic settings](https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings/)

- [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/)

- [Lewis and colleagues on RAG](https://arxiv.org/abs/2005.11401)

- [HNSW paper](https://arxiv.org/abs/1603.09320)

- [pgvector indexing](https://github.com/pgvector/pgvector)

- [pgvector filtering](https://github.com/pgvector/pgvector#filtering)

- [PostgreSQL row security](https://www.postgresql.org/docs/17/ddl-rowsecurity.html)

- [Elastic similarity documentation](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/index-modules-similarity.html)

- [Elastic hybrid search](https://www.elastic.co/docs/solutions/search/hybrid-search)

- [Elastic RRF](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion)

- [Sentence Transformers retrieve and rerank](https://www.sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html)

- [HyDE paper](https://arxiv.org/abs/2212.10496)

- [Lost in the Middle](https://arxiv.org/abs/2307.03172)

- [Stanford ranked retrieval evaluation](https://nlp.stanford.edu/IR-book/html/htmledition/evaluation-of-ranked-retrieval-results-1.html)

- [OWASP prompt injection guidance](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)

- [OWASP RAG security](https://cheatsheetseries.owasp.org/cheatsheets/RAG_Security_Cheat_Sheet.html)

- [uv locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/)

- [Kubernetes probes](https://kubernetes.io/docs/concepts/workloads/pods/probes/)

- [OpenTelemetry signals](https://opentelemetry.io/docs/concepts/signals/)

- [GraphRAG query overview](https://microsoft.github.io/graphrag/query/overview/)

- [RAPTOR paper](https://arxiv.org/abs/2401.18059)

- [JWT](https://www.rfc-editor.org/rfc/rfc7519)

- [OAuth framework](https://www.rfc-editor.org/rfc/rfc6749)

- [FastAPI background tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/)

- [Python coroutines and tasks](https://docs.python.org/3/library/asyncio-task.html)

- [Attention Is All You Need](https://arxiv.org/abs/1706.03762)

- [SPLADE paper](https://arxiv.org/abs/2107.05720)

- [Dense Passage Retrieval paper](https://arxiv.org/abs/2004.04906)

- [ColBERT paper](https://arxiv.org/abs/2004.12832)

- [LoRA paper](https://arxiv.org/abs/2106.09685)

## Completion checklist for your preparation

- Explain every current module and API field without claiming future features are built.
- Calculate the handbook score and toy retrieval metrics.
- Demonstrate upload, a supported result, abstention, duplicate ingestion, and restart data loss.
- Explain chunk termination and idempotency invariants.
- Compare lexical, dense, hybrid, and reranked retrieval using a concrete question.
- Design tenant-safe persistence, jobs, generation, and caching.
- Separate unit tests, evaluation quality, security, and operational metrics.
- Practice at least one latency/cost estimate and one five-minute system-design walkthrough.
- Revisit questions you cannot answer with an example, failure case, and test.
