#!/usr/bin/env python3
"""cadence-tick.py — the human-rhythm driver. ONE cron job, every minute.

Each tick it asks _cadence whether a person would be acting right now. If
outbound is due it fires ONE feed-goodwill browse; if inbound is due it fires
ONE reply-check. Both go through the existing scripts (--live, one item), so all
the caps/windows/jitter/hold-buffer/humanization still apply underneath. This
REPLACES the flat goodwill/inbound/commenter crons.

Usage:
    scripts/cadence-tick.py                      # one tick now (fires if due)
    scripts/cadence-tick.py --dry-run            # decide + print, fire nothing
    scripts/cadence-tick.py --simulate 2026-06-22  # print a simulated human day
    scripts/cadence-tick.py --platform x --verbose
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cadence  # noqa: E402
import _metrics  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT_ROOT / "scripts"


def _fire(cmd: list[str], timeout: int, verbose: bool) -> int:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if verbose:
            print((p.stdout or "").strip()[-400:])
            if p.returncode != 0:
                print(f"[rc={p.returncode}] {(p.stderr or '').strip()[-300:]}", file=sys.stderr)
        return p.returncode
    except subprocess.TimeoutExpired:
        print(f"[cadence] fire timed out: {' '.join(cmd[:3])}…", file=sys.stderr)
        return 124


def run_tick(platform: str, dry_run: bool, verbose: bool) -> int:
    now = datetime.now().astimezone()
    fired = 0

    # OUTBOUND (feed goodwill) — only X has a feed-read/goodwill path today.
    # LinkedIn has no feed scraper yet, so there's nothing to fire for it.
    if platform == "x":
        act_out, why_out = _cadence.should_act_outbound(now, platform)
        if verbose or dry_run:
            print(f"[outbound] {'ACT' if act_out else 'skip'} — {why_out}")
        if act_out and not dry_run:
            _metrics.log_event("cadence_fire", platform=platform, mode="outbound")
            _fire([sys.executable, str(SCRIPTS / "feed-engagement.py"),
                   "--mode", "goodwill", "--max-visits", "1", "--limit", "1", "--live"],
                  timeout=300, verbose=verbose)
            fired += 1
    elif verbose or dry_run:
        print(f"[outbound] skip — no goodwill/feed path for {platform} yet")

    # INBOUND (replies to comments on your posts) — both platforms.
    due_in, why_in = _cadence.inbound_due(now, platform)
    if verbose or dry_run:
        print(f"[inbound]  {'CHECK' if due_in else 'skip'} — {why_in}")
    if due_in and not dry_run:
        _metrics.log_event("cadence_fire", platform=platform, mode="inbound")
        _fire([sys.executable, str(SCRIPTS / "inbound-engagement.py"),
               "--platforms", platform, "--limit", "3", "--live"],
              timeout=300, verbose=verbose)
        fired += 1

    if verbose:
        print(f"[cadence] fired {fired} action(s)")
    return 0


def simulate(date_str: str, platform: str) -> int:
    """Print a simulated day: the outbound plan + the realistic fire timeline."""
    tz = datetime.now().astimezone().tzinfo
    try:
        d = datetime.fromisoformat(date_str + "T12:00:00").replace(tzinfo=tz)
    except ValueError:
        print(f"bad --simulate date {date_str!r} (use YYYY-MM-DD)", file=sys.stderr)
        return 2

    print(_cadence.describe_day(d, platform))

    plan = _cadence.daily_plan(d, platform)
    if plan["skip_day"]:
        return 0

    # In-memory simulation of outbound fires across the day (no metrics needed):
    # walk each session minute-by-minute, honoring budget + a stable human gap.
    pc = (_cadence.cfg().get(platform, {}) or {}).get("outbound", {}) or {}
    fires: list[datetime] = []
    for s in plan["sessions"]:
        last: datetime | None = None
        done = 0
        t = s["start"]
        while t < s["end"] and done < s["budget"]:
            if last is None:
                fire = True
            else:
                gap = _cadence._stable_minutes(last, pc.get("gap_minutes", {"min": 5, "max": 15}))
                fire = (t - last).total_seconds() >= gap * 60
            if fire:
                fires.append(t)
                last = t
                done += 1
            t += timedelta(minutes=1)
    print("\n  Simulated outbound comment times:")
    print("    " + ("  ".join(f.strftime("%H:%M") for f in fires) if fires else "(none)"))
    print(f"\n  → {len(fires)} comments across {len(plan['sessions'])} session(s). "
          "Inbound replies fire separately, clustering after your posts get comments.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--platform", default="x")
    p.add_argument("--dry-run", action="store_true", help="Decide + print, fire nothing.")
    p.add_argument("--simulate", default=None, metavar="YYYY-MM-DD",
                   help="Print a simulated human day for this date; no side effects.")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    if args.simulate:
        return simulate(args.simulate, args.platform)
    try:
        return run_tick(args.platform, args.dry_run, args.verbose)
    except Exception as e:
        import traceback
        sys.stderr.write(f"[cadence-tick] failed: {e}\n")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
