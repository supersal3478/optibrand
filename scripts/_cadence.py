"""_cadence — human-rhythm scheduler (pure logic, no browser).

Decides WHEN to act so the engagement loop looks like a person:
  • Outbound (feed goodwill): 1-2 bursty SESSIONS a day at jittered times, a
    handful of comments total, with human gaps between them — not an all-day
    flat poll. Some days it skips entirely.
  • Inbound (replies on your posts): an adaptive DECAY — checks densely while
    there's fresh comment activity, tapers over the next hours, then drops to a
    background glance a couple times a day. No 24/7 every-10-minutes polling.

State comes from the metrics log (today's `queued_outbox` counts = budget used;
`cadence_fire` events = when we last acted), so there are no fragile new state
files and it survives restarts. The daily plan is DETERMINISTIC per local day
from a per-install salt, so every minute-tick recomputes the same plan, but it
varies day-to-day and per machine.

Config: config/cadence.yaml (knobs) + config/windows.yaml (active hours).
"""
from __future__ import annotations

import hashlib
import os
import random
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

import yaml

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _metrics  # noqa: E402
import _caps  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
CADENCE_PATH = PROJECT_ROOT / "config" / "cadence.yaml"
SALT_PATH = HERMES_HOME / "state" / "cadence_salt"


# ───────────────────────────────── config + seed ──

def cfg() -> dict:
    try:
        return yaml.safe_load(CADENCE_PATH.read_text()) or {}
    except FileNotFoundError:
        return {}


def _salt() -> str:
    """Per-install random salt so two machines don't share a daily plan."""
    try:
        return SALT_PATH.read_text().strip()
    except FileNotFoundError:
        s = hashlib.sha256(os.urandom(16)).hexdigest()[:16]
        try:
            SALT_PATH.parent.mkdir(parents=True, exist_ok=True)
            SALT_PATH.write_text(s)
        except OSError:
            pass
        return s


def _rng(date_key: str, *parts: str) -> random.Random:
    seed = int(hashlib.sha256((":".join((_salt(), date_key, *parts))).encode()).hexdigest(), 16)
    return random.Random(seed % (2**32))


def _rng_int(r: random.Random, spec: dict) -> int:
    return r.randint(int(spec["min"]), int(spec["max"]))


def _stable_minutes(anchor: datetime | None, spec: dict) -> float:
    """A gap/interval that's fixed for a given anchor time (so it doesn't change
    every tick) — derived from the anchor, in minutes."""
    lo, hi = float(spec["min"]), float(spec["max"])
    if anchor is None:
        return lo
    h = int(hashlib.sha256(anchor.isoformat().encode()).hexdigest(), 16)
    return lo + (h % 1000) / 1000.0 * (hi - lo)


# ───────────────────────────────── recovery ramp ──

def recovery_stage(now: datetime) -> dict | None:
    """The active recovery-ramp stage from cadence.yaml, or None when the ramp
    is disabled, hasn't started, or has completed. Platform-independent: the
    same stage applies to every platform."""
    rc = cfg().get("recovery") or {}
    if not rc.get("enabled") or not rc.get("start_date"):
        return None
    try:
        start = datetime.fromisoformat(str(rc["start_date"])).date()
    except ValueError:
        return None
    days = (now.date() - start).days
    if days < 0:
        return None
    week = days // 7
    acc = 0
    for i, stage in enumerate(rc.get("stages") or []):
        acc += int(stage.get("weeks", 1))
        if week < acc:
            return {**stage, "_stage_num": i + 1, "_week": week + 1}
    return None


# Range keys where a LARGER value means LESS activity (waiting intervals/gaps),
# vs. volume keys (sessions, actions, session length) where larger = more.
_INTERVAL_KEYS = {"dense_minutes", "taper_minutes", "quiet_minutes", "gap_minutes"}


