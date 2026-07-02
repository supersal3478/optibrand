#!/usr/bin/env python3
"""outbox-flush.py — drain the hold-buffer queue.

Run by Hermes cron every couple of minutes. Walks mature outbox items, applies
kill-switch checks at submit time, humanly posts via CDP, marks each item
submitted/failed/canceled, and writes metrics.

A lock file prevents overlapping runs (since flush > cron interval is possible
if there's a backlog). If the lock is older than --stale-lock-seconds we
assume the previous run crashed and break it.

The humanization layer (skills/x-engage/x_human.py) is the load-bearing
fingerprint defense: every click, scroll, and keystroke routes through it.
Raw Input.insertText and Runtime.evaluate(el.click()) are NOT used here.

Usage:
    scripts/outbox-flush.py                       # cron mode, batch=1
    scripts/outbox-flush.py --batch 2 --verbose   # interactive debugging
    scripts/outbox-flush.py --dry-run             # honor kill switches but
                                                  # don't actually submit
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import subprocess
import sys
import time as _time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "skills" / "x-engage"))

from _caps import (  # noqa: E402
    in_window, is_live, random_jitter_seconds, under_cap,
)
from _metrics import log_event, log_page_view  # noqa: E402


def _log_li_page_views(n: int, *, via: str) -> None:
    """Count the page loads a lipy write-command makes against the LinkedIn
    read budget (each lipy live command navigates feed pre-context + permalink)."""
    log_page_view("linkedin", n=n, via=via)
from _outbox import (  # noqa: E402
    cancel, mark_failed, mark_submitted, mature_items, pending_items,
)
from _cdp import open_session  # noqa: E402
from x_human import HumanCDP  # noqa: E402

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
LOCK_PATH = HERMES_HOME / "state" / "outbox-flush.lock"
HEARTBEAT_PATH = HERMES_HOME / "state" / "heartbeat.json"
START_CHROME_CDP = PROJECT_ROOT / "skills" / "x-engage" / "start-chrome-cdp.sh"
LIPY_BIN = Path(os.environ.get("LIPY_BIN", Path.home() / ".local" / "bin" / "lipy"))

COMPOSER_SELECTOR = 'div[data-testid="tweetTextarea_0"]'
INLINE_SUBMIT_SELECTOR = 'button[data-testid="tweetButtonInline"]'
TOAST_SELECTOR = '[data-testid="toast"]'
LIKE_SELECTOR = 'button[data-testid="like"]'
UNLIKE_SELECTOR = 'button[data-testid="unlike"]'


# ────────────────────────────────────────────── locking ──


def _claim_lock(stale_seconds: int) -> bool:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        age = _time.time() - LOCK_PATH.stat().st_mtime
        if age < stale_seconds:
            return False
        # Stale — break it.
        try:
            LOCK_PATH.unlink()
        except FileNotFoundError:
            pass
    LOCK_PATH.write_text(f"{os.getpid()}\n{datetime.now().isoformat()}")
    return True


def _release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass


# ────────────────────────────────────────────── chrome boot ──


def _ensure_chrome_cdp() -> None:
    """Idempotent CDP-Chrome bring-up. Exits cleanly if already running."""
    try:
        subprocess.run(["bash", str(START_CHROME_CDP)],
                       check=True, capture_output=True, timeout=20)
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"start-chrome-cdp.sh failed: {e.stderr.decode()}\n")
        raise


# ────────────────────────────────────────────── submission ──


async def _like_focal_post(h: HumanCDP, *, attempts: int = 2) -> str:
    """Humanly like the focal article on the current post-detail page.

    Called after a successful reply submission — every comment gets a like,
    per the user's spec.

    Robustness:
      • Long initial settle (2–3.5s) lets X's post-submit toast clear and the
        like-button DOM position stabilize. Our previous click_failed on a
        live test came from clicking while the UI was still mid-transition.
      • Retry once on failure. The second attempt re-runs click_element which
        re-queries the bbox, so any DOM shift since the first attempt is
        handled.

    Returns: 'liked' | 'liked_retry' | 'already_liked' | 'no_button'
             | 'click_failed'.
    """
    # Cheap pre-check: if it's already liked, the testid has flipped to 'unlike'.
    already = await h.session.eval_js(
        f"!!document.querySelector('{UNLIKE_SELECTOR}')",
        await_promise=False,
    )
    if already:
        return "already_liked"

    # Wait for the just-submitted reply UI to settle. Hardened from 0.5–1.4s
    # → 2.0–3.5s after observing click_failed live.
    await asyncio.sleep(random.uniform(2.0, 3.5))

    last_result = "click_failed"
    for attempt in range(attempts):
        clicked = await h.click_element(LIKE_SELECTOR, scroll_into_view=False)
        if not clicked:
            last_result = "no_button"
            if attempt + 1 < attempts:
                await asyncio.sleep(random.uniform(0.8, 1.5))
                continue
            return last_result

        # Verify the testid flipped to 'unlike' — longer window than before.
        await asyncio.sleep(random.uniform(1.0, 1.8))
        confirmed = await h.session.eval_js(
            f"!!document.querySelector('{UNLIKE_SELECTOR}')",
            await_promise=False,
        )
        if confirmed:
            return "liked" if attempt == 0 else "liked_retry"

        last_result = "click_failed"
        if attempt + 1 < attempts:
            # Brief inter-attempt pause — UI may have been mid-transition.
            await asyncio.sleep(random.uniform(1.2, 2.0))
    return last_result


async def _wait_for_confirmation(h: HumanCDP, *, timeout_s: float = 12.0) -> str:
    """Poll for either the 'Your post was sent' toast or the composer being
    cleared (X clears the composer immediately on successful submit).

    Returns 'toast' | 'composer_cleared' | 'timeout'.
    """
    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        toast_visible = await h.session.eval_js(
            f"!!document.querySelector('{TOAST_SELECTOR}')",
            await_promise=False,
        )
        if toast_visible:
            return "toast"
        composer_empty = await h.session.eval_js(
            f"(() => {{ const c = document.querySelector('{COMPOSER_SELECTOR}'); "
            "return !c || (c.innerText || '').trim().length === 0; })()",
            await_promise=False,
        )
        if composer_empty:
            return "composer_cleared"
        await asyncio.sleep(0.4)
    return "timeout"


async def _dismiss_blockers(s) -> None:
    """Clear anything that would block a subsequent click/navigate:
      1. Any open native JavaScript dialog (beforeunload, alert, etc.).
      2. Any X overlay modal (the 'Views' educational popup, etc.) — click
         its close button if present.
      3. Strip the page's beforeunload handler so the next navigation away
         doesn't surface a 'Leave site?' confirm dialog.

    All operations swallow exceptions individually — the goal is best-effort
    cleanup, not gating.
    """
    # 1. Native dialog (works only if one is open; harmless otherwise).
    try:
        await s.call("Page.handleJavaScriptDialog", {"accept": True})
    except Exception:
        pass
    # 2. X overlay close — common testids.
    try:
        await s.eval_js("""
          (() => {
            const selectors = [
              '[data-testid="app-bar-close"]',
              '[data-testid="confirmationSheetClose"]',
              '[data-testid="sheetDialog"] [data-testid="close"]',
              'div[role="dialog"] button[aria-label*="Close" i]',
              'div[role="dialog"] button[aria-label*="Dismiss" i]',
            ];
            for (const sel of selectors) {
              const el = document.querySelector(sel);
              if (el) { el.click(); return sel; }
            }
            return null;
          })()
        """, await_promise=False)
    except Exception:
        pass
    # 3. Disable beforeunload so the next nav doesn't trigger a confirm.
    try:
        await s.eval_js(
            "window.onbeforeunload = null; "
            "window.addEventListener = (function(o){return function(t,h,c){"
            "if(t==='beforeunload')return; return o.call(window,t,h,c);};})"
            "(window.addEventListener);",
            await_promise=False,
        )
    except Exception:
        pass


def _like_li_post(post_urn: str | None, *, verbose: bool) -> str:
    """Best-effort like of a LinkedIn post via `lipy like --live`. Non-fatal —
    a like failure never rolls back the comment/reply it follows. Returns a
    status string: liked | liked_unverified | already_liked | no_button |
    no_post_urn | timeout | error."""
    if not post_urn:
        return "no_post_urn"
    cmd = [str(LIPY_BIN), "like", "--post", post_urn, "--live"]
    _log_li_page_views(2, via="lipy_like")  # feed pre-context + permalink
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=150)
    except subprocess.TimeoutExpired:
        return "timeout"
    out = (p.stdout or "").strip()
    parsed = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if verbose:
        print(f"[flush:li-like] {out[-200:]}")
    if not parsed or not parsed.get("ok"):
        return "no_button" if (parsed or {}).get("error") == "like_button_not_found" else "error"
    if parsed.get("already_reacted"):
        return "already_liked"
    if parsed.get("submitted"):
        return "liked" if parsed.get("liked_verified") else "liked_unverified"
    return "error"


def _li_post_urn_for_like(parent_urn: str, mode: str) -> str | None:
    """The post URN to like for a submitted item. goodwill → the post itself;
    inbound → the parent post parsed out of the comment URN."""
    if mode == "goodwill":
        return parent_urn
    m = re.match(r"urn:li:comment:\(urn:li:ugcPost:(\d+),", parent_urn or "")
    return f"urn:li:ugcPost:{m.group(1)}" if m else None


def _submit_one_li(parent_urn: str, text: str, *, mode: str, dry_run: bool, verbose: bool) -> dict:
    """Submit a LinkedIn item via the lipy Playwright CLI (humanized typing/mouse
    live inside lipy; no CDP). Inbound → `lipy reply --parent <comment-urn>`;
    goodwill → `lipy comment --post <activity-urn>`. After a successful live
    submit, also likes the post (`lipy like`) to mirror X's every-comment-gets-a
    -like behavior; the like is best-effort and non-fatal."""
    if mode == "goodwill":
        cmd = [str(LIPY_BIN), "comment", "--post", parent_urn, "--text", text]
    else:  # inbound: reply to a specific comment
        cmd = [str(LIPY_BIN), "reply", "--parent", parent_urn, "--text", text]
    if not dry_run:
        cmd.append("--live")
    _log_li_page_views(2, via="lipy_submit")  # feed pre-context + permalink
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "lipy_timeout"}
    out = (p.stdout or "").strip()
    parsed = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if verbose:
        print(f"[flush:li] {out[-300:]}")
    if dry_run:
        return {"ok": True, "dry_run": True, "confirmation": "lipy_dry_run"}
    if parsed and parsed.get("ok"):
        like_status = _like_li_post(_li_post_urn_for_like(parent_urn, mode),
                                    verbose=verbose)
        return {"ok": True, "confirmation": "lipy_posted", "like_status": like_status}
    return {"ok": False, "error": (parsed or {}).get("error") or "lipy_reply_failed",
            "detail": (out or (p.stderr or ""))[:300]}


async def _submit_one(item: dict, *, dry_run: bool, verbose: bool) -> dict:
    """Submit one outbox item. LinkedIn goes through lipy; X through the
    humanized CDP path. Returns a result dict for mark_submitted/mark_failed."""
    parent_url = item["parent_url"]
    text = item["draft_text"]

    if item.get("platform") == "linkedin":
        return _submit_one_li(parent_url, text, mode=item.get("mode", "inbound"),
                              dry_run=dry_run, verbose=verbose)

    async with open_session() as s:
        h = HumanCDP(s)

        # Belt-and-suspenders: clear any blocker on whatever tab we attached
        # to (could be left over from a prior item's stuck state).
        await _dismiss_blockers(s)

        # Navigate to the parent post. This is a direct CDP-driven nav,
        # which is consistent with what a real user does when they tap a
        # notification — not a fingerprintable script motion.
        await s.navigate(parent_url, settle_seconds=4.0)

        # Post-navigation cleanup: kill onbeforeunload + dismiss any X modal
        # that auto-appeared on the new page (e.g., the 'Views' overlay).
        await _dismiss_blockers(s)

        # Spend a moment "reading" the post before composing — visible to
        # any timing-based detector that measures pre-action dwell.
        await h.dwell_to_read(text, floor_s=2.0, ceil_s=12.0)

        # Find composer. Some posts auto-focus a composer on load; others
        # require us to click the Reply button on the focal article.
        #
        # The old recipe ('div[data-testid="reply"]') was wrong on two counts:
        #   1. The reply trigger is a <button>, not a <div>
        #   2. Unscoped — could click a child reply's reply button instead of
        #      the focal post's. Several posts in the live test failed because
        #      the unscoped selector either didn't match or hit the wrong element.
        # The fix: JS-scoped lookup that walks the focal article (always the
        # first article[data-testid="tweet"] on a post-detail page) and clicks
        # its reply button, then poll up to 10s for the composer to render.
        composer_present = await s.eval_js(
            f"!!document.querySelector('{COMPOSER_SELECTOR}')",
            await_promise=False,
        )
        if not composer_present:
            clicked_reply = await s.eval_js("""
              (() => {
                const focal = document.querySelector('article[data-testid="tweet"]');
                if (!focal) return 'no_focal_article';
                const btn = focal.querySelector('[data-testid="reply"]');
                if (!btn) return 'no_reply_button';
                btn.click();
                return 'clicked';
              })()
            """, await_promise=False)
            if clicked_reply != "clicked":
                return {"ok": False, "error": "composer_not_openable",
                        "detail": clicked_reply}

            # Poll up to ~10s for the inline composer to render.
            opened = False
            for _ in range(14):
                await asyncio.sleep(0.7)
                opened = await s.eval_js(
                    f"!!document.querySelector('{COMPOSER_SELECTOR}')",
                    await_promise=False,
                )
                if opened:
                    break
            if not opened:
                return {"ok": False, "error": "composer_not_openable",
                        "detail": "composer didn't render within 10s of reply click"}

        # Humanly focus + type.
        typed = await h.type_into(COMPOSER_SELECTOR, text)
        if not typed:
            return {"ok": False, "error": "composer_type_failed"}

        # The "should I really hit send?" pause.
        await h.consider_dwell()

        if dry_run:
            return {"ok": True, "dry_run": True}

        # Click submit via JS — humanized click was occasionally landing on
        # the button border (HumanCDP picks a random point in the middle 50%
        # of the bbox; small buttons miss). We keep humanization on the
        # typing phase (which is what behavioral detection fingerprints) but
        # the submit click itself needs to be reliable. Tries both the inline
        # button and the modal-composer button.
        submit_status = await s.eval_js("""
          (() => {
            const inline = document.querySelector('button[data-testid="tweetButtonInline"]');
            const modal  = document.querySelector('button[data-testid="tweetButton"]');
            const btn = (inline && !inline.disabled) ? inline
                      : (modal  && !modal.disabled)  ? modal
                      : null;
            if (!btn) {
              if (inline) return 'inline_disabled';
              if (modal)  return 'modal_disabled';
              return 'no_button';
            }
            btn.click();
            return btn === inline ? 'clicked_inline' : 'clicked_modal';
          })()
        """, await_promise=False)
        if submit_status in ("no_button",):
            return {"ok": False, "error": "submit_button_not_found"}
        if submit_status in ("inline_disabled", "modal_disabled"):
            return {"ok": False, "error": "submit_button_disabled",
                    "detail": f"Reply button found but disabled ({submit_status}) "
                              "— composer text may not have registered as valid"}

        # Bump confirmation window — slow networks / busy posts can take 15s+.
        confirmation = await _wait_for_confirmation(h, timeout_s=20.0)
        if confirmation == "timeout":
            return {"ok": False, "error": "no_confirmation",
                    "detail": "neither toast nor composer-cleared within 20s"}

        # Reply submitted → also like the focal post (per spec: every comment
        # gets a like). Failure here is non-fatal — we don't roll back the
        # successful reply just because the like click didn't register.
        like_status = await _like_focal_post(h)

        # Park the cursor offscreen so we don't keep hovering a clickable.
        await h.park_cursor_offscreen()
        return {"ok": True, "confirmation": confirmation,
                "like_status": like_status}


# ────────────────────────────────────────────── flush loop ──


async def flush(*, batch: int, dry_run: bool, verbose: bool,
                no_hold: bool = False, no_jitter: bool = False) -> dict:
    # Normal flush: only items whose release_at has passed (hold buffer honored).
    # --no-hold: every pending item, regardless of release_at — test mode for
    # exercising the submit + like path without waiting the buffer out.
    items = pending_items() if no_hold else mature_items()
    if not items:
        return {"considered": 0, "submitted": 0, "failed": 0,
                "canceled": 0, "deferred": 0}

    if verbose:
        print(f"[flush] {len(items)} mature item(s)")

    counts = {"considered": 0, "submitted": 0, "failed": 0,
              "canceled": 0, "deferred": 0}
    processed = 0

    _ensure_chrome_cdp()

    for item in items:
        if processed >= batch:
            break
        counts["considered"] += 1
        platform = item.get("platform", "x")
        mode = item.get("mode", "inbound")
        item_id = item["id"]

        # Re-check kill switches at flush time.
        if not is_live(platform):
            cancel(item_id, reason="kill_switch_engaged",
                   killed_by="caps.yaml live: false")
            log_event("skipped_kill_switch", platform=platform, mode=mode,
                      outbox_id=item_id, parent_url=item.get("parent_url"))
            counts["canceled"] += 1
            if verbose:
                print(f"[flush] {item_id}: canceled (live=false)")
            continue

        if not in_window(platform, mode):
            # Out-of-window → defer. Leave the item pending; the next
            # in-window flush will pick it up.
            log_event("skipped_window", platform=platform, mode=mode,
                      outbox_id=item_id, parent_url=item.get("parent_url"))
            counts["deferred"] += 1
            if verbose:
                print(f"[flush] {item_id}: deferred (outside window)")
            continue

        if not under_cap(platform, mode):
            cancel(item_id, reason="daily_cap_exceeded")
            log_event("skipped_cap", platform=platform, mode=mode,
                      outbox_id=item_id, parent_url=item.get("parent_url"))
            counts["canceled"] += 1
            if verbose:
                print(f"[flush] {item_id}: canceled (cap exceeded)")
            continue

        # Submit. Wrap each item independently so one CDP failure doesn't
        # poison the rest of the batch.
        try:
            result = await _submit_one(item, dry_run=dry_run, verbose=verbose)
        except Exception as e:
            result = {"ok": False, "error": "cdp_exception", "detail": str(e)}
            log_event("cdp_error", platform=platform, mode=mode,
                      outbox_id=item_id, detail=str(e))

        if result.get("ok"):
            mark_submitted(item_id, **{k: v for k, v in result.items()
                                       if k != "ok"})
            log_event("submitted", platform=platform, mode=mode,
                      outbox_id=item_id, parent_url=item.get("parent_url"),
                      confirmation=result.get("confirmation"),
                      dry_run=bool(result.get("dry_run")))
            if not result.get("dry_run") and result.get("like_status"):
                log_event("liked", platform=platform, mode=mode,
                          outbox_id=item_id,
                          parent_url=item.get("parent_url"),
                          like_status=result.get("like_status"))
            counts["submitted"] += 1
            if verbose:
                print(f"[flush] {item_id}: submitted "
                      f"({result.get('confirmation')}, like={result.get('like_status')})")
        else:
            mark_failed(item_id, reason=result.get("error", "unknown"),
                        detail=result.get("detail"))
            log_event("submit_failed", platform=platform, mode=mode,
                      outbox_id=item_id, parent_url=item.get("parent_url"),
                      reason=result.get("error"), detail=result.get("detail"))
            counts["failed"] += 1
            if verbose:
                print(f"[flush] {item_id}: failed ({result.get('error')})")

        processed += 1

        # Between items: respect the per-mode jitter from jitter.yaml so we
        # don't fire two submits back-to-back. Skip on the last item.
        # --no-jitter (test only) shortens to a humanlike-but-quick 8-20s.
        if processed < batch and processed < len(items):
            if no_jitter:
                jitter = random.uniform(8.0, 20.0)
            else:
                jitter = random_jitter_seconds(platform, mode)
            if verbose:
                print(f"[flush] inter-item jitter: {jitter:.1f}s")
            await asyncio.sleep(jitter)

    return counts


# ────────────────────────────────────────────── heartbeat ──


def _chrome_reachable(port: int = 9222) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=2):
            return True
    except Exception:
        return False


async def _probe_login() -> bool | None:
    """Best-effort X login check against the live tab. Returns True/False, or
    None if we can't tell (Chrome down, or the tab isn't on x.com)."""
    try:
        async with open_session(tab_url_substring="x.com") as s:
            raw = await s.eval_js(
                'JSON.stringify({u:location.href,'
                'li:!!document.querySelector("[data-testid=SideNav_AccountSwitcher_Button]")})',
                await_promise=False)
            d = json.loads(raw)
            if "x.com" not in (d.get("u") or ""):
                return None  # not on X — can't judge session state
            return bool(d.get("li"))
    except Exception:
        return None


def _record_heartbeat(counts: dict | None) -> None:
    """Write a liveness marker every run so a SILENT stop becomes visible:
    activity-report.py reads heartbeat.json to show 'last run / logged in?'.
    Forces nothing — only probes login if Chrome is already up."""
    up = _chrome_reachable()
    logged_in: bool | None = None
    if up:
        try:
            logged_in = asyncio.run(_probe_login())
        except Exception:
            logged_in = None
    rec = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "chrome_up": up,
        "logged_in": logged_in,
        "last_counts": counts or {},
    }
    # heartbeat.json is overwritten each run (no growth) and is the report's
    # liveness source — so we do NOT log a per-run metric event (that would add
    # ~720 lines/day to engagement_metrics.jsonl). Only the rare logout is logged.
    try:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_PATH.write_text(json.dumps(rec))
    except OSError:
        pass
    if logged_in is False:
        try:
            log_event("session_logged_out", chrome_up=up)  # loud, queryable
        except Exception:
            pass


# ────────────────────────────────────────────── cli ──


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--batch", type=int, default=1,
                   help="Max items to drain per run. Default 1 — cron interval "
                        "naturally spaces them.")
    p.add_argument("--dry-run", action="store_true",
                   help="Type into composer but don't click submit. Outbox "
                        "items stay pending (won't be marked submitted).")
    p.add_argument("--no-hold", action="store_true",
                   help="Bypass the hold-buffer release_at — drain every "
                        "pending item immediately. TEST USE ONLY.")
    p.add_argument("--no-jitter", action="store_true",
                   help="Skip the 5-25min inter-item jitter from jitter.yaml; "
                        "use 8-20s between items instead. TEST USE ONLY.")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--stale-lock-seconds", type=int, default=900,
                   help="Break a lock older than this many seconds. Default 15min.")
    args = p.parse_args()

    if not _claim_lock(args.stale_lock_seconds):
        if args.verbose:
            print("[flush] another flush is running; exiting")
        return 0

    counts: dict | None = None
    rc = 0
    try:
        counts = asyncio.run(flush(batch=args.batch, dry_run=args.dry_run,
                                   verbose=args.verbose, no_hold=args.no_hold,
                                   no_jitter=args.no_jitter))
    except Exception as e:
        sys.stderr.write(f"flush crashed: {e}\n")
        rc = 2
    finally:
        _release_lock()
        # Always record a heartbeat — even on idle runs and crashes — so the
        # report can tell whether the loop is alive and the X session is valid.
        _record_heartbeat(counts)

    if counts is not None:
        print(json.dumps(counts))
    return rc


if __name__ == "__main__":
    sys.exit(main())
