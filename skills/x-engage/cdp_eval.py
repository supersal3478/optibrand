#!/usr/bin/env python3
"""cdp_eval.py — evaluate JavaScript in the CDP Chrome session.

The thin CLI for driving X.com (or any page) in the dedicated CDP Chrome.
Wraps Chrome DevTools Protocol Runtime.evaluate with userGesture=true (so X's
React handlers accept synthesized clicks) and Page.navigate.

Assumes Chrome is running with --remote-debugging-port=9222 on a non-default
user-data-dir (see SKILL.md for the launch command). Auto-finds the X tab
unless --tab-url-substring overrides.

Usage:
    cdp_eval.py --expr 'document.title'
    cdp_eval.py --expr 'document.querySelectorAll("article").length'
    cdp_eval.py --tab-url-substring 'x.com' --expr '...'
    cdp_eval.py --navigate 'https://x.com/notifications/mentions'

Requires the Hermes Python venv (its websockets module):
    /Users/salsmacos/Desktop/projects/brand-growth-engine/vendor/hermes-agent/.venv/bin/python
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request
from typing import Any

import websockets

DEFAULT_CDP_PORT = 9222
DEFAULT_TAB_URL_SUBSTRING = "x.com"


def find_tab(cdp_port: int, url_substring: str) -> dict:
    """Find the first page-type tab whose URL contains the substring."""
    url = f"http://localhost:{cdp_port}/json"
    with urllib.request.urlopen(url, timeout=5) as r:
        tabs = json.load(r)
    pages = [t for t in tabs if t.get("type") == "page"]
    for t in pages:
        if url_substring in (t.get("url") or ""):
            return t
    if pages:
        return pages[0]
    raise SystemExit(f"no page-type tabs found at localhost:{cdp_port}")


async def evaluate(ws_url: str, expression: str, await_promise: bool = True) -> Any:
    async with websockets.connect(ws_url, max_size=10_000_000) as ws:
        msg = {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "userGesture": True,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
        }
        await ws.send(json.dumps(msg))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
            data = json.loads(raw)
            if data.get("id") == 1:
                return data


async def navigate(ws_url: str, target_url: str) -> Any:
    async with websockets.connect(ws_url, max_size=10_000_000) as ws:
        msg = {"id": 1, "method": "Page.navigate", "params": {"url": target_url}}
        await ws.send(json.dumps(msg))
        raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
        return json.loads(raw)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--cdp-port", type=int, default=DEFAULT_CDP_PORT)
    p.add_argument(
        "--tab-url-substring",
        default=DEFAULT_TAB_URL_SUBSTRING,
        help="Pick the first tab whose URL contains this substring.",
    )
    p.add_argument("--expr", help="JavaScript expression to evaluate.")
    p.add_argument("--navigate", help="Navigate the tab to this URL instead of evaluating.")
    p.add_argument(
        "--no-await",
        action="store_true",
        help="Don't await a returned Promise (defaults to awaiting).",
    )
    args = p.parse_args()

    if not args.expr and not args.navigate:
        p.error("pass either --expr or --navigate")

    tab = find_tab(args.cdp_port, args.tab_url_substring)
    ws_url = tab["webSocketDebuggerUrl"]

    print(json.dumps({"tab_url": tab["url"], "tab_title": tab["title"]}, indent=2), file=sys.stderr)

    if args.navigate:
        result = asyncio.run(navigate(ws_url, args.navigate))
        print(json.dumps(result, indent=2))
        return

    result = asyncio.run(
        evaluate(ws_url, args.expr, await_promise=not args.no_await)
    )
    # Strip the verbose response down to the actual return value
    out = result.get("result", {}).get("result", {})
    if out.get("type") == "string":
        print(out.get("value", ""))
    else:
        print(json.dumps(out.get("value"), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
