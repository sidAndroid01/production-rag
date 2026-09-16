# System mind map

[Guide home](README.md) · Next: [Architecture and code atlas](02-architecture-and-code.md)

## Current implementation

![Current Production RAG mind map: a central topic with eight colored branches and detailed child nodes.](assets/current-system-mind-map.png)

[Open full-size PNG](assets/current-system-mind-map.png) · [Open scalable SVG](assets/current-system-mind-map.svg)

<details>
<summary>Editable Mermaid source</summary>

```mermaid
mindmap
  root((Production RAG baseline))
    Interface
      FastAPI
      Public health routes
      POST documents
      POST query
      Shared API key
      Default tenant
    Application
      RagService
      Ingestion orchestration
      Query orchestration
      Relevance threshold
      Response and citations
    Ingestion
      Upload size check
      Declared MIME allowlist
      Strict UTF8 decode
      Neutralize role labels
      Hash original bytes
      Normalize whitespace
      Character chunks
      Fresh chunk UUIDs
    Retrieval
      In memory list
      Tenant filter
      ASCII term counts
      Cosine similarity
      Sort descending
      Top k
    Answer
      Query regex screening
      Filter by score
      Extract up to three chunks
      Fixed abstention
      Citation excerpts
      Request ID
    Engineering
      Protocol interfaces
      Pydantic models
      Cached settings
      Logging configuration
      Eight tests
      Ruff and mypy
      CI coverage gate
      Non root container
    Limitations
      Volatile storage
      Single API tenant
      Duplicate reuploads
      Chunk progress defect
      No embeddings or LLM
      No quality benchmark
```

</details>

<details>
<summary>Plain-text alternative</summary>

```text
Production RAG — current baseline
├── Client interface
│   ├── /docs and /openapi.json: interactive docs and schema
│   ├── /health/live and /health/ready: public constant responses
│   ├── POST /v1/documents: multipart text upload
│   └── POST /v1/query: JSON question
├── Request boundary
│   ├── x-api-key compared with configured API_KEY
│   ├── Successful authentication → tenant_id = "default"
│   ├── Upload: size + declared MIME type + UTF-8 checks
│   └── Query: schema + stripped length + regex screening
├── RagService
│   ├── ingest
│   │   ├── Sanitize document role labels
│   │   ├── SHA-256 original bytes → document ID
│   │   ├── TextChunker → text pieces
│   │   └── ChunkRepository.add → process-local list
│   └── query
│       ├── ChunkRepository.search → tenant-filtered lexical top-k
│       ├── Keep hits scoring at least MIN_RELEVANCE_SCORE
│       ├── Generator.generate → up to three source texts or abstention
│       └── QueryResponse → answer, citations, request_id, grounded
├── Replaceable contracts
│   ├── ChunkRepository → InMemoryChunkRepository
│   └── Generator → ExtractiveGenerator
├── Supporting engineering
│   ├── Settings from environment/.env
│   ├── JSON log formatting and generic 500 response
│   ├── pytest + coverage + Ruff + mypy
│   └── Dockerfile + Compose + GitHub Actions workflow
└── Planned production layers, not running today
    ├── Persistent tenant-authorized database and migrations
    ├── Embeddings, hybrid retrieval, reranking
    ├── Queued ingestion, object storage, richer parsers
    ├── Model gateway, answer verification, stronger safety
    └── Evaluations, telemetry, rate limits, deployment and backups
```

</details>

## Current data flow

```mermaid
flowchart TD
    Client[Client] --> Auth[Shared API key authentication]
    Auth --> Tenant[tenant_id is default]
    Tenant --> Upload[Upload endpoint]
    Tenant --> Query[Query endpoint]
    Upload --> Validate[Size and declared MIME checks]
    Validate --> Decode[Strict UTF-8 decode]
    Decode --> Sanitize[Replace document role labels]
    Sanitize --> Chunk[Normalize and split text]
    Chunk --> Memory[(Process-local chunks)]
    Upload -. Original bytes .-> Hash[SHA-256 document identity]
    Hash -. Metadata .-> Chunk
    Query --> Guard[Query length and regex checks]
    Guard --> Search[Tenant-filtered lexical cosine search]
    Memory --> Search
    Search --> TopK[Highest scoring k hits]
    TopK --> Gate[Minimum relevance score]
    Gate --> Generate[Extract up to three chunks or abstain]
    Gate --> Cite[Citations for every admitted hit]
    Generate --> Response[QueryResponse]
    Cite --> Response
```

The memory store is shared by upload and query requests handled by the same application instance. It is not shared across independent workers or replicas. There is no external database hidden behind the store.

## Target architecture: planned only

![Planned Production RAG mind map: eight branches covering future storage, ingestion, retrieval, generation, safety, evaluation, observability, and delivery.](assets/planned-system-mind-map.png)

[Open full-size PNG](assets/planned-system-mind-map.png) · [Open scalable SVG](assets/planned-system-mind-map.svg)

```mermaid
flowchart LR
    Client[Client] --> API[API with authenticated tenant identity]
    API --> Jobs[Ingestion queue and workers]
    Jobs --> Objects[(Object storage)]
    Jobs --> Parse[Isolated parsing and chunking]
    Parse --> Embed[Embedding provider]
    Embed --> DB[(Postgres and pgvector)]
    API --> Retrieve[Hybrid retrieval and reranking]
    Retrieve --> DB
    Retrieve --> Gateway[Model gateway]
    Gateway --> Verify[Answer and citation verification]
    Verify --> API
    API --> Redis[(Rate limits and cache)]
    API -.-> Telemetry[Traces metrics and logs]
    Evals[Golden evaluation suite] -. Release gates .-> API
```

This is a direction derived from the roadmap, not a description of deployed services. The repository does not yet define those components or their detailed orchestration.
