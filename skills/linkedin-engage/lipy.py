#!/usr/bin/env python3
"""
lipy — LinkedIn Playwright CLI wrapper for the linkedin-engage skill.

Subcommands emit JSON to stdout. All errors emit JSON to stderr and exit non-zero.
This is the entire interface the agent sees — Playwright internals never leak
into agent context.

Subcommands:
  lipy login [--headed]                       one-time interactive login (REQUIRES --headed)
  lipy doctor                                 health check (auth, session age)
  lipy status                                 brief status JSON
  lipy posts --limit N                        scrape user's last N posts
  lipy comments --post URN [--max-load N]     read comments on a post
  lipy inbound --since ISO --limit N          fetch new comments across user's posts (composes posts+comments)
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
STATE_DIR = HERMES_HOME / "state" / "playwright" / "linkedin"
PROFILE_DIR = STATE_DIR / "profile"   # persistent chromium user-data-dir
SESSION_FILE = STATE_DIR / "state.json"  # also saved as a portable backup

# Long-running session marker files. When `lipy session` is running, these
# point other commands to the browser via Chrome DevTools Protocol so they
# attach instead of launching a fresh Chromium.
SESSION_PID_FILE = STATE_DIR / "session.pid"
SESSION_PORT_FILE = STATE_DIR / "session.port"
CDP_PORT_DEFAULT = 9222

# URN map cache (activity URN ↔ ugcPost URN ↔ post text). Populated as we scrape,
# consulted by `lipy reply` to translate comment URNs (which carry ugcPost IDs)
# back to the activity URN that the activity-page cards expose.
URN_MAP_FILE = STATE_DIR / "urn_map.json"


def _urn_map_load() -> dict:
    if not URN_MAP_FILE.exists():
        return {"by_ugc": {}, "by_activity": {}}
    try:
        return json.loads(URN_MAP_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"by_ugc": {}, "by_activity": {}}


def _urn_map_save(m: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    URN_MAP_FILE.write_text(json.dumps(m, indent=2))


def _remember_urn(activity_urn: str | None, ugc_urn: str | None,
                  post_text: str | None) -> None:
    """Save whatever we know about a single post. Either URN may be None;
    if both are present we link them. post_text is optional but useful for
    text-hint matching."""
    if not activity_urn and not ugc_urn:
        return
    m = _urn_map_load()
    now = int(time.time())
    if activity_urn:
        a_id = activity_urn.rsplit(":", 1)[-1]
        entry = m["by_activity"].get(a_id, {})
        if ugc_urn:
            entry["ugc_urn"] = ugc_urn
        if post_text:
            entry["post_text"] = post_text[:600]
        entry["activity_urn"] = activity_urn
        entry["updated_at"] = now
        m["by_activity"][a_id] = entry
    if ugc_urn:
        u_id = ugc_urn.rsplit(":", 1)[-1]
        entry = m["by_ugc"].get(u_id, {})
        if activity_urn:
            entry["activity_urn"] = activity_urn
        if post_text:
            entry["post_text"] = post_text[:600]
        entry["ugc_urn"] = ugc_urn
        entry["updated_at"] = now
        m["by_ugc"][u_id] = entry
    _urn_map_save(m)


def _lookup_by_ugc(ugc_urn: str) -> dict | None:
    m = _urn_map_load()
    u_id = ugc_urn.rsplit(":", 1)[-1]
    return m["by_ugc"].get(u_id)


def _lookup_by_activity(activity_urn: str) -> dict | None:
    m = _urn_map_load()
    a_id = activity_urn.rsplit(":", 1)[-1]
    return m["by_activity"].get(a_id)


def _session_running() -> tuple[bool, int | None, int | None]:
    """Return (running, pid, port). Stale PID files are cleaned up."""
    if not SESSION_PID_FILE.exists() or not SESSION_PORT_FILE.exists():
        return False, None, None
    try:
        pid = int(SESSION_PID_FILE.read_text().strip())
        port = int(SESSION_PORT_FILE.read_text().strip())
    except (ValueError, OSError):
        SESSION_PID_FILE.unlink(missing_ok=True)
        SESSION_PORT_FILE.unlink(missing_ok=True)
        return False, None, None
    # Is the PID alive?
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        SESSION_PID_FILE.unlink(missing_ok=True)
        SESSION_PORT_FILE.unlink(missing_ok=True)
        return False, None, None
    return True, pid, port

# Realistic Chrome UA. Playwright's default mentions HeadlessChrome which LI flags.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


def _stealth(page) -> None:
    """Apply stealth tweaks. playwright-stealth's API changed across versions."""
    try:
        from playwright_stealth import Stealth  # newer API
        Stealth().apply_stealth_sync(page)
        return
    except ImportError:
        pass
    try:
        from playwright_stealth import stealth_sync  # older API
        stealth_sync(page)
    except ImportError:
        pass  # best-effort; not fatal


