#!/usr/bin/env python3
"""publish.py — publish a new X post from a markdown file via CDP.

Mirrors the proven Vadim-reply recipe (Input.insertText + Runtime.evaluate
with userGesture=true on the submit button) but for the compose-new flow at
x.com/compose/post.

Defaults to --dry-run (types into the composer + screenshots but does NOT
submit). Pass --live to actually submit.

Usage:
    skills/x-engage/publish.py --file drafts/2026-05-25-x-01.md
    skills/x-engage/publish.py --file drafts/2026-05-25-x-01.md --live
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time as _time
from pathlib import Path

# Allow running this script directly without an enclosing package — load _cdp
# from the same directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cdp import open_session  # noqa: E402

COMPOSE_URL = "https://x.com/compose/post"
COMPOSER_SELECTOR = 'div[data-testid="tweetTextarea_0"]'
SUBMIT_SELECTOR = 'button[data-testid="tweetButton"]'
SCREENSHOT_DIR = Path.home() / ".hermes" / "logs" / "x-screenshots"


def _emit(obj: dict) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False)
    print(flush=True)


async def run(text: str, live: bool, screenshot_path: Path | None) -> dict:
    async with open_session() as s:
        await s.navigate(COMPOSE_URL, settle_seconds=5.0)

        focused = await s.focus(COMPOSER_SELECTOR)
        if not focused:
            return {"ok": False, "error": "composer_not_found",
                    "selector": COMPOSER_SELECTOR}

        await s.insert_text(text)
        await asyncio.sleep(1.5)

        if screenshot_path:
            shot = await s.call("Page.captureScreenshot", {"format": "png"})
            data = shot.get("result", {}).get("data")
            if data:
                screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                screenshot_path.write_bytes(base64.b64decode(data))

        if not live:
            return {"ok": True, "dry_run": True, "chars": len(text),
                    "screenshot": str(screenshot_path) if screenshot_path else None}

        # Submit.
        clicked = await s.click(SUBMIT_SELECTOR)
        if not clicked:
            return {"ok": False, "error": "submit_button_not_found",
                    "selector": SUBMIT_SELECTOR,
                    "note": "Composer was populated but submit button wasn't clickable. "
                            "Common causes: text exceeded 280 chars, composer lost focus, "
                            "or X DOM rolled."}
        await asyncio.sleep(5.0)

        # Capture the resulting post URL from the toast or from the navbar
        # transitioning. Best-effort — failure here doesn't invalidate the post.
        post_url = await s.eval_js(
            "(() => {"
            "  const a = document.querySelector('a[href*=\"/status/\"]');"
            "  return a ? a.href : '';"
            "})()"
        )

        if screenshot_path:
            shot2 = await s.call("Page.captureScreenshot", {"format": "png"})
            data2 = shot2.get("result", {}).get("data")
            if data2:
                after_path = screenshot_path.with_name(screenshot_path.stem + "_after.png")
                after_path.write_bytes(base64.b64decode(data2))

        return {"ok": True, "dry_run": False, "chars": len(text),
                "post_url": post_url or "",
                "screenshot": str(screenshot_path) if screenshot_path else None}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--file", required=True, help="Path to markdown draft.")
    p.add_argument("--live", action="store_true",
                   help="Actually submit. Default is dry-run (types but does not click).")
    p.add_argument("--no-screenshot", action="store_true",
                   help="Skip the audit screenshot.")
    args = p.parse_args()

    path = Path(args.file)
    if not path.exists():
        _emit({"ok": False, "error": "draft_file_missing", "path": str(path)})
        return 1
    text = path.read_text().strip()
    if not text:
        _emit({"ok": False, "error": "draft_file_empty"})
        return 1
    if len(text) > 280:
        _emit({"ok": False, "error": "draft_too_long", "chars": len(text), "max": 280})
        return 1

    screenshot_path = None
    if not args.no_screenshot:
        ts = _time.strftime("%Y%m%dT%H%M%SZ", _time.gmtime())
        screenshot_path = SCREENSHOT_DIR / f"publish_{ts}.png"

    try:
        result = asyncio.run(run(text, live=args.live, screenshot_path=screenshot_path))
    except Exception as e:
        _emit({"ok": False, "error": "cdp_failure", "detail": str(e)})
        return 2

    _emit(result)
    return 0 if result.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main())
