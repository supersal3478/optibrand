#!/usr/bin/env python3
"""li-feed-engagement.py — LinkedIn outbound goodwill on the home feed.

The LinkedIn counterpart to the X feed-engagement, but driven by lipy (Playwright)
instead of CDP:

  1. `lipy feed --limit N`  → posts from your home feed (author + text + urn)
  2. skip your own posts / blocklist / already-seen
  3. draft a short goodwill comment in your voice (reuses inbound-engagement's
     proven drafter + inline brand-guard)
  4. --live : enqueue to the outbox (mode=goodwill). outbox-flush submits each
              via `lipy comment --post <urn>`.
     dry-run : print the draft (default)

Idempotent: skips posts already engaged (state at ~/.hermes/state/li_feed_seen.json).

Usage:
    scripts/li-feed-engagement.py                  # dry-run
    scripts/li-feed-engagement.py --live --limit 3
    scripts/li-feed-engagement.py --reset-seen
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import _outbox  # noqa: E402
import _metrics  # noqa: E402

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
LIPY_BIN = Path(os.environ.get("LIPY_BIN", Path.home() / ".local" / "bin" / "lipy"))
SEEN_PATH = HERMES_HOME / "state" / "li_feed_seen.json"
BLOCKLIST = PROJECT_ROOT / "config" / "blocklist.yaml"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Reuse the proven drafter + voice context from inbound-engagement.py (hyphenated
# filename → load via importlib). draft_reply produces a short, voice-matched,
# brand-guarded comment that engages with the given text — exactly what we want.
def _load_inbound_module():
    spec = importlib.util.spec_from_file_location(
        "inbound_engagement", SCRIPTS / "inbound-engagement.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_IB = _load_inbound_module()
draft_reply = _IB.draft_reply


def _self_name() -> str | None:
    try:
        for line in (PROJECT_ROOT / "BRAND.md").read_text().splitlines():
            s = line.strip()
            if s.startswith("**Name:**"):
                return s.split("**Name:**", 1)[1].strip() or None
    except OSError:
        pass
    return None


def _blocked_terms() -> list[str]:
    try:
        import yaml
        data = yaml.safe_load(BLOCKLIST.read_text()) or {}
        terms: list[str] = []
        # Flat lists of substrings.
        for key in ("keywords", "domains", "accounts"):
            v = data.get(key)
            if isinstance(v, list):
                terms += [str(t).lower() for t in v]
        # `handles` is a dict {platform: [slugs]} — take the LinkedIn slugs only.
        # (Iterating the dict directly yielded its KEYS "x"/"linkedin"/"youtube",
        # and "x" as a substring blocked essentially every post.)
        handles = data.get("handles")
        if isinstance(handles, dict):
            terms += [str(t).lower() for t in (handles.get("linkedin") or [])]
        return [t for t in terms if t]
    except Exception:
        return []


def _load_seen() -> dict:
    try:
        return json.loads(SEEN_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_seen(seen: dict) -> None:
    try:
        SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        SEEN_PATH.write_text(json.dumps(seen))
    except OSError:
        pass


def fetch_feed(limit: int) -> dict:
    """Run `lipy feed` and return {ok, posts:[{urn, url, author, text}]}."""
    try:
        p = subprocess.run([str(LIPY_BIN), "feed", "--limit", str(limit)],
                           capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "lipy_feed_timeout"}
    out = (p.stdout or "").strip()
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"ok": False, "error": (p.stderr or out)[:300]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--limit", type=int, default=5, help="feed posts to consider")
    ap.add_argument("--max-comments", type=int, default=1,
                    help="max goodwill comments to draft this run")
    ap.add_argument("--live", action="store_true",
                    help="enqueue to outbox (else just print)")
    ap.add_argument("--reset-seen", action="store_true")
    args = ap.parse_args()

    if args.reset_seen:
        SEEN_PATH.unlink(missing_ok=True)
        print("[reset] cleared li_feed_seen")

    print("\n=== LinkedIn feed goodwill ===")
    result = fetch_feed(args.limit)
    if not result.get("ok"):
        err = result.get("error") or "unknown"
        if "not_logged_in" in err or "auth_required" in err:
            print("[li] not logged in. Fix with:  lipy login --headed")
        else:
            print(f"[li] feed fetch failed: {err}")
        return 0
    posts = result.get("posts") or []
    if not posts:
        print("[li] feed empty (or logged out?)")
        return 0
    print(f"[li] {len(posts)} feed post(s) returned")

    me = (_self_name() or "").lower()
    blocked = _blocked_terms()
    seen = _load_seen()
    drafted = 0

    for post in posts:
        if drafted >= args.max_comments:
            break
        urn = post.get("urn")
        if not urn or urn in seen:
            continue
        author = post.get("author") or "?"
        text = (post.get("text") or "").strip()
        if not text:
            continue
        if me and me in author.lower():
            seen[urn] = {"ts": _utcnow(), "status": "skip_self"}
            continue
        hay = (author + " " + text).lower()
        if any(b and b in hay for b in blocked):
            seen[urn] = {"ts": _utcnow(), "status": "skip_blocklist"}
            _metrics.log_event("skipped_blocklist", platform="linkedin",
                               mode="goodwill", parent_url=post.get("url"))
            continue

        print(f"\n[post] {author}")
        print(f"       {text[:160]}")
        res = draft_reply("linkedin", author, text)
        if res.get("decision") != "DRAFT" or not res.get("draft"):
            print(f"       SKIP — drafter {res.get('decision', '?')}: {res.get('reasons')}")
            seen[urn] = {"ts": _utcnow(), "status": "refused"}
            continue
        comment = res["draft"].strip()
        print(f"       Draft: {comment}")
        _metrics.log_event("drafted", platform="linkedin", mode="goodwill",
                           parent_url=post.get("url"), chars=len(comment),
                           word_count=len(comment.split()),
                           length_target=res.get("length_target"),
                           autofixes=res.get("autofixes"))

        if args.live:
            try:
                outbox_id = _outbox.enqueue(
                    platform="linkedin", mode="goodwill",
                    parent_url=urn, draft_text=comment,
                    metadata={"author": author, "post_text": text[:500],
                              "post_url": post.get("url"),
                              "length_target": res.get("length_target"),
                              "autofixes": res.get("autofixes") or []},
                )
                print(f"       ✓ queued outbox id={outbox_id}")
                seen[urn] = {"ts": _utcnow(), "status": "queued", "outbox_id": outbox_id}
            except Exception as e:
                print(f"       FAIL queueing — {e}")
                continue
        else:
            print("       (dry-run — not queued)")
            seen[urn] = {"ts": _utcnow(), "status": "drafted"}
        drafted += 1

    _save_seen(seen)
    print(f"\n=== Done. Drafted {drafted} goodwill comment(s). ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
