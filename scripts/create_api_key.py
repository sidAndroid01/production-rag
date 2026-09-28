"""Generate an API key and the JSON entry to add to RAG_API_KEYS_FILE.

    python scripts/create_api_key.py --tenant acme --user alice --groups hr,finance

The key is printed once; only its SHA-256 digest belongs in the key file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--groups", default="", help="comma-separated group names")
    parser.add_argument("--name", default="", help="label for this key")
    args = parser.parse_args()
    key = "rag_" + secrets.token_urlsafe(32)
    entry = {
        "name": args.name or f"{args.tenant}/{args.user}",
        "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
        "tenant_id": args.tenant,
        "user_id": args.user,
        "groups": [group.strip() for group in args.groups.split(",") if group.strip()],
    }
    print(f"API key (shown once): {key}")
    print("Key file entry:")
    print(json.dumps(entry, indent=2))


if __name__ == "__main__":
    main()
