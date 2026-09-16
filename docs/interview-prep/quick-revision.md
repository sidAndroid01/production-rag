# RAG interview quick revision for an Android developer

Use these 220 question and answer pairs for quick recall. The detailed guide uses the same question IDs and includes explanations, examples, code references, and primary-source links.

Current scope: an offline FastAPI service with UTF-8 ingestion, in-memory lexical cosine retrieval, an extractive generator, citations, a shared API key, and engineering scaffolding. Embeddings, a persistent database, a hosted LLM, a durable queue, and a RAG evaluation benchmark are not built.

Code re-inspected 6 September 2026. The recorded 5 September verification passed 8 tests with 94.76% statement coverage plus lint, formatting, and typing checks. Coverage is not answer accuracy.

Android bridges: Retrofit → HTTP contract; Kotlin DTOs → Pydantic schemas; Hilt → dependency injection; repository interface → Protocol; application singleton → per-process app.state; Room → future durable storage; suspend → async with no automatic CPU parallelism.

Numbers to remember for this code: 5,000,000 upload bytes; 2,000 stripped query characters; 900-character chunks; 120-character intended overlap; top-k 5; minimum score 0.10; generation uses up to 3 hits; citation excerpts use 240 characters; document IDs use 24 hex characters.

Important defects and limits: shared default API tenant, duplicate reuploads, non-advancing chunk-loop edge case, volatile storage, constant readiness, and grounding that only means a hit passed the score gate.

Each answer below is one sentence. Use the matching detailed entry whenever you cannot explain the mechanism or tradeoff.

## 1 Backend foundations through Android concepts

- **Q001 What is a backend and what does ours do?** Our backend accepts text documents and questions over HTTP, stores searchable chunks in memory, and returns extracted evidence with citations.

- **Q002 What are HTTP requests and responses?** A request has a method, URL, headers, and optional body; a response has a status, headers, and optional body.

- **Q003 How do GET and POST differ in this project?** GET reads health state; POST carries document uploads or query bodies, and POST is not inherently idempotent.

- **Q004 What are JSON, serialization, and multipart uploads?** JSON represents structured request/response data; multipart separates uploaded files and fields into parts within one request.

- **Q005 What are FastAPI, Uvicorn, and ASGI?** Uvicorn serves network traffic, ASGI is the application-server interface, and FastAPI maps requests to validated Python handlers.

- **Q006 What are a process, thread, coroutine, and event loop?** A process owns memory, threads execute within it, coroutines can suspend, and an event loop schedules asynchronous tasks.

- **Q007 Does async or await make our retrieval parallel?** No; our CPU-bound tokenization and sorting run synchronously despite async method signatures.

- **Q008 What does stateless mean and is our API stateless?** A stateless API keeps durable application state outside individual workers; our current corpus is process-local state.

- **Q009 What are ports, localhost, DNS, and TLS?** A hostname resolves to a server address, a port selects a service, localhost points to the caller itself, and TLS protects transport.

- **Q010 How should an Android client handle backend outcomes?** Represent loading, success, abstention, validation errors, authentication errors, and transport failures as distinct UI states.

## 2 HTTP contract authentication and configuration

- **Q011 Which endpoints have we built?** GET /health/live, GET /health/ready, POST /v1/documents, and POST /v1/query are the four application routes.

- **Q012 How does API key authentication currently work?** The x-api-key header is compared with one configured key, and a successful match returns tenant default.

- **Q013 How are authentication and authorization different?** Authentication establishes identity; authorization decides whether that identity may access a specific resource or action.

- **Q014 What does FastAPI dependency injection do here?** Depends resolves Settings, RagService, and authenticated tenant inputs before invoking the endpoint.

- **Q015 What do 200, 201, 400, 401, 413, 415, 422, and 500 mean here?** They represent successful query, successful upload, policy rejection, failed authentication, oversized file, unsupported MIME, invalid input, and unexpected failure.

- **Q016 Where are schema validation and policy validation applied?** Pydantic validates the JSON shape first; RagService applies stripped length and query policy checks afterward.

- **Q017 What settings exist and what are their defaults?** Defaults are 5,000,000 upload bytes, 2,000 query characters, top_k 5, minimum score 0.10, and the development API key.

- **Q018 How do environment settings and caching work?** Settings loads environment and .env values, while lru_cache reuses the Settings object until the process or cache is reset.

- **Q019 What are request IDs and what have we implemented?** The query echoes a truthy x-request-id or generates a UUID, but it does not propagate that ID through logs and traces.

- **Q020 What are CORS and API versioning concerns?** CORS is a browser cross-origin policy, while /v1 is an API naming convention whose compatibility must still be maintained.

