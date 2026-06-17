"""_caps — read caps.yaml / windows.yaml / jitter.yaml fresh on every call.

The whole point of these YAML files is that flipping a value (`x.live: false`,
`replies_per_day: 0`) halts the loop on the next tick without restarting any
daemon. To preserve that property, every read goes through this module and
hits disk every time. No caching. No memoization. Cost is negligible (~1ms
per read) compared to a 90s polling interval.

Surface area used by the engagement scripts:

  get_phase()                        → int  (master gate; 0 means nothing posts)
  is_live(platform)                  → bool (caps.<platform>.live)
  cap_value(platform, mode)          → int  (replies_per_day / comments_per_day)
  under_cap(platform, mode, event)   → bool (count_today(event) < cap_value)
  in_window(platform, mode)          → bool (windows.yaml says now is allowed)
  random_jitter_seconds(p, m)        → float (uniform pick from jitter.yaml)
  between_job_jitter_seconds()       → float (global cron jitter)
  hold_buffer_seconds(p, m)          → int  (caps.hold_buffer_*_seconds)
  relevance_min(platform, mode)      → float (caps.<p>.outbound.relevance_min)

`platform` is "x" / "linkedin" / "youtube". `mode` is "inbound" / "outbound".

The "commenter_followup" engagement mode counts against `outbound` caps —
it's outbound goodwill on third-party accounts, just triggered by the
inbound event of someone commenting on your post.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, time
from pathlib import Path
from random import uniform
from typing import Any
from zoneinfo import ZoneInfo

try:
    import yaml
except ImportError:
    sys.stderr.write("pyyaml required (installed by setup.sh into the Hermes venv)\n")
    sys.exit(2)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CAPS_PATH = PROJECT_ROOT / "config" / "caps.yaml"
WINDOWS_PATH = PROJECT_ROOT / "config" / "windows.yaml"
JITTER_PATH = PROJECT_ROOT / "config" / "jitter.yaml"


# ────────────────────────────────────────────── raw reads ──


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def caps() -> dict:
    return _load(CAPS_PATH)


def windows() -> dict:
    return _load(WINDOWS_PATH)


def jitter() -> dict:
    return _load(JITTER_PATH)


# ────────────────────────────────────────────── high-level gates ──


def get_phase() -> int:
    """Master gate. 0 = nothing posts; 2 = inbound; 3 = X outbound; etc."""
    return int(caps().get("phase", 0))


def is_live(platform: str) -> bool:
    """caps.<platform>.live — the per-platform kill switch."""
    return bool(caps().get(platform, {}).get("live", False))


def cap_value(platform: str, mode: str) -> int:
    """The integer daily cap for this platform/mode.

    Caps schema (caps.yaml):
        x.inbound.replies_per_day
        x.outbound.comments_per_day
        linkedin.inbound.replies_per_day
        linkedin.outbound.comments_per_day
        youtube.inbound.replies_per_day

    For commenter_followup we count against outbound caps because that's
    the activity type (we're commenting on a third party's post).
    """
    c = caps().get(platform, {})
    if mode == "inbound":
        return int(c.get("inbound", {}).get("replies_per_day", 0))
    # outbound and commenter_followup
    obs = c.get("outbound", {})
    return int(obs.get("comments_per_day", obs.get("replies_per_day", 0)))


def under_cap(platform: str, mode: str, event: str = "submitted") -> bool:
    """Have we used up today's cap? Reads metrics jsonl for today's
    `event` count. For inbound→inbound, outbound→outbound."""
    from _metrics import count_today
    used = count_today(event, platform=platform, mode=_cap_mode(mode))
    limit = cap_value(platform, _cap_mode(mode))
    return used < limit


def _cap_mode(mode: str) -> str:
    """Map orchestrator-level modes to cap-schema modes."""
    if mode in ("inbound",):
        return "inbound"
    # goodwill / outbound / commenter_followup all count as outbound
    return "outbound"


# ────────────────────────────────────────────── windows ──


def _local_tz() -> ZoneInfo:
    tz_name = windows().get("timezone") or "America/Toronto"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("America/Toronto")


def now_local() -> datetime:
    return datetime.now(_local_tz())


def in_window(platform: str, mode: str) -> bool:
    """True if the current local time falls inside the configured window."""
    section = windows().get(platform, {}).get(_cap_mode(mode), {})
    if not section:
        return True  # no window defined → always allowed

    now = now_local()
    days_allowed = section.get("days") or []
    if days_allowed and now.weekday() not in [int(d) for d in days_allowed]:
        return False

    start_str = section.get("start", "00:00")
    end_str = section.get("end", "23:59")
    sh, sm = (int(x) for x in start_str.split(":"))
    eh, em = (int(x) for x in end_str.split(":"))
    start_t = time(sh, sm)
    end_t = time(eh, em)
    now_t = now.time().replace(second=0, microsecond=0)
    return start_t <= now_t <= end_t


# ────────────────────────────────────────────── jitter ──


def random_jitter_seconds(platform: str, mode: str) -> float:
    """Pull a random delay from jitter.yaml for this platform/mode."""
    section = jitter().get(platform, {}).get(_cap_mode(mode), {})
    lo = float(section.get("min", 0))
    hi = float(section.get("max", lo))
    if hi <= lo:
        return lo
    return uniform(lo, hi)


def between_job_jitter_seconds() -> float:
    """Global between-job jitter applied on top of cron interval."""
    section = jitter().get("between_job_secs", {})
    lo = float(section.get("min", 0))
    hi = float(section.get("max", lo))
    if hi <= lo:
        return lo
    return uniform(lo, hi)


# ────────────────────────────────────────────── hold buffer ──


def hold_buffer_seconds(platform: str, mode: str) -> int:
    """How long a draft sits in the outbox before submission. Master safety net
    — flipping `live: false` during this window cancels the submission."""
    c = caps()
    if mode == "inbound":
        return int(c.get("hold_buffer_inbound_seconds", 1800))
    if platform == "youtube":
        return int(c.get("hold_buffer_youtube_seconds", 0))
    return int(c.get("hold_buffer_outbound_seconds", 300))


# ────────────────────────────────────────────── relevance ──


def relevance_min(platform: str, mode: str) -> float:
    """Outbound only — relevance score below this → skip."""
    section = caps().get(platform, {}).get(_cap_mode(mode), {})
    return float(section.get("relevance_min", 0.0))


# ────────────────────────────────────────────── debug print ──


def status_dict(platform: str = "x") -> dict[str, Any]:
    return {
        "phase": get_phase(),
        "live": is_live(platform),
        "inbound_cap": cap_value(platform, "inbound"),
        "outbound_cap": cap_value(platform, "outbound"),
        "hold_buffer_inbound": hold_buffer_seconds(platform, "inbound"),
        "hold_buffer_outbound": hold_buffer_seconds(platform, "outbound"),
        "inbound_window_ok": in_window(platform, "inbound"),
        "outbound_window_ok": in_window(platform, "outbound"),
        "now_local": now_local().isoformat(timespec="seconds"),
    }


def cli() -> None:
    """`python -m _caps status` — print the live policy snapshot."""
    import argparse
    import json as _json
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["status"])
    p.add_argument("--platform", default="x")
    args = p.parse_args()
    print(_json.dumps(status_dict(args.platform), indent=2))


if __name__ == "__main__":
    cli()