def _merge_reduce_only(base: dict, override: dict) -> dict:
    """Apply a recovery override on top of a platform's baseline config, with
    the invariant that recovery can only REDUCE activity: volume ranges are
    element-wise min'd against the baseline, interval/gap ranges element-wise
    max'd (longer waits = quieter), and skip_day_prob is max'd. Keys the
    baseline doesn't have (e.g. force_tier) pass through as-is."""
    merged = dict(base)
    for k, v in override.items():
        if k in ("enabled", "_stage_num", "_week"):
            continue
        b = base.get(k)
        if isinstance(v, dict) and isinstance(b, dict) and "min" in v and "min" in b:
            pick = max if k in _INTERVAL_KEYS else min
            merged[k] = {"min": pick(v["min"], b["min"]), "max": pick(v["max"], b["max"])}
        elif k == "skip_day_prob" and b is not None:
            merged[k] = max(float(v), float(b))
        else:
            merged[k] = v
    return merged


def _effective_cfg(now: datetime, platform: str, mode: str) -> tuple[dict, dict | None]:
    """(platform mode-config with any active recovery stage applied, stage)."""
    pc = (cfg().get(platform, {}) or {}).get(mode, {}) or {}
    stage = recovery_stage(now)
    if stage and isinstance(stage.get(mode), dict):
        pc = _merge_reduce_only(pc, stage[mode])
    return pc, stage


# ───────────────────────────────── rate-limit circuit breaker ──

def rate_limit_cooldown(now: datetime, platform: str) -> str | None:
    """If a `rate_limited` event was logged recently, return a human-readable
    reason to stand down; otherwise None. Cooldown length is stable per event
    (derived from its timestamp): base–2×base hours, base from caps.yaml
    <platform>.reads.rate_limit_cooldown_hours (default 3)."""
    last_rl = _last_ts(now, "rate_limited", platform, None)
    if last_rl is None:
        return None
    base_h = float((_caps.caps().get(platform, {}).get("reads", {}) or {})
                   .get("rate_limit_cooldown_hours", 3))
    cool_min = _stable_minutes(last_rl, {"min": base_h * 60, "max": base_h * 120})
    elapsed = (now - last_rl).total_seconds() / 60
    if elapsed < cool_min:
        return (f"rate-limited {elapsed/60:.1f}h ago — standing down "
                f"{(cool_min - elapsed)/60:.1f}h more")
    return None


def _views_exhausted(platform: str) -> str | None:
    """If today's page-view budget (caps.yaml <platform>.reads.page_views_per_day)
    is used up, return a reason string; None when under budget or no cap set."""
    cap = int((_caps.caps().get(platform, {}).get("reads", {}) or {})
              .get("page_views_per_day", 0))
    if cap <= 0:
        return None
    used = _metrics.page_views_today(platform)
    if used >= cap:
        return f"view budget reached ({used}/{cap} page views today)"
    return None


# ───────────────────────────────── windows ──

def _window(platform: str, mode: str) -> tuple[set[int], int, int]:
    """(allowed weekdays, start_minute_of_day, end_minute_of_day) from windows.yaml."""
    w = (_caps.windows().get(platform, {}) or {}).get(mode, {}) or {}
    days = set(w.get("days") or [0, 1, 2, 3, 4, 5, 6])

    def _mins(s: str, default: int) -> int:
        try:
            hh, mm = str(s).split(":")
            return int(hh) * 60 + int(mm)
        except Exception:
            return default
    return days, _mins(w.get("start", "00:00"), 0), _mins(w.get("end", "23:59"), 1439)


# ───────────────────────────────── metrics helpers ──

def _today_start(now: datetime) -> datetime:
    return datetime.combine(now.date(), dtime.min, tzinfo=now.tzinfo)


def _count_today(now: datetime, event: str, platform: str, mode: str) -> int:
    start = _today_start(now)
    n = 0
    for ev in _metrics.iter_events(since=start):
        if ev.get("event") == event and ev.get("platform") == platform and ev.get("mode") == mode:
            n += 1
    return n


def _last_ts(now: datetime, event: str, platform: str, mode: str) -> datetime | None:
    """Most recent ts for event/platform/mode within the last ~2 days."""
    start = now - timedelta(days=2)
    latest: datetime | None = None
    for ev in _metrics.iter_events(since=start):
        if ev.get("event") == event and ev.get("platform") == platform and ev.get("mode") == mode:
            try:
                ts = datetime.fromisoformat(ev.get("ts", ""))
            except ValueError:
                continue
            if latest is None or ts > latest:
                latest = ts
    return latest


