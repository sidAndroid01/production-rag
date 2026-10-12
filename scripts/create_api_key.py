"""Generate an API key and the JSON entry the server needs to accept it.

    python scripts/create_api_key.py --tenant acme --user alice --groups hr,finance

The key is printed once; only its SHA-256 digest is stored. With --keys-file,
the entry is appended to that JSON list (keep it under the Git-ignored
secrets/ folder) and the whole list is printed on one line, ready to paste
into a hosting secret such as RAG_API_KEYS_JSON.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", required=True, help="document space this key belongs to")
    parser.add_argument("--user", required=True)
    parser.add_argument("--groups", default="", help="comma-separated group names")
    parser.add_argument("--name", default="", help="label for this key")
    parser.add_argument("--keys-file", type=Path, help="JSON list to append the entry to")
    args = parser.parse_args()
    key = "rag_" + secrets.token_urlsafe(32)
    entry = {
        "name": args.name or f"{args.tenant}/{args.user}",
        "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
        "tenant_id": args.tenant,
        "user_id": args.user,
        "groups": [group.strip() for group in args.groups.split(",") if group.strip()],
    }
    print(f"API key for {entry['name']} (shown once, send it privately): {key}")
    if args.keys_file is None:
        print("Key file entry:")
        print(json.dumps(entry, indent=2))
        return
    entries = json.loads(args.keys_file.read_text()) if args.keys_file.exists() else []
    entries.append(entry)
    args.keys_file.parent.mkdir(parents=True, exist_ok=True)
    args.keys_file.write_text(json.dumps(entries, indent=2) + "\n")
    args.keys_file.chmod(0o600)
    print(f"Added to {args.keys_file} ({len(entries)} key(s)). Value for RAG_API_KEYS_JSON:")
    print(json.dumps(entries, separators=(",", ":")))


if __name__ == "__main__":
    main()
