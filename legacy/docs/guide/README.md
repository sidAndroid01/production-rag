# Understanding Production RAG end to end

This guide describes the repository as inspected on **5 September 2026**. Its source of truth is the Python implementation, configuration, and tests. Future features are explicitly marked as planned. No external provider or deployment is needed to understand the current behavior.

## What have we built?

We have built a small, working HTTP backend that accepts text documents, divides them into searchable pieces, finds pieces matching a question, and returns their text with source metadata. It runs locally without paid APIs.

The architectural foundation is modular: HTTP handling, application orchestration, domain contracts, and infrastructure implementations are separated. The current infrastructure is an in-memory lexical search implementation and an extractive answer generator. **There is no embedding model, persistent database, hosted LLM, or background ingestion worker in the running pipeline.**

“Production RAG” is the project direction. The current state is an inspectable baseline with some production-oriented foundations, not a completed production service.

## Reading path

| Read | File | What you will understand |
| --- | --- | --- |
| 1 | [System mind map](01-mind-map.md) | The whole project and how its parts connect |
| 2 | [Architecture and code atlas](02-architecture-and-code.md) | Every source module, data model, dependency, and startup step |
| 3 | [Pipeline walkthrough](03-pipeline-walkthrough.md) | Upload → chunks → storage → search → answer, including exact calculations |
| 4 | [API and local operation](04-api-and-operations.md) | How to run it, call endpoints, configure it, and diagnose behavior |
| 5 | [Quality, limitations, and next steps](05-quality-and-gaps.md) | What is tested, what is incomplete, known defects, and a practical build order |

Start with the mind map, follow the handbook example in the walkthrough, then read its corresponding source files. The mind maps are embedded PNG images that display without Mermaid support, with scalable SVG versions available for zooming. Editable Mermaid source and a plain-text alternative are also provided.

## Current implementation at a glance

| Capability | Actual state |
| --- | --- |
| HTTP API | FastAPI with upload, query, liveness, and readiness routes |
| Authentication | One configured API key; every successful request receives tenant `default` |
| Document inputs | UTF-8 content with an accepted declared MIME type; no PDF/HTML parsing |
| Chunking | Whitespace normalization, character windows, intended overlap |
| Document identity | First 24 hexadecimal characters of SHA-256 of original bytes |
| Storage | Python list in one application process; lost on restart |
| Retrieval | Cosine similarity over lowercase ASCII alphanumeric term counts |
| Relevance policy | Top-k search followed by a minimum-score filter |
| Answer generation | Concatenates up to three retrieved chunks; no LLM |
| Citations | Metadata and excerpts for every result passing the filter |
| Abstention | Fixed message when no result passes the filter |
| Safety | Query length checks, three query regexes, document role-label replacement |
| Operations | JSON logging configuration, constant health responses, container and CI configuration |
| Verification | 8 existing tests pass; 94.76% statement coverage in the inspected environment |

The service/repository boundary carries tenant IDs, but the API does not yet provide distinct tenant identities. Content-addressed document IDs also do not make ingestion idempotent: repeated uploads get fresh chunk UUIDs and are stored again.

## Vocabulary used throughout

| Term | Meaning here |
| --- | --- |
| RAG | Retrieval-augmented generation: retrieve external evidence before producing an answer |
| Ingestion | Turn uploaded bytes into stored searchable chunks |
| Chunk | One piece of normalized document text plus identity and source metadata |
| Corpus | All chunks stored in this process |
| Retrieval | Rank chunks against the question |
| Lexical search | Match words/tokens rather than learned semantic representations |
| Embedding | A learned numeric representation of text; not currently implemented |
| Vector database | Storage/search infrastructure for vectors; not currently connected |
| Context | The retrieved hits supplied to the generator |
| Grounding | Here, the existence of at least one threshold-passing hit; not proof of truth |
| Citation | Document ID, source filename, chunk index, score, and excerpt |
| Tenant | An intended customer or organization scope; currently always `default` through HTTP |
| Port | A Python `Protocol` describing what a replaceable component must support |
| Adapter | A concrete implementation of a port |
| Reranking | A second relevance-ranking stage; planned, not implemented |
| Evaluation | Measuring retrieval/answer quality against examples; the dataset and runner are still absent |

## Verification boundaries

The guide is backed by source inspection, the existing test suite, lint/format/type checks, and small in-process API/repository experiments. Docker deployment, a remote CI run, load testing, and an evaluation benchmark were not executed. An empty `evals/datasets/` directory is scaffolding, not an evaluation system.

For the original project overview see [README](../../README.md); for planned milestones see [ROADMAP](../../ROADMAP.md).