# ───────────────────────────────── outbound: daily plan ──

def daily_plan(now: datetime, platform: str) -> dict:
    """Today's outbound (goodwill) plan: skip-day flag + session windows + budget.
    Deterministic for (date, platform, machine)."""
    pc, stage = _effective_cfg(now, platform, "outbound")
    date_key = now.strftime("%Y-%m-%d")
    r = _rng(date_key, platform, "outbound")
    days, wstart, wend = _window(platform, "outbound")

    plan = {"skip_day": False, "sessions": [], "daily_budget": 0, "recovery_off": False}
    if stage and stage.get("outbound", {}).get("enabled") is False:
        plan["skip_day"] = True
        plan["recovery_off"] = True
        plan["recovery_stage"] = stage.get("_stage_num")
        return plan
    if now.weekday() not in days:
        plan["skip_day"] = True
        return plan
    if r.random() < float(pc.get("skip_day_prob", 0.15)):
        plan["skip_day"] = True
        return plan

    n_sessions = _rng_int(r, pc.get("sessions_per_day", {"min": 1, "max": 2}))
    plan["daily_budget"] = _rng_int(r, pc.get("daily_actions", {"min": 3, "max": 8}))
    span = max(60, wend - wstart)
    chunk = span // max(1, n_sessions)
    budget_left = plan["daily_budget"]
    for i in range(n_sessions):
        length = _rng_int(r, pc.get("session_minutes", {"min": 25, "max": 60}))
        chunk_start = wstart + i * chunk
        latest_start = max(chunk_start, chunk_start + chunk - length)
        start_min = r.randint(chunk_start, max(chunk_start, latest_start))
        start_min = min(start_min, wend - 5)
        s_start = datetime.combine(now.date(), dtime.min, tzinfo=now.tzinfo) + timedelta(minutes=start_min)
        s_end = s_start + timedelta(minutes=length)
        # split the day's budget across sessions (last session takes the remainder)
        sess_budget = budget_left if i == n_sessions - 1 else max(1, round(plan["daily_budget"] / n_sessions))
        sess_budget = min(sess_budget, budget_left)
        budget_left -= sess_budget
        plan["sessions"].append({"start": s_start, "end": s_end, "budget": sess_budget})
    return plan


def _current_session(now: datetime, plan: dict) -> dict | None:
    for s in plan["sessions"]:
        if s["start"] <= now < s["end"]:
            return s
    return None


def should_act_outbound(now: datetime, platform: str) -> tuple[bool, str]:
    if not _caps.is_live(platform):
        return False, "platform not live"
    cool = rate_limit_cooldown(now, platform)
    if cool:
        return False, cool
    views = _views_exhausted(platform)
    if views:
        return False, views
    if not _caps.in_window(platform, "outbound"):
        return False, "outside outbound window"
    plan = daily_plan(now, platform)
    if plan.get("recovery_off"):
        return False, f"recovery ramp stage {plan.get('recovery_stage')}: outbound disabled"
    if plan["skip_day"]:
        return False, "skip day (no engagement today)"
    sess = _current_session(now, plan)
    if not sess:
        return False, "between sessions"
    done = _count_today(now, "queued_outbox", platform, "goodwill")
    if done >= plan["daily_budget"]:
        return False, f"daily budget reached ({done}/{plan['daily_budget']})"
    if not _caps.under_cap(platform, "outbound"):
        return False, "caps.yaml daily cap reached"
    pc, _ = _effective_cfg(now, platform, "outbound")
    last_fire = _last_ts(now, "cadence_fire", platform, "outbound")
    if last_fire is not None:
        gap = _stable_minutes(last_fire, pc.get("gap_minutes", {"min": 5, "max": 15}))
        if (now - last_fire).total_seconds() < gap * 60:
            return False, f"within human gap ({gap:.0f}m)"
    return True, "act (in session, budget + gap ok)"


# ───────────────────────────────── inbound: adaptive decay ──

