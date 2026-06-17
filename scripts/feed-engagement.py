#!/usr/bin/env python3
"""feed-engagement.py — outbound goodwill on the X home feed + self-thread continuations.

Two modes, both designed to fire 30 min before your own scheduled post drops:

  --mode goodwill     Scan x.com/home for posts in your niche, score by topic-keyword
                      match, draft outbound goodwill comments, open a new CDP Chrome
                      tab per draft with the composer pre-filled. Stops short of
                      clicking Reply — that's yours.

  --mode self-thread  Find YOUR most recent post (within --within-minutes), draft a
                      follow-up comment that extends your own thread (same pattern
                      as the manual thread continuations you write today), open a
                      composer tab. Useful right after you've posted manually.

  --mode both         Run goodwill first, then self-thread. Default.

Like engagement-test.py: doesn't post anything. Pre-fills composer tabs, leaves
the final submit click to you.

Usage:
    # Single pass, both modes, default limits
    scripts/feed-engagement.py

    # Just goodwill, 5 drafts, slow scroll
    scripts/feed-engagement.py --mode goodwill --limit 5 --scroll-passes 8

    # Self-thread only (e.g., 5 min after you posted)
    scripts/feed-engagement.py --mode self-thread --within-minutes 30

    # Watch — poll every interval until first draft, then exit
    scripts/feed-engagement.py --mode goodwill --watch --interval-seconds 600

Design notes in: docs/feed-engagement.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import websockets

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
HERMES_BIN = PROJECT_ROOT / "vendor" / "hermes-agent" / ".venv" / "bin" / "hermes"
START_CHROME_CDP = PROJECT_ROOT / "skills" / "x-engage" / "start-chrome-cdp.sh"

SEEN_PATH = HERMES_HOME / "state" / "feed_engagement_seen.json"  # separate from engagement-test.py's seen file
BLOCKLIST_PATH = HERMES_HOME / "state" / "engagement_blocklist.json"  # shared with engagement-test.py

CDP_PORT = 9222
X_HOME_URL = "https://x.com/home"
X_COMPOSER_SELECTOR = 'div[data-testid="tweetTextarea_0"]'

# Niche keywords pulled from BRAND.md positioning + audience sections. Posts in the
# home feed are scored by how many of these appear in their text — top-scoring posts
# become goodwill candidates. Lowercase; match is case-insensitive substring.
NICHE_KEYWORDS = [
    # Tools / stack
    "claude code", "claude-code", "anthropic", "mcp", "browser-use", "browser use",
    "openclaw", "open-claw", "hermes-agent", "hermes agent", "agentic", "agent loop",
    "computer-use", "computer use",
    # Concepts
    "ai automation", "ai-automation", "workflow automation", "agentic workflow",
    "ai agents", "ai agent", "llm agent", "production ai", "ship ai",
    "infrastructure", "operators", "agency owner", "agency operator",
    "cold email", "lead gen", "automation pipeline",
    # Adjacent (lower signal, still relevant)
    "playwright", "cdp", "chrome devtools", "cron", "daemon",
    "openai", "azure openai", "deepseek", "model context protocol",
]

# Posts mentioning these = de-prioritize/skip. From BRAND.md's "audiences to de-prioritize"
# and "off-limits topics". Match anywhere in post text.
SKIP_KEYWORDS = [
    # Crypto/pump
    "$btc", "$eth", "$sol", "memecoin", "pump.fun", "moonshot",
    "next 100x", "100x gem", "next 1000x", "10x gem", "to the moon", "🚀🚀",
    # Self-help / motivational
    "morning routine", "5am club", "grindset", "alpha mindset", "sigma male",
    "manifest", "manifesting", "law of attraction",
    # Job seekers / generic recruiting
    "resume review", "looking for a job", "open to work",
    # Politics
    "biden", "trump", "kamala", "maga", "blue maga",
    # Drama
    "drama", "exposed", "called out", "feud",
]

# JS to extract posts from x.com/home OR profile timeline. Same shape as
# engagement-test.py's extraction so the existing CDP machinery composes.
X_EXTRACT_FEED_JS = """
(() => {
  const cards = Array.from(document.querySelectorAll('article[data-testid="tweet"]')).slice(0, 40);
  return cards.map(a => {
    const u = a.querySelector('[data-testid="User-Name"]');
    const t = a.querySelector('[data-testid="tweetText"]');
    const link = a.querySelector('a[href*="/status/"]');
    const time = a.querySelector('time');
    const ctx = a.querySelector('[data-testid="socialContext"]');
    const ctxText = ctx ? ctx.innerText : '';
    return {
      author: u ? u.innerText.replace(/\\n/g, ' | ') : '',
      text: t ? t.innerText : '',
      url: link ? link.href : '',
      ts: time ? time.getAttribute('datetime') : '',
      pinned: /pinned/i.test(ctxText),
      promoted: /promoted|ad/i.test(ctxText),
      social_context: ctxText,
    };
  }).filter(r => r.url);
})()
"""


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


def is_blocklisted(url: str, blocklist: dict) -> bool:
    return url.rstrip("/") in blocklist.get("x", [])


def _run(cmd: list[str], timeout: int = 180) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def extract_x_handle_from_brand() -> str | None:
    brand = PROJECT_ROOT / "BRAND.md"
    if not brand.exists():
        return None
    m = re.search(r"@([A-Za-z0-9_]+)\s*\(X\)", brand.read_text())
    return m.group(1) if m else None


def author_first_name(author_field: str) -> str:
    """'Vadim Strizheus | @VadimStrizheus | · | Oct 8' → 'Vadim'."""
    name_part = author_field.split("|", 1)[0].strip()
    return name_part.split(" ")[0] if name_part else ""


def author_handle(author_field: str) -> str:
    """'Vadim Strizheus | @VadimStrizheus | · | Oct 8' → '@VadimStrizheus'."""
    m = re.search(r"@[A-Za-z0-9_]+", author_field)
    return m.group(0) if m else ""


# ─── CDP Chrome helpers ───────────────────────────────────────────────

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
    req = urllib.request.Request(
        f"http://localhost:{CDP_PORT}/json/new?{url}", method="PUT",
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


async def _navigate_and_scroll_extract(url: str, scroll_passes: int = 5,
                                       max_wait_s: float = 25.0) -> list[dict]:
    """Navigate the x.com tab to `url`, wait for first article, scroll N times to
    load more, return the extracted post list."""
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
                break
            articles = new_count
        result = await _ws_call(ws, mid, "Runtime.evaluate", {
            "expression": X_EXTRACT_FEED_JS,
            "userGesture": True,
            "returnByValue": True,
        })
        return result.get("result", {}).get("result", {}).get("value") or []


async def navigate_chrome_to(url: str, settle_seconds: float = 3.0) -> None:
    tab = cdp_find_tab("x.com")
    if not tab:
        return
    try:
        async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=20_000_000) as ws:
            await _ws_call(ws, 1, "Page.navigate", {"url": url})
            await asyncio.sleep(settle_seconds)
    except Exception:
        pass


async def fill_composer_in_new_tab(tweet_url: str, text: str,
                                   composer_wait_s: float = 25.0) -> dict:
    """Open new tab at the tweet URL, wait for composer, insert text, leave open."""
    tab = cdp_open_new_tab(tweet_url)
    await asyncio.sleep(3)
    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=20_000_000) as ws:
        mid = 1
        elapsed = 0.0
        composer_found = False
        while elapsed < composer_wait_s:
            r = await _ws_call(ws, mid, "Runtime.evaluate", {
                "expression": f"!!document.querySelector({json.dumps(X_COMPOSER_SELECTOR)})",
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
        await _ws_call(ws, mid, "Runtime.evaluate", {
            "expression": f"document.querySelector({json.dumps(X_COMPOSER_SELECTOR)}).focus()",
            "userGesture": True,
        })
        mid += 1
        await _ws_call(ws, mid, "Input.insertText", {"text": text})
        return {"ok": True, "tab_id": tab.get("id"),
                "tab_url": tab.get("url") or tweet_url}


# ─── Direct-Azure drafter (copied from engagement-test.py, two prompt variants) ─

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
    parts = []
    brand_path = PROJECT_ROOT / "BRAND.md"
    if brand_path.exists():
        parts.append("=== BRAND.md (truncated) ===\n" + brand_path.read_text()[:2500])
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
    """Same rules as engagement-test.py: auto-strip em-dash; refuse on hashtag / sycophantic opener."""
    autofixes = []
    cleaned = draft
    if "—" in cleaned or "–" in cleaned:
        cleaned = cleaned.replace(" — ", ", ").replace("—", ", ")
        cleaned = cleaned.replace(" – ", ", ").replace("–", ", ")
        autofixes.append("stripped em-dash/en-dash")
    refused = []
    if "#" in cleaned:
        refused.append("contains hashtag")
    lowered = cleaned.lower().lstrip()
    bad_openers = ("great question", "absolutely", "thanks for sharing", "i love this", "this is amazing",
                   "great point", "love this", "fantastic", "amazing post")
    for op in bad_openers:
        if lowered.startswith(op):
            refused.append(f"sycophantic opener '{op}'")
            break
    return cleaned, refused, autofixes


def draft_goodwill_comment(handle: str, op_first_name: str, op_handle: str,
                           post_text: str) -> dict:
    """One Azure /chat/completions call. Goodwill comment on a stranger's X post."""
    cfg = _load_azure_config()
    if not cfg["api_key"] or not cfg["base_url"]:
        return {"decision": "REFUSE", "reasons": [{"rule": "no-azure-config"}]}
    voice_ctx = _brand_voice_context()
    system = (
        f"You are Sal AI (@{handle}). You're leaving a goodwill comment on someone ELSE's X post "
        "(an outbound engagement to build relationship with another operator in your space). "
        "Match the voice/tone/vocabulary distilled below from BRAND.md and the voice profile. "
        "Open with the OP's first name only when natural. ADD a concrete experience, counter-point, "
        "or share a relevant anecdote — do NOT just agree. "
        "No em-dash (—). No hashtags. "
        "Never open with 'Great question', 'Absolutely', 'Thanks for sharing', 'I love this', "
        "'This is amazing', 'Great point', or any sycophantic opener. "
        "Length: under 280 characters. "
        "Output ONLY the comment text — no JSON, no quotes, no preamble."
    )
    user = (
        f"=== voice + brand context ===\n{voice_ctx}\n\n"
        f"=== outbound goodwill target ===\n"
        f"platform: x\n"
        f"OP first name: {op_first_name}\n"
        f"OP handle: {op_handle}\n"
        f"OP post text: {post_text}\n\n"
        "Write the goodwill comment now."
    )
    return _post_to_azure(cfg, system, user)