## 3 Architecture models and startup lifecycle

- **Q021 How are the code layers organized?** Routes handle HTTP, RagService orchestrates use cases, domain models and ports define contracts, and adapters implement infrastructure.

- **Q022 What happens during application startup?** The lifespan function loads settings, configures logs, constructs adapters and RagService, and stores the service in app.state.

- **Q023 What is a Python Protocol and how does it relate to a Kotlin interface?** A Protocol defines a structural contract: an object can satisfy it by exposing the required methods without explicit inheritance.

- **Q024 What does RagService own?** RagService owns ingestion/query sequencing, document identity, relevance filtering, citation construction, and response assembly.

- **Q025 What fields does Chunk store?** Chunk stores text, document_id, zero-based index, tenant_id, source filename, random chunk UUID, and UTC creation time.

- **Q026 How do Chunk, SearchHit, Citation, and response DTOs differ?** Chunk is stored evidence, SearchHit adds internal ranking, Citation exposes provenance, and response DTOs define the public API.

- **Q027 What do frozen dataclasses, slots, UUIDs, and UTC timestamps buy us?** They provide stable object fields, compact instance structure, generated chunk identity, and timezone-aware creation metadata.

- **Q028 How would we replace the memory repository or extractive generator?** Implement the existing port, wire the adapter in main.py, and preserve semantic contracts with tests.

- **Q029 Why start without a RAG framework?** A small explicit pipeline makes behavior, errors, tests, and tradeoffs inspectable before adding abstraction.

- **Q030 What are the limits of our clean architecture?** The boundaries are useful, but persistence semantics, tenant identity, observability, and richer provider failures are still incomplete.

## 4 Document ingestion and identity

- **Q031 Walk through an upload end to end.** Authenticate, read at most limit plus one byte, check declared MIME, decode UTF-8, sanitize, hash original bytes, chunk, store, and return 201.

- **Q032 Why read MAX_UPLOAD_BYTES plus one?** The extra byte distinguishes a file exactly at the limit from an oversized file while bounding the route’s read buffer.

- **Q033 Which file types are supported?** Only UTF-8 content with exactly text/plain, text/markdown, or application/octet-stream as the declared MIME value is accepted.

- **Q034 What is UTF-8 and why strict decoding?** UTF-8 encodes Unicode text as bytes; strict decoding rejects invalid byte sequences rather than silently changing evidence.

- **Q035 What does document sanitization actually do?** It replaces line-leading system, assistant, or developer labels with an untrusted-document marker.

- **Q036 How is document identity computed?** SHA-256 hashes the original upload bytes; the full digest is returned and its first 24 hex characters become document_id.

- **Q037 Are repeated uploads idempotent?** No; repeated bytes reuse the document ID but create new chunk UUIDs, so the repository stores duplicates.

- **Q038 What happens with empty uploads?** An empty or whitespace-only text file returns 201 with zero chunks.

- **Q039 What source and version metadata should production ingestion preserve?** Preserve original location, tenant/ACL, document version, effective dates, parser/chunker versions, and page or character spans.

- **Q040 How do you handle updates and deletion?** Create versioned ingestion output, atomically publish the active version, and invalidate retrieval and caches when data changes.

## 5 Chunking choices and failure cases

- **Q041 Why split documents into chunks?** Chunks make long documents searchable at useful granularity and let queries retrieve focused evidence.

- **Q042 What is our exact chunking algorithm?** Collapse whitespace, take a 900-character window, prefer its last space, append the slice, and restart 120 characters before its end.

- **Q043 Why are characters, bytes, words, and tokens different?** Bytes encode text, characters are string units, words depend on language rules, and tokens depend on a model tokenizer.

- **Q044 What is the chunk-size tradeoff?** Smaller chunks improve focus but may lose context; larger chunks preserve context but add noise and consume more retrieval/generation budget.

- **Q045 Why use overlap and what does it cost?** Overlap preserves boundary context but increases stored text, duplicate hits, and downstream token cost.

- **Q046 What is the known chunk-progress defect?** A boundary too close to start makes end minus overlap fail to advance, potentially causing an infinite loop.

- **Q047 How would you fix and test chunking?** Validate sizes and enforce 0 ≤ start < next_start ≤ text_length for every non-final iteration, with boundary and long-token tests.

- **Q048 What are recursive, sentence-aware, and semantic chunking?** They choose boundaries using structural separators, sentence boundaries, or semantic changes instead of only a fixed character window.

- **Q049 What is parent-child or small-to-big retrieval?** Retrieve small precise child chunks, then provide authorized surrounding parent context to the generator.

