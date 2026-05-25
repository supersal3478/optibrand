#!/usr/bin/env python3
"""fetch-comments.py — read replies on a specific X post via CDP.

Navigates the dedicated CDP Chrome to a single tweet's detail URL, scrolls
the reply tree, and returns structured JSON. Optional --since-state persists
seen reply URLs so the orchestrator's monitor loop only sees what's new.

Usage:
    skills/x-engage/fetch-comments.py --post https://x.com/<me>/status/<id>
    skills/x-engage/fetch-comments.py --post <url> --since-state ~/.hermes/state/scheduled-posts/test.seen.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time as _time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cdp import open_session  # noqa: E402


# JS that finds all <article> tweet cards on the page EXCEPT the first
# (which is the parent post we're viewing), and pulls a structured record
# from each. The conversation thread can be deep; we cap at 50.
EXTRACT_JS = """
(() => {
  const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
  // Skip the first article — that's the parent post itself.
  const replies = articles.slice(1, 51);
  return replies.map(a => {
    const userName = a.querySelector('[data-testid="User-Name"]');
    const textEl = a.querySelector('[data-testid="tweetText"]');
    const statusA = a.querySelector('a[href*="/status/"]');
    const timeEl = a.querySelector('time');
    return {
      author: userName ? userName.innerText.replace(/\\n/g, ' | ') : '',
      text: textEl ? textEl.innerText : '',
      url: statusA ? statusA.href : '',
      ts: timeEl ? timeEl.getAttribute('datetime') : ''
    };
  }).filter(r => r.url);
})()
"""

SCROLL_JS = "window.scrollBy(0, 800); true"


async def fetch(post_url: str, max_scrolls: int) -> list[dict]:
    async with open_session() as s:
        await s.navigate(post_url, settle_seconds=5.0)
        # Scroll to load deeper into the reply tree (X lazy-renders).
        for _ in range(max_scrolls):
            await s.eval_js(SCROLL_JS)
            await asyncio.sleep(1.2)
        result = await s.eval_js(EXTRACT_JS, await_promise=False, user_gesture=False)
        return list(result or [])


def apply_since_state(replies: list[dict], state_path: Path) -> tuple[list[dict], dict]:
    """Filter replies to only-new based on URL, then update the state file."""
    prior_seen: set[str] = set()
    if state_path.exists():
        try:
            prior = json.loads(state_path.read_text())
            prior_seen = set(prior.get("seen_urls", []) or [])
        except (json.JSONDecodeError, OSError):
            pass

    new = [r for r in replies if r.get("url") and r["url"] not in prior_seen]
    all_seen = sorted(prior_seen | {r["url"] for r in replies if r.get("url")})
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "seen_urls": all_seen,
        "last_checked": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
    }, indent=2))
    return new, {"prior_seen": len(prior_seen), "now_seen": len(all_seen)}


def _emit(obj: dict) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False)
    print(flush=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--post", required=True,
                   help="Tweet URL (https://x.com/<author>/status/<id>).")
    p.add_argument("--since-state", default=None,
                   help="JSON state file persisting seen reply URLs.")
    p.add_argument("--max-scrolls", type=int, default=6,
                   help="How many times to scrollBy(800px) before extracting (default 6).")
    args = p.parse_args()

    try:
        replies = asyncio.run(fetch(args.post, args.max_scrolls))
    except Exception as e:
        _emit({"ok": False, "error": "cdp_failure", "detail": str(e)})
        return 2

    new_replies = replies
    state_info = None
    if args.since_state:
        new_replies, state_info = apply_since_state(replies, Path(args.since_state))

    _emit({
        "ok": True,
        "post_url": args.post,
        "count": len(replies),
        "comments": replies,
        "new_comments": new_replies,
        "since_state": state_info,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