def draft_self_thread_continuation(handle: str, original_post_text: str) -> dict:
    """One Azure call. Continuation comment on the user's OWN post — extends the thread."""
    cfg = _load_azure_config()
    if not cfg["api_key"] or not cfg["base_url"]:
        return {"decision": "REFUSE", "reasons": [{"rule": "no-azure-config"}]}
    voice_ctx = _brand_voice_context()
    system = (
        f"You are Sal AI (@{handle}). You just posted the following on X. Now draft a follow-up "
        "comment on your own post — a thread continuation that extends YOUR OWN argument with one "
        "more concrete point, an example, or a related observation. This is your own voice continuing "
        "your own thread (the way you naturally extend threads manually). "
        "Match the voice in BRAND.md + voice profile (which IS your voice). "
        "Do NOT repeat or paraphrase your original. Add something new. "
        "No em-dash (—). No hashtags. No sycophantic openers ('great', 'absolutely', etc.). "
        "Length: under 280 characters. "
        "Output ONLY the follow-up comment — no JSON, no quotes, no preamble."
    )
    user = (
        f"=== voice + brand context ===\n{voice_ctx}\n\n"
        f"=== your original post (extend this) ===\n{original_post_text}\n\n"
        "Write the thread continuation now."
    )
    return _post_to_azure(cfg, system, user)