- **Q050 How should tables, code, scanned PDFs, and languages affect chunking?** Preserve their structure and provenance; generic whitespace flattening can destroy the relationships needed for correct answers.

## 6 Lexical retrieval and the current answer path

- **Q051 How does our tokenizer work?** It lowercases text and counts matches of the ASCII pattern [a-zA-Z0-9]+.

- **Q052 What is a term-frequency vector?** It is a sparse mapping from each token to the number of times that token occurs.

- **Q053 How is cosine similarity computed?** Cosine is the dot product divided by the product of vector lengths; zero-norm inputs return zero in our implementation.

- **Q054 Work out the handbook similarity by hand.** The question and handbook share three tokens, so cosine is 3 divided by sqrt(5×7), approximately 0.5071.

- **Q055 In what order do tenant filtering, top-k, and thresholding happen?** Search scores only matching-tenant chunks, sorts and takes top-k, then RagService applies the minimum score.

- **Q056 What does MIN_RELEVANCE_SCORE mean?** It is an inclusive lexical similarity cutoff, not a calibrated confidence or universal threshold.

- **Q057 How does ExtractiveGenerator produce an answer?** It ignores the question and joins the text of the first three admitted hits after a fixed prefix.

- **Q058 How do abstention and grounded work?** No admitted hits produce a fixed insufficient-evidence answer; grounded is simply whether the admitted-hit list is nonempty.

- **Q059 What do our citations prove and where can they mislead?** They identify admitted chunks and show prefix excerpts; they do not establish that each answer claim is supported.

- **Q060 What is the complexity and scalability of current search?** It scans and tokenizes all matching-tenant chunks, then fully sorts their scores, with no search index or cached vectors.

## 7 RAG and language model fundamentals

- **Q061 What is RAG?** RAG retrieves relevant external evidence and conditions answer generation on that evidence at query time.

- **Q062 Why use RAG for enterprise knowledge?** It can supply current, private, attributable information, provided ingestion freshness and authorization are correctly implemented.

- **Q063 How do RAG and fine-tuning differ?** RAG supplies external evidence at inference time; fine-tuning changes model behavior or parameters through additional training.

- **Q064 How do RAG, search, and long-context prompting differ?** Search returns evidence, RAG uses evidence to answer, and long-context prompting supplies a larger body of text directly.

- **Q065 What are an LLM and next-token prediction?** An LLM estimates likely next tokens from context and generates a sequence; fluency does not establish factual correctness.

- **Q066 What is a context window and token budget?** The context window limits tokens processed in a request, so instructions, history, evidence, and output allowance must be budgeted.

- **Q067 What are hallucination, faithfulness, and factual correctness?** Hallucination is unsupported or fabricated content; faithfulness checks support in evidence, while correctness checks truth under the task.

- **Q068 Does temperature zero guarantee correct deterministic answers?** No; low temperature reduces sampling variation but does not guarantee truth or identical behavior across all serving conditions.

- **Q069 What are parametric and nonparametric knowledge?** Parametric knowledge is encoded in model weights; nonparametric knowledge is stored externally and retrieved when needed.

- **Q070 When should you not use RAG?** Avoid adding RAG when the task needs no external evidence, exact structured computation is better, or retrieval adds cost without quality benefit.

## 8 Embeddings and representation choices

- **Q071 What is a text embedding?** A text embedding is a learned numeric representation used to compare texts in a model-defined vector space.

- **Q072 How are embeddings different from LLM generation?** An embedding model returns a vector for retrieval; a generative model returns tokens for an answer.

- **Q073 Must query and document embeddings use compatible models?** Yes; query and document representations must be in compatible spaces with the expected preprocessing and scoring rules.

- **Q074 How do you choose an embedding model?** Evaluate representative domain and language queries under quality, latency, input-length, storage, privacy, and cost constraints.

- **Q075 What is embedding dimension and how does it affect storage?** Dimension is the number of vector coordinates; more coordinates increase raw storage and distance-computation work.

- **Q076 How do cosine, dot product, and Euclidean distance relate?** For unit-normalized vectors cosine equals dot product, and squared Euclidean distance equals 2 minus 2 times cosine.

- **Q077 How should embedding requests be batched and retried?** Batch within token/item limits, bound concurrency, retry transient failures with backoff and jitter, and track per-item completion.

- **Q078 How do you migrate an embedding model safely?** Build a versioned new index, evaluate it, switch query encoding and index selection together, and retain a rollback path.

- **Q079 How do multilingual and domain-specific retrieval change embedding strategy?** Evaluate each language and domain separately, preserving original text and testing cross-language or specialized terminology explicitly.

- **Q080 Are embeddings anonymized or safe to share freely?** No; embeddings are derived from source data and should remain subject to access control and privacy policy.

