# Build roadmap

Status of the phase-built application. Each checked item is implemented and
tested; see the matching README phase for details and limits.

## 1. Reliable foundation

- [x] authenticated API with server-side identity (API key → tenant, user, groups) — Phases 3, 13
- [x] bounded UTF-8 ingestion and deterministic, sentence-aware chunking — Phases 1, 9
- [x] prompt-injection screening, evidence gate, abstention, citations — Phases 2, 14
- [x] one canonical app: migrations, lockfile, CI against PostgreSQL, hardened container — Phase 11

## 2. Retrieval that earns its benchmark

- [x] PostgreSQL + pgvector with numbered migrations and forced row-level security — Phases 4, 8, 11
- [x] local embedding adapter with batching and dimension/model checks — Phase 6
- [x] hybrid vector + full-text search, rank fusion, reranking, group ACLs in SQL — Phases 7, 13
- [x] durable background ingestion jobs with retries and crash recovery — Phase 17
- [x] PDF/HTML/Markdown parsing with content sniffing and a sandboxed parser — Phase 17
- [ ] malware scanning and object storage for original uploads
- [ ] embedding retries/backfill command for a model change

## 3. Generation and safety

- [x] model gateway: timeouts, retries, circuit breaker, fallback, token budgets — Phase 16
- [x] sources isolated from the system prompt; only cited sources returned — Phase 14
- [x] per-user rate limits and per-tenant daily token budgets (in-process) — Phase 18
- [ ] per-claim citation entailment verification
- [ ] PII detection/redaction, moderation, and a configurable policy engine
- [ ] Redis-backed limits and a semantic cache for multi-replica deployments

## 4. Evidence of quality

- [x] versioned golden dataset with paraphrased and unanswerable questions — Phase 15
- [x] retrieval metrics: Recall@k, MRR, nDCG — Phase 15
- [x] answer metrics: answer rate, abstention, citation precision; CI regression gate — Phase 15
- [x] structured logs, request IDs, Prometheus metrics — Phase 18
- [ ] LLM-judged faithfulness and an adversarial prompt-injection suite
- [ ] OpenTelemetry traces and a Grafana dashboard

## 5. Portfolio polish

- [ ] minimal chat UI and document-management page (the API already allows CORS origins)
- [ ] cloud deployment with IaC, autoscaling, backups, and a threat model
- [ ] load-test report, architecture decision records, and a two-minute demo video
