"""_metrics — append-only engagement event log + read-side counters for caps.

Single JSONL at ~/.hermes/logs/engagement_metrics.jsonl. One event per line:

    {"ts": "2026-06-17T14:23:08-04:00", "event": "submitted",
     "platform": "x", "mode": "inbound", "parent_url": "...", ...}

Event types used by the engagement scripts:

    drafted               — reply-drafter (or inlined draft) produced text
    vetoed_brandguard     — brand-guard refused; includes `reason`
    queued_outbox         — draft enqueued; includes `release_at`
    released_outbox       — outbox-flush picked the item up
    submitted             — Reply button clicked successfully
    submit_failed         — click happened but no confirmation toast
    commenter_queued      — added a commenter to the followup queue
    commenter_engaged     — drafted a goodwill reply on a commenter's post
    skipped_variance      — fatigue / probability gate fired
    skipped_window        — outside windows.yaml — pushed to outbox or dropped
    skipped_cap           — caps.yaml daily limit hit
    skipped_blocklist     — blocklist or pinned-post or self-thread filter hit
    skipped_kill_switch   — caps.yaml live: false at submit time
    cdp_error             — anything that bubbled up from the CDP layer

Caller can attach arbitrary `**fields`; they all land in the JSON object.

Counters read the same JSONL. They're O(today's events), which is small (max
a few hundred lines per day), so no index. If the file ever grows past ~10MB
we'll rotate; for now keep it simple.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, time
from pathlib import Path
from typing import Any, Iterable

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
METRICS_PATH = HERMES_HOME / "logs" / "engagement_metrics.jsonl"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log_event(event: str, *, platform: str = "x", mode: str | None = None,
              **fields: Any) -> None:
    """Append one event line to the metrics JSONL. Fire-and-forget; failures
    do not raise — they degrade silently and print to stderr instead, so a
    broken log doesn't break the engagement loop."""
    row = {"ts": _now_iso(), "event": event, "platform": platform}
    if mode is not None:
        row["mode"] = mode
    row.update(fields)
    try:
        METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with METRICS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:  # pragma: no cover — log-write failure is degraded mode
        import sys
        print(f"[metrics] write failed: {e}", file=sys.stderr)


def iter_events(since: datetime | None = None) -> Iterable[dict]:
    """Yield events from the metrics JSONL, optionally filtered by ts >= since.

    Robust to truncated last lines (returns what it can)."""
    if not METRICS_PATH.exists():
        return
    cutoff_iso = since.astimezone().isoformat(timespec="seconds") if since else None
    with METRICS_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff_iso and row.get("ts", "") < cutoff_iso:
                continue
            yield row


def _start_of_local_day() -> datetime:
    now = datetime.now().astimezone()
    return datetime.combine(now.date(), time(0, 0, 0), tzinfo=now.tzinfo)


def count_today(event: str, *, platform: str | None = None,
                mode: str | None = None) -> int:
    """Count events matching event/platform/mode in today's local-day window."""
    start = _start_of_local_day()
    n = 0
    for row in iter_events(since=start):
        if row.get("event") != event:
            continue
        if platform is not None and row.get("platform") != platform:
            continue
        if mode is not None and row.get("mode") != mode:
            continue
        n += 1
    return n


def length_distribution(platform: str = "x", since: datetime | None = None) -> dict:
    """Tally drafted-event length_target values, return counts + percentages.

    Used to verify the 50%/25%/25% (7w/15w/25w) distribution is actually
    being hit in practice — random per-draft assignment plus small sample
    sizes can drift, so we audit empirically.
    """
    if since is None:
        since = _start_of_local_day()
    counts: dict[str, int] = {}
    autofix_counts: dict[str, int] = {}
    word_counts: list[int] = []
    for row in iter_events(since=since):
        if row.get("event") != "drafted" or row.get("platform") != platform:
            continue
        target = row.get("length_target") or "unspecified"
        counts[target] = counts.get(target, 0) + 1
        wc = row.get("word_count")
        if isinstance(wc, int):
            word_counts.append(wc)
        for fix in (row.get("autofixes") or []):
            autofix_counts[fix] = autofix_counts.get(fix, 0) + 1
    total = sum(counts.values()) or 1
    pct = {k: round(100 * v / total, 1) for k, v in counts.items()}
    return {
        "drafts_total": sum(counts.values()),
        "counts_by_target": counts,
        "pct_by_target": pct,
        "autofix_counts": autofix_counts,
        "median_word_count": (sorted(word_counts)[len(word_counts)//2]
                              if word_counts else None),
    }


def today_summary(platform: str = "x") -> dict[str, int]:
    """Compact summary of all event counts today for the given platform.
    Used by daily-report.py to roll up engagement activity."""
    start = _start_of_local_day()
    counts: dict[str, int] = {}
    for row in iter_events(since=start):
        if row.get("platform") != platform:
            continue
        ev = row.get("event", "unknown")
        counts[ev] = counts.get(ev, 0) + 1
    return counts


def cli() -> None:
    """`python -m _metrics today` — print today's event counts.
    `python -m _metrics distribution` — print today's length-target distribution."""
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["today", "distribution"])
    p.add_argument("--platform", default="x")
    args = p.parse_args()
    if args.cmd == "today":
        print(json.dumps(today_summary(args.platform), indent=2))
    elif args.cmd == "distribution":
        print(json.dumps(length_distribution(args.platform), indent=2))


if __name__ == "__main__":
    cli()