## 9 Vector storage indexes and filtering

- **Q081 What does a vector store provide?** It stores vectors with identifiers and metadata and retrieves nearby candidates under a chosen distance function.

- **Q082 How do exact and approximate nearest-neighbor search differ?** Exact search finds the true nearest vectors for the metric; ANN trades some retrieval recall for lower search cost.

- **Q083 What is HNSW?** HNSW is a hierarchical proximity-graph index that navigates through vector neighbors to find approximate matches.

- **Q084 What are common HNSW tuning concepts?** Graph connectivity and construction effort affect index quality and memory, while query search effort affects recall and latency.

- **Q085 What is IVFFlat and how does it differ from HNSW?** IVFFlat partitions vectors into coarse lists and probes selected lists; HNSW navigates a graph of neighbors.

- **Q086 What are quantization and compression tradeoffs?** Quantization reduces vector precision or encodes subspaces to save memory and work, potentially reducing retrieval accuracy.

- **Q087 Why are metadata filtering and ANN interaction tricky?** Approximate candidates may be filtered after search, leaving too few permitted results even when relevant permitted data exists.

- **Q088 How do prefiltering and postfiltering differ?** Prefiltering restricts eligible data before candidate search; postfiltering removes ineligible candidates afterward.

- **Q089 How would Postgres row-level security help?** RLS can enforce row access policies in the database, but runtime roles, tenant context, and bypass behavior must be configured correctly.

- **Q090 What should you evaluate before choosing a vector database?** Measure authorized retrieval quality, latency, update/deletion behavior, durability, scale, operational burden, and total cost.

## 10 Hybrid retrieval reranking and query transformations

- **Q091 What is BM25 and how does it differ from our cosine baseline?** BM25 is a lexical relevance function with term-frequency saturation, inverse-document frequency, and document-length normalization.

- **Q092 What is hybrid search?** Hybrid search combines complementary retrieval signals, often lexical matching and dense semantic similarity.

- **Q093 Why not just add BM25 and cosine scores?** Their scales and distributions differ, so raw addition can let one signal dominate for accidental numerical reasons.

- **Q094 What is reciprocal rank fusion?** RRF sums contributions such as 1 divided by c plus rank from each result list, combining ranks rather than raw scores.

- **Q095 What does a reranker do?** A reranker reorders a bounded candidate set with a more expensive relevance assessment.

- **Q096 How do a bi-encoder and cross-encoder differ?** A bi-encoder embeds texts separately for efficient retrieval; a cross-encoder scores the query and passage together.

- **Q097 What is MMR and why diversify results?** Maximal marginal relevance balances relevance to the question with dissimilarity from already selected results.

- **Q098 What are query rewriting and multi-query retrieval?** They reformulate a question or search several formulations to improve matching, while risking semantic drift and extra cost.

- **Q099 What is HyDE?** HyDE generates a hypothetical answer-like document, embeds it, and uses that representation to retrieve real corpus evidence.

- **Q100 What are multi-hop retrieval and contextual compression?** Multi-hop retrieval gathers linked evidence across steps, while compression reduces selected evidence to the parts useful for the question.

## 11 Generation context and answer verification

- **Q101 What should a grounded generation prompt contain?** It should contain the task, trust boundaries, labeled evidence, question, output requirements, and an explicit insufficient-evidence policy.

- **Q102 How should context be selected and ordered?** Choose authorized, relevant, nonredundant evidence within a token budget and evaluate how ordering affects answer quality.

- **Q103 How should a model gateway be designed?** A gateway should hide provider specifics while enforcing deadlines, budgets, retries, fallbacks, structured results, and observability.

- **Q104 When should the system abstain or ask for clarification?** Abstain when authorized evidence is insufficient; clarify when ambiguity can be resolved by a useful user answer.

- **Q105 How do you verify citations?** Check that cited IDs exist, were authorized and supplied, and support the associated claims under a defined evidence rubric.

- **Q106 How do you handle conflicting or stale evidence?** Use version and authority metadata, state unresolved conflicts, and avoid silently merging incompatible claims.

- **Q107 What are structured outputs and their limits?** A schema constrains output shape, but valid JSON can still contain unsupported or incorrect claims.

- **Q108 How should conversational history be used?** Use scoped history to resolve the current question, but retrieve fresh evidence and enforce tenant/user isolation on every turn.

- **Q109 How do streaming answers change the system?** Streaming improves time to first visible output but adds event contracts, cancellation, partial-failure, and verification tradeoffs.

- **Q110 How do you handle numbers and precise computations?** Retrieve relevant records and definitions, then use deterministic calculation or authorized structured queries for exact arithmetic.