def _jitter(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def _err(msg: str, **extra) -> int:
    json.dump({"ok": False, "error": msg, **extra}, sys.stdout)
    return 1


@contextmanager
def linkedin_session(headed: bool = False, save_on_exit: bool = False):
    """If a `lipy session` daemon is running, ATTACH to it via CDP — no new
    browser, no profile lock contention. Otherwise launch a fresh persistent
    context (single-shot mode). Yields (None, context, page).

    Attach mode: do NOT close the browser on exit (the daemon owns it).
    Single-shot mode: close everything on exit (legacy behavior).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        json.dump(
            {"ok": False, "error": "playwright_not_installed",
             "fix": "cd skills/linkedin-engage && ./install.sh"},
            sys.stderr,
        )
        sys.exit(2)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    running, _pid, port = _session_running()
    pw = sync_playwright().start()

    if running and port:
        # Attach via CDP. The daemon process owns the browser.
        try:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        except Exception as e:
            pw.stop()
            json.dump({"ok": False, "error": "cdp_attach_failed",
                       "port": port, "detail": str(e)}, sys.stderr)
            sys.exit(2)
        # Use the daemon's existing context — DO NOT create a new one.
        if not browser.contexts:
            pw.stop()
            return _err("no_browser_context_on_daemon")  # type: ignore[return-value]
        context = browser.contexts[0]
        # Reuse the current visible tab if it's on LinkedIn; otherwise use the first page.
        page = None
        for p in context.pages:
            try:
                if "linkedin.com" in p.url:
                    page = p
                    break
            except Exception:
                continue
        if page is None:
            page = context.pages[0] if context.pages else context.new_page()
        _stealth(page)
        try:
            yield None, context, page
        finally:
            # Don't close — the daemon keeps the browser alive.
            try:
                browser.close()  # CDP-attached: this just disconnects, doesn't kill.
            except Exception:
                pass
            pw.stop()
        return

    # ── Single-shot mode (no daemon): launch fresh, close on exit ──
    proxy = None
    proxy_url = os.environ.get("LI_RESIDENTIAL_PROXY_URL")
    if proxy_url:
        proxy = {"server": proxy_url}

    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=not headed,
        proxy=proxy,
        user_agent=UA,
        viewport={"width": 1366, "height": 900},
        locale="en-US",
        timezone_id=os.environ.get("LI_TIMEZONE", "America/Los_Angeles"),
        args=[
            "--disable-blink-features=AutomationControlled",
        ],
    )
    page = context.pages[0] if context.pages else context.new_page()
    _stealth(page)
    try:
        yield None, context, page
    finally:
        if save_on_exit:
            try:
                context.storage_state(path=str(SESSION_FILE))
                os.chmod(SESSION_FILE, 0o600)
            except Exception as e:
                sys.stderr.write(f"[warn] save_state failed: {e}\n")
        try:
            context.close()
        finally:
            pw.stop()


def _check_auth(page) -> tuple[bool, str, str]:
    """Visit /feed/ and inspect the resulting URL.
    Returns (logged_in, reason, current_url)."""
    try:
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        return False, f"goto_failed: {e}", ""
    url = page.url
    if "/login" in url or "/uas/login" in url:
        return False, "not_logged_in", url
    if "/checkpoint" in url or "/challenge" in url:
        return False, "challenge_pending", url
    if "linkedin.com" not in url:
        return False, f"unexpected_redirect: {url}", url
    return True, "ok", url


# ─────────────────────────────────────────────────────────── commands ─────────


def _save_state(context) -> bool:
    """Persist context cookies/storage. Safe to call multiple times."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(SESSION_FILE))
        os.chmod(SESSION_FILE, 0o600)
        return True
    except Exception as e:
        sys.stderr.write(f"[warn] save_state failed: {e}\n")
        sys.stderr.flush()
        return False


def _is_logged_in_url(url: str) -> bool:
    if "/login" in url or "/uas/login" in url or "/checkpoint" in url or "/challenge" in url:
        return False
    return any(p in url for p in ("/feed", "/in/", "/notifications", "/messaging", "/jobs"))


def _li_at_cookie(context) -> str | None:
    """Return the value of LinkedIn's li_at auth cookie if present.
    This is the strongest 'logged in' signal — survives URL ambiguity."""
    try:
        cookies = context.cookies("https://www.linkedin.com")
    except Exception:
        return None
    for c in cookies:
        if c.get("name") == "li_at":
            v = c.get("value") or ""
            return v if v else None
    return None


def cmd_login(args: argparse.Namespace) -> int:
    if not args.headed:
        return _err("headed_required",
                    detail="First-time login must use --headed so you can complete 2FA manually.")
    # Avoid auto-save on context-manager exit since we drive saves explicitly.
    with linkedin_session(headed=True, save_on_exit=False) as (_b, context, page):
        try:
            page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            return _err("goto_login_failed", detail=str(e))

        sys.stderr.write(
            "Browser opened. Complete login (including 2FA if prompted). "
            "Waiting up to 15 min for you to land on a logged-in page...\n"
        )
        sys.stderr.flush()

        deadline = time.time() + 900
        last_log = 0.0
        while time.time() < deadline:
            # Inspect EVERY open page in the context (user may open new tabs).
            urls = []
            for p in context.pages:
                try:
                    urls.append(p.url)
                except Exception:
                    continue
            li_at = _li_at_cookie(context)
            url_match = any(_is_logged_in_url(u) for u in urls)
            if li_at or url_match:
                if _save_state(context):
                    json.dump({
                        "ok": True,
                        "session_saved_to": str(SESSION_FILE),
                        "profile_dir": str(PROFILE_DIR),
                        "landed_urls": urls,
                        "detected_via": "cookie" if li_at and not url_match else (
                            "url" if url_match and not li_at else "both"),
                    }, sys.stdout)
                    return 0
                return _err("save_state_failed", landed_urls=urls)
            now = time.time()
            if now - last_log > 15:
                sys.stderr.write(
                    f"[poll] pages={len(context.pages)} urls={urls} li_at={'set' if li_at else 'absent'}\n"
                )
                sys.stderr.flush()
                last_log = now
            time.sleep(1)
        return _err("login_timeout", detail="did not reach a logged-in page within 15 min")


def cmd_doctor(args: argparse.Namespace) -> int:
    out: dict[str, Any] = {
        "ok": False,
        "session_present": SESSION_FILE.exists(),
        "session_age_days": None,
        "proxy_configured": bool(os.environ.get("LI_RESIDENTIAL_PROXY_URL")),
        "username_configured": bool(os.environ.get("LI_USERNAME")),
        "checks": [],
    }
    # With persistent context, presence of the profile dir is the signal,
    # not the portable state.json file (which we save as a backup).
    profile_present = PROFILE_DIR.exists() and any(PROFILE_DIR.iterdir())
    out["profile_present"] = profile_present
    if SESSION_FILE.exists():
        out["session_age_days"] = round(
            (time.time() - SESSION_FILE.stat().st_mtime) / 86400, 1
        )
    if not profile_present:
        out["checks"].append({"name": "profile", "status": "FAIL",
                              "detail": "no chromium profile — run `lipy login --headed`"})
        json.dump(out, sys.stdout)
        return 1

    with linkedin_session(headed=False) as (_b, _c, page):
        ok, reason, url = _check_auth(page)
    out["auth_reason"] = reason
    out["landing_url"] = url
    if ok:
        out["ok"] = True
        out["checks"].append({"name": "auth", "status": "OK"})
    else:
        out["checks"].append({"name": "auth", "status": "FAIL", "detail": reason})
    json.dump(out, sys.stdout)
    return 0 if ok else 1


def cmd_status(_: argparse.Namespace) -> int:
    running, pid, port = _session_running()
    json.dump({
        "ok": True,
        "profile_present": PROFILE_DIR.exists(),
        "session_age_days": (
            round((time.time() - SESSION_FILE.stat().st_mtime) / 86400, 1)
            if SESSION_FILE.exists() else None
        ),
        "daemon_running": running,
        "daemon_pid": pid,
        "daemon_cdp_port": port,
    }, sys.stdout)
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    """Launch a long-running Chromium session that other lipy commands attach to.
    Foreground: stays open until you Ctrl+C. Other commands attach via CDP."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _err("playwright_not_installed",
                    fix="cd skills/linkedin-engage && ./install.sh")

    running, pid, port = _session_running()
    if running:
        return _err("session_already_running",
                    pid=pid, port=port,
                    note="Use `lipy session stop` first, or attach commands now.")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    port = args.port
    pw = sync_playwright().start()
    proxy = None
    proxy_url = os.environ.get("LI_RESIDENTIAL_PROXY_URL")
    if proxy_url:
        proxy = {"server": proxy_url}

    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,  # always visible — you're meant to use it like a real browser
        proxy=proxy,
        user_agent=UA,
        viewport={"width": 1366, "height": 900},
        locale="en-US",
        timezone_id=os.environ.get("LI_TIMEZONE", "America/Los_Angeles"),
        args=[
            f"--remote-debugging-port={port}",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    _stealth(page)

    # Navigate naturally to the feed (like a real user opening a tab).
    try:
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded",
                  timeout=30_000)
    except Exception as e:
        sys.stderr.write(f"[warn] could not reach /feed/: {e}\n")

    SESSION_PID_FILE.write_text(str(os.getpid()))
    SESSION_PORT_FILE.write_text(str(port))

    sys.stderr.write(
        f"╭──────────────────────────────────────────────────────────╮\n"
        f"│ lipy session ready                                       │\n"
        f"│   pid:       {os.getpid():<43} │\n"
        f"│   cdp port:  {port:<43} │\n"
        f"│                                                          │\n"
        f"│ Leave this terminal open. Other lipy commands will       │\n"
        f"│ attach to this browser instead of launching new ones.    │\n"
        f"│ You can also use the browser yourself — scroll, browse.  │\n"
        f"│                                                          │\n"
        f"│ Ctrl+C to stop the session.                              │\n"
        f"╰──────────────────────────────────────────────────────────╯\n"
    )
    sys.stderr.flush()

    def _cleanup(*_):
        SESSION_PID_FILE.unlink(missing_ok=True)
        SESSION_PORT_FILE.unlink(missing_ok=True)
        try:
            ctx.close()
        except Exception:
            pass
        pw.stop()
        sys.stderr.write("\nlipy session stopped.\n")

    import atexit
    import signal
    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))

    # Block until Ctrl+C or the user closes the browser window.
    try:
        while True:
            time.sleep(1)
            # If the context lost its pages (user closed the window), exit.
            try:
                if not ctx.pages:
                    sys.stderr.write("Browser window closed. Stopping session.\n")
                    break
            except Exception:
                break
    except KeyboardInterrupt:
        pass
    return 0


def cmd_session_stop(_: argparse.Namespace) -> int:
    running, pid, port = _session_running()
    if not running:
        json.dump({"ok": True, "was_running": False}, sys.stdout)
        return 0
    import signal as _signal
    try:
        os.kill(pid, _signal.SIGTERM)
    except ProcessLookupError:
        pass
    SESSION_PID_FILE.unlink(missing_ok=True)
    SESSION_PORT_FILE.unlink(missing_ok=True)
    json.dump({"ok": True, "stopped_pid": pid, "was_on_port": port}, sys.stdout)
    return 0


# ─── click-through navigation (no direct post URLs) ─────────────────────────


def _on_post_detail(page, post_urn_or_ugc: str) -> bool:
    """True if the current page is the detail view for this post."""
    try:
        u = page.url
    except Exception:
        return False
    if "linkedin.com/feed/update/" not in u:
        return False
    # The activity URN and the ugcPost URN are aliases; the URL may use either.
    return post_urn_or_ugc in u or post_urn_or_ugc.replace("activity", "ugcPost") in u


def navigate_to_own_post(
    page,
    post_urn: str,
    *,
    text_hint: str | None = None,
    max_scrolls: int = 12,
) -> bool:
    """Mimic a human: open the activity page, scroll until the target post is
    visible, then click into it. Returns True on success.

    Matching strategy (the activity page card carries `urn:li:activity:X` on its
    data-urn, but the same post is also referenced as `urn:li:ugcPost:Y` inside
    the card's HTML — comments are keyed by ugcPost while cards are keyed by
    activity, and the two numeric IDs are NOT equal):
      1. exact `data-urn` match (works if caller passed the activity URN)
      2. card outerHTML substring match for the full URN
      3. card outerHTML contains the numeric ID portion
      4. case-insensitive substring match on `text_hint` in card inner_text
    """
    ha = _import_human_actions()

    if _on_post_detail(page, post_urn):
        return True

    try:
        u = page.url
    except Exception:
        u = ""
    if "linkedin.com/feed" not in u and "linkedin.com/in/" not in u:
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded",
                  timeout=30_000)
        ha.dwell(0.8, 1.6)
        ha.smooth_scroll(page, 600)

    page.goto("https://www.linkedin.com/in/me/recent-activity/all/",
              wait_until="domcontentloaded", timeout=30_000)
    ha.dwell(1.4, 2.6)

    # Numeric ID portion (last component of the URN).
    target_num = post_urn.rsplit(":", 1)[-1] if ":" in post_urn else post_urn
    text_hint_norm = (text_hint or "").strip().lower()

    seen_target = None
    matched_via = None
    for _ in range(max_scrolls):
        cards = page.query_selector_all(
            '[data-urn^="urn:li:activity:"], [data-id^="urn:li:activity:"]'
        )
        for c in cards:
            data_urn = c.get_attribute("data-urn") or c.get_attribute("data-id") or ""
            if data_urn == post_urn:
                seen_target, matched_via = c, "data-urn"; break
            try:
                outer = c.evaluate("e => e.outerHTML") or ""
            except Exception:
                outer = ""
            if post_urn and post_urn in outer:
                seen_target, matched_via = c, "outerHTML(urn)"; break
            if target_num and target_num in outer:
                seen_target, matched_via = c, "outerHTML(numeric)"; break
            if text_hint_norm:
                try:
                    txt = (c.inner_text() or "").lower()
                except Exception:
                    txt = ""
                if text_hint_norm in txt:
                    seen_target, matched_via = c, "text_hint"; break
        if seen_target:
            break
        ha.smooth_scroll(page, 1200)

    if not seen_target:
        sys.stderr.write(f"[nav] no card matched urn={post_urn} hint={text_hint!r}\n")
        return False

    sys.stderr.write(f"[nav] matched via {matched_via}\n")
    try:
        seen_target.scroll_into_view_if_needed(timeout=4000)
    except Exception:
        pass
    ha.dwell(0.8, 1.6)

    # Activity-page cards have a different DOM than post-detail pages. Dump what's
    # available so we can pick the right click target.
    try:
        candidates = seen_target.evaluate("""e => {
            const out = [];
            // Every <a> with href
            for (const a of e.querySelectorAll('a[href]')) {
                out.push({tag: 'a', href: a.getAttribute('href'),
                          aria: a.getAttribute('aria-label') || '',
                          text: (a.innerText || '').slice(0, 60)});
            }
            // Every element with data-test* or role that suggests it's clickable
            for (const x of e.querySelectorAll('[role="button"], [role="link"], button')) {
                out.push({tag: x.tagName.toLowerCase(), href: '',
                          aria: x.getAttribute('aria-label') || '',
                          text: (x.innerText || '').slice(0, 60)});
            }
            return out.slice(0, 20);
        }""")
        sys.stderr.write(f"[nav] clickable candidates in card:\n")
        for c in candidates:
            sys.stderr.write(f"      {c}\n")
    except Exception as e:
        sys.stderr.write(f"[nav] could not enumerate clickables: {e}\n")

    # Activity-page cards expose post navigation only via buttons (no anchors to
    # /feed/update/). The "N comments on X's post" button opens the post detail
    # focused on the comments section — perfect for reply workflows.
    clickable = (
        seen_target.query_selector('button[aria-label*="comments on"]')
        or seen_target.query_selector('button[aria-label="Comment"]')
        or seen_target.query_selector('[componentkey^="feed-commentary_"]')
        or seen_target.query_selector('a[href*="/feed/update/"]')
    )
    if not clickable:
        sys.stderr.write("[nav] no clickable found; trying card itself\n")
        clickable = seen_target

    try:
        tag = clickable.evaluate("e => e.tagName.toLowerCase()")
        aria = clickable.get_attribute("aria-label") or ""
        href = clickable.get_attribute("href") or ""
        sys.stderr.write(f"[nav] clicking <{tag} aria='{aria[:60]}' href='{href[:80]}'>\n")
    except Exception:
        pass

    # Try human click first; if that fails to change the page, fall back to a
    # plain click (still works, just looks more robotic to LinkedIn).
    ha.human_click(page, clickable)
    ha.dwell(2.5, 4.5)
    try:
        post_url_check = page.url
    except Exception:
        post_url_check = ""
    if "/feed/update/" not in post_url_check:
        sys.stderr.write("[nav] human_click had no effect; retrying with plain .click()\n")
        try:
            clickable.click(timeout=4000)
            ha.dwell(2.0, 3.5)
        except Exception as e:
            sys.stderr.write(f"[nav] plain click also failed: {e}\n")
    # Still nothing? Try JS-dispatched click (synthetic but on the real element).
    try:
        post_url_check = page.url
    except Exception:
        post_url_check = ""
    if "/feed/update/" not in post_url_check:
        sys.stderr.write("[nav] retrying with JS .click()\n")
        try:
            clickable.evaluate("e => e.click()")
            ha.dwell(2.0, 3.5)
        except Exception as e:
            sys.stderr.write(f"[nav] JS click failed: {e}\n")

    # Two ways navigate can succeed:
    #   (a) we ended up on /feed/update/<urn>/ (true post-detail page), or
    #   (b) the comments expanded INLINE on the activity page and the parent
    #       comment is now in the DOM.
    try:
        new_url = page.url
    except Exception:
        new_url = ""
    on_detail = ("/feed/update/" in new_url) and (post_urn in new_url or target_num in new_url)
    inline_comment = False
    try:
        any_inline = page.query_selector('[componentkey^="replaceableComment_urn:li:comment:"]')
        inline_comment = any_inline is not None
    except Exception:
        pass
    success = on_detail or inline_comment
    sys.stderr.write(f"[nav] url={new_url[:140]} on_detail={on_detail} inline_comment={inline_comment}\n")
    return success


def _scrape_posts(page, limit: int) -> list[dict]:
    """Scroll the user's recent-activity feed and harvest posts.
    Returns up to `limit` items as [{urn, url, text, n_comments, n_reactions, n_impressions}]."""
    page.goto("https://www.linkedin.com/in/me/recent-activity/all/",
              wait_until="domcontentloaded", timeout=30_000)
    try:
        page.wait_for_selector("main", timeout=15_000)
    except Exception:
        pass
    _jitter(1.5, 3.0)

    seen: set[str] = set()
    posts: list[dict] = []
    max_scrolls = 12
    for _ in range(max_scrolls):
        cards = page.query_selector_all(
            '[data-urn^="urn:li:activity:"], [data-id^="urn:li:activity:"]'
        )
        for card in cards:
            urn = card.get_attribute("data-urn") or card.get_attribute("data-id")
            if not urn or urn in seen:
                continue
            seen.add(urn)
            # Post body text. LinkedIn rolled the activity-page DOM around 2026-05:
            # componentkey + data-testid attributes were dropped from this view and the
            # body is back inside class-named wrappers. Post-detail pages still use the
            # componentkey form, so keep it as a final fallback.
            body = ""
            body_el = (
                card.query_selector('.update-components-text')
                or card.query_selector('.feed-shared-update-v2__description')
                or card.query_selector('[componentkey^="feed-commentary_"]')
            )
            if body_el:
                try:
                    body = re.sub(r"\s+", " ", body_el.inner_text()).strip()[:1500]
                except Exception:
                    body = ""
            # Counts from the surrounding chrome.
            try:
                blob = card.inner_text()
            except Exception:
                blob = ""
            def _num(pattern: str) -> int | None:
                m = re.search(pattern, blob, re.I)
                return int(m.group(1)) if m else None
            # Remember activity URN + post text for later URN-map lookups.
            _remember_urn(activity_urn=urn, ugc_urn=None, post_text=body)
            posts.append({
                "urn": urn,
                "url": f"https://www.linkedin.com/feed/update/{urn}/",
                "text": body,
                "n_comments": _num(r"(\d+)\s*comments?"),
                "n_impressions": _num(r"(\d+)\s*impressions"),
            })
            if len(posts) >= limit:
                return posts
        page.mouse.wheel(0, 3200)
        _jitter(1.8, 3.6)
    return posts


def cmd_posts(args: argparse.Namespace) -> int:
    with linkedin_session(headed=args.headed) as (_b, _c, page):
        ok, reason, _ = _check_auth(page)
        if not ok:
            return _err(reason)
        posts = _scrape_posts(page, args.limit)
    json.dump({"ok": True, "count": len(posts), "posts": posts}, sys.stdout)
    return 0


def _scrape_comments(page, post_urn: str, max_load_clicks: int = 10) -> list[dict]:
    """Open a single post and harvest comments.
    Uses JS scrollIntoView to trigger LazyColumn rendering. Returns
    [{urn, author_name, author_handle, author_url, text, posted_label}]."""
    url = f"https://www.linkedin.com/feed/update/{post_urn}/"
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    _jitter(2.0, 3.5)

    # Force the LazyColumn for comments to render by scrolling it into view.
    triggered = page.evaluate("""() => {
        const sel = '[componentkey*="commentsSectionAnchor"], '
                  + '[componentkey*="pagedCommentsContainer"], '
                  + '[data-testid*="commentsSectionAnchor"], '
                  + '[data-testid*="pagedCommentsContainer"]';
        const el = document.querySelector(sel);
        if (el) { el.scrollIntoView({behavior: 'instant', block: 'center'}); return true; }
        return false;
    }""")
    _jitter(3.0, 4.5)

    # Click any load-more / show-previous buttons (idempotent loop).
    for _ in range(max_load_clicks):
        clicked = False
        for selector in (
            'button:has-text("Load more comments")',
            'button:has-text("Show more comments")',
            'button:has-text("more comments")',
            'button:has-text("Show previous comments")',
            'button:has-text("Show more replies")',
        ):
            try:
                btn = page.query_selector(selector)
                if btn and btn.is_visible():
                    btn.click()
                    clicked = True
                    _jitter(1.0, 2.0)
            except Exception:
                pass
        if not clicked:
            break

    # Each comment is a div with componentkey="replaceableComment_<urn>".
    items = page.query_selector_all('[componentkey^="replaceableComment_urn:li:comment:"]')
    # De-dup by URN — the same comment appears as multiple wrapper divs.
    seen: set[str] = set()
    comments: list[dict] = []
    for it in items:
        ck = it.get_attribute("componentkey") or ""
        m = re.match(r"replaceableComment_(urn:li:comment:\([^)]+\))", ck)
        if not m:
            continue
        urn = m.group(1)
        if urn in seen:
            continue
        seen.add(urn)

        # Author from the first /in/<handle> link.
        # The aria-label "View <Name>'s profile" lives on the SVG inside the <a>,
        # not on the <a> itself.
        author_name: str | None = None
        author_handle: str | None = None
        author_url: str | None = None
        try:
            a = it.query_selector('a[href*="/in/"]')
            if a:
                href = a.get_attribute("href") or ""
                author_url = href.split("?")[0]
                hm = re.search(r"/in/([^/?#]+)", href)
                if hm:
                    author_handle = hm.group(1)
                labelled = (
                    a.query_selector('[aria-label*="profile"]')
                    or a.query_selector("svg[aria-label]")
                )
                aria = labelled.get_attribute("aria-label") if labelled else ""
                am = re.match(r"View (.+?)['’]s profile", aria or "")
                if am:
                    author_name = am.group(1)
        except Exception:
            pass

        # Comment body — the <span data-testid="expandable-text-box">.
        text = ""
        try:
            t = it.query_selector('[data-testid="expandable-text-box"]')
            if not t:
                t = it.query_selector('[componentkey^="comment-commentary_"]')
            if t:
                text = re.sub(r"\s+", " ", t.inner_text()).strip()[:2000]
        except Exception:
            pass

        # Posted label: match time patterns like 1mo, 2d, 3h, 4m, 5w, 6y.
        # Exclude "1st" (connection-degree badge) and other non-time tokens.
        posted_label: str | None = None
        try:
            blob = it.inner_text()
            pm = re.search(r"\b(\d+)\s*(mo|[dhmwy])\b", blob, re.I)
            if pm:
                posted_label = f"{pm.group(1)}{pm.group(2).lower()}"
        except Exception:
            pass

        comments.append({
            "urn": urn,
            "author_name": author_name,
            "author_handle": author_handle,
            "author_url": author_url,
            "text": text,
            "posted_label": posted_label,
        })

    # The comment URN's first arg is the parent post's ugcPost URN.
    # The current page URL tells us the activity URN. Save the mapping.
    try:
        cur_url = page.url
    except Exception:
        cur_url = ""
    activity_match = re.search(r"urn:li:activity:\d+", cur_url)
    activity_urn = activity_match.group(0) if activity_match else None
    ugc_match = re.search(r"urn:li:ugcPost:\d+", cur_url)
    ugc_from_url = ugc_match.group(0) if ugc_match else None
    # Comment URNs also embed the ugcPost URN — use one as authoritative.
    ugc_from_comments = None
    if comments:
        cm = re.match(r"urn:li:comment:\((urn:li:ugcPost:\d+)", comments[0]["urn"] or "")
        if cm:
            ugc_from_comments = cm.group(1)
    if activity_urn or ugc_from_url or ugc_from_comments:
        _remember_urn(
            activity_urn=activity_urn,
            ugc_urn=ugc_from_url or ugc_from_comments,
            post_text=None,
        )
    return comments


def cmd_comments(args: argparse.Namespace) -> int:
    with linkedin_session(headed=args.headed) as (_b, _c, page):
        ok, reason, _ = _check_auth(page)
        if not ok:
            return _err(reason)
        comments = _scrape_comments(page, args.post, args.max_load)
    json.dump({"ok": True, "post_urn": args.post,
               "count": len(comments), "comments": comments}, sys.stdout)
    return 0


# ─── write ops with human emulation ───────────────────────────────────────────


def _import_human_actions():
    """Lazy-load human_actions so reads don't depend on it."""
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    import human_actions  # type: ignore
    return human_actions


def _find_top_level_comment_textbox(page, timeout_ms: int = 8000):
    """The TOP-level 'add a comment' input (above the comment list)."""
    selectors = [
        '[aria-label="Text editor for creating comment"]',
        '[aria-label*="Text editor for creating comment"]',
    ]
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        for sel in selectors:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return el
        time.sleep(0.25)
    return None


def _find_inline_reply_textbox(parent_el, page, timeout_ms: int = 10_000):
    """The inline reply input LinkedIn renders after Reply is clicked.
    Identify by DOM POSITION relative to the parent comment, NOT by aria-label
    (LinkedIn reuses 'Text editor for creating comment' for both top-level and
    inline reply boxes — only their location differs)."""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        # Try descendant first (some LI layouts render reply UI inside parent).
        try:
            tb = parent_el.query_selector('div[contenteditable="true"][role="textbox"]')
            if tb and tb.is_visible():
                return tb
        except Exception:
            pass

        # Then a following sibling of the parent (the common case).
        try:
            hdl = parent_el.evaluate_handle("""e => {
                let n = e.nextElementSibling;
                while (n) {
                    const t = n.querySelector('div[contenteditable="true"][role="textbox"]');
                    if (t && t.offsetParent !== null) return t;
                    n = n.nextElementSibling;
                }
                return null;
            }""")
            el = hdl.as_element() if hdl else None
            if el:
                return el
        except Exception:
            pass

        time.sleep(0.25)
    return None


def _find_submit_button(textbox, label_options: tuple[str, ...]):
    """Find a submit button near the given textbox. Walks up DOM parents and
    looks for a <button> whose visible text matches one of label_options."""
    labels_lower = [s.lower() for s in label_options]
    try:
        hdl = textbox.evaluate_handle(f"""(el, labels) => {{
            let n = el;
            while (n && n !== document.body) {{
                const btns = n.querySelectorAll('button');
                for (const b of btns) {{
                    if (b.disabled) continue;
                    const t = (b.innerText || '').trim().toLowerCase();
                    if (labels.includes(t)) return b;
                }}
                n = n.parentElement;
            }}
            return null;
        }}""", labels_lower)
        return hdl.as_element() if hdl else None
    except Exception:
        return None


def cmd_comment(args: argparse.Namespace) -> int:
    """Post a top-level comment on a post. Uses human emulation throughout.
    Default is --dry-run; pass --live to actually submit."""
    ha = _import_human_actions()
    submit_for_real = bool(args.live)  # --live overrides --dry-run default

    with linkedin_session(headed=args.headed, save_on_exit=True) as (_b, _c, page):
        ok, reason, _ = _check_auth(page)
        if not ok:
            return _err(reason)

        # Pre-context: come from the feed (real users don't teleport).
        try:
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded",
                      timeout=20_000)
            ha.dwell(1.2, 2.8)
            ha.smooth_scroll(page, 1200)
            ha.dwell(0.6, 1.4)
        except Exception:
            pass  # not critical

        # NAVIGATE BY CLICKING when possible: go to own activity page, find the
        # post, click it. Fall back to direct URL only if click-through fails.
        if not _on_post_detail(page, args.post):
            navigated = navigate_to_own_post(page, args.post)
            if not navigated:
                sys.stderr.write("[warn] click-through nav failed; falling back to direct URL\n")
                page.goto(f"https://www.linkedin.com/feed/update/{args.post}/",
                          wait_until="domcontentloaded", timeout=30_000)
                ha.dwell(1.5, 3.0)

        # Read the post.
        body_el = page.query_selector('[componentkey^="feed-commentary_"]')
        post_text = (body_el.inner_text() if body_el else "") or ""
        ha.read_dwell(post_text)
        ha.smooth_scroll(page, 600)

        # Force comment area to render.
        page.evaluate("""() => {
            const sel = '[componentkey*="commentsSectionAnchor"], '
                      + '[componentkey*="pagedCommentsContainer"]';
            const el = document.querySelector(sel);
            if (el) el.scrollIntoView({behavior: 'instant', block: 'center'});
        }""")
        ha.dwell(2.0, 3.5)

        # Locate the TOP-LEVEL comment textbox.
        textbox = _find_top_level_comment_textbox(page)
        if not textbox:
            return _err("comment_input_not_found")

        # Human-click + type.
        ha.human_click(page, textbox)
        ha.dwell(0.4, 0.9)
        try:
            textbox.click(timeout=2000)
        except Exception:
            pass
        ha.dwell(0.2, 0.5)
        ha.human_type(page, args.text)
        ha.consider_dwell()

        if not submit_for_real:
            try:
                typed = textbox.inner_text()
            except Exception:
                typed = ""
            json.dump({
                "ok": True,
                "dry_run": True,
                "target": "top_level_comment_box",
                "post_urn": args.post,
                "would_submit": args.text,
                "typed_in_box_preview": typed[:300],
                "note": "Pass --live to actually submit.",
            }, sys.stdout)
            return 0

        # Click the Comment button (LinkedIn's contenteditable doesn't submit on Cmd+Enter).
        submit_btn = _find_submit_button(textbox, ("comment", "post"))
        if not submit_btn:
            return _err("submit_button_not_found", note="typed but couldn't find the Comment button")
        ha.dwell(0.5, 1.2)
        ha.human_click(page, submit_btn)
        ha.dwell(2.5, 4.5)

        # Best-effort verification: scrape comments and look for ours.
        comments = _scrape_comments(page, args.post, max_load_clicks=2)
        head = (args.text[:40] or "").strip()
        landed = any(head and head in c["text"] for c in comments)
        json.dump({
            "ok": True,
            "dry_run": False,
            "post_urn": args.post,
            "submitted": True,
            "verified_in_thread": landed,
            "thread_size_after": len(comments),
        }, sys.stdout)
        return 0


