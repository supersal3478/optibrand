"""
youtube_token — fetch a fresh access token from the stored refresh token, with caching.

Used by the youtube-engage skill. Prints the token to stdout on success.
On any failure prints to STDERR (not stdout) and exits non-zero, so the agent
session never sees a bearer in its context.

One-time bootstrap (run by the user, outside the agent session):

    python youtube_auth.py    # opens a browser, writes ~/.hermes/state/youtube_token.json

Then this module is invoked at runtime:

    python -m youtube_token
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import urllib.parse
import urllib.request

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
TOKEN_FILE = HERMES_HOME / "state" / "youtube_token.json"
CACHE_FILE = HERMES_HOME / "state" / "youtube_access_token.cache"


def _load_refresh_token() -> dict:
    if not TOKEN_FILE.exists():
        print(f"ERROR: {TOKEN_FILE} missing. Complete OAuth bootstrap first.", file=sys.stderr)
        sys.exit(2)
    return json.loads(TOKEN_FILE.read_text())


def _read_cache() -> str | None:
    if not CACHE_FILE.exists():
        return None
    try:
        cache = json.loads(CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if cache.get("expires_at", 0) - 60 < time.time():
        return None
    return cache.get("access_token")


def _write_cache(token: str, expires_in: int) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps({
        "access_token": token,
        "expires_at": int(time.time()) + int(expires_in),
    }))
    os.chmod(CACHE_FILE, 0o600)


def _refresh(refresh_token: str, client_id: str, client_secret: str) -> tuple[str, int]:
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as e:
        print(f"ERROR: token refresh failed: {e}", file=sys.stderr)
        sys.exit(3)
    return payload["access_token"], payload.get("expires_in", 3600)


def main() -> int:
    cached = _read_cache()
    if cached:
        sys.stdout.write(cached)
        return 0

    stored = _load_refresh_token()
    refresh_token = stored.get("refresh_token")
    client_id = os.environ.get("YT_OAUTH_CLIENT_ID") or stored.get("client_id")
    client_secret = os.environ.get("YT_OAUTH_CLIENT_SECRET") or stored.get("client_secret")
    if not (refresh_token and client_id and client_secret):
        print("ERROR: missing refresh_token / client_id / client_secret", file=sys.stderr)
        return 4

    access_token, expires_in = _refresh(refresh_token, client_id, client_secret)
    _write_cache(access_token, expires_in)
    sys.stdout.write(access_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
