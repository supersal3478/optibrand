#!/usr/bin/env python3
"""test-humanization.py — interactive smoke test for x_human primitives.

You run this. You watch the visible CDP Chrome. You decide whether the mouse
arcs, the scrolls ease, and the typing cadence look like a person.

DOES NOT SUBMIT ANYTHING. Composer text is cleared before exit so you
don't leave a draft. No --live flag exists on purpose.

Usage:
    # All demos against current x.com/home tab — mouse + scroll only
    scripts/test-humanization.py

    # Mouse + scroll + typing into a specific post's composer (will NOT submit)
    scripts/test-humanization.py --typing-target https://x.com/<author>/status/<id>

    # Just one demo
    scripts/test-humanization.py --mouse
    scripts/test-humanization.py --scroll
    scripts/test-humanization.py --typing --typing-target https://x.com/...

What you should see:
    mouse  — cursor moves from a corner to mid-screen, then traces a loop
             of four points; path should arc, not jump in straight lines
    scroll — page scrolls smoothly down ~1500px, pauses, smoothly back up
    typing — composer fills char-by-char with variable speed, occasional
             pauses, ~1 typo every 70-ish chars. Text is then cleared.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time as _time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "skills" / "x-engage"))

from _cdp import open_session  # noqa: E402
from x_human import HumanCDP  # noqa: E402

START_CHROME_CDP = PROJECT_ROOT / "skills" / "x-engage" / "start-chrome-cdp.sh"

COMPOSER_SELECTOR = 'div[data-testid="tweetTextarea_0"]'

DEMO_TEXT = (
    "testing the typing primitive — variable cadence, "
    "occasional pause, the odd typo. nothing fancy."
)


def _ensure_chrome() -> None:
    subprocess.run(["bash", str(START_CHROME_CDP)],
                   check=True, capture_output=True, timeout=20)


async def _mouse_demo(h: HumanCDP) -> dict:
    """Move cursor through five waypoints. The Bezier arcs and slight
    overshoots should be visually obvious."""
    w, h_px = await h.viewport_size()
    waypoints = [
        (w * 0.2, h_px * 0.3),
        (w * 0.8, h_px * 0.25),
        (w * 0.85, h_px * 0.75),
        (w * 0.15, h_px * 0.75),
        (w * 0.5, h_px * 0.5),
    ]
    timings = []
    for tx, ty in waypoints:
        t0 = _time.perf_counter()
        await h.move_to(tx, ty)
        timings.append(round((_time.perf_counter() - t0) * 1000, 1))
        await h.jitter(0.2, 0.6)
    return {"waypoints": len(waypoints), "per_move_ms": timings}


async def _scroll_demo(h: HumanCDP) -> dict:
    """Scroll down ~1500px then back up. Should be visibly smooth, not a jump."""
    t0 = _time.perf_counter()
    await h.scroll(1500)
    down_ms = round((_time.perf_counter() - t0) * 1000, 1)
    await h.jitter(1.0, 2.0)
    t1 = _time.perf_counter()
    await h.scroll(-1500)
    up_ms = round((_time.perf_counter() - t1) * 1000, 1)
    return {"down_ms": down_ms, "up_ms": up_ms}


async def _typing_demo(h: HumanCDP, target_url: str,
                       clear_after: bool) -> dict:
    """Navigate to a post, click into the reply composer, type DEMO_TEXT
    char-by-char, optionally clear the composer at the end."""
    await h.session.navigate(target_url, settle_seconds=4.0)

    # Some posts auto-show composer; others need a click.
    composer_present = await h.session.eval_js(
        f"!!document.querySelector('{COMPOSER_SELECTOR}')",
        await_promise=False,
    )
    opened_reply_button = False
    if not composer_present:
        opened_reply_button = await h.click_element('div[data-testid="reply"]',
                                                     scroll_into_view=True)
        await asyncio.sleep(1.2)

    t0 = _time.perf_counter()
    typed = await h.type_into(COMPOSER_SELECTOR, DEMO_TEXT)
    elapsed_ms = round((_time.perf_counter() - t0) * 1000, 1)

    if not typed:
        return {"ok": False, "error": "composer_type_failed",
                "opened_reply_button": opened_reply_button}

    # Brief consider-dwell so the user can see the finished draft.
    await h.consider_dwell()

    cleared = False
    if clear_after:
        # Cmd+A then Backspace to wipe the composer. Uses raw key events,
        # which is fine — clearing is not part of the human-fingerprint path.
        await h.session.call("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "a", "code": "KeyA",
            "modifiers": 4,  # 4 = Meta on macOS in CDP
            "windowsVirtualKeyCode": 65,
        })
        await h.session.call("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "a", "code": "KeyA",
            "modifiers": 4, "windowsVirtualKeyCode": 65,
        })
        await asyncio.sleep(0.2)
        await h.session.call("Input.dispatchKeyEvent", {
            "type": "rawKeyDown", "key": "Backspace",
            "code": "Backspace", "windowsVirtualKeyCode": 8,
        })
        await h.session.call("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "Backspace",
            "code": "Backspace", "windowsVirtualKeyCode": 8,
        })
        await asyncio.sleep(0.4)
        # Confirm composer is empty
        empty = await h.session.eval_js(
            f"(() => {{ const c = document.querySelector('{COMPOSER_SELECTOR}'); "
            "return !c || (c.innerText || '').trim().length === 0; })()",
            await_promise=False,
        )
        cleared = bool(empty)

    return {"ok": True, "typed_chars": len(DEMO_TEXT),
            "type_ms": elapsed_ms, "cleared": cleared}


async def run(*, do_mouse: bool, do_scroll: bool, do_typing: bool,
              typing_target: str | None, clear_after: bool) -> dict:
    async with open_session() as s:
        h = HumanCDP(s)

        # Park cursor in a known corner so the first move is visible.
        w, hp = await h.viewport_size()
        h.cursor_x, h.cursor_y = 30, hp - 30

        out: dict = {"viewport": {"w": w, "h": hp}}

        if do_mouse:
            out["mouse"] = await _mouse_demo(h)
            await h.jitter(0.8, 1.4)

        if do_scroll:
            out["scroll"] = await _scroll_demo(h)
            await h.jitter(0.8, 1.4)

        if do_typing:
            if not typing_target:
                out["typing"] = {"ok": False,
                                 "error": "missing --typing-target URL"}
            else:
                out["typing"] = await _typing_demo(h, typing_target,
                                                    clear_after)
        return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--mouse", action="store_true",
                   help="Run only the mouse demo.")
    p.add_argument("--scroll", action="store_true",
                   help="Run only the scroll demo.")
    p.add_argument("--typing", action="store_true",
                   help="Run only the typing demo. Requires --typing-target.")
    p.add_argument("--typing-target", default=None,
                   help="Tweet URL to type into. NOTHING IS SUBMITTED.")
    p.add_argument("--no-clear", action="store_true",
                   help="Don't wipe the composer after typing demo. Default "
                        "is to wipe it so no draft is left behind.")
    args = p.parse_args()

    # If user passed no specific flag, run all available demos.
    any_specific = args.mouse or args.scroll or args.typing
    if not any_specific:
        do_mouse, do_scroll = True, True
        do_typing = bool(args.typing_target)
    else:
        do_mouse, do_scroll, do_typing = args.mouse, args.scroll, args.typing
        if do_typing and not args.typing_target:
            sys.stderr.write("--typing requires --typing-target\n")
            return 2

    try:
        _ensure_chrome()
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"chrome-cdp boot failed: {e.stderr.decode()}\n")
        return 2

    try:
        result = asyncio.run(run(
            do_mouse=do_mouse,
            do_scroll=do_scroll,
            do_typing=do_typing,
            typing_target=args.typing_target,
            clear_after=not args.no_clear,
        ))
    except Exception as e:
        sys.stderr.write(f"smoke test crashed: {e}\n")
        return 2

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
