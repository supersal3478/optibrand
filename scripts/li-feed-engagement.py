#!/usr/bin/env python3
"""li-feed-engagement.py — LinkedIn outbound goodwill on the home feed.

The LinkedIn counterpart to the X feed-engagement, but driven by lipy (Playwright)
instead of CDP:

  1. `lipy feed --limit N`  → posts from your home feed (author + text + urn)
  2. skip your own posts / blocklist / already-seen
  3. draft a short goodwill comment in your voice (reuses inbound-engagement's
     proven drafter + inline brand-guard)
  4. --live : enqueue to the outbox (mode=goodwill). outbox-flush submits each
              via `lipy comment --post <urn>`.
     dry-run : print the draft (default)

Idempotent: skips posts already engaged (state at ~/.hermes/state/li_feed_seen.json).

Usage:
    scripts/li-feed-engagement.py                  # dry-run
    scripts/li-feed-engagement.py --live --limit 3
    scripts/li-feed-engagement.py --reset-seen
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import _outbox  # noqa: E402
import _metrics  # noqa: E402

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
LIPY_BIN = Path(os.environ.get("LIPY_BIN", Path.home() / ".local" / "bin" / "lipy"))
SEEN_PATH = HERMES_HOME / "state" / "li_feed_seen.json"
BLOCKLIST = PROJECT_ROOT / "config" / "blocklist.yaml"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Reuse the proven drafter + voice context from inbound-engagement.py (hyphenated
# filename → load via importlib). draft_reply produces a short, voice-matched,
# brand-guarded comment that engages with the given text — exactly what we want.
def _load_inbound_module():
    spec = importlib.util.spec_from_file_location(
        "inbound_engagement", SCRIPTS / "inbound-engagement.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_IB = _load_inbound_module()
draft_reply = _IB.draft_reply  # (inbound framing; goodwill uses draft_goodwill below)


# ── Goodwill quality gate + drafter ───────────────────────────────────────────
# Goodwill comments on STRANGERS' posts carry the most restriction risk, so unlike
# inbound (replying on your own posts) we (1) JUDGE each post first — skip promo /
# low-value / off-brand — and (2) draft with an outbound "additive comment" framing
# (the inbound draft_reply treats the text as a comment ON your post, which produced
# dismissive replies like "No thanks, I'll pass"). Mirrors X feed-engagement.

# Minimum judge confidence to engage. Goodwill is higher-risk → be conservative.
JUDGE_MIN_CONFIDENCE = 0.55


def _brand_audience_context() -> str:
    """The slices of BRAND.md the judge needs: who the brand serves + what's
    off-limits. Kept small so the judgment stays about fit, not voice/veto."""
    try:
        text = (PROJECT_ROOT / "BRAND.md").read_text()
    except OSError:
        return ""
    wanted = ("## Identity", "## Audiences", "## Off-limits topics",
              "## Engagement principles")
    sections, lines, keep = [], text.splitlines(), False
    cur: list[str] = []
    for ln in lines:
        if ln.startswith("## "):
            if keep and cur:
                sections.append("\n".join(cur).strip())
            cur, keep = [], ln.strip() in wanted
        if keep:
            cur.append(ln)
    if keep and cur:
        sections.append("\n".join(cur).strip())
    return "\n\n".join(s for s in sections if s)[:2500]


def judge_goodwill_post(author: str, text: str) -> dict:
    """Decide whether this feed post is worth a positive, additive goodwill
    comment. Returns {decision: 'yes'|'no', confidence: float, reason: str}.
    Fail-CLOSED: any error → 'no' (never comment on an unjudged stranger post)."""
    import httpx
    import re
    import time
    cfg = _IB._load_azure_config()
    if not cfg.get("api_key") or not cfg.get("base_url"):
        return {"decision": "no", "confidence": 0.0, "reason": "judge_error:no-azure-config"}
    audience_ctx = _brand_audience_context()
    if not audience_ctx:
        return {"decision": "no", "confidence": 0.0, "reason": "judge_error:no-brand-audience"}
    system = (
        "You decide whether the brand below should leave a PUBLIC goodwill comment "
        "on a stranger's LinkedIn post. A goodwill comment is outbound relationship-"
        "building, so it must be on a post where the brand can add a genuine, "
        "positive, on-topic point in front of an aligned audience.\n\n"
        "BE PERMISSIVE on substantive tech/AI/operator topics: AI, agents, "
        "automation, agentic workflows, LLMs, Claude, MCP, browser automation, dev "
        "tooling, infrastructure, shipping software, building products, running an "
        "agency/ops — even if generic or hype-y, the commenters are the brand's "
        "audience. When in doubt on a substantive tech-adjacent post → YES.\n\n"
        "Decide NO when the post is:\n"
        "  - Promotional / an ad / a lead-magnet ('comment WORD and I'll DM you', "
        "    'DM me', selling a course/tool/webinar), or engagement-bait\n"
        "  - Low-value: no substantive point to engage with (one-liner, pure quote, "
        "    'thoughts?', reposted meme, giveaway)\n"
        "  - Off-brand / off-target: crypto/trading, hustle-porn/motivational, "
        "    politics, drama/call-outs, job-seeking, unrelated lifestyle\n"
        "  - Anything where a brand comment would read as spam or self-promotion\n\n"
        "NEVER endorse a 'yes' just to participate. A wrong comment is worse than a "
        "missed one. Return ONLY JSON: "
        '{"decision":"yes"|"no","confidence":<float 0..1>,"reason":<short string>}. '
        "No prose, no markdown, no code fences."
    )
    user = (
        f"=== brand context ===\n{audience_ctx}\n\n"
        f"=== target LinkedIn post ===\n"
        f"Author: {author}\n"
        f"Post text:\n{text}\n\n"
        "Decide. JSON only."
    )
    t0 = time.time()
    try:
        resp = httpx.post(
            f"{cfg['base_url']}/chat/completions",
            headers={"api-key": cfg["api_key"], "Content-Type": "application/json"},
            json={"model": cfg["model"],
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}],
                  "max_tokens": 200, "temperature": 0.2},
            timeout=60,
        )
    except httpx.RequestError as e:
        return {"decision": "no", "confidence": 0.0, "reason": f"judge_error:http:{str(e)[:80]}"}
    print(f"        … judge returned in {time.time()-t0:.1f}s (HTTP {resp.status_code})", flush=True)
    if resp.status_code != 200:
        return {"decision": "no", "confidence": 0.0,
                "reason": f"judge_error:non-200:{resp.status_code}"}
    raw = (resp.json().get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        out = json.loads(raw)
        decision = str(out.get("decision", "no")).strip().lower()
        confidence = float(out.get("confidence", 0.5))
        reason = str(out.get("reason", ""))[:240]
    except (json.JSONDecodeError, ValueError, TypeError):
        m = re.search(r'"decision"\s*:\s*"?(yes|no)"?', raw, re.IGNORECASE)
        if not m:
            return {"decision": "no", "confidence": 0.0,
                    "reason": f"judge_error:bad-json:{raw[:120]}"}
        decision = m.group(1).lower()
        mc = re.search(r'"confidence"\s*:\s*([0-9]+(?:\.[0-9]+)?)', raw)
        confidence = float(mc.group(1)) if mc else 0.5
        mr = re.search(r'"reason"\s*:\s*"([^"]{1,240})"', raw)
        reason = (mr.group(1) if mr else "no-reason") + " [recovered]"
    if decision not in ("yes", "no"):
        decision = "no"
    return {"decision": decision, "confidence": max(0.0, min(1.0, confidence)), "reason": reason}


def draft_goodwill(author: str, text: str) -> dict:
    """Draft a goodwill comment on a stranger's LinkedIn post (outbound framing —
    ADD a point, don't just agree). Returns the same shape as draft_reply
    (decision DRAFT|REFUSE, draft, length_target, autofixes)."""
    import httpx
    import time
    cfg = _IB._load_azure_config()
    if not cfg.get("api_key") or not cfg.get("base_url"):
        return {"decision": "REFUSE", "reasons": [{"rule": "no-azure-config"}]}
    voice_ctx = _IB._brand_voice_context()
    length_label, length_instruction, max_words = _IB.pick_length_target()
    system = (
        "You are Sal AI (in/sal-ai on LinkedIn). You're leaving a GOODWILL comment on "
        "someone ELSE's LinkedIn post you came across in your feed — outbound "
        "engagement to build relationships with other operators in your space. "
        "Match the voice/tone/vocabulary distilled below from BRAND.md and your voice profile.\n\n"
        "ADD a concrete experience, a specific counter-point, or a relevant anecdote "
        "that moves the conversation forward — do NOT just agree, summarize, or praise. "
        "DO NOT use the author's name, first name, display name, or handle anywhere. "
        "Engage directly with the actual point they made.\n\n"
        f"{length_instruction}\n\n"
        "No em-dash (—). No hashtags. No links. No pitching yourself or your services. "
        "Never open with 'Great post', 'Great question', 'Absolutely', 'Thanks for sharing', "
        "'I love this', 'This is amazing', 'Great point', or any sycophantic opener. "
        "Output ONLY the comment text — no JSON, no quotes, no preamble."
    )
    user = (
        f"=== voice + brand context ===\n{voice_ctx}\n\n"
        f"=== outbound goodwill target (LinkedIn) ===\n"
        f"author (DO NOT mention): {author}\n"
        f"post text: {text}\n\n"
        "Write the goodwill comment now."
    )
    t0 = time.time()
    try:
        resp = httpx.post(
            f"{cfg['base_url']}/chat/completions",
            headers={"api-key": cfg["api_key"], "Content-Type": "application/json"},
            json={"model": cfg["model"],
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}],
                  "max_tokens": 400, "temperature": 0.7},
            timeout=90,
        )
    except httpx.RequestError as e:
        return {"decision": "REFUSE", "reasons": [{"rule": "http-error", "detail": str(e)[:200]}]}
    print(f"        … LLM returned in {time.time()-t0:.1f}s (HTTP {resp.status_code})", flush=True)
    if resp.status_code != 200:
        return {"decision": "REFUSE",
                "reasons": [{"rule": "azure-non-200", "status": resp.status_code,
                             "body": resp.text[:300]}]}
    draft = ((resp.json().get("choices", [{}])[0].get("message", {}).get("content")) or "").strip()
    if not draft:
        return {"decision": "REFUSE", "reasons": [{"rule": "empty-llm-output"}]}
    cleaned, refused, autofixes = _IB._inline_brand_guard(draft)
    if refused:
        return {"decision": "REFUSE", "draft": cleaned, "raw_draft": draft,
                "reasons": [{"rule": r} for r in refused],
                "autofixes": autofixes, "length_target": length_label}
    result = {"decision": "DRAFT", "draft": cleaned, "raw_draft": draft,
              "autofixes": autofixes, "length_target": length_label}
    _IB.apply_length_and_punctuation_fixes(result, max_words)
    return result


def _self_name() -> str | None:
    try:
        for line in (PROJECT_ROOT / "BRAND.md").read_text().splitlines():
            s = line.strip()
            if s.startswith("**Name:**"):
                return s.split("**Name:**", 1)[1].strip() or None
    except OSError:
        pass
    return None


def _blocked_terms() -> list[str]:
    try:
        import yaml
        data = yaml.safe_load(BLOCKLIST.read_text()) or {}
        terms: list[str] = []
        # Flat lists of substrings.
        for key in ("keywords", "domains", "accounts"):
            v = data.get(key)
            if isinstance(v, list):
                terms += [str(t).lower() for t in v]
        # `handles` is a dict {platform: [slugs]} — take the LinkedIn slugs only.
        # (Iterating the dict directly yielded its KEYS "x"/"linkedin"/"youtube",
        # and "x" as a substring blocked essentially every post.)
        handles = data.get("handles")
        if isinstance(handles, dict):
            terms += [str(t).lower() for t in (handles.get("linkedin") or [])]
        return [t for t in terms if t]
    except Exception:
        return []


def _load_seen() -> dict:
    try:
        return json.loads(SEEN_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_seen(seen: dict) -> None:
    try:
        SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        SEEN_PATH.write_text(json.dumps(seen))
    except OSError:
        pass


def fetch_feed(limit: int) -> dict:
    """Run `lipy feed` and return {ok, posts:[{urn, url, author, text}]}."""
    try:
        p = subprocess.run([str(LIPY_BIN), "feed", "--limit", str(limit)],
                           capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "lipy_feed_timeout"}
    out = (p.stdout or "").strip()
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"ok": False, "error": (p.stderr or out)[:300]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--limit", type=int, default=5, help="feed posts to consider")
    ap.add_argument("--max-comments", type=int, default=1,
                    help="max goodwill comments to draft this run")
    ap.add_argument("--live", action="store_true",
                    help="enqueue to outbox (else just print)")
    ap.add_argument("--reset-seen", action="store_true")
    args = ap.parse_args()

    if args.reset_seen:
        SEEN_PATH.unlink(missing_ok=True)
        print("[reset] cleared li_feed_seen")

    print("\n=== LinkedIn feed goodwill ===")
    result = fetch_feed(args.limit)
    if not result.get("ok"):
        err = result.get("error") or "unknown"
        if "not_logged_in" in err or "auth_required" in err:
            print("[li] not logged in. Fix with:  lipy login --headed")
        else:
            print(f"[li] feed fetch failed: {err}")
        return 0
    posts = result.get("posts") or []
    if not posts:
        print("[li] feed empty (or logged out?)")
        return 0
    print(f"[li] {len(posts)} feed post(s) returned")

    me = (_self_name() or "").lower()
    blocked = _blocked_terms()
    seen = _load_seen()
    drafted = 0

    for post in posts:
        if drafted >= args.max_comments:
            break
        urn = post.get("urn")
        if not urn or urn in seen:
            continue
        author = post.get("author") or "?"
        text = (post.get("text") or "").strip()
        if not text:
            continue
        if me and me in author.lower():
            seen[urn] = {"ts": _utcnow(), "status": "skip_self"}
            continue
        hay = (author + " " + text).lower()
        if any(b and b in hay for b in blocked):
            seen[urn] = {"ts": _utcnow(), "status": "skip_blocklist"}
            _metrics.log_event("skipped_blocklist", platform="linkedin",
                               mode="goodwill", parent_url=post.get("url"))
            continue

        print(f"\n[post] {author}")
        print(f"       {text[:160]}")

        # Quality gate FIRST — goodwill only on worthwhile, on-brand posts.
        verdict = judge_goodwill_post(author, text)
        decision = verdict.get("decision")
        conf = float(verdict.get("confidence") or 0.0)
        if decision != "yes" or conf < JUDGE_MIN_CONFIDENCE:
            print(f"       SKIP — judge {decision} (conf {conf:.2f}): {verdict.get('reason')}")
            seen[urn] = {"ts": _utcnow(), "status": "skip_judge",
                         "reason": verdict.get("reason")}
            _metrics.log_event("skipped_judge", platform="linkedin", mode="goodwill",
                               parent_url=post.get("url"),
                               judge_decision=decision, judge_confidence=conf,
                               judge_reason=verdict.get("reason"))
            continue
        print(f"       judge: yes (conf {conf:.2f}) — {verdict.get('reason')}")

        res = draft_goodwill(author, text)
        if res.get("decision") != "DRAFT" or not res.get("draft"):
            print(f"       SKIP — drafter {res.get('decision', '?')}: {res.get('reasons')}")
            seen[urn] = {"ts": _utcnow(), "status": "refused"}
            continue
        comment = res["draft"].strip()
        print(f"       Draft: {comment}")
        _metrics.log_event("drafted", platform="linkedin", mode="goodwill",
                           parent_url=post.get("url"), chars=len(comment),
                           word_count=len(comment.split()),
                           length_target=res.get("length_target"),
                           autofixes=res.get("autofixes"))

        if args.live:
            try:
                outbox_id = _outbox.enqueue(
                    platform="linkedin", mode="goodwill",
                    parent_url=urn, draft_text=comment,
                    metadata={"author": author, "post_text": text[:500],
                              "post_url": post.get("url"),
                              "length_target": res.get("length_target"),
                              "autofixes": res.get("autofixes") or []},
                )
                print(f"       ✓ queued outbox id={outbox_id}")
                seen[urn] = {"ts": _utcnow(), "status": "queued", "outbox_id": outbox_id}
            except Exception as e:
                print(f"       FAIL queueing — {e}")
                continue
        else:
            print("       (dry-run — not queued)")
            seen[urn] = {"ts": _utcnow(), "status": "drafted"}
        drafted += 1

    _save_seen(seen)
    print(f"\n=== Done. Drafted {drafted} goodwill comment(s). ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
