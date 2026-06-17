"""x_human — humanization primitives layered over the raw CDP session.

Why this exists:
  The original x-engage primitives (insert_text + Runtime.evaluate click) work,
  but their fingerprint screams automation: text appears instantaneously, clicks
  land at the same pixel inside a button, and there's no mouse movement between
  actions. X's anti-bot heuristics weigh exactly those signals.

  This module wraps a CDPSession with humanized versions of the same operations:
    • Mouse moves along a 2-control-point cubic Bezier with ease-in/out timing.
    • Clicks are dispatchMouseEvent press/release at a jittered point inside the
      target element's bounding box, not the centroid.
    • Typing is character-by-character Input.insertText with per-char delay
      (DraftJS — the X composer — accepts insertText reliably; dispatchKeyEvent
      char events drop on the floor). Typos + backspace correction sprinkled in.
    • Scrolling is many small wheel events with easing, not one big jump.
    • Dwell helpers model reading and "should I really comment?" pauses.

Design:
  • Composition, not inheritance. HumanCDP wraps a CDPSession; you can still
    reach the raw .session for low-level operations.
  • All randomness is uniform-with-biased-ranges. This is plausibility, not a
    perfect human simulator. The goal is "doesn't look like a script", not
    "indistinguishable from a real person".
  • Stateless across sessions, except for the mouse cursor position (kept on
    the instance so successive moves chain naturally).

Tunables live in config/jitter.yaml so the user can dial behavior up/down
without code edits. Defaults here match the values from jitter.yaml at write
time as a sensible fallback when caller doesn't pass overrides.
"""
from __future__ import annotations

import asyncio
import json
import math
import random
from dataclasses import dataclass
from typing import Any


# ────────────────────────────────────────────── randomness helpers ──


def _ease_in_out(t: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * t)


def _cubic_bezier(p0, p1, p2, p3, t: float) -> tuple[float, float]:
    one_t = 1 - t
    x = (one_t**3) * p0[0] + 3 * (one_t**2) * t * p1[0] + 3 * one_t * (t**2) * p2[0] + (t**3) * p3[0]
    y = (one_t**3) * p0[1] + 3 * (one_t**2) * t * p1[1] + 3 * one_t * (t**2) * p2[1] + (t**3) * p3[1]
    return x, y


# ────────────────────────────────────────────── HumanCDP ──


