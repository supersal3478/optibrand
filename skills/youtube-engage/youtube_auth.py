"""
youtube_auth — one-time OAuth bootstrap for YouTube Data API v3.

Run this manually, OUTSIDE any agent session, to authorize the agent against
your channel:

    python youtube_auth.py

This opens a browser, completes the OAuth flow, and writes:
    ~/.hermes/state/youtube_token.json   (refresh token + client config)

After this runs successfully, the youtube-engage skill can use the refresh
token to mint short-lived access tokens at runtime.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Lazy import so plain-package install doesn't pull google libs unless this is run.
try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print(
        "ERROR: google-auth-oauthlib not installed.\n"
        "Run: pip install google-auth-oauthlib google-auth-httplib2",
        file=sys.stderr,
    )
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
TOKEN_FILE = HERMES_HOME / "state" / "youtube_token.json"


def main() -> int:
    client_id = os.environ.get("YT_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("YT_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            "ERROR: set YT_OAUTH_CLIENT_ID and YT_OAUTH_CLIENT_SECRET in ~/.hermes/.env first.",
            file=sys.stderr,
        )
        return 2

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")

    if not creds.refresh_token:
        print(
            "ERROR: no refresh_token in response. Revoke prior auth at "
            "https://myaccount.google.com/permissions then re-run with prompt=consent.",
            file=sys.stderr,
        )
        return 3

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({
        "refresh_token": creds.refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": SCOPES,
    }))
    os.chmod(TOKEN_FILE, 0o600)
    print(f"OK — wrote {TOKEN_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