def cmd_reply(args: argparse.Namespace) -> int:
    """Reply to a specific comment. Uses human emulation throughout.
    Default is --dry-run; pass --live to actually submit."""
    ha = _import_human_actions()
    submit_for_real = bool(args.live)  # --live overrides --dry-run default

    # Parse the post-activity URN out of the comment URN:
    # urn:li:comment:(urn:li:ugcPost:<id>,<commentid>)  →  ugcPost is the parent.
    cm = re.match(r"urn:li:comment:\(urn:li:ugcPost:(\d+),\d+\)", args.parent)
    if not cm:
        return _err("bad_parent_urn",
                    detail="expected urn:li:comment:(urn:li:ugcPost:<id>,<commentid>)")
    ugc_id = cm.group(1)
    post_ugc_urn = f"urn:li:ugcPost:{ugc_id}"

    with linkedin_session(headed=args.headed, save_on_exit=True) as (_b, _c, page):
        ok, reason, _ = _check_auth(page)
        if not ok:
            return _err(reason)

        # Resolve the activity URN + post text via the cache so we can navigate-by-click.
        cached = _lookup_by_ugc(post_ugc_urn)
        cache_activity_urn = (cached or {}).get("activity_urn")
        cache_text_hint = (cached or {}).get("post_text")
        # Caller-supplied hint wins over cache.
        text_hint = args.post_hint or cache_text_hint

        if not _on_post_detail(page, post_ugc_urn):
            navigated = False
            # Try the activity URN from cache first (best match for the activity page).
            if cache_activity_urn and navigate_to_own_post(page, cache_activity_urn,
                                                          text_hint=text_hint):
                navigated = True
            # Fall back to ugcPost URN (may match via outerHTML/text/hint).
            if not navigated and navigate_to_own_post(page, post_ugc_urn,
                                                     text_hint=text_hint):
                navigated = True
            if not navigated:
                sys.stderr.write("[warn] click-through nav failed; falling back to direct URL\n")
                page.goto(f"https://www.linkedin.com/feed/update/{post_ugc_urn}/",
                          wait_until="domcontentloaded", timeout=30_000)
                ha.dwell(1.5, 3.0)
        ha.smooth_scroll(page, 500)

        # Force comment area to render + expand all.
        page.evaluate("""() => {
            const sel = '[componentkey*="commentsSectionAnchor"], '
                      + '[componentkey*="pagedCommentsContainer"]';
            const el = document.querySelector(sel);
            if (el) el.scrollIntoView({behavior: 'instant', block: 'center'});
        }""")
        ha.dwell(2.5, 4.0)
        # Expand any "more comments" sections.
        for _ in range(6):
            clicked = False
            for selector in (
                'button:has-text("Load more comments")',
                'button:has-text("Show more comments")',
                'button:has-text("more comments")',
            ):
                btn = page.query_selector(selector)
                if btn and btn.is_visible():
                    ha.human_click(page, btn); clicked = True
                    ha.dwell(1.0, 2.0)
            if not clicked:
                break

        # Find the parent comment by URN.
        parent_el = page.query_selector(
            f'[componentkey="replaceableComment_{args.parent}"]'
        )
        if not parent_el:
            return _err("parent_comment_not_found", parent=args.parent)

        # Scroll the parent comment into view and read it.
        try:
            parent_el.scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass
        ha.dwell(0.6, 1.2)
        try:
            parent_text = parent_el.inner_text()
        except Exception:
            parent_text = ""
        ha.read_dwell(parent_text)

        # Find the Reply button INSIDE the parent comment.
        reply_btn = parent_el.query_selector(
            'button:has-text("Reply"), button[aria-label*="Reply"]'
        )
        if not reply_btn or not reply_btn.is_visible():
            return _err("reply_button_not_found")
        ha.human_click(page, reply_btn)
        ha.dwell(1.2, 2.4)

        # Find the INLINE reply textbox, scoped to the parent comment so we never
        # grab the top-level "Add a comment" box at the top of the page.
        textbox = _find_inline_reply_textbox(parent_el, page, timeout_ms=10_000)
        if not textbox:
            # Diagnostic: dump every contenteditable + its relationship to the parent.
            diag = page.evaluate("""(parentSel) => {
                const parent = document.querySelector(parentSel);
                const tbs = document.querySelectorAll('div[contenteditable="true"][role="textbox"]');
                return Array.from(tbs).map(t => {
                    const inParent = parent ? parent.contains(t) : false;
                    let inSibling = false;
                    if (parent) {
                        let n = parent.nextElementSibling;
                        while (n) {
                            if (n === t || n.contains(t)) { inSibling = true; break; }
                            n = n.nextElementSibling;
                        }
                    }
                    return {
                        aria: t.getAttribute('aria-label') || '',
                        visible: t.offsetParent !== null,
                        inParent: inParent,
                        inSibling: inSibling,
                    };
                });
            }""", f'[componentkey="replaceableComment_{args.parent}"]')
            return _err("inline_reply_input_not_found", textboxes_seen=diag)

        ha.human_click(page, textbox)
        ha.dwell(0.4, 0.9)
        try:
            textbox.click(timeout=2000)
        except Exception:
            pass
        ha.dwell(0.2, 0.5)
        ha.human_type(page, args.text)
        ha.consider_dwell()

        # Verify position via JS: is the textbox in a following sibling of
        # the parent comment? (The top-level box never is.)
        is_inline = False
        try:
            is_inline = textbox.evaluate("""(el, parentSel) => {
                const parent = document.querySelector(parentSel);
                if (!parent) return false;
                if (parent.contains(el)) return true;
                let n = parent.nextElementSibling;
                while (n) {
                    if (n === el || n.contains(el)) return true;
                    n = n.nextElementSibling;
                }
                return false;
            }""", f'[componentkey="replaceableComment_{args.parent}"]')
        except Exception:
            pass

        if not is_inline:
            return _err("wrong_textbox_targeted",
                        note="finder returned a box that isn't a child/sibling of the parent comment")

        try:
            typed = textbox.inner_text()
        except Exception:
            typed = ""

        if not submit_for_real:
            json.dump({
                "ok": True,
                "dry_run": True,
                "target": "inline_reply",
                "parent_urn": args.parent,
                "would_submit": args.text,
                "typed_in_box_preview": typed[:400],
                "note": "Pass --live to actually submit.",
            }, sys.stdout)
            return 0

        # Click the Reply button (LinkedIn's contenteditable doesn't submit on Cmd+Enter).
        submit_btn = _find_submit_button(textbox, ("reply",))
        if not submit_btn:
            return _err("submit_button_not_found",
                        note="typed but couldn't find the Reply button")
        ha.dwell(0.5, 1.2)
        ha.human_click(page, submit_btn)
        ha.dwell(2.5, 4.5)

        json.dump({
            "ok": True,
            "dry_run": False,
            "target": "inline_reply",
            "parent_urn": args.parent,
            "submitted": True,
        }, sys.stdout)
        return 0


