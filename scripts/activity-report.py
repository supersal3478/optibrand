#!/usr/bin/env python3
"""activity-report.py — what the engagement tool actually did, with receipts.

Reads the REAL engagement logs (the ones the pipeline writes) and produces:

  1. A chronological LEDGER — every comment / reply / like, with timestamp,
     type, the post it targeted, the author, the exact text left, and whether
     it also got liked. Source: ~/.hermes/state/outbox.jsonl (full text + target
     + author + status) joined with ~/.hermes/logs/engagement_metrics.jsonl
     (the `liked` events).
  2. A SUMMARY — counts for the day: comments left on the feed, replies to your
     posts, commenter follow-ups, likes given, brand-guard vetoes, skips (by
     reason), and failures.
  3. A LIVENESS line — is the loop actually running? Reads the heartbeat written
     by outbox-flush.py: last run time, whether Chrome was up, and whether your
     X session was still logged in. This is how you catch a SILENT stop.

Why this exists: the old daily-report.py read ~/.hermes/logs/audit.jsonl, which
only the SCHEDULER writes — not the engagement pipeline. So it never showed your
actual comments/likes/replies. This reads the right files.

Usage:
    scripts/activity-report.py                      # today, markdown to stdout
    scripts/activity-report.py --date 2026-06-17    # a specific local day
    scripts/activity-report.py --days 7             # last 7 days
    scripts/activity-report.py --save               # also write ~/.hermes/reports/activity-<date>.md
    scripts/activity-report.py --csv /tmp/ledger.csv  # dump the ledger as CSV
    scripts/activity-report.py --summary-only       # counts + liveness, no ledger
"""
from __future__ import annotations

import argparse
import csv as _csv
import json
import os
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _metrics  # noqa: E402
import _outbox  # noqa: E402

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
HEARTBEAT_PATH = HERMES_HOME / "state" / "heartbeat.json"
REPORTS_DIR = HERMES_HOME / "reports"

# Human-readable labels for the internal `mode` values.
MODE_LABEL = {
    "goodwill": "comment on feed post",
    "inbound": "reply to comment on your post",
    "commenter_followup": "follow-up on a commenter",
}
LIKE_OK = {"liked", "liked_retry", "already_liked"}


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _local_day_bounds(day: datetime, days: int) -> tuple[datetime, datetime]:
    """[start, end) spanning `days` local days ending on `day` (inclusive)."""
    tz = datetime.now().astimezone().tzinfo
    end_date = day.date()
    start = datetime.combine(end_date - timedelta(days=days - 1), time.min, tzinfo=tz)
    end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=tz)
    return start, end


def _in_window(ts: datetime | None, start: datetime, end: datetime) -> bool:
    return ts is not None and start <= ts < end


def gather(start: datetime, end: datetime) -> dict:
    """Pull ledger rows + counts for the window from outbox + metrics."""
    # ── likes: map outbox_id -> like_status (from metrics `liked` events) ──
    like_by_id: dict[str, str] = {}
    counts = {
        "comment on feed post": 0,
        "reply to comment on your post": 0,
        "follow-up on a commenter": 0,
        "likes_given": 0,
        "drafted": 0,
        "vetoed_brandguard": 0,
        "submit_failed": 0,
        "canceled": 0,
    }
    skips: dict[str, int] = {}
    last_action: datetime | None = None

    for ev in _metrics.iter_events(since=start):
        ts = _parse_ts(ev.get("ts"))
        if not _in_window(ts, start, end):
            continue
        name = ev.get("event", "")
        if name == "liked":
            oid = ev.get("outbox_id")
            if oid:
                like_by_id[oid] = ev.get("like_status", "liked")
            if ev.get("like_status", "liked") in LIKE_OK:
                counts["likes_given"] += 1
                last_action = max(last_action or ts, ts)
        elif name == "drafted":
            counts["drafted"] += 1
        elif name == "vetoed_brandguard":
            counts["vetoed_brandguard"] += 1
        elif name == "submit_failed":
            counts["submit_failed"] += 1
        elif name.startswith("skipped_"):
            skips[name] = skips.get(name, 0) + 1
        elif name == "submitted":
            last_action = max(last_action or ts, ts)

    # ── ledger: terminal outbox items in the window ──
    rows = []
    for item in _outbox._all_items_latest().values():
        status = item.get("status")
        if status not in ("submitted", "failed", "canceled"):
            continue
        when = _parse_ts(item.get("submitted_at")) or _parse_ts(item.get("created_at"))
        if not _in_window(when, start, end):
            continue
        mode = item.get("mode", "")
        label = MODE_LABEL.get(mode, mode or "?")
        meta = item.get("metadata") or {}
        oid = item.get("id")
        liked = like_by_id.get(oid)
        row = {
            "ts": when,
            "time": when.strftime("%Y-%m-%d %H:%M:%S") if when else "?",
            "kind": label,
            "mode": mode,
            "author": meta.get("author") or "—",
            "target": item.get("parent_url") or "—",
            "text": (item.get("draft_text") or "").replace("\n", " ").strip(),
            "status": status,
            "liked": liked or ("" if status != "submitted" else "no"),
            "fail_reason": item.get("fail_reason") or "",
            "id": oid,
        }
        rows.append(row)
        if status == "submitted":
            counts[label] = counts.get(label, 0) + 1
            if when:
                last_action = max(last_action or when, when)
        elif status == "canceled":
            counts["canceled"] += 1

    rows.sort(key=lambda r: r["ts"] or datetime.min.replace(tzinfo=start.tzinfo))
    return {"rows": rows, "counts": counts, "skips": skips, "last_action": last_action}