def _post_to_azure(cfg: dict, system: str, user: str) -> dict:
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
                "reasons": [{"rule": r} for r in refused], "autofixes": autofixes}
    return {"decision": "DRAFT", "draft": cleaned, "raw_draft": draft, "autofixes": autofixes}


# ─── Topic scoring + filtering ────────────────────────────────────────

def topic_score(text: str) -> tuple[int, list[str]]:
    """Returns (score, matched_keywords). Score = count of niche keywords found."""
    lower = (text or "").lower()
    matched = [k for k in NICHE_KEYWORDS if k in lower]
    return len(matched), matched


def has_skip_signal(text: str) -> tuple[bool, str]:
    lower = (text or "").lower()
    for s in SKIP_KEYWORDS:
        if s in lower:
            return True, s
    return False, ""


def is_promoted(item: dict) -> bool:
    return bool(item.get("promoted"))


# ─── Mode: goodwill ───────────────────────────────────────────────────

def run_goodwill(handle: str, limit: int, min_score: int, scroll_passes: int,
                 seen: dict, blocklist: dict) -> int:
    print("\n=== Goodwill (X home feed → outbound engagement) ===")
    if not ensure_cdp_chrome():
        print("[x] CDP Chrome unreachable on :9222", file=sys.stderr)
        return 0

    print(f"[x] scanning x.com/home for niche-matching posts (score ≥ {min_score}) ...")
    items = asyncio.run(_navigate_and_scroll_extract(X_HOME_URL, scroll_passes=scroll_passes))
    if not items:
        print("[x] nothing visible in feed (logged out? home feed empty?)")
        return 0
    print(f"[x] {len(items)} posts pulled from feed")

    self_marker = f"@{handle}".lower()
    seen_goodwill = seen.setdefault("x_goodwill", {})

    # Filter + score
    candidates: list[dict] = []
    skipped_own = skipped_promoted = skipped_skipsig = skipped_low_score = 0
    for it in items:
        if not it.get("url"):
            continue
        if self_marker in (it.get("author", "") or "").lower():
            skipped_own += 1
            continue
        if is_promoted(it):
            skipped_promoted += 1
            continue
        if is_blocklisted(it["url"], blocklist):
            continue
        if it["url"] in seen_goodwill:
            continue
        skip_hit, skip_word = has_skip_signal(it.get("text", ""))
        if skip_hit:
            skipped_skipsig += 1
            continue
        score, matched = topic_score(it.get("text", ""))
        if score < min_score:
            skipped_low_score += 1
            continue
        candidates.append({**it, "score": score, "matched": matched})

    if skipped_own:
        print(f"[x]   skipped {skipped_own} of your own posts in the feed")
    if skipped_promoted:
        print(f"[x]   skipped {skipped_promoted} promoted/ads")
    if skipped_skipsig:
        print(f"[x]   skipped {skipped_skipsig} post(s) with off-target signals")
    if skipped_low_score:
        print(f"[x]   skipped {skipped_low_score} post(s) below score {min_score}")

    candidates.sort(key=lambda c: c["score"], reverse=True)
    if not candidates:
        print("[x] no goodwill candidates in current feed snapshot")
        return 0
    print(f"[x] {len(candidates)} candidate(s) for goodwill; engaging top {min(limit, len(candidates))}")

    drafted = 0
    for c in candidates[:limit]:
        first = author_first_name(c["author"])
        op_handle = author_handle(c["author"])
        print(f"\n[goodwill #{drafted+1}] {c['author']}")
        print(f"        URL:   {c['url']}")
        print(f"        Score: {c['score']}  matched: {c['matched']}")
        print(f"        Text:  {(c['text'] or '')[:200]}")
        result = draft_goodwill_comment(handle, first, op_handle, c["text"] or "")
        if result.get("decision") != "DRAFT":
            raw = (result.get("raw_draft") or "").strip()
            if raw:
                print(f"        Refused draft: {raw}")
            print(f"        SKIP — {result.get('reasons')}")
            seen_goodwill[c["url"]] = {"ts": utcnow(), "status": "refused"}
            continue
        reply_text = result["draft"].strip()
        if result.get("autofixes"):
            print(f"        Autofixes: {result['autofixes']}")
        print(f"        Draft: {reply_text}")
        fill = asyncio.run(fill_composer_in_new_tab(c["url"], reply_text))
        if not fill.get("ok"):
            print(f"        FAIL — {fill.get('error')}")
            continue
        print(f"        ✓ new tab open with composer pre-filled — review and submit in Chrome")
        seen_goodwill[c["url"]] = {"ts": utcnow(), "status": "drafted",
                                   "score": c["score"], "matched": c["matched"]}
        drafted += 1
    # Back to home after the run so Chrome doesn't sit on a stale post.
    asyncio.run(navigate_chrome_to(X_HOME_URL))
    print(f"\n[goodwill] drafted {drafted} of {len(candidates[:limit])} candidates this pass")
    return drafted