## 12 Evaluation datasets and metrics

- **Q111 Why is test coverage not RAG quality?** Coverage measures executed code statements; RAG quality measures retrieval usefulness, supported answers, and task outcomes.

- **Q112 What belongs in a golden evaluation dataset?** Include representative questions, relevant evidence labels, answer criteria, tenant/version context, and unanswerable or adversarial cases.

- **Q113 What are precision at k and recall at k?** Precision@k is relevant retrieved items divided by k; Recall@k is retrieved relevant items divided by all labeled relevant items.

- **Q114 What are MRR and nDCG?** MRR rewards the rank of the first relevant result; nDCG rewards an entire graded ranking with position discounting.

- **Q115 What are answer faithfulness, relevance, correctness, and citation precision?** They respectively assess evidence support, responsiveness to the question, truth under the task, and support from cited sources.

- **Q116 How do you evaluate abstention?** Measure both unsupported answers on unanswerable cases and unnecessary refusals on answerable cases.

- **Q117 What are the risks of LLM-as-a-judge?** A model judge can scale evaluation but has biases, nondeterminism, prompt sensitivity, and imperfect agreement with humans.

- **Q118 How do you prevent evaluation leakage?** Keep a held-out test set and avoid tuning prompts, thresholds, or models repeatedly on its answers.

- **Q119 How do you run a fair RAG experiment?** Freeze unrelated variables, change one component, evaluate paired queries, inspect slices, and report quality plus latency/cost uncertainty.

- **Q120 How do offline evaluation and online monitoring complement each other?** Offline sets make changes comparable; online signals reveal real traffic, drift, latency, and user outcomes.

## 13 Security privacy and tenant boundaries

- **Q121 What is direct versus indirect prompt injection?** Direct injection appears in the user request; indirect injection arrives through external content such as retrieved documents.

- **Q122 What do our three query guardrail patterns cover?** They match selected ignore-previous-instructions, reveal-system/developer-prompt, and print-secret-style phrases.

- **Q123 What makes tenant isolation an end-to-end property?** Every storage, retrieval, reranking, generation, source-access, cache, and logging path must respect trusted tenant and user permissions.

- **Q124 How should API keys and provider secrets be handled?** Keep privileged secrets server-side, rotate and scope them, and never ship a provider credential in an Android APK.

- **Q125 What are PII redaction and data-minimization concerns?** Only send, store, log, and retain the data required for the authorized task, with explicit handling of sensitive fields.

- **Q126 How do ingestion attacks differ from prompt injection?** Uploads can exhaust resources or exploit parsers even without an LLM, while prompt injection targets model behavior.

- **Q127 What is retrieval poisoning?** An attacker adds or modifies corpus content to make malicious or misleading evidence rank for legitimate questions.

- **Q128 What if we add URL ingestion or model tools?** Add network and action boundaries such as SSRF protection, least-privilege tools, validated arguments, and explicit write policies.

- **Q129 How should generated content be rendered safely?** Treat model output and source labels as untrusted content, escaping HTML and validating links or actions before display.

- **Q130 What should a practical threat model cover?** Identify assets, actors, trust boundaries, abuse paths, controls, and residual risks across ingestion, retrieval, generation, and operations.

## 14 Persistence jobs and distributed correctness

- **Q131 Why is a Python list not a production database?** It lacks durable storage, shared multi-worker state, transactional constraints, query indexes, backups, and lifecycle management.

- **Q132 What schema would you propose for durable RAG storage?** Use tenant-scoped documents, versions, chunks, embeddings, and ingestion jobs with explicit keys, states, and lineage.

- **Q133 What are transactions and why do they matter?** A transaction groups database changes under defined atomicity and isolation so readers do not observe invalid partial state.

- **Q134 How should uniqueness and upserts support idempotency?** Enforce scoped uniqueness in the database and define repeat-request behavior atomically instead of relying on a preliminary read.

- **Q135 Why use background ingestion jobs?** Slow parsing, embedding, and indexing should run in bounded workers with durable status rather than hold HTTP requests open indefinitely.

- **Q136 What are at-least-once delivery and exactly-once effects?** A job may be delivered again after failure, so idempotent processing is needed to make repeated execution safe.

- **Q137 What is the transactional outbox pattern?** Write the business change and an event record in one database transaction, then publish the event asynchronously with deduplication.

- **Q138 What are eventual consistency and read-your-writes?** Eventual consistency allows propagation delay; read-your-writes means a client can observe its completed write under the promised contract.

- **Q139 How do locks, optimistic concurrency, and job leases help?** They coordinate competing updates through exclusive ownership, version checks, or time-bounded processing claims.