def read_heartbeat() -> dict | None:
    try:
        return json.loads(HEARTBEAT_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _ago(ts: datetime | None) -> str:
    if not ts:
        return "never"
    delta = datetime.now().astimezone() - ts
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 1440:
        return f"{mins // 60}h {mins % 60}m ago"
    return f"{mins // 1440}d ago"


def render_md(data: dict, start: datetime, end: datetime, *, ledger: bool) -> str:
    c = data["counts"]
    out: list[str] = []
    label = start.strftime("%Y-%m-%d")
    if (end - start).days > 1:
        label = f"{start.strftime('%Y-%m-%d')} → {(end - timedelta(days=1)).strftime('%Y-%m-%d')}"
    out.append(f"# Engagement activity — {label}\n")

    # ── Liveness ──
    hb = read_heartbeat()
    out.append("## Is it running?\n")
    if hb:
        hb_ts = _parse_ts(hb.get("ts"))
        chrome = "up" if hb.get("chrome_up") else "DOWN"
        li = hb.get("logged_in")
        login = "logged in" if li is True else ("LOGGED OUT ⚠️" if li is False else "unknown")
        flag = "" if (hb.get("chrome_up") and li is not False) else "  ← needs attention"
        out.append(f"- Last heartbeat: **{_ago(hb_ts)}** ({hb.get('ts','?')}){flag}")
        out.append(f"- Chrome CDP: **{chrome}**  ·  X session: **{login}**")
    else:
        out.append("- No heartbeat yet — `outbox-flush.py` hasn't recorded one "
                   "(loop not armed/running on this machine, or no flush has run).")
    out.append(f"- Last posting action in window: **{_ago(data['last_action'])}**\n")

    # ── Summary ──
    out.append("## Summary\n")
    out.append(f"- Comments left on feed posts: **{c['comment on feed post']}**")
    out.append(f"- Replies to comments on your posts: **{c['reply to comment on your post']}**")
    out.append(f"- Commenter follow-ups: **{c['follow-up on a commenter']}**")
    out.append(f"- Likes given: **{c['likes_given']}**")
    out.append(f"- Drafts produced: {c['drafted']}  ·  Brand-guard vetoes: {c['vetoed_brandguard']}")
    out.append(f"- Failures: {c['submit_failed']}  ·  Canceled (kill-switch/cap): {c['canceled']}")
    if data["skips"]:
        sk = ", ".join(f"{k.replace('skipped_','')}: {v}" for k, v in sorted(data["skips"].items()))
        out.append(f"- Skips: {sk}")
    total_actions = (c['comment on feed post'] + c['reply to comment on your post']
                     + c['follow-up on a commenter'])
    out.append(f"- **Total posted interactions: {total_actions}** (+{c['likes_given']} likes)\n")

    # ── Ledger ──
    if ledger:
        out.append("## Ledger (every interaction)\n")
        if not data["rows"]:
            out.append("_No interactions recorded in this window._\n")
        else:
            out.append("| Time | Type | Author | Liked | Status | Comment | Post |")
            out.append("|---|---|---|---|---|---|---|")
            for r in data["rows"]:
                text = r["text"]
                if len(text) > 90:
                    text = text[:87] + "…"
                text = text.replace("|", "\\|")
                liked = "✓" if r["liked"] in LIKE_OK else (r["liked"] or "")
                st = r["status"] + (f" ({r['fail_reason']})" if r["fail_reason"] else "")
                out.append(f"| {r['time']} | {r['kind']} | {r['author']} | {liked} "
                           f"| {st} | {text} | {r['target']} |")
            out.append("")
    return "\n".join(out)


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["time", "kind", "mode", "author", "liked", "status",
                    "fail_reason", "text", "target", "id"])
        for r in rows:
            w.writerow([r["time"], r["kind"], r["mode"], r["author"], r["liked"],
                        r["status"], r["fail_reason"], r["text"], r["target"], r["id"]])


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--date", default=None, help="Local day YYYY-MM-DD (default: today).")
    p.add_argument("--days", type=int, default=1, help="Span N days ending on --date.")
    p.add_argument("--save", action="store_true", help="Also write to ~/.hermes/reports/.")
    p.add_argument("--csv", default=None, help="Write the ledger to this CSV path.")
    p.add_argument("--summary-only", action="store_true", help="Omit the ledger table.")
    args = p.parse_args()

    day = _parse_ts(args.date + "T00:00:00") if args.date else datetime.now().astimezone()
    if day is None:
        print(f"bad --date: {args.date!r} (use YYYY-MM-DD)", file=sys.stderr)
        return 2
    start, end = _local_day_bounds(day, max(1, args.days))

    data = gather(start, end)
    md = render_md(data, start, end, ledger=not args.summary_only)
    print(md)

    if args.csv:
        write_csv(data["rows"], Path(args.csv))
        print(f"\n[csv] wrote {len(data['rows'])} rows → {args.csv}", file=sys.stderr)
    if args.save:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = REPORTS_DIR / f"activity-{start.strftime('%Y-%m-%d')}.md"
        out_path.write_text(md, encoding="utf-8")
        print(f"\n[save] wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
