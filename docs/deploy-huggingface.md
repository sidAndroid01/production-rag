# Deploy for free: Hugging Face Spaces + Neon (+ users' own LLM keys)

The result is a public HTTPS URL such as `https://<you>-production-rag.hf.space`,
with the same console you use locally. No credit card is needed.

**You do not pay for anyone's LLM usage.** Each person enters their own provider
key (Groq, OpenAI, OpenRouter, Gemini, Together, Mistral, or a custom
OpenAI-compatible URL) in the console under **AI: Built-in → My own AI account**. The key stays in their
browser and is used only for their own questions. People without a key still get
cited answers, written as quotes from the documents.

```text
Browser ──x-api-key + (their LLM key)──▶ Hugging Face Space (UI + API + worker)
                                               ├──▶ Neon           PostgreSQL + pgvector
                                               └──▶ their provider  e.g. Groq, with their key
```

| Piece | Free tier (check each site for current limits) | Notes |
| --- | --- | --- |
| Hugging Face Space (Docker) | 2 vCPU, 16 GB RAM | Sleeps after a period without visits; the first request then takes ~30 s while it wakes |
| Neon | ~0.5 GB storage, compute pauses when idle | First query after a pause takes ~1 s |
| Users' LLM providers | Each user's own account and limits | Groq and Gemini have free tiers; users create their own keys |

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

## 2. LLM: nothing to set up on the server

Leave `RAG_MODEL_*` unset. Each user brings their own key in the console:

1. They click **AI: Built-in** above the chat, choose **My own AI account**, pick a
   provider, and paste their key. **(get one)** links to that provider's key page
   (Groq's free tier is a good default: <https://console.groq.com/keys>).
2. **Save** first makes one tiny call with the key and switches to it only if it
   works; otherwise it says why (wrong key, unknown model, rate limit).
3. The key is kept in this browser tab only, or on this device when "Remember my
   key on this device" is ticked. Choosing **Built-in** and **Save** removes it.

What happens to the key: it is sent with each question over HTTPS, used for that
one request, and never stored, logged, or returned by the server. Passages from
the user's documents go to *their* provider under *their* account.

Optional: if you do set `RAG_MODEL_*` (your own key), users without a key get
your model instead of quotes, and `RAG_TENANT_DAILY_TOKEN_BUDGET` caps that
spending per tenant. Users' own keys never count against it.

Custom URLs (for example a self-hosted Ollama behind HTTPS) must be public
`https` addresses; private and internal addresses are refused, so the server
cannot be used to reach its own network. Set `RAG_ALLOW_CUSTOM_LLM_URL=0` to
offer only the listed providers.

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
   | Secret | `RAG_METRICS_TOKEN` | any long random string (protects `/metrics`) |
   | Variable | `APP_ENV` | `production` |
   | Variable | `RAG_DB_POOL_MAX` | `5` |
   | Variable | `RAG_RATE_LIMIT_PER_MINUTE` | `30` |
   | Variable | `RAG_ALLOW_CUSTOM_LLM_URL` | `1` to allow custom public https LLM URLs, `0` to allow only the listed providers |

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
The top bar should read `Tenant … · user … · saved in database`. Click **AI: Built-in**,
choose **My own AI account**, pick your provider, paste your key, and **Save**.

To deploy new code: commit it, run the script again.

## Managing users

- **Add a user:** run `create_api_key.py` with `--keys-file secrets/api-keys.json`,
  paste the new JSON line into the `RAG_API_KEYS_JSON` secret (the Space restarts),
  and send the key privately.
- **Remove a user:** delete their entry from `secrets/api-keys.json` and update the
  secret. Their documents stay in their tenant until you delete them.
- **Lost key:** keys cannot be recovered (only hashes are stored); issue a new one.

Users cannot sign up by themselves yet (you issue their app keys), but each brings
their own LLM key, so their usage is billed to their own provider account.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Build fails | Read the Space's **Logs → Build**; usually a network error, so rebuild |
| Page loads, "That API key was not accepted" | Key not in `RAG_API_KEYS_JSON`, or the secret has a typo; check the Space restarted after you saved it |
| `/health/ready` returns 503 "database is unavailable" | `DATABASE_URL` is wrong, or you used the pooled endpoint; use the direct one with `?sslmode=require` |
| Answers marked "fallback answer" | The user's provider rejected the call; the reason is shown under the answer (wrong key, wrong model ID, or the provider's rate limit). **Save** in the AI panel re-checks the settings |
| First request very slow | The Space or Neon was asleep; later requests are fast |
| Upload fails with "has no text" | Scanned PDFs have no text layer (no OCR yet) |