- **Q140 What do backups, recovery, and deletion guarantees require?** Define acceptable data loss and recovery time, test restoration, and account for originals, indexes, metadata, and retained backups.

## 15 Latency cost caching and resilience

- **Q141 How do you break down query latency?** Measure authentication, query processing, embedding, retrieval, reranking, context assembly, generation, and verification separately.

- **Q142 Why monitor p50, p95, and p99 rather than only averages?** Percentiles expose typical and tail latency, while averages can hide a small but painful set of slow requests.

- **Q143 How do you estimate RAG cost?** Include ingestion embeddings, query embeddings, reranking, input/output generation tokens, storage, compute, retries, and evaluation.

- **Q144 What can be cached and what belongs in cache keys?** Cache embeddings, authorized retrieval, or answers using keys that include tenant/ACL scope, corpus version, and relevant model/prompt settings.

- **Q145 What is semantic caching and what are its risks?** Semantic caching reuses answers for similar questions, but similarity does not prove identical intent, permissions, or required freshness.

- **Q146 How do timeouts, retries, backoff, and jitter work together?** Use per-stage and total deadlines, retry only suitable transient failures, and add bounded exponential backoff with jitter.

- **Q147 What are circuit breakers and bulkheads?** Circuit breakers stop repeated calls to a failing dependency; bulkheads isolate resource pools so one workload cannot consume everything.

- **Q148 What are rate limiting and backpressure?** Rate limiting controls admitted demand; backpressure prevents producers from overwhelming downstream capacity.

- **Q149 How do you estimate concurrency and capacity?** Under stable conditions, average in-flight work is roughly arrival rate times average time in the system.

- **Q150 How should you optimize a slow or expensive RAG system?** Profile first, then remove avoidable work while checking quality: duplicates, excess candidates, oversized context, repeated embeddings, and retry storms.

## 16 Deployment observability and development tooling

- **Q151 What do Docker and Compose provide here?** Docker packages the Python API runtime; Compose configures one local API container with port and runtime restrictions.

- **Q152 Why does reproducible dependency installation matter?** A lockfile keeps resolved dependencies consistent, but the installer must actually use it.

- **Q153 What are liveness, readiness, and startup probes?** Liveness detects a stuck process, readiness controls whether it receives traffic, and startup probes allow initialization time.

- **Q154 What logging have we built?** We configure structlog JSON output and log unhandled exceptions with the request path; request-level observability is still sparse.

- **Q155 How do logs, metrics, and traces differ?** Logs describe events, metrics aggregate measurements, and traces connect timed operations across a request path.

- **Q156 What are SLI, SLO, and error budget?** An SLI measures service behavior, an SLO sets its target, and an error budget is the permitted miss rate over a window.

- **Q157 What does CI currently enforce?** The workflow installs locked dev dependencies, runs Ruff checks, strict mypy, and pytest with an 85% coverage threshold.

- **Q158 What roles do pyproject, uv.lock, Ruff, mypy, and pytest play?** They define package/tool settings, resolved dependencies, lint/format rules, static type checks, and executable tests.

- **Q159 How would you deploy safely with multiple replicas?** Externalize durable state, use compatible versioned contracts, drain requests during rollout, and verify readiness and rollback behavior.

- **Q160 What are the first production-readiness priorities for this repository?** Fix chunk termination, define idempotency, establish real tenant identity, add durable storage, and build representative evaluations.

## 17 Testing and practical debugging

- **Q161 What do the existing eight tests actually verify?** They cover public liveness, missing query auth, one grounded upload/query, disallowed MIME, basic chunk bounds, overlap validation, and two guardrail examples.

- **Q162 How do unit, integration, end-to-end, load, and evaluation tests differ?** They validate isolated logic, component interactions, user flows, performance under traffic, and retrieval/answer quality respectively.

- **Q163 How would you test tenant isolation?** Use distinct verified identities, ingest unique evidence for each, and assert no cross-access through query, source retrieval, jobs, or cache.

- **Q164 How do you debug no answer despite an uploaded document?** Trace whether the document was accepted, parsed into chunks, stored in this worker, authorized, retrieved, and admitted by the gate.

- **Q165 How do you debug a wrong answer with apparently good retrieval?** Inspect exact passages, source versions, missing caveats, context selection, prompt behavior, and claim-to-citation support.

- **Q166 How do you debug duplicate answers or citations?** Check repeated document ingestion, overlap, duplicate corpus sources, and whether result diversification or deduplication is applied.

- **Q167 How do you debug slow requests?** Break latency into stages, check queueing and resource saturation, then reproduce the slow input under controlled conditions.

- **Q168 How would you test failure handling with fake adapters?** Inject repositories or generators that return controlled hits, delay, or raise errors, then assert the service’s intended response and cleanup.

