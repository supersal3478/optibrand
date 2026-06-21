#!/usr/bin/env python3
"""schedule-tick.py — the minute-cadence orchestrator.

Reads schedule.yaml every minute, decides what should happen in this
minute (publish a scheduled post; poll a post's inbound for the 24h
monitor window; daily report; weekly voice retrain), and invokes the
right primitive.

Safety boundaries enforced here (so primitives stay dumb):
  - config/caps.yaml      live flags + daily/weekly caps
  - config/windows.yaml   per-platform business-hour windows
  - hold buffer           inbound drafts queue for N seconds before posting

Idempotency: every action checks the audit log for "did I already fire
this in this minute?" before running, so cron drift or wake-from-sleep
doesn't cause duplicate publishes.

Usage:
    scripts/schedule-tick.py                        # one tick at the current minute
    scripts/schedule-tick.py --dry-run              # plan-only; print actions, no side effects
    scripts/schedule-tick.py --now 2026-05-26T09:00 # synthetic time (for testing)
    scripts/schedule-tick.py --verbose              # log every skip reason
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import yaml
except ImportError:
    sys.stderr.write("pyyaml required (installed by setup.sh into the Hermes venv)\n")
    sys.exit(2)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
HERMES_BIN = PROJECT_ROOT / "vendor" / "hermes-agent" / ".venv" / "bin" / "hermes"
LIPY_BIN = Path(os.environ.get("LIPY_BIN", Path.home() / ".local" / "bin" / "lipy"))
X_ENGAGE_DIR = PROJECT_ROOT / "skills" / "x-engage"
SCHEDULE_PATH = PROJECT_ROOT / "schedule.yaml"
CAPS_PATH = PROJECT_ROOT / "config" / "caps.yaml"
WINDOWS_PATH = PROJECT_ROOT / "config" / "windows.yaml"
AUDIT_LOG = HERMES_HOME / "logs" / "audit.jsonl"
QUEUE_DIR = HERMES_HOME / "queue"
SCHEDULED_STATE_DIR = HERMES_HOME / "state" / "scheduled-posts"
START_CHROME_CDP = X_ENGAGE_DIR / "start-chrome-cdp.sh"


def utcnow() -> str:
    return datetime.now(tz=ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def parse_local(iso_str: str, tz: ZoneInfo) -> datetime:
    """Parse 'YYYY-MM-DDTHH:MM[:SS]' as wall time in tz."""
    s = str(iso_str).replace("Z", "")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def in_window(now: datetime, win: dict) -> bool:
    """Check if now is inside an entry like {days: [0..6], start: HH:MM, end: HH:MM}.
    Empty days list means every day."""
    if not win:
        return True
    days = win.get("days") or []
    if days and now.weekday() not in days:
        return False
    start = dtime.fromisoformat(str(win.get("start", "00:00")))
    end = dtime.fromisoformat(str(win.get("end", "23:59")))
    return start <= now.time() <= end


# ─────────────────────────── audit log ───────────────────────────


class AuditLog:
    """Append-only JSON-lines log. One line per action, schema matches daily-report.py."""

    def __init__(self, path: Path, dry_run: bool = False):
        self.path = path
        self.dry_run = dry_run
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: list[dict] | None = None  # lazy-loaded for dedup queries

    _ROTATE_BYTES = 25_000_000      # archive + truncate the active log past this
    _KEEP_BYTES = 4_000_000         # how much recent tail to retain on rotate
    _TAIL_READ_BYTES = 2_000_000    # cap per-tick read so a big log can't slow ticks

    def write(self, **fields) -> None:
        rec = {"ts": utcnow(), **fields}
        if self.dry_run:
            print(f"[audit dry-run] {json.dumps(rec, ensure_ascii=False)}")
            return
        self._maybe_rotate()
        with self.path.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._cache = None  # invalidate

    def _maybe_rotate(self) -> None:
        """When the audit log gets large, archive it to a dated file and keep
        only the recent tail active. Bounds disk use AND keeps _entries() fast,
        since it previously re-read the WHOLE file on every minute tick."""
        try:
            if not self.path.exists() or self.path.stat().st_size <= self._ROTATE_BYTES:
                return
            size = self.path.stat().st_size
            with self.path.open("rb") as f:
                f.seek(max(0, size - self._KEEP_BYTES))
                f.readline()  # drop partial line
                tail = f.read()
            stamp = utcnow()[:10]
            archive = self.path.with_name(f"{self.path.stem}-{stamp}{self.path.suffix}")
            n = 1
            while archive.exists():
                archive = self.path.with_name(f"{self.path.stem}-{stamp}.{n}{self.path.suffix}")
                n += 1
            self.path.rename(archive)
            self.path.write_bytes(tail)
        except OSError:
            pass

    def _entries(self) -> list[dict]:
        if self._cache is not None:
            return self._cache
        out: list[dict] = []
        if self.path.exists():
            # Read only the tail. Idempotency needs the current UTC minute and
            # count_today needs today's lines — both live at the end of the file.
            # Reading the whole file every tick was the #1 slow-death risk.
            size = self.path.stat().st_size
            with self.path.open("rb") as f:
                if size > self._TAIL_READ_BYTES:
                    f.seek(size - self._TAIL_READ_BYTES)
                    f.readline()  # discard the partial first line
                blob = f.read()
            for line in blob.decode("utf-8", "replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        self._cache = out
        return out

    def fired_this_minute(self, action: str, post_id: str | None, minute_iso_utc: str) -> bool:
        for rec in reversed(self._entries()):
            ts = rec.get("ts", "")
            if not ts.startswith(minute_iso_utc):
                continue
            if rec.get("action") != action:
                continue
            if post_id is not None and rec.get("post_id") != post_id:
                continue
            return True
        return False

    def count_today(self, action: str, platform: str, today_utc: str) -> int:
        n = 0
        for rec in self._entries():
            if not rec.get("ts", "").startswith(today_utc):
                continue
            if rec.get("action") == action and rec.get("platform") == platform:
                if rec.get("result") == "ok":
                    n += 1
        return n


# ─────────────────────────── primitive runners ───────────────────────────


def _run(cmd: list[str], timeout: int = 180) -> tuple[int, str, str]:
    """Run a subprocess and return (rc, stdout, stderr)."""
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _parse_json(s: str) -> dict | None:
    s = (s or "").strip()
    if not s:
        return None
    # Some commands print log lines before the JSON; take the last balanced JSON object.
    for line in reversed(s.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def ensure_chrome_cdp(audit: AuditLog) -> bool:
    """For any X action: make sure the dedicated CDP Chrome is up."""
    rc, _, err = _run(["bash", str(START_CHROME_CDP)], timeout=30)
    if rc != 0:
        audit.write(action="ensure_chrome_cdp", platform="x", result="failed", error=err.strip())
        return False
    return True


def run_publish_linkedin(post: dict, draft_path: Path, dry_run: bool) -> dict:
    cmd = [str(LIPY_BIN), "publish", "--file", str(draft_path)]
    if not dry_run:
        cmd.append("--live")
    rc, out, err = _run(cmd, timeout=300)
    parsed = _parse_json(out) or {}
    parsed.setdefault("rc", rc)
    if rc != 0 and "error" not in parsed:
        parsed["error"] = (err or "").strip()[:400]
    return parsed


def run_publish_x(post: dict, draft_path: Path, dry_run: bool) -> dict:
    cmd = [
        sys.executable, str(X_ENGAGE_DIR / "publish.py"),
        "--file", str(draft_path),
    ]
    if not dry_run:
        cmd.append("--live")
    rc, out, err = _run(cmd, timeout=180)
    parsed = _parse_json(out) or {}
    parsed.setdefault("rc", rc)
    if rc != 0 and "error" not in parsed:
        parsed["error"] = (err or "").strip()[:400]
    return parsed


def run_fetch_new_comments(post: dict, post_urn_or_url: str, since_state_file: Path) -> list[dict]:
    """Returns list of NEW comments since the last call (state file persistence).
    Empty list means nothing new (or call failed — check audit log for the error)."""
    if post["platform"] == "linkedin":
        cmd = [
            str(LIPY_BIN), "comments",
            "--post", post_urn_or_url,
            "--since-state", str(since_state_file),
        ]
    else:  # x
        cmd = [
            sys.executable, str(X_ENGAGE_DIR / "fetch-comments.py"),
            "--post", post_urn_or_url,
            "--since-state", str(since_state_file),
        ]
    rc, out, _ = _run(cmd, timeout=180)
    parsed = _parse_json(out) or {}
    if rc != 0 or not parsed.get("ok"):
        return []
    return parsed.get("new_comments", []) or []


def draft_reply_via_hermes(platform: str, parent: dict, post_id: str, draft_id: str) -> dict:
    """Invoke reply-drafter + brand-guard via a one-shot Hermes session.
    Returns {decision: DRAFT|REFUSE, draft|reasons: ...}."""
    parent_summary = json.dumps({
        "author": parent.get("author_name") or parent.get("author") or "",
        "text": parent.get("text", ""),
        "platform": platform,
    }, ensure_ascii=False)
    prompt = (
        f"Run the reply-drafter skill. Inputs:\n"
        f"  platform: {platform}\n"
        f"  target_kind: own_post_reply\n"
        f"  parent (JSON): {parent_summary}\n"
        f"Follow your SKILL.md exactly. Call brand-guard. "
        f"Output ONLY the final JSON object (decision DRAFT or REFUSE) on the last line. "
        f"Do not include any extra prose."
    )
    cmd = [
        str(HERMES_BIN), "chat",
        "--skills", "reply-drafter,brand-guard",
        "-q", prompt,
    ]
    rc, out, err = _run(cmd, timeout=180)
    parsed = _parse_json(out)
    if not parsed:
        return {"decision": "REFUSE", "reasons": [{"rule": "llm-no-json", "detail": (err or out)[:200]}]}
    return parsed


def run_post_queued_reply_linkedin(parent_urn: str, text: str, dry_run: bool) -> dict:
    cmd = [str(LIPY_BIN), "reply", "--parent", parent_urn, "--text", text]
    if not dry_run:
        cmd.append("--live")
    rc, out, err = _run(cmd, timeout=300)
    parsed = _parse_json(out) or {}
    parsed.setdefault("rc", rc)
    if rc != 0 and "error" not in parsed:
        parsed["error"] = (err or "").strip()[:400]
    return parsed


def run_post_queued_reply_x(parent_url: str, text: str, dry_run: bool) -> dict:
    cmd = [
        sys.executable, str(X_ENGAGE_DIR / "reply.py"),
        "--parent", parent_url,
        "--text", text,
    ]
    if not dry_run:
        cmd.append("--live")
    rc, out, err = _run(cmd, timeout=180)
    parsed = _parse_json(out) or {}
    parsed.setdefault("rc", rc)
    if rc != 0 and "error" not in parsed:
        parsed["error"] = (err or "").strip()[:400]
    return parsed


# ─────────────────────────── scheduled-post state ───────────────────────────


def state_path(post_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in post_id)
    return SCHEDULED_STATE_DIR / f"{safe}.json"


def load_state(post_id: str) -> dict:
    p = state_path(post_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def save_state(post_id: str, state: dict) -> None:
    SCHEDULED_STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path(post_id).write_text(json.dumps(state, indent=2))


# ─────────────────────────── caps + windows checks ───────────────────────────


def is_live(caps: dict, platform: str) -> bool:
    return bool(((caps or {}).get(platform) or {}).get("live"))


def get_window(windows: dict, platform: str, kind: str = "inbound") -> dict:
    return ((windows or {}).get(platform) or {}).get(kind) or {}


def at_cap(audit: AuditLog, caps: dict, action: str, platform: str, today_utc: str) -> bool:
    section = (caps or {}).get(platform) or {}
    limit = ((section.get("inbound") or {}).get("replies_per_day")
             if "reply" in action
             else (section.get("outbound") or {}).get("comments_per_day"))
    if not limit:
        return False
    used = audit.count_today(action, platform, today_utc)
    return used >= int(limit)


# ─────────────────────────── tick ───────────────────────────


def tick(args: argparse.Namespace) -> int:
    audit_path = Path(args.audit_log) if args.audit_log else AUDIT_LOG
    audit = AuditLog(audit_path, dry_run=args.dry_run)

    schedule_path = Path(args.schedule) if args.schedule else SCHEDULE_PATH
    if not schedule_path.exists():
        if args.verbose:
            print(f"[skip] {schedule_path} missing — nothing to orchestrate")
        return 0
    schedule = load_yaml(schedule_path)
    caps = load_yaml(Path(args.caps) if args.caps else CAPS_PATH)
    windows = load_yaml(Path(args.windows) if args.windows else WINDOWS_PATH)

    tz_name = schedule.get("timezone") or windows.get("timezone") or "UTC"
    tz = ZoneInfo(tz_name)

    if args.now:
        now_local = datetime.fromisoformat(args.now).replace(tzinfo=tz)
    else:
        now_local = datetime.now(tz=tz)

    minute_utc = now_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M")
    today_utc = now_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d")

    if args.verbose:
        print(f"[tick] now={now_local.isoformat()} ({tz_name}) | minute_utc={minute_utc}")

    defaults = schedule.get("defaults", {}) or {}
    hold_inbound = int((caps.get("hold_buffer_inbound_seconds") or 0))

    # ─── 1. publish + monitor for each scheduled post ───
    for post in schedule.get("posts") or []:
        post_id = post.get("id")
        platform = post.get("platform")
        if not post_id or platform not in ("linkedin", "x"):
            continue
        try:
            post_time = parse_local(post["time"], tz)
        except (KeyError, ValueError):
            continue

        state = load_state(post_id)
        monitor_hours = float(post.get("monitor_hours_after", defaults.get("monitor_hours_after", 24)))
        monitor_end = post_time + timedelta(hours=monitor_hours)
        poll_minutes = int(post.get("inbound_poll_minutes", defaults.get("inbound_poll_minutes", 15)))

        # 1a. publish at exact post_time minute (or first tick after, if missed)
        publish_due = (
            state.get("publish_status") not in ("ok", "live")
            and now_local >= post_time
            and now_local < post_time + timedelta(minutes=10)  # 10-min grace
        )
        if publish_due:
            if not is_live(caps, platform):
                audit.write(action="publish_skip", platform=platform, post_id=post_id,
                            result="skipped", reason="platform_not_live")
            elif audit.fired_this_minute("publish", post_id, minute_utc):
                if args.verbose:
                    print(f"[skip] publish for {post_id} already fired this minute")
            else:
                draft_rel = post.get("draft")
                draft_path = (PROJECT_ROOT / draft_rel).resolve() if draft_rel else None
                if not draft_rel:
                    audit.write(action="publish_skip", platform=platform, post_id=post_id,
                                result="skipped", reason="no_draft_field")
                elif not draft_path or not draft_path.exists():
                    audit.write(action="publish_skip", platform=platform, post_id=post_id,
                                result="skipped", reason="draft_file_missing",
                                draft=str(draft_path))
                else:
                    if args.dry_run:
                        audit.write(action="publish", platform=platform, post_id=post_id,
                                    result="dry_run", draft=str(draft_path))
                    else:
                        if platform == "x" and not ensure_chrome_cdp(audit):
                            audit.write(action="publish", platform=platform, post_id=post_id,
                                        result="failed", error="chrome_cdp_unavailable")
                        else:
                            save_state(post_id, {**state, "publish_status": "in_progress",
                                                 "publish_started_at": utcnow()})
                            result = (run_publish_linkedin(post, draft_path, dry_run=False)
                                      if platform == "linkedin"
                                      else run_publish_x(post, draft_path, dry_run=False))
                            published_ref = (result.get("post_urn") or result.get("post_url")
                                             or result.get("url") or "")
                            if result.get("ok") and published_ref:
                                save_state(post_id, {
                                    **state,
                                    "publish_status": "ok",
                                    "publish_completed_at": utcnow(),
                                    "published_ref": published_ref,
                                    "platform": platform,
                                })
                                audit.write(action="publish", platform=platform, post_id=post_id,
                                            result="ok", published_ref=published_ref)
                            else:
                                save_state(post_id, {**state, "publish_status": "failed",
                                                     "publish_failed_at": utcnow(),
                                                     "publish_error": result.get("error", "")})
                                audit.write(action="publish", platform=platform, post_id=post_id,
                                            result="failed", error=result.get("error", "")[:300])

        # 1b. monitor: poll for new comments on the published post
        state = load_state(post_id)  # reload after possible publish
        published_ref = state.get("published_ref")
        in_monitor_window = post_time <= now_local <= monitor_end
        if in_monitor_window and published_ref:
            # Only poll at the configured cadence (poll_minutes since post_time).
            minutes_since_post = int((now_local - post_time).total_seconds() // 60)
            if minutes_since_post % poll_minutes != 0:
                pass  # not this tick
            elif not is_live(caps, platform):
                audit.write(action="monitor_skip", platform=platform, post_id=post_id,
                            result="skipped", reason="platform_not_live")
            elif not in_window(now_local, get_window(windows, platform, "inbound")):
                if args.verbose:
                    audit.write(action="monitor_skip", platform=platform, post_id=post_id,
                                result="skipped", reason="outside_window")
            elif audit.fired_this_minute("monitor_poll", post_id, minute_utc):
                if args.verbose:
                    print(f"[skip] monitor for {post_id} already polled this minute")
            elif at_cap(audit, caps, "inbound_reply_posted", platform, today_utc):
                audit.write(action="monitor_skip", platform=platform, post_id=post_id,
                            result="skipped", reason="daily_cap_reached")
            else:
                if args.dry_run:
                    audit.write(action="monitor_poll", platform=platform, post_id=post_id,
                                result="dry_run", published_ref=published_ref)
                else:
                    if platform == "x" and not ensure_chrome_cdp(audit):
                        audit.write(action="monitor_poll", platform=platform, post_id=post_id,
                                    result="failed", error="chrome_cdp_unavailable")
                    else:
                        since_state = SCHEDULED_STATE_DIR / f"{post_id}.seen.json"
                        new_comments = run_fetch_new_comments(post, published_ref, since_state)
                        audit.write(action="monitor_poll", platform=platform, post_id=post_id,
                                    result="ok", new_comments=len(new_comments))
                        # Draft + queue (do NOT post yet — that's the hold-buffer flush stage)
                        for parent in new_comments:
                            draft_id = f"{post_id}__{parent.get('urn') or parent.get('url') or parent.get('id') or utcnow()}"
                            draft_id = "".join(c if c.isalnum() or c in "-_." else "_" for c in draft_id)[:120]
                            result = draft_reply_via_hermes(platform, parent, post_id, draft_id)
                            if result.get("decision") == "DRAFT" and result.get("draft"):
                                queue_entry = {
                                    "draft_id": draft_id,
                                    "post_id": post_id,
                                    "platform": platform,
                                    "parent": parent,
                                    "draft": result["draft"],
                                    "drafted_at": utcnow(),
                                    "hold_until_ts": ((now_local.astimezone(ZoneInfo("UTC"))
                                                      + timedelta(seconds=hold_inbound)).strftime("%Y-%m-%dT%H:%M:%SZ")),
                                }
                                QUEUE_DIR.mkdir(parents=True, exist_ok=True)
                                (QUEUE_DIR / f"{draft_id}.json").write_text(
                                    json.dumps(queue_entry, indent=2, ensure_ascii=False))
                                audit.write(action="inbound_reply_drafted", platform=platform,
                                            post_id=post_id, draft_id=draft_id, result="queued")
                            else:
                                audit.write(action="inbound_reply_drafted", platform=platform,
                                            post_id=post_id, result="refused",
                                            veto_reasons=[r.get("rule") for r in
                                                          (result.get("reasons") or [])])

    # ─── 2. flush the hold-buffer queue ───
    if QUEUE_DIR.exists():
        now_utc = now_local.astimezone(ZoneInfo("UTC"))
        for qf in sorted(QUEUE_DIR.glob("*.json")):
            try:
                entry = json.loads(qf.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            hold_until = entry.get("hold_until_ts")
            if not hold_until:
                continue
            try:
                hold_dt = datetime.fromisoformat(hold_until.replace("Z", "+00:00"))
            except ValueError:
                continue
            if now_utc < hold_dt:
                continue  # still in hold

            platform = entry.get("platform")
            if not is_live(caps, platform):
                audit.write(action="inbound_reply_post_skip", platform=platform,
                            post_id=entry.get("post_id"), draft_id=entry.get("draft_id"),
                            result="skipped", reason="platform_not_live")
                continue
            if audit.fired_this_minute("inbound_reply_posted", entry.get("draft_id"), minute_utc):
                continue
            if at_cap(audit, caps, "inbound_reply_posted", platform, today_utc):
                audit.write(action="inbound_reply_post_skip", platform=platform,
                            post_id=entry.get("post_id"), draft_id=entry.get("draft_id"),
                            result="skipped", reason="daily_cap_reached")
                continue

            if args.dry_run:
                audit.write(action="inbound_reply_posted", platform=platform,
                            post_id=entry.get("post_id"), draft_id=entry.get("draft_id"),
                            result="dry_run", draft_preview=entry.get("draft", "")[:80])
                continue

            if platform == "x" and not ensure_chrome_cdp(audit):
                audit.write(action="inbound_reply_posted", platform=platform,
                            post_id=entry.get("post_id"), draft_id=entry.get("draft_id"),
                            result="failed", error="chrome_cdp_unavailable")
                continue

            parent = entry.get("parent") or {}
            text = entry.get("draft") or ""
            if platform == "linkedin":
                parent_urn = parent.get("urn") or ""
                result = run_post_queued_reply_linkedin(parent_urn, text, dry_run=False)
            else:
                parent_url = parent.get("url") or ""
                result = run_post_queued_reply_x(parent_url, text, dry_run=False)
            if result.get("ok"):
                audit.write(action="inbound_reply_posted", platform=platform,
                            post_id=entry.get("post_id"), draft_id=entry.get("draft_id"),
                            result="ok")
                try:
                    qf.unlink()
                except OSError:
                    pass
            else:
                audit.write(action="inbound_reply_posted", platform=platform,
                            post_id=entry.get("post_id"), draft_id=entry.get("draft_id"),
                            result="failed", error=str(result.get("error", ""))[:300])

    # ─── 3. recurring jobs ───
    recurring = schedule.get("recurring", {}) or {}
    dr = recurring.get("daily_report", {}) or {}
    if dr.get("enabled") and dr.get("time"):
        try:
            tgt = dtime.fromisoformat(str(dr["time"]))
            if (now_local.time().hour, now_local.time().minute) == (tgt.hour, tgt.minute):
                if not audit.fired_this_minute("daily_report", None, minute_utc):
                    if args.dry_run:
                        audit.write(action="daily_report", platform="-", result="dry_run")
                    else:
                        rc, out, err = _run([sys.executable,
                                             str(PROJECT_ROOT / "scripts" / "daily-report.py")],
                                            timeout=60)
                        audit.write(action="daily_report", platform="-",
                                    result="ok" if rc == 0 else "failed",
                                    error=(err or "")[:200] if rc != 0 else None)
        except ValueError:
            pass

    vr = recurring.get("voice_retrain", {}) or {}
    if vr.get("enabled") and vr.get("time") and vr.get("weekday"):
        weekdays = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                    "friday": 4, "saturday": 5, "sunday": 6}
        wd = weekdays.get(str(vr["weekday"]).lower())
        try:
            tgt = dtime.fromisoformat(str(vr["time"]))
            if wd is not None and now_local.weekday() == wd \
               and (now_local.time().hour, now_local.time().minute) == (tgt.hour, tgt.minute):
                if not audit.fired_this_minute("voice_retrain", None, minute_utc):
                    if args.dry_run:
                        audit.write(action="voice_retrain", platform="-", result="dry_run")
                    else:
                        rc, _, err = _run([sys.executable,
                                           str(PROJECT_ROOT / "scripts" / "voice-train.py"),
                                           "--retrain"], timeout=900)
                        audit.write(action="voice_retrain", platform="-",
                                    result="ok" if rc == 0 else "failed",
                                    error=(err or "")[:200] if rc != 0 else None)
        except ValueError:
            pass

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dry-run", action="store_true",
                   help="Plan only; print actions and audit lines, no side effects.")
    p.add_argument("--now", default=None,
                   help="Synthetic local time for testing (e.g. 2026-05-26T09:00).")
    p.add_argument("--verbose", action="store_true", help="Log skip reasons too.")
    p.add_argument("--schedule", default=None, help="Override schedule.yaml path (for testing).")
    p.add_argument("--caps", default=None, help="Override caps.yaml path (for testing).")
    p.add_argument("--windows", default=None, help="Override windows.yaml path (for testing).")
    p.add_argument("--audit-log", default=None, help="Override audit.jsonl path (for testing).")
    args = p.parse_args()
    try:
        return tick(args)
    except Exception as e:
        # A bad config or transient error must not crash the tick with an
        # unhandled traceback. Log it and exit non-zero; next minute retries.
        import traceback
        sys.stderr.write(f"[schedule-tick] tick failed: {e}\n")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
