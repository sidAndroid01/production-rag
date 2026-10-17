# Deploy for free: Render + Neon (+ users' own LLM keys)

The result is a public HTTPS URL such as `https://production-rag-xxxx.onrender.com`
with the same console you use locally, every feature on. No credit card is needed.

```text
Browser ──x-api-key + (their LLM key)──▶ Render free web service (UI + API + worker)
                                               ├──▶ Neon           PostgreSQL + pgvector
                                               └──▶ their provider  e.g. Groq, with their key
```

| Piece | Free tier (check each site for current limits) | Notes |
| --- | --- | --- |
| Render web service (Docker) | 512 MB RAM, 0.1 CPU | Sleeps after 15 minutes without visits; the next visit takes about a minute while it wakes |
| Neon | 0.5 GB storage, 100 CU-hours a month | A 575 KB PDF takes about 0.3 MB; compute pauses when idle |
| Users' LLM providers | Each user's own account | Without a key, answers are cited quotes |

Measured under the same limits (512 MB, 0.1 CPU, Docker) against Neon: the app is
ready in ~6 s, a 575 KB PDF (27 passages) uploads in ~100 s, questions take ~2 s,
and memory peaks at ~310 MB. Uploads are slow because of the 0.1 CPU; nothing is
turned off to fit.

**The one setting that differs from localhost:** `RAG_EMBED_BATCH_SIZE=4` and
`RAG_EMBED_THREADS=1`, so the embedding model works on 4 passages at a time instead
of 32. The vectors are the same (cosine similarity 0.99999 against the default), so
search and answers do not change; only uploads take longer. With the defaults, the
model needs ~700 MB and a 512 MB instance is killed.

Before showing it to someone, open the URL a minute early so it is awake.

## 1. Database: Neon

Follow steps 1 and 3–4 of the Hugging Face guide's
[Neon section](deploy-huggingface.md#1-database-neon): create the project, run
`migrate.py` with the owner connection, and build the `rag_app` connection string.
Then create keys as in [step 3 there](deploy-huggingface.md#3-api-keys-for-you-and-your-users)
with `--keys-file secrets/api-keys.json`.

## 2. Render

1. Sign up at <https://render.com> with **Sign in with GitHub**. Stay on the free plan.
2. **Account Settings → API Keys → Create API Key.**
3. Put the values in `secrets/deploy.env` (the `secrets/` folder is Git-ignored):

   ```bash
   RENDER_API_KEY='rnd_...'
   DATABASE_URL='postgresql://rag_app:...@ep-....neon.tech/neondb?sslmode=require'
   RAG_METRICS_TOKEN='<any long random string>'   # protects /metrics
   ```

## 3. Deploy

```bash
set -a; source secrets/deploy.env; set +a
python3 scripts/deploy_render.py            # creates the service with all settings
python3 scripts/deploy_render.py --status   # build status and the URL
```

The script creates a free Docker web service named `production-rag` in Ohio (next
to Neon's us-east-2), builds this repository's `Dockerfile` from `main`, and sets
the settings, `DATABASE_URL`, `RAG_API_KEYS_JSON` (from `secrets/api-keys.json`)
and `RAG_METRICS_TOKEN`. It prints no values. The first build takes 5–10 minutes;
follow it under the service's **Logs** in the Render dashboard.

When the status is `live`, open the URL, paste your key, and click **Connect**.

- **New code:** auto-deploy is on, so every push to `main` redeploys.
- **Users added or removed** in `secrets/api-keys.json`, or a changed secret: run
  `deploy_render.py` again; it replaces the settings and redeploys.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Deploy fails with "Out of memory" | `RAG_EMBED_BATCH_SIZE`/`RAG_EMBED_THREADS` were not applied; run the script again |
| `/health/ready` returns 503 | `DATABASE_URL` is wrong or uses Neon's pooled host; use the direct one with `?sslmode=require` |
| "That API key was not accepted" | The key is not in `secrets/api-keys.json`, or the script was not rerun after adding it |
| First visit takes a minute | The service was asleep; later requests are fast |
| Upload takes a few minutes | Expected on 0.1 CPU; large files run as background jobs, so the page stays usable |
