#!/usr/bin/env python3
"""reply.py — post a reply to a specific X tweet via CDP.

Mirrors the proven 2026-05-14 Vadim recipe verbatim. Navigates to the parent
tweet's detail URL, focuses the inline composer, inserts text, screenshots,
optionally clicks submit.

Usage:
    skills/x-engage/reply.py --parent https://x.com/<author>/status/<id> --text "..."
    skills/x-engage/reply.py --parent ... --text "..." --live
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time as _time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cdp import open_session  # noqa: E402

COMPOSER_SELECTOR = 'div[data-testid="tweetTextarea_0"]'
INLINE_SUBMIT_SELECTOR = 'button[data-testid="tweetButtonInline"]'
SCREENSHOT_DIR = Path.home() / ".hermes" / "logs" / "x-screenshots"


def _emit(obj: dict) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False)
    print(flush=True)


async def run(parent_url: str, text: str, live: bool, screenshot_path: Path | None) -> dict:
    async with open_session() as s:
        await s.navigate(parent_url, settle_seconds=5.0)

        focused = await s.focus(COMPOSER_SELECTOR)
        if not focused:
            return {"ok": False, "error": "composer_not_found",
                    "selector": COMPOSER_SELECTOR,
                    "parent_url": parent_url}

        await s.insert_text(text)
        await asyncio.sleep(1.2)

        if screenshot_path:
            shot = await s.call("Page.captureScreenshot", {"format": "png"})
            data = shot.get("result", {}).get("data")
            if data:
                screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                screenshot_path.write_bytes(base64.b64decode(data))

        if not live:
            return {"ok": True, "dry_run": True, "chars": len(text),
                    "parent_url": parent_url,
                    "screenshot": str(screenshot_path) if screenshot_path else None}

        clicked = await s.click(INLINE_SUBMIT_SELECTOR)
        if not clicked:
            return {"ok": False, "error": "submit_button_not_found",
                    "selector": INLINE_SUBMIT_SELECTOR,
                    "note": "Composer was populated but inline Reply button wasn't clickable."}
        await asyncio.sleep(5.0)

        if screenshot_path:
            shot2 = await s.call("Page.captureScreenshot", {"format": "png"})
            data2 = shot2.get("result", {}).get("data")
            if data2:
                after_path = screenshot_path.with_name(screenshot_path.stem + "_after.png")
                after_path.write_bytes(base64.b64decode(data2))

        return {"ok": True, "dry_run": False, "chars": len(text),
                "parent_url": parent_url,
                "screenshot": str(screenshot_path) if screenshot_path else None}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--parent", required=True,
                   help="URL of the tweet to reply to (https://x.com/<author>/status/<id>).")
    p.add_argument("--text", required=True, help="Reply text (max 280 chars).")
    p.add_argument("--live", action="store_true",
                   help="Actually submit. Default is dry-run.")
    p.add_argument("--no-screenshot", action="store_true")
    args = p.parse_args()

    if not args.text.strip():
        _emit({"ok": False, "error": "empty_text"})
        return 1
    if len(args.text) > 280:
        _emit({"ok": False, "error": "text_too_long", "chars": len(args.text), "max": 280})
        return 1

    screenshot_path = None
    if not args.no_screenshot:
        ts = _time.strftime("%Y%m%dT%H%M%SZ", _time.gmtime())
        screenshot_path = SCREENSHOT_DIR / f"reply_{ts}.png"

    try:
        result = asyncio.run(run(args.parent, args.text, args.live, screenshot_path))
    except Exception as e:
        _emit({"ok": False, "error": "cdp_failure", "detail": str(e)})
        return 2

    _emit(result)
    return 0 if result.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main())
