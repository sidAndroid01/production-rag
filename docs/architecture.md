# Architecture

The phase-by-phase explanation lives in the top-level [README](../README.md).
This page is the one-screen map. (`legacy/docs/guide/` describes the archived
FastAPI scaffold, not this application.)

```text
client ──x-api-key──▶ api.py  (route table, request ID, JSON logs, metrics, rate limit)
                        │ auth.py: key digest → Principal(tenant, user, groups)
          ┌─────────────┼────────────────────────────┐
          ▼             ▼                            ▼
  upload / job     query / chat                 documents CRUD
  parsers.py       query.py validate            persistence.py / memory_store.py
  (sniff, sandbox) providers.rewrite_query       (versions, ACL, RLS)
  rag.py chunks    retrieve:
  embeddings.py      pgvector + full-text (SQL ACL, ready versions only)
  persist            → reranker.py (scores in [0,1]) → evidence gates
  workers.py       gateway.py: budgets, retries, breaker, fallback, usage
  (Postgres queue) providers.py: sources as data, cited-only citations
                        │
                        ▼
              PostgreSQL + pgvector (forced RLS per tenant)
              documents · chunks · ingestion_jobs · chat_messages
```

## Trust boundaries

| Boundary | Control |
| --- | --- |
| Client → API | API key hashed and mapped server-side; body identity ignored or must match; body size, JSON shape, and query length limits; per-user rate limit |
| API → database | restricted `rag_app` role (no DDL, no RLS bypass); tenant set per transaction; ACL applied in SQL before `LIMIT` |
| Upload → parser | type sniffed from bytes; PDF/HTML parsed in a resource-limited child process |
| Documents → model | sources sent as delimited data in the user turn, never the system prompt; only cited sources are returned; uncited answers abstain |
| API → model provider | timeouts, retries, circuit breaker, deadline, token budgets; extractive fallback instead of a 500 |

## Where to look next

- Schema: [`migrations/`](../migrations/) (applied by [`migrate.py`](../migrate.py))
- Quality numbers and thresholds: [`evals/`](../evals/) and README Phase 15
- Deployment: [`compose.yaml`](../compose.yaml), [`Dockerfile`](../Dockerfile), [`.env.example`](../.env.example)
- Remaining work: [`ROADMAP.md`](../ROADMAP.md)
