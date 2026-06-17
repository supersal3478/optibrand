"""_outbox — hold-buffer queue for engagement drafts.

A draft lands here after brand-guard passes but BEFORE it gets posted. It
matures after `hold_buffer_seconds` (from caps.yaml) elapse. A separate
cron (outbox-flush.py) wakes up periodically, finds mature items, re-checks
kill switches (caps.live, cap_value, in_window) at submit time, and humanly
posts via the x-engage CDP recipe.

Why a hold buffer:
  • Flipping `x.live: false` in caps.yaml cancels every queued draft on the
    next flush — emergency stop is one config edit, no daemon restart.
  • Burst protection: if the agent decides to reply to 8 things at once,
    they arrive on X spaced out (real users don't fire 8 comments in 4s).
  • Sanity window: gives YOU 30 minutes (inbound) / 5 minutes (outbound) to
    yank a draft you don't like by editing/deleting its line in outbox.jsonl.

Storage:
  ~/.hermes/state/outbox.jsonl — single append-only file. Each line is a
  JSON object. New writes for the same `id` override earlier writes on read
  (dedupe-keeping-latest). The first write per id has the full record;
  subsequent writes can be partial updates (id + changed fields).

  This is poor-man's event-sourced storage. Adequate for our volume
  (hundreds of records/day max). Migrate to SQLite if we ever cross 10MB.

Status lifecycle:
  pending → submitted   (outbox-flush clicked Reply, X confirmed)
  pending → failed      (click happened, no confirmation toast)
  pending → canceled    (kill switch flipped, or manual cancel)

After terminal status, the record stays in the file as an audit trail —
nothing prunes it. `daily-report.py` reads here for the outbox section.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
OUTBOX_PATH = HERMES_HOME / "state" / "outbox.jsonl"


# ────────────────────────────────────────────── primitives ──


def _now() -> datetime:
    return datetime.now().astimezone()


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _append(record: dict) -> None:
    OUTBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTBOX_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _all_items_latest() -> dict[str, dict]:
    """Return latest version per id. Empty dict if file missing."""
    by_id: dict[str, dict] = {}
    if not OUTBOX_PATH.exists():
        return by_id
    with OUTBOX_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            id_ = row.get("id")
            if not id_:
                continue
            # Merge so partial updates carry forward earlier fields.
            by_id[id_] = {**by_id.get(id_, {}), **row}
    return by_id


# ────────────────────────────────────────────── public API ──


def enqueue(*, platform: str, mode: str, parent_url: str, draft_text: str,
            metadata: dict[str, Any] | None = None) -> str:
    """Persist a draft to the outbox and return its id.

    `mode` is one of: "inbound" / "goodwill" / "commenter_followup".
    The hold-buffer duration is read from caps.yaml via _caps.
    """
    # Imported lazily so this module is importable without _caps in dev/test.
    from _caps import hold_buffer_seconds

    item_id = uuid4().hex[:12]
    now = _now()
    hold_seconds = hold_buffer_seconds(platform, mode)
    record = {
        "id": item_id,
        "platform": platform,
        "mode": mode,
        "parent_url": parent_url,
        "draft_text": draft_text,
        "metadata": metadata or {},
        "created_at": now.isoformat(timespec="seconds"),
        "release_at": (now + timedelta(seconds=hold_seconds)).isoformat(timespec="seconds"),
        "status": "pending",
        "submitted_at": None,
        "fail_reason": None,
    }
    _append(record)

    try:
        from _metrics import log_event
        log_event("queued_outbox", platform=platform, mode=mode,
                  outbox_id=item_id, parent_url=parent_url,
                  release_at=record["release_at"],
                  hold_buffer_seconds=hold_seconds)
    except Exception:
        pass
    return item_id


def mature_items() -> list[dict]:
    """Pending items whose release_at has passed. Ordered by release_at."""
    now_iso = _now_iso()
    items = [it for it in _all_items_latest().values()
             if it.get("status") == "pending"
             and (it.get("release_at") or "") <= now_iso]
    items.sort(key=lambda it: it.get("release_at", ""))
    return items


def pending_items(platform: str | None = None) -> list[dict]:
    """All pending items (mature OR not), optionally filtered by platform.
    Used by daily-report.py / for visibility."""
    items = [it for it in _all_items_latest().values()
             if it.get("status") == "pending"]
    if platform is not None:
        items = [it for it in items if it.get("platform") == platform]
    items.sort(key=lambda it: it.get("release_at", ""))
    return items


def get_item(item_id: str) -> dict | None:
    return _all_items_latest().get(item_id)


def mark_submitted(item_id: str, **fields: Any) -> None:
    record = {"id": item_id, "status": "submitted",
              "submitted_at": _now_iso(), **fields}
    _append(record)
    _log_terminal(item_id, "submitted", **fields)


def mark_failed(item_id: str, reason: str, **fields: Any) -> None:
    record = {"id": item_id, "status": "failed", "fail_reason": reason,
              "submitted_at": None, **fields}
    _append(record)
    _log_terminal(item_id, "failed", reason=reason, **fields)


def cancel(item_id: str, reason: str = "canceled", **fields: Any) -> None:
    """Move a pending item to canceled. Used by outbox-flush when the kill
    switch is engaged at flush time, or by humans editing outbox manually."""
    record = {"id": item_id, "status": "canceled", "fail_reason": reason,
              "submitted_at": None, **fields}
    _append(record)
    _log_terminal(item_id, "canceled", reason=reason, **fields)


def _log_terminal(item_id: str, terminal: str, **fields: Any) -> None:
    try:
        from _metrics import log_event
        item = get_item(item_id) or {}
        log_event("released_outbox",
                  platform=item.get("platform"), mode=item.get("mode"),
                  outbox_id=item_id, terminal=terminal,
                  parent_url=item.get("parent_url"), **fields)
    except Exception:
        pass


# ────────────────────────────────────────────── cli ──


def cli() -> None:
    """`python -m _outbox status` — pending/mature counts.
    `python -m _outbox list` — pretty-print pending items.
    `python -m _outbox cancel <id>` — manually cancel a pending draft."""
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("list")
    c = sub.add_parser("cancel")
    c.add_argument("id")
    c.add_argument("--reason", default="manual_cancel")
    args = p.parse_args()

    if args.cmd == "status":
        latest = _all_items_latest().values()
        counts = {"pending": 0, "mature": 0, "submitted": 0,
                  "failed": 0, "canceled": 0}
        now_iso = _now_iso()
        for it in latest:
            st = it.get("status", "")
            counts[st] = counts.get(st, 0) + 1
            if st == "pending" and (it.get("release_at") or "") <= now_iso:
                counts["mature"] += 1
        print(json.dumps(counts, indent=2))
    elif args.cmd == "list":
        for it in pending_items():
            print(json.dumps(it, ensure_ascii=False))
    elif args.cmd == "cancel":
        item = get_item(args.id)
        if not item:
            raise SystemExit(f"no outbox item with id={args.id}")
        if item.get("status") != "pending":
            raise SystemExit(f"item {args.id} is {item.get('status')}, not pending")
        cancel(args.id, reason=args.reason)
        print(f"canceled {args.id}")


if __name__ == "__main__":
    cli()