# ─── Mode: self-thread ────────────────────────────────────────────────

def run_self_thread(handle: str, within_minutes: int, seen: dict,
                    blocklist: dict, scroll_passes: int = 4) -> int:
    print(f"\n=== Self-thread (extend your most recent post posted within last {within_minutes}m) ===")
    if not ensure_cdp_chrome():
        print("[x] CDP Chrome unreachable on :9222", file=sys.stderr)
        return 0

    print(f"[x] scanning @{handle}'s timeline ...")
    items = asyncio.run(_navigate_and_scroll_extract(f"https://x.com/{handle}",
                                                     scroll_passes=scroll_passes))
    if not items:
        print("[x] couldn't read your profile timeline")
        return 0

    cutoff = datetime.now(tz=ZoneInfo("UTC")) - timedelta(minutes=within_minutes)
    seen_thread = seen.setdefault("x_self_thread", {})
    candidate = None
    for it in items:
        if it.get("pinned"):
            continue
        if is_blocklisted(it.get("url", ""), blocklist):
            continue
        ts_str = it.get("ts") or ""
        if not ts_str:
            continue
        try:
            post_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if post_dt < cutoff:
            continue
        if it["url"] in seen_thread:
            continue
        # Most recent post that fits criteria
        candidate = it
        break

    if not candidate:
        print(f"[x] no fresh post within last {within_minutes}m to continue")
        return 0

    print(f"\n[self-thread] target post: {candidate['url']}")
    print(f"        Posted:    {candidate.get('ts')}")
    print(f"        Text:      {(candidate.get('text') or '')[:240]}")

    result = draft_self_thread_continuation(handle, candidate.get("text") or "")
    if result.get("decision") != "DRAFT":
        raw = (result.get("raw_draft") or "").strip()
        if raw:
            print(f"        Refused draft: {raw}")
        print(f"        SKIP — {result.get('reasons')}")
        seen_thread[candidate["url"]] = {"ts": utcnow(), "status": "refused"}
        return 0

    cont = result["draft"].strip()
    if result.get("autofixes"):
        print(f"        Autofixes: {result['autofixes']}")
    print(f"        Continuation: {cont}")
    fill = asyncio.run(fill_composer_in_new_tab(candidate["url"], cont))
    if not fill.get("ok"):
        print(f"        FAIL — {fill.get('error')}")
        return 0
    print(f"        ✓ new tab open at your post with composer pre-filled — review and submit in Chrome")
    seen_thread[candidate["url"]] = {"ts": utcnow(), "status": "drafted"}
    return 1