def inbound_due(now: datetime, platform: str) -> tuple[bool, str]:
    if not _caps.is_live(platform):
        return False, "platform not live"
    cool = rate_limit_cooldown(now, platform)
    if cool:
        return False, cool
    views = _views_exhausted(platform)
    if views:
        return False, views
    if not _caps.in_window(platform, "inbound"):
        return False, "outside inbound window"
    pc, _ = _effective_cfg(now, platform, "inbound")
    done = _count_today(now, "queued_outbox", platform, "inbound")
    budget = _rng_int(_rng(now.strftime("%Y-%m-%d"), platform, "inbound"),
                      pc.get("daily_actions", {"min": 0, "max": 12}))
    if done >= budget:
        return False, f"daily inbound budget reached ({done}/{budget})"

    # Intensity tier = how recently there was fresh comment activity (a drafted
    # reply means there WAS a new comment). Dense right after, then taper, then a
    # quiet background glance.
    last_activity = _last_ts(now, "queued_outbox", platform, "inbound")
    fresh_h = float(pc.get("fresh_window_hours", 2))
    taper_h = float(pc.get("taper_window_hours", 10))
    if pc.get("force_tier") == "quiet":
        tier, spec = "quiet (recovery)", pc.get("quiet_minutes", {"min": 150, "max": 240})
    elif last_activity is not None and (now - last_activity) < timedelta(hours=fresh_h):
        tier, spec = "dense", pc.get("dense_minutes", {"min": 12, "max": 25})
    elif last_activity is not None and (now - last_activity) < timedelta(hours=fresh_h + taper_h):
        tier, spec = "taper", pc.get("taper_minutes", {"min": 45, "max": 90})
    else:
        tier, spec = "quiet", pc.get("quiet_minutes", {"min": 150, "max": 240})

    last_fire = _last_ts(now, "cadence_fire", platform, "inbound")
    interval = _stable_minutes(last_fire, spec)
    if last_fire is not None and (now - last_fire).total_seconds() < interval * 60:
        return False, f"{tier}: within {interval:.0f}m interval"
    return True, f"check ({tier} tier, every ~{interval:.0f}m)"


# ───────────────────────────────── describe (for --simulate) ──

def describe_day(date: datetime, platform: str) -> str:
    """Human-readable simulation of a day's outbound plan + inbound tiers."""
    plan = daily_plan(date, platform)
    lines = [f"=== {platform} cadence — {date.strftime('%Y-%m-%d (%a)')} ==="]
    stage = recovery_stage(date)
    if stage:
        lines.append(f"  RECOVERY RAMP: stage {stage['_stage_num']}, week {stage['_week']} "
                     "(activity reduced; see cadence.yaml recovery block)")
    if plan.get("recovery_off"):
        lines.append("  OUTBOUND: disabled by recovery ramp — no goodwill this stage.")
    elif plan["skip_day"]:
        lines.append("  OUTBOUND: skip day — no feed goodwill today.")
    else:
        lines.append(f"  OUTBOUND: {len(plan['sessions'])} session(s), "
                     f"~{plan['daily_budget']} comments total")
        for i, s in enumerate(plan["sessions"], 1):
            lines.append(f"    session {i}: {s['start'].strftime('%H:%M')}–"
                         f"{s['end'].strftime('%H:%M')}  (up to {s['budget']} comments, "
                         f"~5-15m apart)")
    pc, _ = _effective_cfg(date, platform, "inbound")
    lines.append("  INBOUND (decay, anchored to fresh comment activity):")
    if pc.get("force_tier") == "quiet":
        lines.append("    recovery: quiet tier FORCED — no dense polling this stage")
    lines.append(f"    dense  ~{pc.get('dense_minutes',{}).get('min','?')}-"
                 f"{pc.get('dense_minutes',{}).get('max','?')}m  for first "
                 f"{pc.get('fresh_window_hours','?')}h of activity")
    lines.append(f"    taper  ~{pc.get('taper_minutes',{}).get('min','?')}-"
                 f"{pc.get('taper_minutes',{}).get('max','?')}m  next "
                 f"{pc.get('taper_window_hours','?')}h")
    lines.append(f"    quiet  ~{pc.get('quiet_minutes',{}).get('min','?')}-"
                 f"{pc.get('quiet_minutes',{}).get('max','?')}m  background glance")
    return "\n".join(lines)
