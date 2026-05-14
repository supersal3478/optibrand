"""
human_actions — pretend-to-be-a-human primitives for Playwright pages.

What's emulated:
  • Mouse cursor moves along a 2-point cubic Bezier curve with ease-in/out timing.
  • Clicks dwell briefly between mouse-down and mouse-up, with sub-pixel jitter.
  • Typing uses per-character delays plus longer pauses at punctuation and
    occasional "thinking" pauses.
  • Scrolling uses many small wheel events instead of one big jump.
  • Dwell helpers model reading time and "consider before action" time.

Design notes:
  • Playwright doesn't expose the cursor's current position. We track it ourselves
    in a Mouse instance that lives on the page object as `page._human_mouse`.
  • All randomness is uniform but with biased ranges (e.g., typing is faster
    for common chars, slower at punctuation). Keep it cheap; this is plausibility,
    not perfect simulation.
  • Nothing here changes the DOM by itself. Use these primitives from skill code.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import Optional


def _bezier(p0, p1, p2, p3, t: float) -> tuple[float, float]:
    """Cubic Bezier evaluation at parameter t in [0, 1]."""
    one_t = 1 - t
    x = (one_t**3) * p0[0] + 3 * (one_t**2) * t * p1[0] + 3 * one_t * (t**2) * p2[0] + (t**3) * p3[0]
    y = (one_t**3) * p0[1] + 3 * (one_t**2) * t * p1[1] + 3 * one_t * (t**2) * p2[1] + (t**3) * p3[1]
    return x, y


def _ease_in_out(t: float) -> float:
    """Smooth-step easing — fast in the middle, slow at the ends."""
    return 0.5 - 0.5 * math.cos(math.pi * t)


@dataclass
class HumanMouse:
    """Tracks the simulated cursor position across calls."""
    x: float = 100.0
    y: float = 100.0

    def move_to(self, page, tx: float, ty: float, *, steps: int = 0,
                duration_ms: int = 0) -> None:
        """Move along a Bezier curve from (self.x, self.y) to (tx, ty).
        steps: how many points to sample; auto-derived from distance if 0.
        duration_ms: total time for the move; auto-derived from distance if 0.
        """
        dx, dy = tx - self.x, ty - self.y
        dist = math.hypot(dx, dy)
        if dist < 1:
            self.x, self.y = tx, ty
            page.mouse.move(tx, ty)
            return

        if steps <= 0:
            steps = max(20, min(80, int(dist / 8)))
        if duration_ms <= 0:
            # ~600ms per 500 px, with jitter
            duration_ms = int(dist * 1.2 + random.uniform(-80, 120))
            duration_ms = max(150, min(1400, duration_ms))

        # Two random control points biased toward the midpoint with perpendicular sway.
        mid_x, mid_y = (self.x + tx) / 2, (self.y + ty) / 2
        perp_x, perp_y = -dy / dist, dx / dist  # unit perpendicular
        sway = random.uniform(-dist * 0.18, dist * 0.18)
        c1 = (self.x + dx * 0.25 + perp_x * sway, self.y + dy * 0.25 + perp_y * sway)
        c2 = (mid_x + dx * 0.25 + perp_x * sway * 0.4, mid_y + dy * 0.25 + perp_y * sway * 0.4)

        per_step = duration_ms / 1000.0 / steps
        for i in range(1, steps + 1):
            t = _ease_in_out(i / steps)
            x, y = _bezier((self.x, self.y), c1, c2, (tx, ty), t)
            # Small jitter so the path is never perfectly smooth.
            jx = random.uniform(-0.5, 0.5)
            jy = random.uniform(-0.5, 0.5)
            page.mouse.move(x + jx, y + jy)
            time.sleep(per_step)
        self.x, self.y = tx, ty


def _get_mouse(page) -> HumanMouse:
    """Lazily attach a HumanMouse to the page object."""
    m = getattr(page, "_human_mouse", None)
    if m is None:
        m = HumanMouse()
        page._human_mouse = m
    return m


# ────────────────────────────────────────────── public API ──────


def dwell(lo: float, hi: float) -> None:
    """Sleep for a uniform-random duration in seconds."""
    time.sleep(random.uniform(lo, hi))


def read_dwell(text: str) -> None:
    """Sleep proportional to how long a human would take to read `text`.
    Assumes 250 wpm (4 words/sec) with 0.8–1.4× variance and 0.4s floor."""
    if not text:
        time.sleep(random.uniform(0.4, 0.8))
        return
    words = max(1, len(text.split()))
    seconds = words / random.uniform(3.2, 4.5)
    seconds = max(0.4, min(seconds, 20.0))
    time.sleep(seconds)


def consider_dwell() -> None:
    """The 'should I really comment?' pause before clicking the post button."""
    time.sleep(random.uniform(1.4, 3.6))


def smooth_scroll(page, total_pixels: int, *, chunks: int = 0,
                  per_chunk_lo: float = 0.18, per_chunk_hi: float = 0.55) -> None:
    """Scroll by total_pixels via many small wheel events. Mostly downward;
    pass negative to scroll up."""
    if chunks <= 0:
        chunks = max(4, abs(total_pixels) // random.randint(120, 280))
    chunk_size = total_pixels / chunks
    for _ in range(chunks):
        # Vary each chunk so it's not arithmetically uniform.
        delta = chunk_size * random.uniform(0.7, 1.3)
        page.mouse.wheel(0, delta)
        time.sleep(random.uniform(per_chunk_lo, per_chunk_hi))


def human_click(page, element, *, dwell_ms_lo: int = 80, dwell_ms_hi: int = 280) -> None:
    """Move the simulated cursor to the element along a Bezier path, then
    click — with a small mouse-down/up dwell and sub-pixel offset on release."""
    bbox = element.bounding_box()
    if not bbox:
        # Fall back to direct click — element may have no layout (off-screen?).
        element.click()
        return
    # Target a slightly randomized point within the element, not dead center.
    margin_x = bbox["width"] * 0.25
    margin_y = bbox["height"] * 0.25
    tx = bbox["x"] + bbox["width"] / 2 + random.uniform(-margin_x, margin_x)
    ty = bbox["y"] + bbox["height"] / 2 + random.uniform(-margin_y, margin_y)

    mouse = _get_mouse(page)
    mouse.move_to(page, tx, ty)
    time.sleep(random.uniform(dwell_ms_lo, dwell_ms_hi) / 1000.0)

    page.mouse.down()
    time.sleep(random.uniform(0.04, 0.12))
    # Small drift between down and up — real mice don't stay perfectly still.
    drift_x = tx + random.uniform(-1.2, 1.2)
    drift_y = ty + random.uniform(-1.2, 1.2)
    page.mouse.move(drift_x, drift_y)
    page.mouse.up()
    mouse.x, mouse.y = drift_x, drift_y


def human_type(page, text: str, *, base_delay_ms: tuple[int, int] = (70, 180),
               think_every_chars: tuple[int, int] = (24, 70),
               think_pause_ms: tuple[int, int] = (380, 1400),
               typo_rate: float = 0.012) -> None:
    """Type `text` into the currently-focused element character by character,
    with realistic per-char delays, longer pauses at punctuation, occasional
    'thinking' pauses, and a small chance of typo+backspace correction.

    Caller is responsible for ensuring the right element has focus
    (typically by `human_click(page, comment_input_element)` first).
    """
    next_think = random.randint(*think_every_chars)
    typed = 0
    for ch in text:
        if random.random() < typo_rate and ch.isalpha():
            # Type a wrong char, hold, then backspace.
            wrong = random.choice("etaoinsrhdlcumg")
            page.keyboard.type(wrong, delay=random.uniform(*base_delay_ms))
            time.sleep(random.uniform(0.12, 0.32))
            page.keyboard.press("Backspace")
            time.sleep(random.uniform(0.08, 0.22))
        delay = random.uniform(*base_delay_ms) / 1000.0
        if ch in ".,!?:;":
            delay += random.uniform(0.10, 0.35)
        elif ch == " ":
            delay += random.uniform(-0.02, 0.04)
        elif ch == "\n":
            delay += random.uniform(0.18, 0.45)
        page.keyboard.type(ch)
        time.sleep(delay)
        typed += 1
        if typed >= next_think:
            time.sleep(random.uniform(*think_pause_ms) / 1000.0)
            next_think = typed + random.randint(*think_every_chars)
