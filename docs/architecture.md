# Architecture

For the full explanation, read the [end-to-end guide](guide/README.md),
[system mind map](guide/01-mind-map.md), and [code atlas](guide/02-architecture-and-code.md).
The [gap assessment](guide/05-quality-and-gaps.md) qualifies the production-oriented
foundations described below: the API currently uses one shared tenant, storage is
volatile, and chunking has a known progress defect.

```text
Client -> FastAPI -> authentication/input policy -> RagService
                                                   |-> chunking -> ChunkRepository
                                                   |-> retrieval -> relevance gate
                                                   `-> Generator -> cited response
```

The domain uses ports so infrastructure is replaceable. The repository starts with a deterministic
in-memory lexical retriever and extractive generator: this keeps tests free of network calls and makes
failure behavior inspectable. Production milestones add a persistent vector adapter and hosted LLM
without coupling those SDKs to the API or domain.

## Trust boundaries

- API clients are untrusted: authenticate, cap request size, validate type and length.
- uploaded documents are untrusted data, never instructions; role-like labels are neutralized.
- retrieved context is admitted only above a relevance threshold.
- with the default positive threshold, answers without matching evidence abstain;
  admitted hits produce citations, but claim-level grounding is not verified.
- settings load from the environment or local `.env`; `.env` is Git-ignored.
  No secret-manager integration is implemented; the sample key is for development.
- chunks carry tenant identity and search filters by tenant, but current authentication
  maps every accepted API request to the same `default` tenant.

## Production target

```text
Load balancer -> stateless API -> Postgres/pgvector
                         |-----> object storage
                         |-----> Redis (rate limit/cache/jobs)
                         `-----> model gateway
Telemetry: OpenTelemetry -> traces/metrics/logs; evals gate releases in CI.
```

See `ROADMAP.md` for incremental, interview-friendly milestones.

