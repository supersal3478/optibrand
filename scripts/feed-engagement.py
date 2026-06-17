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

# Phase-2 shared libs (humanization, outbox, metrics). Imported lazily where
# possible so dry-run / smoke paths don't blow up if a transient import error
# happens during a cron run — we degrade gracefully and log.
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "skills" / "x-engage"))
from _cdp import open_session as _open_human_session  # noqa: E402
from x_human import HumanCDP  # noqa: E402
import _outbox  # noqa: E402
import _metrics  # noqa: E402
from _drafter import (  # noqa: E402
    pick_length_target as _pick_length_target,
    apply_length_and_punctuation_fixes as _apply_length_and_punctuation_fixes,
    strip_trailing_period as _strip_trailing_period,
    enforce_word_cap as _enforce_word_cap,
)

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
    // Ad detection — X marks promoted posts via several markers. We tried
    // including [data-testid="placementTracking"] but verified empirically
    // that X puts it on EVERY feed article (it's tracking, not an ad flag).
    // Reliable ad-only markers:
    //   1. socialContext contains "Promoted" word
    //   2. reportPromotedTweet button (overflow menu — only ads have this)
    //   3. ad-redirect link (/i/redirect or /i/ads)
    //   4. "Promoted" word-boundary text anywhere in the article
    const hasPromotedReport = !!a.querySelector('[data-testid="reportPromotedTweet"]');
    const hasAdRedirect     = !!a.querySelector('a[href*="/i/redirect"], a[href*="/i/ads"]');
    const promotedByContext = /\\bPromoted\\b/i.test(ctxText);
    const fullText          = a.innerText || '';
    const promotedByBadge   = /\\bPromoted\\b/.test(fullText);
    const promoted = hasPromotedReport || hasAdRedirect
                  || promotedByContext || promotedByBadge;
    return {
      author: u ? u.innerText.replace(/\\n/g, ' | ') : '',
      text: t ? t.innerText : '',
      url: link ? link.href : '',
      ts: time ? time.getAttribute('datetime') : '',
      pinned: /pinned/i.test(ctxText),
      promoted: promoted,
      promoted_reasons: [
        hasPromotedReport ? 'promoted_report' : null,
        hasAdRedirect     ? 'ad_redirect'     : null,
        promotedByContext ? 'social_ctx'      : null,
        promotedByBadge   ? 'badge_text'      : null,
      ].filter(Boolean),
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
    length_label, length_instruction, max_words = _pick_length_target()
    system = (
        f"You are Sal AI (@{handle}). You're leaving a goodwill comment on someone ELSE's X post "
        "(an outbound engagement to build relationship with another operator in your space). "
        "Match the voice/tone/vocabulary distilled below from BRAND.md and the voice profile.\n\n"
        "DO NOT use the OP's name, first name, display name, or handle anywhere in the comment. "
        "Engage directly with the point they made — no 'Mario,' opener, no '@user', nothing. "
        "ADD a concrete experience, counter-point, or share a relevant anecdote — do NOT just agree.\n\n"
        f"{length_instruction}\n\n"
        "No em-dash (—). No hashtags. "
        "Never open with 'Great question', 'Absolutely', 'Thanks for sharing', 'I love this', "
        "'This is amazing', 'Great point', or any sycophantic opener. "
        "Output ONLY the comment text — no JSON, no quotes, no preamble."
    )
    user = (
        f"=== voice + brand context ===\n{voice_ctx}\n\n"
        f"=== outbound goodwill target ===\n"
        f"platform: x\n"
        f"OP handle (DO NOT mention): {op_handle}\n"
        f"OP post text: {post_text}\n\n"
        "Write the goodwill comment now."
    )
    out = _post_to_azure(cfg, system, user)
    if isinstance(out, dict):
        out["length_target"] = length_label
        _apply_length_and_punctuation_fixes(out, max_words)
    return out


def draft_self_thread_continuation(handle: str, original_post_text: str) -> dict:
    """One Azure call. Continuation comment on the user's OWN post — extends the thread."""
    cfg = _load_azure_config()
    if not cfg["api_key"] or not cfg["base_url"]:
        return {"decision": "REFUSE", "reasons": [{"rule": "no-azure-config"}]}
    voice_ctx = _brand_voice_context()
    length_label, length_instruction, max_words = _pick_length_target()
    system = (
        f"You are Sal AI (@{handle}). You just posted the following on X. Now draft a follow-up "
        "comment on your own post — a thread continuation that extends YOUR OWN argument with one "
        "more concrete point, an example, or a related observation. This is your own voice continuing "
        "your own thread (the way you naturally extend threads manually). "
        "Match the voice in BRAND.md + voice profile (which IS your voice). "
        "Do NOT repeat or paraphrase your original. Add something new.\n\n"
        f"{length_instruction}\n\n"
        "No em-dash (—). No hashtags. No sycophantic openers ('great', 'absolutely', etc.). "
        "Output ONLY the follow-up comment — no JSON, no quotes, no preamble."
    )
    user = (
        f"=== voice + brand context ===\n{voice_ctx}\n\n"
        f"=== your original post (extend this) ===\n{original_post_text}\n\n"
        "Write the thread continuation now."
    )
    out = _post_to_azure(cfg, system, user)
    if isinstance(out, dict):
        out["length_target"] = length_label
        _apply_length_and_punctuation_fixes(out, max_words)
    return out


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


# ─── Audience-alignment judge (LLM, replaces keyword scoring for goodwill) ─

def _brand_audience_context() -> str:
    """Pull just the Positioning + Audiences section from BRAND.md. The audience
    judge gets this — not the whole file — so the prompt stays small and the
    judgment focuses on audience fit, not voice or veto rules."""
    brand = PROJECT_ROOT / "BRAND.md"
    if not brand.exists():
        return ""
    text = brand.read_text()
    # Take from the Positioning header through to the Voice section heading.
    m = re.search(r"(\*\*Positioning.*?)(?=\n##\s+Voice)", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Fallback: first 1800 chars of BRAND.md
    return text[:1800]


def judge_audience_alignment(handle: str, op_handle: str, op_display: str,
                             post_text: str) -> dict:
    """Cheap-Flash call. Decide whether commenting under this post would put
    the brand in front of an aligned audience.

    Returns {"decision": "yes"|"no", "confidence": float, "reason": str}.
    On any error: returns decision="no" with reason="judge_error:..." so a
    transient LLM failure does NOT default us into commenting on unknown posts.
    """
    cfg = _load_azure_config()
    if not cfg["api_key"] or not cfg["base_url"]:
        return {"decision": "no", "confidence": 0.0,
                "reason": "judge_error:no-azure-config"}
    audience_ctx = _brand_audience_context()
    if not audience_ctx:
        return {"decision": "no", "confidence": 0.0,
                "reason": "judge_error:no-brand-audience"}
    system = (
        "You are evaluating whether commenting on a stranger's X post would put a "
        "specific brand in front of audience members aligned with that brand.\n\n"
        "BE PERMISSIVE ON TECH/AI TOPICS. If the post touches AI, agents, "
        "automation, agentic workflows, LLMs, Claude, MCP, browser automation, "
        "computer use, dev tooling, infrastructure, dev productivity, "
        "shipping software, building products, or any adjacent technical topic — "
        "decide YES even if the post is generic, hype-y, or written by a 'thought "
        "leader'. Why: the brand's primary audience (operators / agency owners "
        "building AI automation) reads and engages with those threads — to push "
        "back, share what they shipped, compare notes, or argue specifics. The "
        "commenters under such posts ARE the brand's audience, regardless of the "
        "OP's own register.\n\n"
        "Decide NO only when the post is clearly off-target:\n"
        "  - Crypto pump / memecoin / trading signals / 'next 100x'\n"
        "  - Self-help / morning routines / motivational / 5am-club / mindset\n"
        "  - Politics / partisan commentary / culture-war takes\n"
        "  - Drama / call-outs / personal feuds / dunk threads\n"
        "  - Job-seeking / resume requests / 'open to work'\n"
        "  - Unrelated lifestyle / fashion / sports / entertainment\n"
        "  - Empty content (no text and no inferable context)\n\n"
        "When in doubt on a tech-adjacent topic → YES. We'd rather draft and have "
        "brand-guard veto a bad draft than miss a relevant engagement.\n\n"
        "Return ONLY a JSON object: "
        '{"decision":"yes"|"no","confidence":<float 0..1>,"reason":<short string>}. '
        "No prose, no markdown, no code fences."
    )
    user = (
        f"=== brand context ===\n{audience_ctx}\n\n"
        f"=== target post ===\n"
        f"Author display: {op_display}\n"
        f"Author handle: {op_handle}\n"
        f"Post text:\n{post_text}\n\n"
        "Decide. JSON only."
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
                "max_tokens": 200,
                "temperature": 0.2,
            },
            timeout=60,
        )
    except httpx.RequestError as e:
        return {"decision": "no", "confidence": 0.0,
                "reason": f"judge_error:http:{str(e)[:80]}"}
    print(f"        … judge returned in {time.time()-t0:.1f}s "
          f"(HTTP {resp.status_code})", flush=True)
    if resp.status_code != 200:
        return {"decision": "no", "confidence": 0.0,
                "reason": f"judge_error:non-200:{resp.status_code}"}
    raw = (resp.json().get("choices", [{}])[0].get("message", {})
           .get("content") or "").strip()
    # Strip code fences in case the model wraps despite instructions.
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        out = json.loads(raw)
        decision = str(out.get("decision", "no")).strip().lower()
        confidence = float(out.get("confidence", 0.5))
        reason = str(out.get("reason", ""))[:240]
    except (json.JSONDecodeError, ValueError, TypeError):
        # LLMs occasionally emit malformed JSON (stray quote in a number,
        # unescaped char, etc.) even after explicit instructions. Don't lose
        # an otherwise-valid verdict to a typo — regex-recover decision,
        # confidence, reason. If decision can't be recovered, fall back to "no".
        m_dec = re.search(r'"decision"\s*:\s*"?(yes|no)"?', raw, re.IGNORECASE)
        if not m_dec:
            return {"decision": "no", "confidence": 0.0,
                    "reason": f"judge_error:bad-json:{raw[:120]}"}
        decision = m_dec.group(1).lower()
        m_conf = re.search(r'"confidence"\s*:\s*([0-9]+(?:\.[0-9]+)?)', raw)
        confidence = float(m_conf.group(1)) if m_conf else 0.5
        m_reason = re.search(r'"reason"\s*:\s*"([^"]{1,240})"', raw)
        reason = ((m_reason.group(1) if m_reason else "no-reason")
                  + " [recovered from malformed JSON]")
    if decision not in ("yes", "no"):
        decision = "no"
    confidence = max(0.0, min(1.0, confidence))
    return {"decision": decision, "confidence": confidence, "reason": reason}


# ─── Humanized visit + back-to-feed ───────────────────────────────────

X_DETAIL_EXTRACT_JS = """
(() => {
  // On a post-detail page the focal post is the first article — but in some
  // layouts it's the one with the tweet-detail aria-label. Try both.
  const arts = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
  if (arts.length === 0) return null;
  const a = arts[0];
  const u = a.querySelector('[data-testid="User-Name"]');
  const t = a.querySelector('[data-testid="tweetText"]');
  return {
    author: u ? u.innerText.replace(/\\n/g, ' | ') : '',
    text: t ? t.innerText : '',
  };
})()
"""


async def _humanly_visit_and_extract(post_url: str) -> dict:
    """Humanly navigate to the post detail, dwell to read, extract focal-post
    author + full text. Returns {"ok": bool, "author": str, "text": str}."""
    async with _open_human_session() as s:
        h = HumanCDP(s)
        await s.navigate(post_url, settle_seconds=3.5)
        # Wait for the focal article to render.
        for _ in range(10):
            present = await s.eval_js(
                'document.querySelectorAll(\'article[data-testid="tweet"]\').length > 0',
                await_promise=False)
            if present:
                break
            await asyncio.sleep(1.0)
        else:
            return {"ok": False, "error": "detail_not_loaded"}
        data = await s.eval_js(X_DETAIL_EXTRACT_JS, await_promise=False)
        if not data:
            return {"ok": False, "error": "detail_extract_failed"}
        # Read-dwell proportional to post length — visible "I'm reading" signal.
        await h.dwell_to_read(data.get("text") or "",
                              floor_s=2.0, ceil_s=12.0)
        # Gentle scroll on the detail page so we look like we're skimming
        # the replies, then back to top.
        await h.scroll(random.randint(400, 900))
        await asyncio.sleep(random.uniform(0.8, 1.6))
        await h.scroll(-random.randint(300, 600))
        return {"ok": True, "author": data.get("author") or "",
                "text": data.get("text") or ""}


async def _humanly_back_to_feed(home_url: str = X_HOME_URL) -> None:
    """Navigate back to the home feed and pause briefly — gives a human-looking
    "returning to scroll" beat between visits."""
    async with _open_human_session() as s:
        h = HumanCDP(s)
        await s.navigate(home_url, settle_seconds=2.5)
        # Light scroll on return so we're not always sitting at scrollY=0.
        await h.scroll(random.randint(200, 700))
        await asyncio.sleep(random.uniform(0.6, 1.4))


# Local random — separate import so the dwell/scroll randomness above isn't
# accidentally affected by any seeding elsewhere.
import random  # noqa: E402  (placed after functions for clarity)


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


# ─── Mode: goodwill (scroll-and-read browse pattern) ─────────────────
#
# Human-pattern browse, not script crawl:
#   1. Open x.com/home; wait for first articles.
#   2. Loop, until max_visits clicks OR max_reads judgments OR 4 empty batches:
#      a. Extract currently-visible posts.
#      b. Drop ads/own/blocklist/skip-signal/already-seen (cheap pre-filter).
#      c. Of the survivors, pick 1–3 at random (NOT in feed order — humans
#         don't read top-to-bottom strictly).
#      d. For each pick: judge from preview text (no click). If preview
#         is empty (link/quote-tweet) we skip; if judge="no", mark read
#         and move on; if judge="yes", click in, read full, draft, comment
#         (outbox if --live, composer-pre-fill if dry), back to feed,
#         restore scrollY so the next batch picks up where we were.
#      e. End of batch → scroll: usually forward 0.8–2.0 viewports,
#         15% chance of a back-scroll 0.4–0.8 viewports.
#   3. Park cursor offscreen on exit.
#
# Caps:
#   max_visits — clicks (the commenting budget).
#   max_reads  — judgments (the LLM-cost budget; defaults higher because
#                most reads are cheap rejections).

async def _goodwill_browse_pass(handle: str, max_visits: int, max_reads: int,
                                 seen_goodwill: dict, blocklist: dict,
                                 live: bool) -> tuple[int, int, int]:
    drafted = 0
    visited = 0
    read = 0
    self_marker = f"@{handle}".lower()

    try:
      async with _open_human_session() as s:
        h = HumanCDP(s)
        await s.navigate(X_HOME_URL, settle_seconds=4.0)

        # Wait for first articles to render.
        articles = 0
        for _ in range(12):
            n = await s.eval_js(
                'document.querySelectorAll(\'article[data-testid="tweet"]\').length',
                await_promise=False)
            articles = int(n or 0)
            if articles > 0:
                break
            await asyncio.sleep(1.5)
        if articles == 0:
            print("[goodwill] feed didn't load — empty or logged out?")
            return 0, 0, 0
        print(f"[goodwill] feed loaded ({articles} initial articles)")

        empty_batches = 0
        while visited < max_visits and read < max_reads and empty_batches < 4:
            # 1. Snapshot what's currently visible.
            visible = await s.eval_js(X_EXTRACT_FEED_JS, await_promise=False) or []

            # 2. Pre-filter (cheap, no LLM).
            fresh: list[dict] = []
            for p in visible:
                url = p.get("url")
                if not url or url in seen_goodwill:
                    continue
                author_field = p.get("author") or ""
                if self_marker in author_field.lower():
                    seen_goodwill[url] = {"ts": utcnow(), "status": "own_post"}
                    continue
                if is_promoted(p):
                    promoted_reasons = p.get("promoted_reasons") or []
                    seen_goodwill[url] = {"ts": utcnow(), "status": "ad",
                                          "promoted_reasons": promoted_reasons}
                    _metrics.log_event("skipped_blocklist", platform="x",
                                       mode="goodwill", parent_url=url,
                                       reason="promoted_ad",
                                       promoted_reasons=promoted_reasons)
                    continue
                if is_blocklisted(url, blocklist):
                    seen_goodwill[url] = {"ts": utcnow(), "status": "blocklist"}
                    continue
                skip_hit, skip_word = has_skip_signal(p.get("text", ""))
                if skip_hit:
                    seen_goodwill[url] = {"ts": utcnow(),
                                          "status": "skip_signal",
                                          "word": skip_word}
                    _metrics.log_event("skipped_blocklist", platform="x",
                                       mode="goodwill", parent_url=url,
                                       reason=f"skip_signal:{skip_word}")
                    continue
                fresh.append(p)

            if not fresh:
                empty_batches += 1
                vw, vh = await h.viewport_size()
                amount = int(vh * random.uniform(1.0, 1.8))
                print(f"[batch] nothing fresh in view — scrolling +{amount}px")
                await h.scroll(amount)
                await h.jitter(2.0, 5.0)
                continue
            empty_batches = 0

            # 3. Random-pick (not feed-order). 1–3 reads per batch.
            random.shuffle(fresh)
            batch_size = random.randint(1, min(3, len(fresh)))
            batch = fresh[:batch_size]
            print(f"\n[batch] {len(visible)} visible, {len(fresh)} fresh; "
                  f"reading {batch_size} non-sequentially")

            for post in batch:
                if visited >= max_visits or read >= max_reads:
                    break
                read += 1
                url = post["url"]
                op_display = post.get("author") or ""
                op_handle = author_handle(op_display)
                preview = (post.get("text") or "").strip()

                print(f"\n[read #{read}] {op_display}")
                print(f"          URL:  {url}")
                print(f"          Preview: {preview[:160]}")

                if not preview:
                    print("          (empty preview — would need click to read; skipping)")
                    seen_goodwill[url] = {"ts": utcnow(),
                                          "status": "empty_preview"}
                    await h.jitter(1.5, 3.5)
                    continue

                # Judge from preview text — no click needed for the "no" case.
                verdict = judge_audience_alignment(handle, op_handle,
                                                   op_display, preview)
                print(f"          Judge: {verdict['decision']}  "
                      f"conf={verdict['confidence']:.2f}  "
                      f"reason={verdict['reason'][:120]}")
                _metrics.log_event("audience_judged", platform="x",
                                   mode="goodwill", parent_url=url,
                                   decision=verdict["decision"],
                                   confidence=verdict["confidence"],
                                   reason=verdict["reason"])

                if verdict["decision"] != "yes":
                    seen_goodwill[url] = {"ts": utcnow(),
                                          "status": "judge_rejected",
                                          "confidence": verdict["confidence"],
                                          "reason": verdict["reason"]}
                    await h.jitter(2.0, 5.0)
                    continue

                # YES → click into post to read full + draft + comment.
                visited += 1
                saved_scrollY = await s.eval_js("window.scrollY",
                                                 await_promise=False) or 0

                await s.navigate(url, settle_seconds=3.5)
                detail_ok = False
                for _ in range(8):
                    n = await s.eval_js(
                        'document.querySelectorAll(\'article[data-testid="tweet"]\').length',
                        await_promise=False)
                    if n and int(n) > 0:
                        detail_ok = True
                        break
                    await asyncio.sleep(1.0)

                full_text = preview
                if detail_ok:
                    detail = await s.eval_js(X_DETAIL_EXTRACT_JS,
                                              await_promise=False)
                    if detail and detail.get("text"):
                        full_text = detail["text"]

                # Human-read dwell proportional to length.
                await h.dwell_to_read(full_text, floor_s=2.0, ceil_s=10.0)

                # Draft.
                op_first = author_first_name(op_display)
                result = draft_goodwill_comment(handle, op_first,
                                                 op_handle, full_text)
                if result.get("decision") != "DRAFT":
                    raw = (result.get("raw_draft") or "").strip()
                    if raw:
                        print(f"          Refused draft: {raw}")
                    print(f"          SKIP — {result.get('reasons')}")
                    _metrics.log_event("vetoed_brandguard", platform="x",
                                       mode="goodwill", parent_url=url,
                                       reasons=result.get("reasons"))
                    seen_goodwill[url] = {"ts": utcnow(),
                                          "status": "brandguard_refused",
                                          "reasons": result.get("reasons")}
                else:
                    reply_text = result["draft"].strip()
                    if result.get("autofixes"):
                        print(f"          Autofixes: {result['autofixes']}")
                    print(f"          Draft: {reply_text}")
                    _metrics.log_event("drafted", platform="x",
                                       mode="goodwill", parent_url=url,
                                       chars=len(reply_text),
                                       word_count=len(reply_text.split()),
                                       length_target=result.get("length_target"),
                                       autofixes=result.get("autofixes"),
                                       confidence=verdict["confidence"])
                    if live:
                        try:
                            outbox_id = _outbox.enqueue(
                                platform="x", mode="goodwill",
                                parent_url=url, draft_text=reply_text,
                                metadata={
                                    "op_handle": op_handle,
                                    "op_display": op_display,
                                    "judge_confidence": verdict["confidence"],
                                    "judge_reason": verdict["reason"],
                                    "autofixes": result.get("autofixes") or [],
                                },
                            )
                            print(f"          ✓ queued outbox id={outbox_id}")
                            seen_goodwill[url] = {"ts": utcnow(),
                                                  "status": "queued",
                                                  "outbox_id": outbox_id}
                            drafted += 1
                        except Exception as e:
                            print(f"          FAIL queueing — {e}")
                            seen_goodwill[url] = {"ts": utcnow(),
                                                  "status": "queue_failed",
                                                  "reason": str(e)[:200]}
                    else:
                        fill = await fill_composer_in_new_tab(url, reply_text)
                        if fill.get("ok"):
                            print("          ✓ composer tab pre-filled")
                            seen_goodwill[url] = {"ts": utcnow(),
                                                  "status": "drafted_dry"}
                            drafted += 1
                        else:
                            print(f"          FAIL pre-fill — {fill.get('error')}")
                            seen_goodwill[url] = {"ts": utcnow(),
                                                  "status": "compose_failed",
                                                  "reason": fill.get("error")}
                        # Composer tab stole focus → bring home-feed tab back
                        # to foreground so subsequent CDP calls aren't throttled
                        # by Chrome's backgrounded-tab heuristics.
                        try:
                            await s.call("Page.bringToFront")
                        except Exception:
                            pass

                # Back to feed + restore scroll so the next batch picks up
                # where we left off (otherwise we'd always re-read the top).
                await s.navigate(X_HOME_URL, settle_seconds=2.5)
                await asyncio.sleep(1.0)
                await s.eval_js(f"window.scrollTo(0, {saved_scrollY})",
                                 await_promise=False)
                await h.jitter(2.0, 5.0)

            # End-of-batch scroll: usually forward, occasionally back.
            vw, vh = await h.viewport_size()
            if random.random() < 0.15:
                back = -int(vh * random.uniform(0.4, 0.8))
                print(f"[scroll] back {back}px (skim-back)")
                await h.scroll(back)
            else:
                fwd = int(vh * random.uniform(0.8, 2.0))
                print(f"[scroll] forward {fwd}px")
                await h.scroll(fwd)
            await h.jitter(1.5, 4.5)

        await h.park_cursor_offscreen()
    except (websockets.exceptions.ConnectionClosedError,
            websockets.exceptions.ConnectionClosedOK,
            asyncio.TimeoutError, TimeoutError) as e:
        # CDP can become unresponsive when the home tab is backgrounded by
        # Chrome's tab throttling (after a new composer tab steals focus),
        # or when the websocket drops on idle. The seen-state dict was
        # mutated in-place, so progress to this point is already persisted
        # on the caller's side — just report partial counts and exit cleanly.
        print(f"[goodwill] CDP unresponsive mid-pass "
              f"({type(e).__name__}) — returning partial counts "
              f"(drafted={drafted}, visited={visited}, read={read})",
              file=sys.stderr)

    return drafted, visited, read


def run_goodwill(handle: str, max_visits: int, max_reads: int,
                 seen: dict, blocklist: dict, live: bool) -> int:
    """Sync wrapper around the async scroll-and-read browse pass."""
    print("\n=== Goodwill (scroll-and-read; click only to comment) ===")
    if not ensure_cdp_chrome():
        print("[x] CDP Chrome unreachable on :9222", file=sys.stderr)
        return 0
    seen_goodwill = seen.setdefault("x_goodwill", {})
    try:
        drafted, visited, read = asyncio.run(_goodwill_browse_pass(
            handle, max_visits, max_reads, seen_goodwill, blocklist, live))
    except Exception as e:
        import traceback
        print(f"[goodwill] async pass crashed: {type(e).__name__}: {e}",
              file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 0
    print(f"\n[goodwill] read {read}, visited {visited}, drafted {drafted} "
          f"(live={live})")
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
    p.add_argument("--max-visits", type=int, default=10,
                   help="Goodwill: max posts to actually click INTO per pass "
                        "(your commenting budget, default 10).")
    p.add_argument("--max-reads", type=int, default=30,
                   help="Goodwill: max posts to LLM-judge per pass (your "
                        "judgment-cost budget, default 30). Most reads are "
                        "cheap rejections; this caps the Flash bill.")
    p.add_argument("--live", action="store_true",
                   help="Goodwill: enqueue drafts to ~/.hermes/state/outbox.jsonl "
                        "(outbox-flush.py submits them after the hold-buffer). "
                        "Default is dry-run: composer-pre-fill tab, you submit manually.")
    p.add_argument("--limit", type=int, default=5,
                   help="DEPRECATED — alias for --max-visits. Kept so old cron entries don't break.")
    p.add_argument("--min-score", type=int, default=1,
                   help="DEPRECATED — keyword scoring was replaced by the LLM audience judge. "
                        "Ignored in the new flow.")
    p.add_argument("--scroll-passes", type=int, default=5,
                   help="DEPRECATED — the scroll-and-read loop manages its own scrolling. "
                        "Ignored in the new flow.")
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

    # --limit is the deprecated alias for --max-visits. We only fall back to
    # --limit if the user didn't pass an explicit --max-visits. Detecting that
    # by checking against the parser default keeps the explicit flag winning.
    user_set_max_visits = "--max-visits" in sys.argv
    effective_max_visits = args.max_visits if user_set_max_visits else args.limit

    def _one_pass() -> int:
        seen = load_seen()
        blocklist = load_blocklist()
        drafted = 0
        if args.mode in ("goodwill", "both"):
            drafted += run_goodwill(handle, effective_max_visits,
                                    args.max_reads, seen, blocklist,
                                    args.live)
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
