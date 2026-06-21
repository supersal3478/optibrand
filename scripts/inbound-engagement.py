#!/usr/bin/env python3
"""inbound-engagement.py — reply to third-party comments on YOUR posts.

For each new inbound comment on X / LinkedIn:
  1. Draft a reply via voice + BRAND.md (no first names, length-distributed:
     50% ≤7w, 25% ≤15w, 25% ≤25w, no trailing period).
  2. Inline brand-guard (em-dash strip, hashtag/sycophantic refuse).
  3. --live   : enqueue to ~/.hermes/state/outbox.jsonl with mode="inbound"
                (30-min hold buffer via caps.yaml; outbox-flush.py submits +
                likes); ALSO queue the commenter for follow-up engagement in
                ~/.hermes/state/commenter_queue.jsonl.
     dry-run  : X — open composer-prefilled tab; LinkedIn — stdout copy-paste.

Idempotent. Re-running skips items already drafted (state at
~/.hermes/state/engagement_seen.json). Pass --reset-seen to clear.

Usage:
    scripts/inbound-engagement.py                       # both platforms
    scripts/inbound-engagement.py --platforms x         # X only
    scripts/inbound-engagement.py --live                # autonomous; outbox
    scripts/inbound-engagement.py --limit 3
    scripts/inbound-engagement.py --reset-seen
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import websockets

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
HERMES_BIN = PROJECT_ROOT / "vendor" / "hermes-agent" / ".venv" / "bin" / "hermes"
LIPY_BIN = Path(os.environ.get("LIPY_BIN", Path.home() / ".local" / "bin" / "lipy"))
START_CHROME_CDP = PROJECT_ROOT / "skills" / "x-engage" / "start-chrome-cdp.sh"
SEEN_PATH = HERMES_HOME / "state" / "engagement_seen.json"
BLOCKLIST_PATH = HERMES_HOME / "state" / "engagement_blocklist.json"
COMMENTER_QUEUE_PATH = HERMES_HOME / "state" / "commenter_queue.jsonl"
CDP_PORT = 9222

# Phase-2 shared libs.
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import _outbox  # noqa: E402
import _metrics  # noqa: E402
from _drafter import (  # noqa: E402
    pick_length_target,
    apply_length_and_punctuation_fixes,
)

X_MENTIONS_URL = "https://x.com/notifications/mentions"
X_COMPOSER_SELECTOR = 'div[data-testid="tweetTextarea_0"]'
X_SETTLE_SECONDS = 10  # X SPA is slow; profile took 12s in practice
X_RECENT_POSTS_LIMIT = 40  # cap how many of my recent posts to inspect for replies

# Pull all visible items (mentions OR my own posts) on the current page.
# Includes pinned-flag detection (X marks pinned posts with a 'Pinned' socialContext)
# and the time element so we can filter by age.
X_EXTRACT_MENTIONS_JS = """
(() => {
  const cards = Array.from(document.querySelectorAll('article[data-testid="tweet"]')).slice(0, 25);
  return cards.map(a => {
    const u = a.querySelector('[data-testid="User-Name"]');
    const t = a.querySelector('[data-testid="tweetText"]');
    const link = a.querySelector('a[href*="/status/"]');
    const replyBtn = a.querySelector('button[data-testid="reply"]');
    const time = a.querySelector('time');
    const ctx = a.querySelector('[data-testid="socialContext"]');
    const ctxText = ctx ? ctx.innerText : '';
    return {
      author: u ? u.innerText.replace(/\\n/g, ' | ') : '',
      text: t ? t.innerText : '',
      url: link ? link.href : '',
      reply_aria: replyBtn ? replyBtn.getAttribute('aria-label') : '',
      ts: time ? time.getAttribute('datetime') : '',
      pinned: /pinned/i.test(ctxText),
      social_context: ctxText,
    };
  }).filter(r => r.url);
})()
"""


def parse_reply_count(reply_aria: str) -> int:
    """'3 Replies. Reply' → 3; '0 Replies. Reply' → 0; '' → 0."""
    if not reply_aria:
        return 0
    head = reply_aria.split(" ")[0].replace(",", "").strip()
    try:
        return int(head)
    except ValueError:
        return 0


def extract_x_handle_from_brand() -> str | None:
    """Parse the X handle out of BRAND.md so we don't hard-code it.
    Looks for '@something (X)' or 'Handle: @something'."""
    brand = PROJECT_ROOT / "BRAND.md"
    if not brand.exists():
        return None
    import re
    txt = brand.read_text()
    m = re.search(r"@([A-Za-z0-9_]+)\s*\(X\)", txt)
    if m:
        return m.group(1)
    return None


def utcnow() -> str:
    return datetime.now(tz=ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_seen() -> dict:
    if not SEEN_PATH.exists():
        return {}
    try:
        return json.loads(SEEN_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def save_seen(seen: dict) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen, indent=2))


def load_blocklist() -> dict:
    """Returns {'x': [urls], 'linkedin': [urns]}.

    Layer-three defense against engaging with the wrong content:
      1. socialContext 'Pinned' DOM detection (can break if X redesigns)
      2. max_age_days cap on parent post (default 14)
      3. THIS hard URL blocklist (immutable user-curated kill list)
    """
    if not BLOCKLIST_PATH.exists():
        return {"x": [], "linkedin": []}
    try:
        data = json.loads(BLOCKLIST_PATH.read_text())
    except json.JSONDecodeError:
        return {"x": [], "linkedin": []}
    return {
        "x": [str(u).rstrip("/") for u in (data.get("x") or [])],
        "linkedin": [str(u) for u in (data.get("linkedin") or [])],
    }


def is_blocklisted(platform: str, url: str, blocklist: dict) -> bool:
    return url.rstrip("/") in blocklist.get(platform, [])


def _run(cmd: list[str], timeout: int = 180) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _parse_json(s: str) -> dict | None:
    s = (s or "").strip()
    if not s:
        return None
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


# ─── reply-drafter via direct Azure OpenAI call ────────────────────────
# Bypass `hermes chat` (7+ min per call, too slow for engagement). Single
# /chat/completions request returns in 5–15s. We inline a minimal brand-guard
# (em-dash, sycophantic opener, hashtag) — the full brand-guard skill can be
# wired back in once we move from test to production via the daemon path.

_AZURE_CONFIG: dict = {}


def _load_azure_config() -> dict:
    global _AZURE_CONFIG
    if _AZURE_CONFIG:
        return _AZURE_CONFIG
    env_path = HERMES_HOME / ".env"
    cfg = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"')
    _AZURE_CONFIG = {
        "api_key": cfg.get("AZURE_FOUNDRY_API_KEY", ""),
        "base_url": cfg.get("AZURE_FOUNDRY_BASE_URL", "").rstrip("/"),
        "model": cfg.get("AZURE_FOUNDRY_MODEL", "DeepSeek-V4-Flash"),
    }
    return _AZURE_CONFIG


def _brand_voice_context() -> str:
    """Build a compact voice/brand context string from BRAND.md + voice_profile.json.
    Keep under ~2.5K chars so the prompt stays cheap."""
    parts = []
    brand_path = PROJECT_ROOT / "BRAND.md"
    if brand_path.exists():
        # Take ~2000 chars of BRAND.md — enough for identity + voice rules.
        parts.append("=== BRAND.md (truncated) ===\n" + brand_path.read_text()[:2200])
    vp_path = HERMES_HOME / "memories" / "voice_profile.json"
    if vp_path.exists():
        try:
            vp = json.loads(vp_path.read_text())
            sig = vp.get("vocabulary", {}).get("signature_phrases", [])[:15]
            pref = vp.get("vocabulary", {}).get("preferred_words", [])[:20]
            tone = vp.get("tone", {})
            parts.append("=== voice_profile.json (distilled) ===\n"
                         f"tone: {json.dumps(tone)}\n"
                         f"signature_phrases: {json.dumps(sig)}\n"
                         f"preferred_words: {json.dumps(pref)}")
        except json.JSONDecodeError:
            pass
    return "\n\n".join(parts)


def _inline_brand_guard(draft: str) -> tuple[str, list[str], list[str]]:
    """Minimal brand-guard pass.

    Returns (cleaned_draft, reasons_refused, autofixes_applied).
    - Em-dash / en-dash → replaced with ', ' (DeepSeek loves them; not worth refusing over).
    - Hashtags → REFUSE (signals lazy / off-brand).
    - Sycophantic openers → REFUSE.
    """
    autofixes: list[str] = []
    cleaned = draft
    if "—" in cleaned or "–" in cleaned:
        cleaned = cleaned.replace(" — ", ", ").replace("—", ", ")
        cleaned = cleaned.replace(" – ", ", ").replace("–", ", ")
        autofixes.append("stripped em-dash/en-dash")

    refused: list[str] = []
    if "#" in cleaned:
        refused.append("contains hashtag")
    lowered = cleaned.lower().lstrip()
    bad_openers = ("great question", "absolutely", "thanks for sharing", "i love this", "this is amazing")
    for op in bad_openers:
        if lowered.startswith(op):
            refused.append(f"sycophantic opener '{op}'")
            break
    return (cleaned, refused, autofixes)


def draft_reply(platform: str, author: str, text: str) -> dict:
    """Returns {decision: DRAFT|REFUSE, draft|reasons: ..., length_target: ...}.
    One direct Azure /chat/completions call. ~5–15s.

    Applies the same stylistic rules as feed-engagement.py goodwill drafter:
      • No first name / display name / handle of the commenter
      • Random length target (50% 7w, 25% 15w, 25% 25w)
      • No trailing period
      • Inline brand-guard (em-dash strip, hashtag/sycophantic refuse)
    """
    import httpx
    cfg = _load_azure_config()
    if not cfg["api_key"] or not cfg["base_url"]:
        return {"decision": "REFUSE", "reasons": [{"rule": "no-azure-config"}]}

    voice_ctx = _brand_voice_context()
    length_label, length_instruction, max_words = pick_length_target()
    system = (
        "You are Sal AI (@salaicreates). You're drafting a reply to a comment that "
        "someone left on one of your X / LinkedIn posts. "
        "Match the voice/tone/vocabulary distilled below from BRAND.md and your voice profile.\n\n"
        "DO NOT use the commenter's name, first name, display name, or handle anywhere in the reply. "
        "Engage directly with what they said.\n\n"
        f"{length_instruction}\n\n"
        "No em-dash (—). No hashtags. No sycophantic openers ('Great question', "
        "'Absolutely', 'Thanks for sharing', 'I love this', 'Great point', etc.). "
        "Output ONLY the reply text — no JSON, no quotes, no preamble."
    )
    user = (
        f"=== voice + brand context ===\n{voice_ctx}\n\n"
        f"=== reply target ===\n"
        f"platform: {platform}\n"
        f"commenter handle (DO NOT mention): {author}\n"
        f"comment text: {text}\n\n"
        "Write the reply now."
    )

    t0 = time.time()
    try:
        resp = httpx.post(
            f"{cfg['base_url']}/chat/completions",
            headers={"api-key": cfg["api_key"], "Content-Type": "application/json"},
            json={
                "model": cfg["model"],
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "max_tokens": 400,
                "temperature": 0.7,
            },
            timeout=90,
        )
    except httpx.RequestError as e:
        return {"decision": "REFUSE", "reasons": [{"rule": "http-error", "detail": str(e)[:200]}]}
    print(f"        … LLM returned in {time.time()-t0:.1f}s (HTTP {resp.status_code})", flush=True)
    if resp.status_code != 200:
        return {"decision": "REFUSE",
                "reasons": [{"rule": "azure-non-200", "status": resp.status_code,
                             "body": resp.text[:300]}]}
    body = resp.json()
    draft = (body.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    if not draft:
        return {"decision": "REFUSE", "reasons": [{"rule": "empty-llm-output"}]}

    cleaned, refused, autofixes = _inline_brand_guard(draft)
    if refused:
        return {"decision": "REFUSE", "draft": cleaned, "raw_draft": draft,
                "reasons": [{"rule": r} for r in refused],
                "autofixes": autofixes,
                "length_target": length_label}
    result = {"decision": "DRAFT", "draft": cleaned, "raw_draft": draft,
              "autofixes": autofixes, "length_target": length_label}
    apply_length_and_punctuation_fixes(result, max_words)
    return result


# ─── X side ───────────────────────────────────────────────────────────

def cdp_chrome_alive() -> bool:
    try:
        with urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_cdp_chrome() -> bool:
    if cdp_chrome_alive():
        return True
    rc, _, err = _run(["bash", str(START_CHROME_CDP)], timeout=30)
    if rc != 0:
        print(f"[x] start-chrome-cdp failed: {err.strip()}", file=sys.stderr)
        return False
    for _ in range(10):
        if cdp_chrome_alive():
            return True
        time.sleep(1)
    return False


def cdp_open_new_tab(url: str) -> dict:
    """Open a new tab in CDP Chrome pointing at url. Returns the tab object."""
    req = urllib.request.Request(
        f"http://localhost:{CDP_PORT}/json/new?{url}",
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def cdp_find_tab(url_substring: str) -> dict | None:
    with urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json", timeout=5) as r:
        tabs = json.load(r)
    for t in tabs:
        if t.get("type") == "page" and url_substring in (t.get("url") or ""):
            return t
    return None


async def _ws_call(ws, mid: int, method: str, params: dict | None = None) -> dict:
    await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=20)
        data = json.loads(raw)
        if data.get("id") == mid:
            return data


async def _navigate_and_extract(url: str, max_wait_s: float = 25.0,
                                scroll_passes: int = 0) -> list[dict]:
    """Navigate the existing x.com tab to `url`, poll for articles, optionally scroll
    to load more, then extract.

    X's SPA renders timelines lazily:
      - The initial Page.navigate returns immediately; article[data-testid=tweet]
        elements may not exist for 10–15s.
      - Even after they appear, only the first 2–3 are above-the-fold. To see more
        posts on a profile, you have to scroll. We do scroll_passes window.scrollBy
        calls with a 1.5s wait each, which X uses to lazy-load the next batch.
    """
    tab = cdp_find_tab("x.com")
    if not tab:
        tab = cdp_open_new_tab(url)
        await asyncio.sleep(3)
    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=20_000_000) as ws:
        await _ws_call(ws, 1, "Page.navigate", {"url": url})
        await asyncio.sleep(3)
        mid = 2
        elapsed = 3.0
        articles = 0
        # Phase 1: wait for the FIRST article to appear.
        while elapsed < max_wait_s:
            r = await _ws_call(ws, mid, "Runtime.evaluate", {
                "expression": 'document.querySelectorAll("article[data-testid=\\"tweet\\"]").length',
                "returnByValue": True,
            })
            mid += 1
            articles = r.get("result", {}).get("result", {}).get("value") or 0
            if articles > 0:
                break
            await asyncio.sleep(2)
            elapsed += 2
        if articles == 0:
            print(f"[x]   (still 0 articles after {elapsed:.0f}s — page may be empty or DOM shifted)",
                  flush=True)
            return []
        # Phase 2: scroll to load more posts. X lazy-loads each scroll.
        for i in range(scroll_passes):
            await _ws_call(ws, mid, "Runtime.evaluate", {
                "expression": "window.scrollBy(0, window.innerHeight * 1.5); true",
                "userGesture": True,
            })
            mid += 1
            await asyncio.sleep(1.8)
            r = await _ws_call(ws, mid, "Runtime.evaluate", {
                "expression": 'document.querySelectorAll("article[data-testid=\\"tweet\\"]").length',
                "returnByValue": True,
            })
            mid += 1
            new_count = r.get("result", {}).get("result", {}).get("value") or 0
            print(f"[x]   scroll #{i+1}: {new_count} articles loaded", flush=True)
            if new_count == articles:
                break  # no more lazy-load to be had
            articles = new_count
        # Phase 3: extract.
        result = await _ws_call(ws, mid, "Runtime.evaluate", {
            "expression": X_EXTRACT_MENTIONS_JS,
            "userGesture": True,
            "returnByValue": True,
        })
        return result.get("result", {}).get("result", {}).get("value") or []


async def fetch_x_mentions() -> list[dict]:
    return await _navigate_and_extract(X_MENTIONS_URL)


async def fetch_x_my_recent_posts(handle: str, max_age_days: int = 14,
                                  blocklist: dict | None = None,
                                  scroll_passes: int = 4) -> list[dict]:
    """Scrape my profile timeline for posts that:
      - have ≥1 reply
      - are NOT pinned (X marks pinned posts via socialContext "Pinned")
      - are NOT in the user-curated blocklist
      - are posted within the last `max_age_days` days

    Scrolls the profile `scroll_passes` times to load more posts beyond above-the-fold.
    Three defense layers (any one blocks): pinned-flag, blocklist, age cutoff.
    Returns [{url, text, reply_count, ts, age_days}, ...].
    """
    blocklist = blocklist or {"x": [], "linkedin": []}
    items = await _navigate_and_extract(f"https://x.com/{handle}", scroll_passes=scroll_passes)
    cutoff = datetime.now(tz=ZoneInfo("UTC")) - __import__("datetime").timedelta(days=max_age_days)
    handle_segment = f"/{handle.lower()}/status/"
    out = []
    skipped_pinned = 0
    skipped_old = 0
    skipped_blocklist = 0
    skipped_repost = 0
    for it in items[:X_RECENT_POSTS_LIMIT]:
        # CRITICAL: filter to posts AUTHORED by `handle`. The profile timeline
        # also shows reposts / quote-tweets where the article's status link
        # points to the ORIGINAL author's tweet, not the user's. Replying to
        # comments on those would post AS IF the user authored the original —
        # a real failure mode (caught 2026-06-17 when an Elon repost surfaced
        # 16 comments that would have been replied to as if user were Elon).
        post_url_lower = (it.get("url") or "").lower()
        if handle_segment not in post_url_lower:
            skipped_repost += 1
            continue
        replies = parse_reply_count(it.get("reply_aria", ""))
        if replies <= 0:
            continue
        if it.get("pinned"):
            skipped_pinned += 1
            continue
        if is_blocklisted("x", it["url"], blocklist):
            skipped_blocklist += 1
            continue
        ts_str = it.get("ts") or ""
        post_dt = None
        if ts_str:
            try:
                post_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                post_dt = None
        if post_dt and post_dt < cutoff:
            skipped_old += 1
            continue
        age_days = (datetime.now(tz=ZoneInfo("UTC")) - post_dt).days if post_dt else None
        out.append({
            "url": it["url"],
            "text": it["text"],
            "reply_count": replies,
            "ts": ts_str,
            "age_days": age_days,
        })
    if skipped_repost:
        print(f"[x]   skipped {skipped_repost} repost/quote-tweet(s) not authored by @{handle}")
    if skipped_pinned:
        print(f"[x]   skipped {skipped_pinned} pinned post(s)")
    if skipped_blocklist:
        print(f"[x]   skipped {skipped_blocklist} blocklisted post(s)")
    if skipped_old:
        print(f"[x]   skipped {skipped_old} post(s) older than {max_age_days} days")
    return out


def fetch_x_replies_to_post(post_url: str) -> list[dict]:
    """Call skills/x-engage/fetch-comments.py for a specific post.
    Returns the list of reply items (author, text, url, ts)."""
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "skills" / "x-engage" / "fetch-comments.py"),
        "--post", post_url,
    ]
    rc, out, _ = _run(cmd, timeout=120)
    parsed = _parse_json(out)
    if not parsed or not parsed.get("ok"):
        return []
    return parsed.get("comments") or parsed.get("new_comments") or []


async def navigate_chrome_to(url: str, settle_seconds: float = 3.0) -> None:
    """Navigate the existing x.com tab to a URL — used for the 'back to profile'
    behavior after reading a post's replies, so Chrome doesn't sit on a stale post."""
    tab = cdp_find_tab("x.com")
    if not tab:
        return
    try:
        async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=20_000_000) as ws:
            await _ws_call(ws, 1, "Page.navigate", {"url": url})
            await asyncio.sleep(settle_seconds)
    except Exception:
        pass  # navigate-back is cosmetic; don't crash the loop if Chrome went away


async def fill_composer_in_new_tab(tweet_url: str, text: str,
                                   composer_wait_s: float = 25.0) -> dict:
    """Open a new tab at the tweet URL, wait for composer to render, focus + insert text.

    The inline composer ('tweetTextarea_0') doesn't exist on page load — it appears
    after the tweet detail SPA hydrates. Poll for it before focusing.
    """
    tab = cdp_open_new_tab(tweet_url)
    await asyncio.sleep(3)  # initial nav
    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=20_000_000) as ws:
        # Poll for composer selector to exist.
        mid = 1
        elapsed = 0.0
        composer_found = False
        while elapsed < composer_wait_s:
            r = await _ws_call(ws, mid, "Runtime.evaluate", {
                "expression": (
                    f"!!document.querySelector({json.dumps(X_COMPOSER_SELECTOR)})"
                ),
                "returnByValue": True,
            })
            mid += 1
            if r.get("result", {}).get("result", {}).get("value"):
                composer_found = True
                break
            await asyncio.sleep(2)
            elapsed += 2
        if not composer_found:
            return {"ok": False, "error": "composer_not_found",
                    "tab_url": tweet_url, "waited_s": elapsed}
        # Focus
        await _ws_call(ws, mid, "Runtime.evaluate", {
            "expression": (
                f"document.querySelector({json.dumps(X_COMPOSER_SELECTOR)}).focus()"
            ),
            "userGesture": True,
        })
        mid += 1
        # Insert
        await _ws_call(ws, mid, "Input.insertText", {"text": text})
        return {"ok": True, "tab_id": tab.get("id"),
                "tab_url": tab.get("url") or tweet_url, "composer_wait_s": elapsed}


# ─── LinkedIn side ────────────────────────────────────────────────────

def _li_self_name() -> str | None:
    """Your LinkedIn display name from BRAND.md (**Name:** …), to skip your own
    comments when scanning inbound."""
    try:
        for line in (PROJECT_ROOT / "BRAND.md").read_text().splitlines():
            s = line.strip()
            if s.startswith("**Name:**"):
                return s.split("**Name:**", 1)[1].strip() or None
    except OSError:
        pass
    return None


def fetch_li_inbound(limit: int) -> dict:
    """Flatten lipy inbound (posts each with a nested `comments` list) into a flat
    list of OTHER people's comments to reply to: {ok, items:[{urn, author, text,
    post_url}]}. The `urn` is the comment URN that `lipy reply --parent` needs."""
    rc, out, err = _run([str(LIPY_BIN), "inbound", "--limit", str(limit)], timeout=180)
    parsed = _parse_json(out)
    if not parsed or not parsed.get("ok"):
        return {"ok": False, "error": (err or out).strip()[:300]}
    me = _li_self_name()
    items = []
    for post in parsed.get("posts", []) or []:
        post_url = post.get("url") or ""
        for c in post.get("comments", []) or []:
            urn = c.get("urn")
            if not urn:
                continue
            author = c.get("author_name") or c.get("author") or "?"
            if me and me.lower() in author.lower():
                continue  # don't reply to your own comments
            items.append({"urn": urn, "author": author,
                          "text": c.get("text") or "", "post_url": post_url})
    return {"ok": True, "items": items}


# ─── main ─────────────────────────────────────────────────────────────

def _collect_x_items(source: str, max_age_days: int = 14) -> list[dict]:
    """Returns a flat list of inbound items: [{author, text, url, parent_post_url}, ...].

    source == 'mentions' → /notifications/mentions (only @-mentions)
    source == 'my-posts' → my recent posts' reply trees (more useful when you've just started)
    source == 'auto'     → my-posts first, fall back to mentions if empty
    """
    items: list[dict] = []
    self_handle = (extract_x_handle_from_brand() or "").lower()
    if source in ("my-posts", "auto"):
        if not self_handle:
            print("[x] couldn't read X handle from BRAND.md — falling back to mentions tab", file=sys.stderr)
        else:
            blocklist = load_blocklist()
            bl_count = len(blocklist.get("x", []))
            if bl_count:
                print(f"[x] blocklist active: {bl_count} URL(s) in {BLOCKLIST_PATH.name}")
            print(f"[x] scanning @{self_handle}'s posts from the last {max_age_days} days ...")
            my_posts = asyncio.run(fetch_x_my_recent_posts(
                self_handle, max_age_days=max_age_days, blocklist=blocklist
            ))
            if not my_posts:
                print(f"[x] no recent (≤{max_age_days}d) posts with replies on @{self_handle}'s timeline")
            skipped_self = 0
            profile_url = f"https://x.com/{self_handle}"
            for i, post in enumerate(my_posts, 1):
                age = post.get("age_days")
                age_str = f"{age}d ago" if age is not None else "age?"
                print(f"[x]   post {i}/{len(my_posts)} ({post['reply_count']} replies, {age_str}): {post['url']}")
                replies = fetch_x_replies_to_post(post["url"])
                new_for_this_post = 0
                for r in replies:
                    author = (r.get("author") or "").lower()
                    # Skip our own thread continuations — author field has '@salaicreates' in it
                    if f"@{self_handle}" in author:
                        skipped_self += 1
                        continue
                    items.append({
                        "author": r.get("author", ""),
                        "text": r.get("text", ""),
                        "url": r.get("url", ""),
                        "parent_post_url": post["url"],
                    })
                    new_for_this_post += 1
                if not replies:
                    print(f"[x]     (fetch-comments returned 0 — slow render or dom-shift)")
                elif new_for_this_post == 0:
                    print(f"[x]     no third-party replies on this post (all self or already seen)")
                else:
                    print(f"[x]     {new_for_this_post} third-party reply/replies queued")
                # Click back to profile so Chrome doesn't sit on the post URL between iterations.
                print(f"[x]     ↩ back to profile")
                asyncio.run(navigate_chrome_to(profile_url))
            if skipped_self:
                print(f"[x] skipped {skipped_self} self-authored reply/thread continuation(s)")
            if items or source == "my-posts":
                return items
    # mentions branch (or fall-through from auto)
    print("[x] checking /notifications/mentions ...")
    try:
        mentions = asyncio.run(fetch_x_mentions())
    except Exception as e:
        print(f"[x] failed to fetch mentions: {e}", file=sys.stderr)
        return items
    for m in mentions:
        items.append({
            "author": m.get("author", ""),
            "text": m.get("text", ""),
            "url": m.get("url", ""),
            "parent_post_url": "",
        })
    return items


def run_x(limit: int, source: str, max_age_days: int, seen: dict,
          live: bool = False) -> int:
    if not ensure_cdp_chrome():
        print("[x] CDP Chrome unreachable on :9222 — start it manually or check ~/.local/bin/start-chrome-cdp",
              file=sys.stderr)
        return 0
    print(f"\n=== X (Twitter) inbound  [live={live}] ===")
    items = _collect_x_items(source, max_age_days=max_age_days)
    if not items:
        print("[x] nothing to engage with right now")
        return 0
    print(f"[x] {len(items)} candidate item(s) collected")

    seen_x = seen.setdefault("x", {})
    drafted = 0
    for m in items:
        if drafted >= limit:
            break
        url = m["url"]
        if not url or url in seen_x:
            continue
        author = m.get("author", "?")
        text = (m.get("text") or "").strip()
        parent = m.get("parent_post_url") or ""
        print(f"\n[x #{drafted+1}] {author}")
        print(f"        Reply URL: {url}")
        if parent:
            print(f"        On post:   {parent}")
        print(f"        Reply text: {text[:200]}")
        result = draft_reply("x", author, text)
        if result.get("decision") != "DRAFT":
            raw = (result.get("raw_draft") or "").strip()
            reasons = result.get("reasons") or [{"rule": "unknown"}]
            if raw:
                print(f"        Refused draft: {raw}")
            print(f"        SKIP — {reasons}")
            _metrics.log_event("vetoed_brandguard", platform="x", mode="inbound",
                               parent_url=url, reasons=reasons)
            seen_x[url] = {"ts": utcnow(), "status": "refused"}
            continue
        reply_text = result["draft"].strip()
        if result.get("autofixes"):
            print(f"        Autofixes: {result['autofixes']}")
        print(f"        Draft:     {reply_text}")
        _metrics.log_event("drafted", platform="x", mode="inbound",
                           parent_url=url, chars=len(reply_text),
                           word_count=len(reply_text.split()),
                           length_target=result.get("length_target"),
                           autofixes=result.get("autofixes"))

        if live:
            # Live: enqueue to outbox, queue the commenter for follow-up.
            try:
                outbox_id = _outbox.enqueue(
                    platform="x", mode="inbound",
                    parent_url=url, draft_text=reply_text,
                    metadata={
                        "author": author,
                        "comment_text": text[:500],
                        "parent_post": parent,
                        "length_target": result.get("length_target"),
                        "autofixes": result.get("autofixes") or [],
                    },
                )
                print(f"        ✓ queued outbox id={outbox_id}")
                _queue_commenter_for_followup(author, parent_post=parent,
                                               source_reply_url=url)
                seen_x[url] = {"ts": utcnow(), "status": "queued",
                               "outbox_id": outbox_id}
            except Exception as e:
                print(f"        FAIL queueing — {e}")
                seen_x[url] = {"ts": utcnow(), "status": "queue_failed",
                               "reason": str(e)[:200]}
                continue
        else:
            # Dry-run: composer pre-fill (legacy behavior).
            fill_result = asyncio.run(fill_composer_in_new_tab(url, reply_text))
            if not fill_result.get("ok"):
                print(f"        FAIL — {fill_result.get('error')}")
                continue
            print(f"        ✓ new tab open with composer pre-filled — review and submit in Chrome")
            seen_x[url] = {"ts": utcnow(), "status": "drafted_dry"}
        drafted += 1
    return drafted


def _queue_commenter_for_followup(commenter_field: str, *,
                                    parent_post: str = "",
                                    source_reply_url: str = "") -> None:
    """Append commenter to ~/.hermes/state/commenter_queue.jsonl.

    `commenter_field` is the raw author string from the X DOM (e.g.,
    'Vadim Strizheus | @VadimStrizheus | · | Oct 8'). We extract the @handle.
    engage-commenter.py pops from this queue, navigates to the profile,
    scores their recent posts, drafts goodwill on top 1-2.
    """
    import re as _re
    m = _re.search(r"@([A-Za-z0-9_]+)", commenter_field or "")
    if not m:
        return
    handle = m.group(1)
    record = {
        "ts": utcnow(),
        "platform": "x",
        "handle": handle,
        "raw_author": commenter_field,
        "parent_post": parent_post,
        "source_reply_url": source_reply_url,
        "status": "queued",
    }
    COMMENTER_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with COMMENTER_QUEUE_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        _metrics.log_event("commenter_queued", platform="x", mode="inbound",
                           handle=handle, parent_url=parent_post,
                           source_reply_url=source_reply_url)
    except Exception:
        pass


def run_li(limit: int, seen: dict, live: bool = False) -> int:
    print("\n=== LinkedIn inbound ===")
    result = fetch_li_inbound(limit)
    if not result.get("ok"):
        err = result.get("error") or "unknown"
        if "not_logged_in" in err or "auth_required" in err:
            print("[li] not logged in. Fix with:  lipy login --headed")
        else:
            print(f"[li] fetch failed: {err}")
        return 0
    items = result.get("items") or result.get("comments") or []
    if not items:
        print("[li] no new comments on your recent posts")
        return 0
    print(f"[li] {len(items)} comment(s) returned")

    seen_li = seen.setdefault("linkedin", {})
    drafted = 0
    for item in items:
        if drafted >= limit:
            break
        urn = item.get("urn") or item.get("comment_urn") or item.get("id")
        if not urn:
            continue
        if urn in seen_li:
            continue
        author = item.get("author") or item.get("author_name") or "?"
        text = (item.get("text") or item.get("comment_text") or "").strip()
        post_url = item.get("post_url") or item.get("permalink") or ""
        print(f"\n[li #{drafted+1}] {author}")
        print(f"         URN:  {urn}")
        if post_url:
            print(f"         Post: {post_url}")
        print(f"         Text: {text[:300]}")
        result = draft_reply("linkedin", author, text)
        if result.get("decision") != "DRAFT" or not result.get("draft"):
            reasons = result.get("reasons") or [{"rule": "unknown"}]
            print(f"         SKIP — drafter returned {result.get('decision', '?')}: {reasons}")
            seen_li[urn] = {"ts": utcnow(), "status": "refused"}
            continue
        reply_text = result["draft"].strip()
        if result.get("autofixes"):
            print(f"         Autofixes: {result['autofixes']}")
        print(f"         Draft: {reply_text}")
        _metrics.log_event("drafted", platform="linkedin", mode="inbound",
                           parent_url=urn, chars=len(reply_text),
                           word_count=len(reply_text.split()),
                           length_target=result.get("length_target"),
                           autofixes=result.get("autofixes"))
        if live:
            # Live: enqueue to the outbox. outbox-flush submits via `lipy reply`.
            try:
                outbox_id = _outbox.enqueue(
                    platform="linkedin", mode="inbound",
                    parent_url=urn, draft_text=reply_text,
                    metadata={
                        "author": author,
                        "comment_text": text[:500],
                        "parent_post": post_url,
                        "length_target": result.get("length_target"),
                        "autofixes": result.get("autofixes") or [],
                    },
                )
                print(f"         ✓ queued outbox id={outbox_id}")
                seen_li[urn] = {"ts": utcnow(), "status": "queued",
                                "outbox_id": outbox_id}
            except Exception as e:
                print(f"         FAIL queueing — {e}")
                seen_li[urn] = {"ts": utcnow(), "status": "queue_failed",
                                "reason": str(e)[:200]}
                continue
        else:
            print(f"         → To post manually: open the thread on LinkedIn, paste the draft, click Post.")
            seen_li[urn] = {"ts": utcnow(), "status": "drafted"}
        drafted += 1
    return drafted


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--platforms", default="x,linkedin",
                   help="Comma-separated subset of {x, linkedin}. Default: both.")
    p.add_argument("--limit", type=int, default=5,
                   help="Max new items to draft per platform (default 5).")
    p.add_argument("--source", choices=["mentions", "my-posts", "auto"], default="auto",
                   help="X inbound source. 'my-posts' scans replies under your recent tweets. "
                        "'mentions' scans /notifications/mentions. 'auto' tries my-posts first.")
    p.add_argument("--max-age-days", type=int, default=14,
                   help="Skip your posts older than this many days (default 14). Excludes pinned posts.")
    p.add_argument("--live", action="store_true",
                   help="Enqueue drafts to ~/.hermes/state/outbox.jsonl (mode=inbound, "
                        "30-min hold buffer). outbox-flush.py humanly submits + likes. "
                        "Also queues each commenter for follow-up engagement. "
                        "Default: dry-run with composer-pre-fill tabs.")
    p.add_argument("--reset-seen", action="store_true",
                   help="Wipe the engagement_seen state file before running.")
    p.add_argument("--watch", action="store_true",
                   help="Poll on an interval. Default: exit on first iteration that drafts ≥1 reply.")
    p.add_argument("--keep-going", action="store_true",
                   help="With --watch: keep polling forever, even after drafts are found. Ctrl-C to stop.")
    p.add_argument("--interval-seconds", type=int, default=90,
                   help="With --watch: seconds between polls (default 90s — tight enough to feel responsive).")
    args = p.parse_args()

    if args.reset_seen and SEEN_PATH.exists():
        SEEN_PATH.unlink()
        print(f"[reset] cleared {SEEN_PATH}")

    platforms = {p.strip() for p in args.platforms.split(",") if p.strip()}

    def _one_pass() -> int:
        seen = load_seen()
        drafted = 0
        if "x" in platforms:
            drafted += run_x(args.limit, args.source, args.max_age_days, seen,
                             live=args.live)
        if "linkedin" in platforms:
            drafted += run_li(args.limit, seen, live=args.live)
        save_seen(seen)
        return drafted

    if not args.watch:
        n = _one_pass()
        print(f"\n=== Done. Drafted {n} item(s). State: {SEEN_PATH} ===")
        return 0

    # Watch mode — poll on interval until we find something to draft, OR until Ctrl-C.
    stop_on_first = not args.keep_going
    mode = "until first draft" if stop_on_first else "forever (Ctrl-C to stop)"
    print(f"\n=== Watch mode — polling every {args.interval_seconds}s, {mode}. ===")
    iteration = 0
    grand_total = 0
    try:
        while True:
            iteration += 1
            stamp = datetime.now(tz=ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S UTC")
            print(f"\n──────── iteration #{iteration} at {stamp} ────────")
            try:
                n = _one_pass()
            except Exception as e:
                print(f"[watch] iteration crashed: {e}", file=sys.stderr)
                n = 0
            grand_total += n
            if n > 0:
                print(f"[watch] drafted {n} this pass (total this session: {grand_total})")
                if stop_on_first:
                    print(f"\n=== Found something. Stopped after {iteration} iteration(s). "
                          f"{grand_total} draft(s) — review tabs in Chrome. ===")
                    return 0
            print(f"[watch] sleeping {args.interval_seconds}s …")
            time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        print(f"\n=== Stopped. {iteration} iteration(s), {grand_total} draft(s) this session. ===")
        return 0


if __name__ == "__main__":
    sys.exit(main())
