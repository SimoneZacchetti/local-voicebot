"""Print fresh secrets for .env (never written to disk).

Usage: uv run python scripts/gen_secrets.py [device ...]
One API token per device (e.g. iphone ipad): revoke one by removing it from API_TOKENS.
"""

from __future__ import annotations

import secrets
import sys

devices = sys.argv[1:] or ["iphone"]
tokens = {d: secrets.token_urlsafe(32) for d in devices}
for d, t in tokens.items():
    print(f"# {d}: {t}")
print("API_TOKENS=" + ",".join(tokens.values()))
