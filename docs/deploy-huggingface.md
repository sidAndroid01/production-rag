# Deploy for free: Hugging Face Spaces + Neon + Groq

The result is a public HTTPS URL such as `https://<you>-production-rag.hf.space`,
with the same console you use locally. No credit card is needed for any of the
three services.

```text
Browser ──x-api-key──▶ Hugging Face Space (this Docker image: UI + API + worker)
                              ├──▶ Neon        PostgreSQL + pgvector (your data)
                              └──▶ Groq        OpenAI-compatible LLM (writes answers)
```

| Piece | Free tier (check each site for current limits) | Notes |
| --- | --- | --- |
| Hugging Face Space (Docker) | 2 vCPU, 16 GB RAM | Sleeps after a period without visits; the first request then takes ~30 s while it wakes |
| Neon | ~0.5 GB storage, compute pauses when idle | First query after a pause takes ~1 s |
| Groq | Rate-limited requests per minute and per day | Shared by every user of your Space |

Time needed: about 30 minutes.

## 1. Database: Neon

1. Sign up at <https://neon.tech> and create a project (pick the region closest to you).
2. On the dashboard, open **Connect**, turn **off** "Connection pooling", and copy the
   connection string. It looks like
   `postgresql://neondb_owner:...@ep-xxxx.<region>.aws.neon.tech/neondb?sslmode=require`.
   This is the **owner** connection: it is only for creating the schema, from your
   laptop. The app never gets it.
3. From this repository on your laptop, create the schema and the restricted app role:

   ```bash
   export RAG_OWNER_DATABASE_URL='<the owner connection string>'
   export RAG_APP_DB_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
   uv run python migrate.py          # → applied 6 migration(s) …
   echo "$RAG_APP_DB_PASSWORD"       # keep this for step 4
   ```

4. Build the app's connection string by swapping the user and password in the owner
   string. Keep host, database name and `?sslmode=require`:

   ```text
   postgresql://rag_app:<RAG_APP_DB_PASSWORD>@ep-xxxx.<region>.aws.neon.tech/neondb?sslmode=require
   ```

   This is your `DATABASE_URL`. The `rag_app` role cannot change the schema or bypass
   tenant isolation.

Run `migrate.py` the same way after pulling new code that adds migrations.

## 2. LLM: Groq

1. Sign up at <https://console.groq.com>, open **API Keys**, and create a key.
2. Note a model ID from <https://console.groq.com/docs/models>, for example
   `llama-3.1-8b-instant` (fast) or a larger Llama model (better answers, lower limits).

Passages from your users' documents are sent to Groq to write answers.

## 3. API keys for you and your users

Each person gets their own key. The `--tenant` decides whose documents they see:
different tenants are fully separate; people in the same tenant share documents,
and `--groups` restricts documents within it.

```bash
uv run python scripts/create_api_key.py --tenant me    --user siddhant --keys-file secrets/api-keys.json
uv run python scripts/create_api_key.py --tenant alice --user alice    --keys-file secrets/api-keys.json
```

Each run prints the new key **once** (send it to that person privately) and, on the
last line, the complete key list as one line of JSON. That line is the value of
`RAG_API_KEYS_JSON`. `secrets/` is Git-ignored, and the file stores only hashes of
the keys.

## 4. The Space

1. Sign up at <https://huggingface.co>, then **New Space**: any name (for example
   `production-rag`), SDK **Docker**, template **Blank**. Choose **Private** if only
   you should be able to open the page at all; with **Public**, anyone can load the
   page but can do nothing without a key.
2. In the Space's **Settings → Variables and secrets**, add:

   | Type | Name | Value |
   | --- | --- | --- |
   | Secret | `DATABASE_URL` | the `rag_app` connection string from step 1.4 |
   | Secret | `RAG_API_KEYS_JSON` | the JSON line from step 3 |
   | Secret | `RAG_MODEL_API_KEY` | your Groq key |
   | Secret | `RAG_METRICS_TOKEN` | any long random string (protects `/metrics`) |
   | Variable | `APP_ENV` | `production` |
   | Variable | `RAG_MODEL_BASE_URL` | `https://api.groq.com/openai/v1` |
   | Variable | `RAG_MODEL_NAME` | the Groq model ID from step 2 |
   | Variable | `RAG_DB_POOL_MAX` | `5` |
   | Variable | `RAG_RATE_LIMIT_PER_MINUTE` | `30` |
   | Variable | `RAG_TENANT_DAILY_TOKEN_BUDGET` | `200000` (optional cap per tenant per day) |

3. Create a Hugging Face **access token** with write permission
   (**Settings → Access Tokens**).

## 5. Deploy

```bash
export HF_TOKEN=hf_...                                   # the write token
DRY_RUN=1 scripts/deploy_hf_space.sh <hf-username>/production-rag   # optional: list what is sent
scripts/deploy_hf_space.sh <hf-username>/production-rag
```

The script sends only files committed to Git (never `.env` or `secrets/`). The Space
then builds the Docker image, which takes a few minutes the first time (follow the
**Logs** tab). When it shows **Running**, open
`https://<hf-username>-production-rag.hf.space`, paste your key, and click **Connect**.
The top bar should read `postgres · model: <groq model> → extractive`.

To deploy new code: commit it, run the script again.

## Managing users

- **Add a user:** run `create_api_key.py` with `--keys-file secrets/api-keys.json`,
  paste the new JSON line into the `RAG_API_KEYS_JSON` secret (the Space restarts),
  and send the key privately.
- **Remove a user:** delete their entry from `secrets/api-keys.json` and update the
  secret. Their documents stay in their tenant until you delete them.
- **Lost key:** keys cannot be recovered (only hashes are stored); issue a new one.

Users cannot sign up by themselves or bring their own LLM key yet; every answer
uses your Groq key and counts toward your Groq limits. The per-user rate limit and
the daily token budget per tenant protect those limits.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Build fails | Read the Space's **Logs → Build**; usually a network error, so rebuild |
| Page loads, "That API key was not accepted" | Key not in `RAG_API_KEYS_JSON`, or the secret has a typo; check the Space restarted after you saved it |
| `/health/ready` returns 503 "database is unavailable" | `DATABASE_URL` is wrong, or you used the pooled endpoint; use the direct one with `?sslmode=require` |
| Answers marked "fallback answer" | Groq rejected the call: wrong key or model ID, or a rate limit. The Logs show `provider … failed` with the reason |
| First request very slow | The Space or Neon was asleep; later requests are fast |
| Upload fails with "has no text" | Scanned PDFs have no text layer (no OCR yet) |