def _scrape_my_comments(page, limit: int, debug: bool = False) -> list[dict]:
    """Scrape the user's own outbound comment history from
    /in/me/recent-activity/comments/. Each entry is a card showing the user's
    comment plus a preview of the parent post."""
    ha = _import_human_actions()

    page.goto("https://www.linkedin.com/in/me/recent-activity/comments/",
              wait_until="domcontentloaded", timeout=30_000)
    try:
        page.wait_for_selector("main", timeout=15_000)
    except Exception:
        pass
    ha.dwell(1.5, 2.8)

    if debug:
        sample = page.evaluate("""() => {
            const cards = document.querySelectorAll('[data-urn^="urn:li:activity:"]');
            const c = cards[0];
            if (!c) return null;
            // Walk children breadth-first and dump every element with TEXT (>30 chars).
            const out = [];
            const stack = [c];
            while (stack.length && out.length < 40) {
                const n = stack.shift();
                for (const child of n.children || []) {
                    stack.push(child);
                    const txt = (child.innerText || '').trim();
                    if (txt.length > 30 && txt.length < 500) {
                        out.push({
                            tag: child.tagName,
                            cls: (child.getAttribute('class') || '').slice(0, 40),
                            ck: (child.getAttribute('componentkey') || '').slice(0, 60),
                            txt: txt.slice(0, 200).replace(/\\n/g, ' | '),
                        });
                    }
                }
            }
            return {urn: c.getAttribute('data-urn'), elements: out};
        }""")
        if sample:
            sys.stderr.write(f"[debug] urn: {sample['urn']}\n")
            for e in sample['elements'][:30]:
                sys.stderr.write(f"  {e['tag']} ck={e['ck'][:30]} :: {e['txt']}\n")

    seen: set[str] = set()
    items: list[dict] = []
    max_scrolls = max(8, limit // 3)

    for _ in range(max_scrolls):
        cards = page.query_selector_all(
            '[data-urn^="urn:li:activity:"], [data-id^="urn:li:activity:"]'
        )
        for c in cards:
            urn = c.get_attribute("data-urn") or c.get_attribute("data-id") or ""
            try:
                blob = c.inner_text() or ""
            except Exception:
                blob = ""
            if not blob:
                continue

            # inner_text returns newline-separated lines. The user's comment text
            # comes immediately after the LAST line that is just a time label
            # (e.g. "1h", "3d", "1mo"). Earlier time labels in the card belong
            # to the parent post or embedded comments.
            lines = [ln.strip() for ln in blob.split("\n") if ln.strip()]
            time_re = re.compile(r"^\d+\s*(?:mo|[dhmwy])$", re.I)
            last_time_idx = None
            posted_label = None
            for i, ln in enumerate(lines):
                if time_re.match(ln):
                    last_time_idx = i
                    posted_label = re.sub(r"\s+", "", ln.lower())

            if last_time_idx is None:
                continue

            # Walk forward from the time label, accumulating comment lines.
            # Stop when we hit an action-bar line or a clear UI token.
            STOP_TOKENS = {
                "Like", "Reply", "Repost", "Send", "Comment",
                "…more", "More", "View analytics", "View activity",
            }
            comment_lines: list[str] = []
            for ln in lines[last_time_idx + 1:]:
                if ln in STOP_TOKENS:
                    break
                if re.fullmatch(r"\d+", ln):  # bare reaction count
                    break
                if re.match(r"^\d+\s*(comments?|replies|reactions?|impressions?)", ln, re.I):
                    break
                if re.match(r"^(Loaded\b|Your document)", ln):
                    break
                comment_lines.append(ln)
            comment_text = " ".join(comment_lines).strip()
            if not comment_text:
                continue

            # Dedup by normalized comment text alone — LinkedIn re-renders the
            # same comment across multiple cards (engagement view, archive view,
            # etc.) with different parent URNs but identical content.
            key = re.sub(r"\s+", " ", comment_text.lower())[:120]
            if key in seen:
                continue
            seen.add(key)

            # Action header — "Sal AI ... replied to X's comment on this" or
            # "Sal AI ... commented on this". Search the raw blob.
            ah = re.search(r"(replied to .+?’s comment|commented on this)", blob)
            action = ah.group(0) if ah else None
            is_reply = action is not None and "replied to" in action

            # Parent author — the SECOND /in/ link (first is the user themselves).
            parent_author_handle = None
            parent_author_name = None
            try:
                links = c.query_selector_all('a[href*="/in/"]')
                # First link in the card is the actor (you). Find the next distinct profile.
                user_handle = None
                for a in links:
                    href = a.get_attribute("href") or ""
                    hm = re.search(r"/in/([^/?#]+)", href)
                    if not hm:
                        continue
                    handle = hm.group(1)
                    if user_handle is None:
                        user_handle = handle
                        continue
                    if handle != user_handle:
                        parent_author_handle = handle
                        labelled = a.query_selector('[aria-label*="profile"]') or a.query_selector("svg[aria-label]")
                        aria = labelled.get_attribute("aria-label") if labelled else ""
                        am = re.match(r"View (.+?)['’]s profile", aria or "")
                        if am:
                            parent_author_name = am.group(1)
                        break
            except Exception:
                pass

            parent_url = (f"https://www.linkedin.com/feed/update/{urn}/"
                          if urn.startswith("urn:li:activity:") else None)

            items.append({
                "parent_urn": urn,
                "parent_url": parent_url,
                "parent_author_name": parent_author_name,
                "parent_author_handle": parent_author_handle,
                "is_reply_to_comment": is_reply,
                "comment_text": comment_text[:2000],
                "posted_label": posted_label,
            })
            if len(items) >= limit:
                return items
        ha.smooth_scroll(page, 1400)

    return items


def cmd_my_comments(args: argparse.Namespace) -> int:
    """List the user's own comments across all posts. Highest-signal voice data."""
    with linkedin_session(headed=args.headed) as (_b, _c, page):
        ok, reason, _ = _check_auth(page)
        if not ok:
            return _err(reason)
        items = _scrape_my_comments(page, args.limit, debug=args.debug)

    out = {"ok": True, "count": len(items), "comments": items}

    if args.save:
        # Append JSONL into corpus/linkedin_comments.jsonl in the project.
        project_root = Path(__file__).resolve().parents[2]  # skills/linkedin-engage → project root
        corpus_path = project_root / "corpus" / "linkedin_comments.jsonl"
        corpus_path.parent.mkdir(parents=True, exist_ok=True)
        # De-dup against existing entries by (parent_urn + comment_text-first-80).
        existing_keys: set[str] = set()
        if corpus_path.exists():
            for ln in corpus_path.read_text().splitlines():
                try:
                    rec = json.loads(ln)
                    existing_keys.add(
                        (rec.get("parent_urn") or "") + "::" +
                        (rec.get("comment_text") or "")[:80]
                    )
                except json.JSONDecodeError:
                    continue
        added = 0
        with corpus_path.open("a") as f:
            for item in items:
                key = (item.get("parent_urn") or "") + "::" + (item.get("comment_text") or "")[:80]
                if key in existing_keys:
                    continue
                f.write(json.dumps(item) + "\n")
                added += 1
        out["saved_to"] = str(corpus_path)
        out["added_to_corpus"] = added
        out["skipped_dupes"] = len(items) - added

    json.dump(out, sys.stdout)
    return 0


def cmd_inbound(args: argparse.Namespace) -> int:
    """Compose posts + comments: list user's recent posts, then comments on each."""
    with linkedin_session(headed=args.headed) as (_b, _c, page):
        ok, reason, _ = _check_auth(page)
        if not ok:
            return _err(reason)

        posts = _scrape_posts(page, args.limit)
        results = []
        for p in posts:
            cs = _scrape_comments(page, p["urn"], max_load_clicks=6)
            results.append({**p, "comments": cs})
            _jitter(3, 8)  # be polite between posts
    json.dump({"ok": True, "count": len(results), "posts": results}, sys.stdout)
    return 0


# ───────────────────────────────────────────────────────────────────────── main


def main() -> int:
    p = argparse.ArgumentParser(prog="lipy")
    sp = p.add_subparsers(dest="cmd", required=True)

    login = sp.add_parser("login")
    login.add_argument("--headed", action="store_true")
    login.set_defaults(fn=cmd_login)

    sp.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sp.add_parser("status").set_defaults(fn=cmd_status)

    posts = sp.add_parser("posts")
    posts.add_argument("--limit", type=int, default=5)
    posts.add_argument("--headed", action="store_true",
                       help="run with a visible browser (debugging only)")
    posts.set_defaults(fn=cmd_posts)

    comments = sp.add_parser("comments")
    comments.add_argument("--post", required=True, help="urn:li:activity:...")
    comments.add_argument("--max-load", type=int, default=10,
                          help="max clicks on 'Load more comments'")
    comments.add_argument("--headed", action="store_true")
    comments.set_defaults(fn=cmd_comments)

    my_comments = sp.add_parser("my-comments",
        help="Scrape your own outbound comment history (voice corpus).")
    my_comments.add_argument("--limit", type=int, default=50,
                             help="max comments to return (default 50)")
    my_comments.add_argument("--headed", action="store_true",
                             help="visible browser if no daemon running")
    my_comments.add_argument("--save", action="store_true",
                             help="append results to corpus/linkedin_comments.jsonl")
    my_comments.add_argument("--debug", action="store_true",
                             help="dump DOM samples for selector tuning")
    my_comments.set_defaults(fn=cmd_my_comments)

    inbound = sp.add_parser("inbound")
    inbound.add_argument("--since", required=False, default=None,
                         help="(reserved; not yet used by read-only mode)")
    inbound.add_argument("--limit", type=int, default=5)
    inbound.add_argument("--headed", action="store_true")
    inbound.set_defaults(fn=cmd_inbound)

    session = sp.add_parser("session",
        help="Open a long-running Chromium window. Other lipy commands attach to it via CDP.")
    session.add_argument("--port", type=int, default=CDP_PORT_DEFAULT,
                         help=f"CDP port (default {CDP_PORT_DEFAULT})")
    session.set_defaults(fn=cmd_session)

    session_stop = sp.add_parser("session-stop", help="Stop the running session daemon.")
    session_stop.set_defaults(fn=cmd_session_stop)

    reply = sp.add_parser("reply", help="Reply to a specific comment (human-emulated).")
    reply.add_argument("--parent", required=True,
                       help="urn:li:comment:(urn:li:ugcPost:<id>,<commentid>)")
    reply.add_argument("--text", required=True)
    reply.add_argument("--post-hint", default=None,
                       help="first words of the post (manual fallback for navigate-by-click "
                            "when the URN cache is empty)")
    reply.add_argument("--headed", action="store_true", default=True,
                       help="visible browser (default; safer for writes)")
    reply.add_argument("--headless", dest="headed", action="store_false")
    reply.add_argument("--dry-run", action="store_true", default=True,
                       help="default: type but do NOT submit")
    reply.add_argument("--live", action="store_true",
                       help="actually submit (overrides --dry-run)")
    reply.set_defaults(fn=cmd_reply)

    comment = sp.add_parser("comment", help="Top-level comment on a post (human-emulated).")
    comment.add_argument("--post", required=True,
                         help="urn:li:activity:... or urn:li:ugcPost:...")
    comment.add_argument("--text", required=True)
    comment.add_argument("--headed", action="store_true", default=True)
    comment.add_argument("--headless", dest="headed", action="store_false")
    comment.add_argument("--dry-run", action="store_true", default=True)
    comment.add_argument("--live", action="store_true")
    comment.set_defaults(fn=cmd_comment)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