- **Q169 What is an oracle-context test?** Give the generator known-correct evidence to determine whether answer failure persists when retrieval is ideal.

- **Q170 Which regression tests should be added first?** Prioritize chunk termination, duplicate-ingestion semantics, tenant boundaries, abstention edges, upload limits, and exact citation behavior.

## 18 Advanced RAG patterns and their tradeoffs

- **Q171 What is agentic RAG?** Agentic RAG lets a controller choose retrieval steps or tools dynamically rather than always execute one fixed search.

- **Q172 What is GraphRAG?** GraphRAG uses entity/relationship structure and sometimes community summaries to retrieve evidence for connected or corpus-wide questions.

- **Q173 What is RAPTOR?** RAPTOR recursively clusters and summarizes text into a retrieval tree spanning different levels of abstraction.

- **Q174 What are adaptive and self-correcting retrieval pipelines?** They use feedback signals to decide whether to retrieve again, change strategy, verify evidence, or abstain.

- **Q175 What is late-interaction retrieval?** It keeps multiple token-level representations and combines fine-grained query-document interactions instead of one vector per passage.

- **Q176 How does multimodal RAG differ?** It retrieves evidence from modalities such as images, tables, audio, or video while preserving modality-specific meaning and citations.

- **Q177 How do SQL and knowledge graphs complement text retrieval?** Structured queries answer exact filters and aggregations, while text retrieval supplies narrative evidence; routing should match the task.

- **Q178 What is retrieval-model fine-tuning and what data does it need?** It adapts a retriever using relevance supervision such as query-positive-negative examples, with held-out evaluation.

- **Q179 What are freshness-aware and time-aware retrieval?** They use version, effective-date, and publication metadata to retrieve evidence valid for the requested time and scope.

- **Q180 How do you choose whether an advanced pattern is worth adding?** Tie it to a specific observed error class, run a controlled evaluation, and include complexity, latency, cost, and operability.

## 19 System design interview scenarios

- **Q181 How would you design a company knowledge assistant?** Separate authorized ingestion from querying, persist versioned evidence, retrieve and rerank within scope, generate cited answers, and evaluate continuously.

- **Q182 How would you scale from one thousand to one million documents?** Estimate chunk count and workload, introduce durable indexed storage and workers, then scale measured bottlenecks with tenant-aware limits.

- **Q183 How would you meet a two-second response target?** Define whether the target is first token or completion, measure stage budgets, and trade off model/output size, reranking, retrieval, and caching.

- **Q184 How would you design for frequently changing policies?** Use versioned ingestion with explicit publication, effective-date metadata, and cache invalidation tied to document and permission changes.

- **Q185 How would you handle confidential multi-tenant data?** Derive trusted identity, enforce tenant/ACL constraints before external processing, scope every artifact/cache, and test revocation and isolation.

- **Q186 How would you work under a very small model budget?** Measure quality per unit cost, reduce avoidable context and calls, batch ingestion, cache safely, and use simpler retrieval/answers when sufficient.

- **Q187 How would you build an Android chat client for this backend?** Use Retrofit DTOs and a repository/use-case layer, model abstention and errors explicitly, and keep privileged model access on the backend.

- **Q188 How would you handle a model-provider outage?** Apply bounded timeouts and circuit breaking, preserve retrieval where possible, and return a clear degraded result or an approved fallback.

- **Q189 How would you investigate declining answer quality after deployment?** Compare versioned evaluation and traffic slices, inspect source freshness and retrieval traces, and isolate model, index, prompt, and corpus changes.

- **Q190 How would you answer an interviewer asking for your architecture in five minutes?** State requirements and current scope, walk ingestion and query paths, explain key tradeoffs, then discuss correctness gaps and the production plan.

## 20 Coding exercises behavioral answers and common traps

- **Q191 How would you implement cosine similarity in a coding round?** Tokenize into counters, compute shared-token dot product and norms, handle zero vectors, and test known scores.

- **Q192 How would you implement safe overlapping chunking?** Maintain a strict forward-progress invariant, enforce valid parameters, preserve normalized coverage, and use bounded boundary selection.

- **Q193 How would you implement idempotent ingestion?** Choose a tenant-scoped idempotency key and payload hash, enforce uniqueness atomically, and return the stored result for a valid retry.

- **Q194 How would you implement a retrieval evaluation loop?** Load versioned questions and relevance labels, retrieve deterministically, calculate metrics, save per-query results, and compare configurations.

- **Q195 What is an honest ninety-second project pitch?** I built an inspectable FastAPI RAG baseline with modular ingestion, lexical retrieval, extractive answers, citations, tests, and explicit production gaps.

