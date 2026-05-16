#!/usr/bin/env python3
"""Generate the daily operations report.

Reads ~/.hermes/logs/audit.jsonl for the last 24h (or a user-specified date),
aggregates per-platform, and writes a markdown report to ~/.hermes/reports/.

Audit log shape (one JSON object per line):
    {"ts": "2026-05-15T14:23:01Z", "action": "linkedin_reply_posted",
     "platform": "linkedin", "post_id": "urn:...",
     "draft": "...", "result": "ok"|"failed"|"vetoed",
     "veto_reasons": [...], "cost_usd": 0.0023}

The audit log doesn't exist yet on a fresh setup — this script handles that case
by emitting an "empty day" report. Once the engagement/monitor cron jobs are
wired (next milestone), they'll append to this log and the report will fill in.

Usage:
    scripts/daily-report.py              # today
    scripts/daily-report.py 2026-05-15   # specific day
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
AUDIT_LOG = HERMES_HOME / "logs" / "audit.jsonl"
REPORTS_DIR = HERMES_HOME / "reports"


def parse_day(arg: str | None) -> date:
    if arg is None:
        return date.today()
    return date.fromisoformat(arg)


def load_entries(target_day: date) -> list[dict]:
    if not AUDIT_LOG.exists():
        return []
    start = datetime.combine(target_day, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    out: list[dict] = []
    with AUDIT_LOG.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_raw = rec.get("ts")
            if not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if start <= ts < end:
                out.append(rec)
    return out


def render(target_day: date, entries: list[dict]) -> str:
    lines: list[str] = []
    lines.append(f"# Daily report — {target_day.isoformat()}")
    lines.append("")
    if not entries:
        lines.append("_No audit-log entries for this day. Either the daemon wasn't running, or no actions occurred._")
        lines.append("")
        lines.append(f"(Log path: `{AUDIT_LOG}`)")
        return "\n".join(lines)

    by_action: Counter = Counter()
    by_platform: dict[str, Counter] = defaultdict(Counter)
    cost_by_platform: dict[str, float] = defaultdict(float)
    veto_reasons: Counter = Counter()
    posts_published: list[dict] = []
    failures: list[dict] = []

    for rec in entries:
        action = rec.get("action", "unknown")
        platform = rec.get("platform", "unknown")
        result = rec.get("result", "ok")
        by_action[action] += 1
        by_platform[platform][action] += 1
        cost_by_platform[platform] += float(rec.get("cost_usd") or 0)
        if result == "vetoed":
            for r in rec.get("veto_reasons", []) or []:
                veto_reasons[r] += 1
        if result == "failed":
            failures.append(rec)
        if action.endswith("_post_published"):
            posts_published.append(rec)

    lines.append("## Headline")
    lines.append("")
    total_actions = sum(by_action.values())
    lines.append(f"- **{total_actions}** total actions logged")
    lines.append(f"- **{len(posts_published)}** posts published")
    lines.append(f"- **{by_action.get('outbound_comment_posted', 0)}** outbound (goodwill) comments")
    lines.append(f"- **{by_action.get('inbound_reply_posted', 0)}** inbound replies on own posts")
    lines.append(f"- **{by_action.get('brand_guard_veto', 0)}** brand-guard vetoes")
    lines.append(f"- **${sum(cost_by_platform.values()):.4f}** total LLM cost")
    lines.append("")

    lines.append("## By platform")
    lines.append("")
    lines.append("| Platform | Actions | Outbound | Inbound replies | Cost |")
    lines.append("|---|---:|---:|---:|---:|")
    for plat in sorted(by_platform):
        c = by_platform[plat]
        lines.append(f"| {plat} | {sum(c.values())} | {c.get('outbound_comment_posted', 0)} | {c.get('inbound_reply_posted', 0)} | ${cost_by_platform[plat]:.4f} |")
    lines.append("")

    if veto_reasons:
        lines.append("## Brand-guard vetoes")
        lines.append("")
        for reason, n in veto_reasons.most_common():
            lines.append(f"- `{reason}` × {n}")
        lines.append("")

    if failures:
        lines.append("## Failures")
        lines.append("")
        for f in failures[:20]:
            lines.append(f"- `{f.get('action')}` on `{f.get('platform')}`: {f.get('error', '?')}")
        if len(failures) > 20:
            lines.append(f"- … {len(failures) - 20} more")
        lines.append("")

    lines.append("## All actions (raw counts)")
    lines.append("")
    for a, n in by_action.most_common():
        lines.append(f"- `{a}` × {n}")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    day = parse_day(arg)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"{day.isoformat()}.md"
    entries = load_entries(day)
    out_path.write_text(render(day, entries))
    print(f"wrote {out_path} ({len(entries)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