# ─── main ─────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--mode", choices=["goodwill", "self-thread", "both"], default="both",
                   help="What to do. Default: both (goodwill then self-thread).")
    p.add_argument("--limit", type=int, default=5,
                   help="Max goodwill drafts per pass (default 5).")
    p.add_argument("--min-score", type=int, default=1,
                   help="Min niche-keyword matches a feed post needs to be a goodwill candidate (default 1).")
    p.add_argument("--scroll-passes", type=int, default=5,
                   help="How many times to scroll the home feed before extracting (default 5).")
    p.add_argument("--within-minutes", type=int, default=60,
                   help="Self-thread: only target posts within last N minutes (default 60).")
    p.add_argument("--reset-seen", action="store_true",
                   help="Wipe ~/.hermes/state/feed_engagement_seen.json before running.")
    p.add_argument("--watch", action="store_true",
                   help="Loop on interval. Default: exit on first iteration that drafts ≥1.")
    p.add_argument("--keep-going", action="store_true",
                   help="With --watch: loop forever even after drafts found. Ctrl-C to stop.")
    p.add_argument("--interval-seconds", type=int, default=300,
                   help="With --watch: seconds between polls (default 300 = 5 min).")
    args = p.parse_args()

    if args.reset_seen and SEEN_PATH.exists():
        SEEN_PATH.unlink()
        print(f"[reset] cleared {SEEN_PATH}")

    handle = extract_x_handle_from_brand()
    if not handle:
        print("[x] couldn't read X handle from BRAND.md — aborting", file=sys.stderr)
        return 2

    def _one_pass() -> int:
        seen = load_seen()
        blocklist = load_blocklist()
        drafted = 0
        if args.mode in ("goodwill", "both"):
            drafted += run_goodwill(handle, args.limit, args.min_score, args.scroll_passes,
                                    seen, blocklist)
        if args.mode in ("self-thread", "both"):
            drafted += run_self_thread(handle, args.within_minutes, seen, blocklist)
        save_seen(seen)
        return drafted

    if not args.watch:
        n = _one_pass()
        print(f"\n=== Done. Drafted {n} item(s). State: {SEEN_PATH} ===")
        return 0

    stop_on_first = not args.keep_going
    mode_msg = "until first draft" if stop_on_first else "forever (Ctrl-C)"
    print(f"\n=== Watch mode — every {args.interval_seconds}s, {mode_msg}. ===")
    iteration = 0
    grand = 0
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
            grand += n
            if n > 0 and stop_on_first:
                print(f"\n=== Found something. {iteration} iter(s), {grand} draft(s) — review in Chrome. ===")
                return 0
            print(f"[watch] sleeping {args.interval_seconds}s …")
            time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        print(f"\n=== Stopped. {iteration} iter(s), {grand} draft(s). ===")
        return 0


if __name__ == "__main__":
    sys.exit(main())