@dataclass
class HumanCDP:
    """Wraps a CDPSession with humanized input dispatchers.

    Usage:
        async with open_session() as s:
            h = HumanCDP(s)
            await h.move_to(400, 300)
            await h.scroll(800)
            ok = await h.click_element('button[data-testid="tweetButtonInline"]')
            await h.type_into('div[data-testid="tweetTextarea_0"]', "hello world")
    """
    session: Any  # CDPSession from _cdp.py — typed as Any to keep this file standalone
    cursor_x: float = 100.0
    cursor_y: float = 100.0

    # ── low-level CDP dispatches ──────────────────────────────────

    async def _mouse_event(self, type_: str, x: float, y: float, *,
                           button: str = "none", click_count: int = 0,
                           delta_x: float = 0.0, delta_y: float = 0.0) -> None:
        await self.session.call("Input.dispatchMouseEvent", {
            "type": type_,
            "x": x,
            "y": y,
            "button": button,
            "clickCount": click_count,
            "deltaX": delta_x,
            "deltaY": delta_y,
        })

    async def _key_event(self, type_: str, *,
                         text: str | None = None,
                         key: str | None = None,
                         code: str | None = None,
                         windows_vk: int | None = None) -> None:
        params: dict[str, Any] = {"type": type_}
        if text is not None:
            params["text"] = text
        if key is not None:
            params["key"] = key
        if code is not None:
            params["code"] = code
        if windows_vk is not None:
            params["windowsVirtualKeyCode"] = windows_vk
            params["nativeVirtualKeyCode"] = windows_vk
        await self.session.call("Input.dispatchKeyEvent", params)

    # ── mouse: move + click + scroll ──────────────────────────────

    async def move_to(self, tx: float, ty: float, *,
                      duration_ms: int = 0, steps: int = 0) -> None:
        """Move cursor from current position to (tx, ty) along a Bezier curve.
        Issues dispatchMouseEvent mouseMoved events along the way."""
        dx, dy = tx - self.cursor_x, ty - self.cursor_y
        dist = math.hypot(dx, dy)
        if dist < 1:
            self.cursor_x, self.cursor_y = tx, ty
            return

        if steps <= 0:
            steps = max(18, min(72, int(dist / 9)))
        if duration_ms <= 0:
            duration_ms = int(dist * 1.4 + random.uniform(-60, 140))
            duration_ms = max(180, min(1400, duration_ms))

        # Two control points offset perpendicular to the travel direction so
        # the path arcs gently — never a perfectly straight line.
        perp_x, perp_y = -dy / dist, dx / dist
        sway = random.uniform(-dist * 0.18, dist * 0.18)
        c1 = (self.cursor_x + dx * 0.25 + perp_x * sway,
              self.cursor_y + dy * 0.25 + perp_y * sway)
        c2 = (self.cursor_x + dx * 0.65 + perp_x * sway * 0.5,
              self.cursor_y + dy * 0.65 + perp_y * sway * 0.5)
        start = (self.cursor_x, self.cursor_y)
        end = (tx, ty)

        per_step = duration_ms / 1000.0 / steps
        for i in range(1, steps + 1):
            t = _ease_in_out(i / steps)
            x, y = _cubic_bezier(start, c1, c2, end, t)
            x += random.uniform(-0.6, 0.6)
            y += random.uniform(-0.6, 0.6)
            await self._mouse_event("mouseMoved", x, y)
            await asyncio.sleep(per_step)
        self.cursor_x, self.cursor_y = tx, ty

    async def click_at(self, tx: float, ty: float, *,
                       press_dwell_ms: tuple[int, int] = (40, 130)) -> None:
        """Press + release at (tx, ty). Assumes cursor is already there."""
        await self._mouse_event("mousePressed", tx, ty,
                                button="left", click_count=1)
        await asyncio.sleep(random.uniform(*press_dwell_ms) / 1000.0)
        # Tiny drift between press and release — real fingers move.
        drift_x = tx + random.uniform(-1.5, 1.5)
        drift_y = ty + random.uniform(-1.5, 1.5)
        if abs(drift_x - tx) > 0.1 or abs(drift_y - ty) > 0.1:
            await self._mouse_event("mouseMoved", drift_x, drift_y)
        await self._mouse_event("mouseReleased", drift_x, drift_y,
                                button="left", click_count=1)
        self.cursor_x, self.cursor_y = drift_x, drift_y

    async def click_element(self, css_selector: str, *,
                            scroll_into_view: bool = True,
                            settle_seconds: float = 0.2) -> bool:
        """Find element by CSS selector, scroll into view, mouse-bezier to a
        jittered point inside its bbox, click. Returns True if element found."""
        bbox = await self._bbox_of(css_selector, scroll_into_view=scroll_into_view)
        if not bbox:
            return False
        # Aim for a jittered point inside the middle 50% of the element,
        # not dead-center (centroid clicking is a known bot tell).
        margin_x = bbox["w"] * 0.25
        margin_y = bbox["h"] * 0.25
        tx = bbox["x"] + bbox["w"] / 2 + random.uniform(-margin_x, margin_x)
        ty = bbox["y"] + bbox["h"] / 2 + random.uniform(-margin_y, margin_y)
        await self.move_to(tx, ty)
        await asyncio.sleep(random.uniform(0.08, 0.28))
        await self.click_at(tx, ty)
        await asyncio.sleep(settle_seconds)
        return True

    async def _bbox_of(self, css_selector: str, *,
                       scroll_into_view: bool) -> dict | None:
        sel = json.dumps(css_selector)
        scroll_js = ("el.scrollIntoView({block:'center',behavior:'instant'}); "
                     if scroll_into_view else "")
        expression = (
            f"(() => {{ const el = document.querySelector({sel}); "
            f"if (!el) return null; {scroll_js}"
            "const r = el.getBoundingClientRect(); "
            "return {x: r.left, y: r.top, w: r.width, h: r.height}; })()"
        )
        return await self.session.eval_js(expression, await_promise=False)

    async def scroll(self, total_pixels: int, *, chunks: int = 0,
                     per_chunk_lo: float = 0.18, per_chunk_hi: float = 0.55) -> None:
        """Scroll by total_pixels via N small wheel events. Positive=down."""
        if total_pixels == 0:
            return
        if chunks <= 0:
            chunks = max(4, abs(total_pixels) // random.randint(110, 240))
        chunk_size = total_pixels / chunks
        # Scroll happens at the cursor's current position — make sure we have one.
        for _ in range(chunks):
            delta = chunk_size * random.uniform(0.65, 1.35)
            await self._mouse_event("mouseWheel",
                                    self.cursor_x, self.cursor_y,
                                    delta_y=delta)
            await asyncio.sleep(random.uniform(per_chunk_lo, per_chunk_hi))

    # ── typing ────────────────────────────────────────────────────

    async def focus(self, css_selector: str) -> bool:
        return await self.session.focus(css_selector)

    async def type_into(self, css_selector: str, text: str, *,
                        click_first: bool = True,
                        base_delay_ms: tuple[int, int] = (70, 180),
                        think_every_chars: tuple[int, int] = (24, 70),
                        think_pause_ms: tuple[int, int] = (380, 1400),
                        typo_rate: float = 0.015) -> bool:
        """Focus the composer (by clicking, then focusing as belt-and-suspenders),
        then type `text` character-by-character via Input.insertText with
        per-char delay. Backspace-based typos are sent via dispatchKeyEvent.

        Why per-char insertText (not dispatchKeyEvent for the text chars):
          X's composer is DraftJS, which listens for `input` events derived
          from beforeinput. dispatchKeyEvent char events sometimes drop on
          DraftJS in headless and reduced-permissions modes. insertText fires
          the right input events every time. Per-char insertText still defeats
          the "instant fill" detection vector that bulk insertText creates.

        Returns True on success, False if the composer couldn't be focused.
        """
        if click_first:
            ok = await self.click_element(css_selector, scroll_into_view=True)
            if not ok:
                return False
            await asyncio.sleep(random.uniform(0.15, 0.35))
        focused = await self.focus(css_selector)
        if not focused:
            return False

        next_think = random.randint(*think_every_chars)
        typed = 0
        for ch in text:
            # Occasional typo: type a wrong letter, then backspace, then carry on.
            if random.random() < typo_rate and ch.isalpha():
                wrong = random.choice("etaoinsrhdlcumg")
                if wrong.lower() == ch.lower():
                    wrong = "x" if wrong != "x" else "z"
                await self.session.insert_text(wrong)
                await asyncio.sleep(random.uniform(0.12, 0.32))
                await self._key_event("rawKeyDown", key="Backspace",
                                      code="Backspace", windows_vk=8)
                await self._key_event("keyUp", key="Backspace",
                                      code="Backspace", windows_vk=8)
                await asyncio.sleep(random.uniform(0.08, 0.22))

            # The real character. Insert via Input.insertText so DraftJS sees
            # a proper input event chain.
            await self.session.insert_text(ch)

            delay = random.uniform(*base_delay_ms) / 1000.0
            if ch in ".,!?:;":
                delay += random.uniform(0.10, 0.35)
            elif ch == " ":
                delay += random.uniform(-0.02, 0.05)
            elif ch == "\n":
                delay += random.uniform(0.18, 0.45)
            await asyncio.sleep(delay)

            typed += 1
            if typed >= next_think:
                await asyncio.sleep(random.uniform(*think_pause_ms) / 1000.0)
                next_think = typed + random.randint(*think_every_chars)
        return True

    # ── dwells ────────────────────────────────────────────────────

    async def dwell_to_read(self, text: str, *, wpm: int = 250,
                            floor_s: float = 0.4, ceil_s: float = 20.0) -> None:
        """Sleep proportional to how long a human would take to read `text`."""
        if not text:
            await asyncio.sleep(random.uniform(floor_s, max(floor_s + 0.2, 0.8)))
            return
        words = max(1, len(text.split()))
        seconds = words / random.uniform(wpm / 75.0, wpm / 55.0)
        seconds = max(floor_s, min(seconds, ceil_s))
        await asyncio.sleep(seconds)

    async def consider_dwell(self) -> None:
        """The 'should I really comment?' pause before clicking the post button."""
        await asyncio.sleep(random.uniform(1.4, 3.6))

    async def jitter(self, lo: float, hi: float) -> None:
        """Sleep a uniform-random duration in seconds."""
        await asyncio.sleep(random.uniform(lo, hi))

    # ── viewport helpers ──────────────────────────────────────────

    async def viewport_size(self) -> tuple[int, int]:
        v = await self.session.eval_js(
            "({w: window.innerWidth, h: window.innerHeight})",
            await_promise=False,
        )
        if not v:
            return (1280, 800)
        return int(v["w"]), int(v["h"])

    async def park_cursor_offscreen(self) -> None:
        """Move the cursor to a corner so it's not hovering over a clickable
        element during a dwell. Useful between candidates so X doesn't see a
        cursor camping on the same tweet for minutes."""
        w, h = await self.viewport_size()
        tx = random.uniform(20, 80)
        ty = random.uniform(h - 80, h - 20)
        await self.move_to(tx, ty)