- **Q196 How should you answer “Why did you choose this design?”** Explain the goal of a cheap reproducible baseline, the benefits of explicit boundaries, and the tradeoffs you intentionally accepted.

- **Q197 How should you answer “What was the hardest bug?”** Use the observed chunk-progress defect to explain symptom, root cause, invariant, proposed fix, and verification without claiming an unperformed fix.

- **Q198 Which claims should you avoid in interviews about this project?** Do not claim embeddings, a vector database, an LLM, durable multi-tenancy, verified citations, or production deployment are already implemented.

- **Q199 How do you answer an unfamiliar RAG design question?** Clarify the required behavior, state assumptions, reason from data and failure boundaries, and propose a measurable experiment.

- **Q200 What should you be able to demonstrate before the interview?** Explain the code, run upload/query and abstention examples, calculate similarity and metrics, diagnose failures, and sketch a measured production design.

## 21 Additional backend interview depth

- **Q201 How do JWT and OAuth differ?** JWT is a claims/token representation; OAuth is an authorization framework, and a signed JWT is not necessarily encrypted.

- **Q202 What is a connection pool?** A connection pool reuses a bounded set of database or HTTP connections instead of creating one for each operation.

- **Q203 What do database indexes and transaction isolation add?** Indexes accelerate selected access patterns; isolation defines what concurrent transactions can observe and modify safely.

- **Q204 What are ORMs, migrations, and SQL injection?** An ORM maps code to database operations, migrations version schema changes, and parameterized queries help prevent SQL injection.

- **Q205 Are FastAPI BackgroundTasks a durable job queue?** No; post-response in-process tasks do not provide durable distributed job ownership or recovery by themselves.

- **Q206 How should cancellation and structured concurrency work?** Tie related tasks to a request or job scope, propagate cancellation deliberately, and ensure cleanup without assuming completed side effects are undone.

- **Q207 What should you know about Python CPU work, the GIL, and GPUs?** Async is for cooperative concurrency; CPU/GPU execution requires deliberate runtime and worker design, not an async keyword.

- **Q208 How should pagination and payload limits be designed?** Bound list sizes and request/response sizes, and use stable pagination contracts for growing document collections.

- **Q209 How should exceptions be translated across layers?** Adapters expose meaningful internal failures, services apply business policy, and routes translate outcomes into stable public errors.

- **Q210 How do network partitions affect consistency and availability?** When systems cannot communicate, you must define which operations fail, wait, or serve possibly stale data while preserving required invariants.

## 22 Additional model and evaluation interview depth

- **Q211 What is attention in a transformer?** Attention computes context-dependent combinations of token representations so tokens can use information from other permitted positions.

- **Q212 How are retrieval embeddings trained with contrastive learning?** Training rewards relevant query-passage pairs relative to negative passages so compatible encoders learn useful retrieval geometry.

- **Q213 How do TF-IDF, BM25, sparse learned retrieval, and dense retrieval differ?** They use lexical weights, saturated probabilistic-style lexical scoring, learned sparse term weights, and learned dense vectors respectively.

- **Q214 What is SPLADE?** SPLADE learns sparse vocabulary-based representations with term expansion for first-stage retrieval.

- **Q215 What is Dense Passage Retrieval or DPR?** DPR uses compatible question and passage encoders to retrieve passages through dense-vector similarity.

- **Q216 What is ColBERT?** ColBERT uses contextual token representations and late interaction to score fine-grained query-passage matches efficiently.

- **Q217 What is LoRA and does it replace RAG?** LoRA trains low-rank parameter updates for adaptation; it does not replace retrieval of current authorized evidence.

- **Q218 How do candidate recall, context precision, and answer quality connect?** Retrieval must find useful evidence, packing must retain the right parts, and generation must use them correctly; failure at any stage can dominate.

- **Q219 What does calibrated confidence mean?** A calibrated confidence estimate matches observed correctness frequencies on a defined population; similarity scores are not automatically calibrated.

- **Q220 How do annotation quality and uncertainty affect evaluation?** Clear relevance rubrics, multiple reviewers where needed, disagreement review, and uncertainty reporting make metric conclusions more trustworthy.

## Final interview reminders

- Similarity is not confidence; code coverage is not answer accuracy.
- Authentication is not authorization; a tenant field is not complete isolation.
- Async is not CPU parallelism; a background callback is not a durable queue.
- Hashing is not encryption or complete idempotency.
- Citations are not proof of entailment; valid JSON is not factual correctness.
- More chunks, larger models, and advanced patterns are experiments, not automatic improvements.
- State assumptions, measure tradeoffs, and describe only work actually implemented.
