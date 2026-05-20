#!/usr/bin/env python3
"""Scrape the user's recent X replies and posts via CDP for corpus training.

Navigates the dedicated CDP Chrome (must already be running on :9222 — run
`start-chrome-cdp` first) to https://x.com/<handle>/with_replies, scrolls until
~N records collected, and writes:
    corpus/x_replies.jsonl   (your replies on others' posts)
    corpus/x_posts.jsonl     (your original tweets — quote/repost are skipped)

The CDP Chrome must already be logged in as the target handle.

Usage:
    scripts/scrape-x-corpus.py --handle salaicreates --limit 200
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

# Requires the websockets module — installed in the Hermes venv by setup.sh.
try:
    import websockets
except ImportError:
    sys.stderr.write(
        "websockets not available. Run this with the Hermes venv python:\n"
        "  vendor/hermes-agent/.venv/bin/python scripts/scrape-x-corpus.py ...\n"
    )
    sys.exit(2)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORPUS = PROJECT_ROOT / "corpus"
CDP_PORT = 9222


def find_x_tab():
    with urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json", timeout=5) as r:
        for t in json.load(r):
            if t.get("type") == "page" and "x.com" in (t.get("url") or ""):
                return t
    raise SystemExit("No x.com tab found in CDP Chrome. Start one with `start-chrome-cdp` and open x.com first.")


async def cdp_call(ws, mid, method, params=None):
    await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        data = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
        if data.get("id") == mid:
            return data


EXTRACT_JS = r"""
(() => {
  return [...document.querySelectorAll("article[data-testid=tweet]")].map(a => {
    const author_line = (a.querySelector("[data-testid=User-Name]") || {}).innerText || "";
    const handle_match = author_line.match(/@([\w_]+)/);
    return {
      handle: handle_match ? handle_match[1] : null,
      text: ((a.querySelector("[data-testid=tweetText]") || {}).innerText || ""),
      url: (a.querySelector("a[href*=\"/status/\"]") || {}).href || null,
      timestamp: (a.querySelector("time") || {}).getAttribute && a.querySelector("time").getAttribute("datetime"),
      reply_aria: (a.querySelector("button[data-testid=reply]") || {}).getAttribute && a.querySelector("button[data-testid=reply]").getAttribute("aria-label"),
      is_reply: /Replying to/.test(a.innerText.split("\n").slice(0,5).join(" ")),
    };
  });
})()
"""


def _load_existing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


async def main_async(handle: str, limit: int):
    tab = find_x_tab()
    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=20_000_000) as ws:
        await cdp_call(ws, 1, "Page.navigate", {"url": f"https://x.com/{handle}/with_replies"})
        await asyncio.sleep(6)

        # Load prior records so the scraper appends+dedupes instead of clobbering.
        replies: list[dict] = _load_existing(CORPUS / "x_replies.jsonl")
        posts: list[dict] = _load_existing(CORPUS / "x_posts.jsonl")
        seen_urls: set = {r.get("url") for r in replies + posts if r.get("url")}
        baseline = len(replies) + len(posts)
        last_count = -1
        stuck = 0

        for scroll_iter in range(60):
            r = await cdp_call(ws, 100 + scroll_iter, "Runtime.evaluate", {
                "expression": "JSON.stringify(" + EXTRACT_JS + ")",
                "returnByValue": True,
            })
            raw = r.get("result", {}).get("result", {}).get("value", "[]")
            for art in json.loads(raw):
                url = art.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                if art.get("handle") != handle:
                    continue  # Skip retweets / quote-context tweets from other accounts
                rec = {
                    "url": url,
                    "text": (art.get("text") or "").strip(),
                    "posted_at": art.get("timestamp"),
                }
                if art.get("is_reply"):
                    replies.append(rec)
                else:
                    posts.append(rec)

            print(f"  scroll {scroll_iter+1}: replies={len(replies)} posts={len(posts)}")
            # `limit` is the number of NEW records we want this run, not total.
            if (len(replies) + len(posts)) - baseline >= limit:
                break
            if len(seen_urls) == last_count:
                stuck += 1
                if stuck >= 4:
                    print("  no more results loading; stopping.")
                    break
            else:
                stuck = 0
            last_count = len(seen_urls)
            await cdp_call(ws, 1000 + scroll_iter, "Runtime.evaluate", {
                "expression": "window.scrollTo(0, document.body.scrollHeight)",
            })
            await asyncio.sleep(2.5)

        CORPUS.mkdir(parents=True, exist_ok=True)
        (CORPUS / "x_replies.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in replies))
        (CORPUS / "x_posts.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in posts))
        print(f"\nwrote {CORPUS}/x_replies.jsonl ({len(replies)} records)")
        print(f"wrote {CORPUS}/x_posts.jsonl   ({len(posts)} records)")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--handle", required=True, help="Your X handle (without @).")
    p.add_argument("--limit", type=int, default=200, help="Approx max records to collect.")
    args = p.parse_args()
    asyncio.run(main_async(args.handle, args.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
