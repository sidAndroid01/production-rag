"""Create or update the Render web service and start a deploy.

    set -a; source secrets/deploy.env; set +a
    python3 scripts/deploy_render.py              # create the service, or update it and redeploy
    python3 scripts/deploy_render.py --status     # show the latest deploy and the URL

Reads RENDER_API_KEY, DATABASE_URL and RAG_METRICS_TOKEN from the environment
and the key list from secrets/api-keys.json. Values are sent to Render's API
and never printed. Render builds the public GitHub repo's Dockerfile; with
auto-deploy on, every push to main redeploys.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API = "https://api.render.com/v1"
NAME = "production-rag"
REPO = "https://github.com/sidAndroid01/production-rag"
# Neon's project is in AWS us-east-2 (Ohio); keeping the app there saves a round trip per query.
REGION = "ohio"
KEYS_FILE = Path(__file__).resolve().parent.parent / "secrets" / "api-keys.json"

# A 512 MB / 0.1 CPU instance: embed 4 passages at a time on one thread.
# Measured peak with a 575 KB PDF: ~310 MB. The vectors are the same as with larger batches.
SETTINGS = {
    "APP_ENV": "production",
    "PORT": "8000",
    "RAG_DB_POOL_MAX": "5",
    "RAG_RATE_LIMIT_PER_MINUTE": "30",
    "RAG_ALLOW_CUSTOM_LLM_URL": "1",
    "RAG_EMBED_BATCH_SIZE": "4",
    "RAG_EMBED_THREADS": "1",
}
SECRETS = ("DATABASE_URL", "RAG_METRICS_TOKEN")


def call(method: str, path: str, body: Any = None) -> Any:
    request = urllib.request.Request(
        API + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['RENDER_API_KEY']}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "production-rag-deploy/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        sys.exit(f"Render API {method} {path}: HTTP {exc.code} {exc.read().decode()[:300]}")
    return json.loads(raw) if raw else None


def environment() -> list[dict[str, str]]:
    missing = [name for name in ("RENDER_API_KEY", *SECRETS) if not os.environ.get(name)]
    if missing:
        sys.exit(f"missing from the environment: {', '.join(missing)} (see secrets/deploy.env)")
    if not KEYS_FILE.exists():
        sys.exit(f"{KEYS_FILE} not found; create keys with scripts/create_api_key.py --keys-file")
    keys = json.dumps(json.loads(KEYS_FILE.read_text()), separators=(",", ":"))
    values = {**SETTINGS, **{name: os.environ[name] for name in SECRETS}, "RAG_API_KEYS_JSON": keys}
    return [{"key": key, "value": value} for key, value in values.items()]


def find_service() -> dict[str, Any] | None:
    for item in call("GET", f"/services?name={NAME}&limit=20"):
        if item["service"]["name"] == NAME:
            return dict(item["service"])
    return None


def show_status(service: dict[str, Any]) -> None:
    deploys = call("GET", f"/services/{service['id']}/deploys?limit=1")
    status = deploys[0]["deploy"]["status"] if deploys else "no deploys yet"
    print(f"service {service['id']}: latest deploy {status}")
    print(f"URL: {service['serviceDetails'].get('url', '(assigned after the first deploy)')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--status", action="store_true", help="only show the latest deploy")
    args = parser.parse_args()
    if args.status:
        if not os.environ.get("RENDER_API_KEY"):
            sys.exit("missing from the environment: RENDER_API_KEY")
        service = find_service()
        if service is None:
            sys.exit(f"no service named {NAME}")
        show_status(service)
        return

    env_vars = environment()
    service = find_service()
    if service is None:
        owners = call("GET", "/owners?limit=20")
        if not owners:
            sys.exit("this Render API key has no workspace")
        service = call(
            "POST",
            "/services",
            {
                "type": "web_service",
                "name": NAME,
                "ownerId": owners[0]["owner"]["id"],
                "repo": REPO,
                "branch": "main",
                "autoDeploy": "yes",
                "envVars": env_vars,
                "serviceDetails": {
                    "runtime": "docker",
                    "plan": "free",
                    "region": REGION,
                    "healthCheckPath": "/health/ready",
                    "envSpecificDetails": {"dockerfilePath": "./Dockerfile", "dockerContext": "."},
                },
            },
        )["service"]
        print(f"created {NAME}; the first build takes about 5-10 minutes")
    else:
        call("PUT", f"/services/{service['id']}/env-vars", env_vars)
        call("POST", f"/services/{service['id']}/deploys", {})
        print(f"updated {len(env_vars)} settings on {NAME} and started a deploy")
    show_status(service)


if __name__ == "__main__":
    main()
