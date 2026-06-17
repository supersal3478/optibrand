#!/usr/bin/env python3
"""engage-commenter.py — goodwill follow-up on people who commented on your posts.

The third stage in the X engagement pipeline:

  feed-engagement.py    →   goodwill on home-feed posts (T-30 before each post)
  inbound-engagement.py →   reply to comments on YOUR posts
  engage-commenter.py   →   THIS — for every person who commented on your post,
                            visit their profile, audience-judge their recent
                            posts, draft goodwill on top 1–2 (so we engage
                            their audience, not just them)

State files:
  ~/.hermes/state/commenter_queue.jsonl       — append-only queue (written by
                                                inbound-engagement.py on every
                                                drafted reply)
  ~/.hermes/state/commenter_engagement_seen.json — handles we've engaged within
                                                   the last --cooldown-days

Flow per run:
  1. Read queue. Filter out:
       - handles we already engaged within --cooldown-days (default 14)
       - handles that match our own X handle (BRAND.md)
       - empty/malformed entries
  2. Take up to --max-commenters fresh handles (default 5).
  3. For each: humanly visit their x.com/<handle> profile, scroll, audience-
     judge each post (Flash). Up to --max-per-commenter goodwill drafts per
     commenter (default 1). Enqueue to outbox with mode="commenter_followup".
  4. Mark handle as engaged.

Same humanization layer (skills/x-engage/x_human.py) as feed/inbound. Same
length distribution (50% 7w / 25% 15w / 25% 25w) via _drafter. Honors
caps.yaml (commenter_followup counts against the outbound cap).

Usage:
    scripts/engage-commenter.py                   # dry-run (composer pre-fill)
    scripts/engage-commenter.py --live            # autonomous; outbox
    scripts/engage-commenter.py --max-commenters 3 --max-per-commenter 1
    scripts/engage-commenter.py --cooldown-days 30
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "skills" / "x-engage"))

from _cdp import open_session  # noqa: E402
from x_human import HumanCDP  # noqa: E402
import _outbox  # noqa: E402
import _metrics  # noqa: E402
from _drafter import (  # noqa: E402
    pick_length_target,
    apply_length_and_punctuation_fixes,
)

# Pull audience-judge + helpers from feed-engagement.py (it's a script file
# with a dash in the name, so a normal `import feed_engagement` doesn't work —
# load via importlib.util.spec_from_file_location).
_fe_spec = importlib.util.spec_from_file_location(
    "feed_engagement",
    PROJECT_ROOT / "scripts" / "feed-engagement.py",
)
_fe = importlib.util.module_from_spec(_fe_spec)
_fe_spec.loader.exec_module(_fe)
judge_audience_alignment = _fe.judge_audience_alignment
extract_x_handle_from_brand = _fe.extract_x_handle_from_brand
ensure_cdp_chrome = _fe.ensure_cdp_chrome
_load_azure_config = _fe._load_azure_config
_brand_voice_context = _fe._brand_voice_context
_inline_brand_guard = _fe._inline_brand_guard
_post_to_azure = _fe._post_to_azure
has_skip_signal = _fe.has_skip_signal
is_promoted = _fe.is_promoted
is_blocklisted = _fe.is_blocklisted
load_blocklist = _fe.load_blocklist
author_handle = _fe.author_handle
X_EXTRACT_FEED_JS = _fe.X_EXTRACT_FEED_JS  # same DOM shape works on profiles

QUEUE_PATH = HERMES_HOME / "state" / "commenter_queue.jsonl"
ENGAGED_PATH = HERMES_HOME / "state" / "commenter_engagement_seen.json"


# ─── queue / seen state ───────────────────────────────────────────────


def utcnow() -> str:
    return datetime.now(tz=ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_queue() -> list[dict]:
    if not QUEUE_PATH.exists():
        return []
    rows: list[dict] = []
    with QUEUE_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _load_engaged() -> dict:
    if not ENGAGED_PATH.exists():
        return {}
    try:
        return json.loads(ENGAGED_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def _save_engaged(data: dict) -> None:
    ENGAGED_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENGAGED_PATH.write_text(json.dumps(data, indent=2))


def _eligible_handles(max_count: int, cooldown_days: int,
                       own_handle: str) -> list[dict]:
    """Pick up to max_count handles from the queue who are NOT in cooldown,
    NOT ourselves, and NOT empty. Most-recent first."""
    engaged = _load_engaged()
    own_marker = (own_handle or "").lower()
    cutoff = (datetime.now(tz=ZoneInfo("UTC"))
              - timedelta(days=cooldown_days)).isoformat()
    seen_now: set[str] = set()
    out: list[dict] = []
    # Walk queue newest-first
    for row in reversed(_read_queue()):
        h = (row.get("handle") or "").strip()
        if not h:
            continue
        h_lower = h.lower()
        if h_lower in seen_now:
            continue  # dedupe within this run
        if h_lower == own_marker:
            continue  # never engage yourself
        last_engaged = engaged.get(h_lower, {}).get("last_engaged_at")
        if last_engaged and last_engaged > cutoff:
            continue  # still in cooldown
        seen_now.add(h_lower)
        out.append(row)
        if len(out) >= max_count:
            break
    return out


def _mark_engaged(handle: str, *, posts_engaged: list[str],
                   pass_summary: dict) -> None:
    data = _load_engaged()
    rec = data.get(handle.lower(), {})
    rec["last_engaged_at"] = utcnow()
    rec["total_engagements"] = int(rec.get("total_engagements", 0)) + 1
    history = list(rec.get("history") or [])
    history.append({"ts": utcnow(), "posts": posts_engaged,
                    "summary": pass_summary})
    rec["history"] = history[-10:]  # cap audit trail at last 10 passes
    data[handle.lower()] = rec
    _save_engaged(data)


# ─── per-commenter goodwill drafter ───────────────────────────────────


def draft_commenter_followup(handle: str, op_handle: str,
                             post_text: str) -> dict:
    """Same shape as feed-engagement's draft_goodwill_comment but tailored
    prompt: we explicitly state this is a follow-up because they engaged with
    our content, so the agent can lean slightly warmer."""
    cfg = _load_azure_config()
    if not cfg["api_key"] or not cfg["base_url"]:
        return {"decision": "REFUSE",
                "reasons": [{"rule": "no-azure-config"}]}
    voice_ctx = _brand_voice_context()
    length_label, length_instruction, max_words = pick_length_target()
    system = (
        f"You are Sal AI (@{handle}). This person previously commented on one "
        "of YOUR X posts — now you're returning the goodwill by commenting on "
        "one of THEIR posts. Match the voice/tone/vocabulary distilled from "
        "BRAND.md and your voice profile.\n\n"
        "DO NOT use the OP's name, first name, display name, or handle anywhere "
        "in the comment. Engage directly with the point they made. "
        "ADD a concrete experience, counter-point, or share a relevant "
        "anecdote — do NOT just agree.\n\n"
        f"{length_instruction}\n\n"
        "No em-dash (—). No hashtags. "
        "Never open with 'Great question', 'Absolutely', 'Thanks for sharing', "
        "'I love this', 'Great point', or any sycophantic opener. "
        "Output ONLY the comment text — no JSON, no quotes, no preamble."
    )
    user = (
        f"=== voice + brand context ===\n{voice_ctx}\n\n"
        f"=== commenter follow-up target ===\n"
        f"platform: x\n"
        f"OP handle (DO NOT mention): {op_handle}\n"
        f"OP post text: {post_text}\n\n"
        "Write the goodwill comment now."
    )
    out = _post_to_azure(cfg, system, user)
    if isinstance(out, dict):
        out["length_target"] = length_label
        apply_length_and_punctuation_fixes(out, max_words)
    return out


# ─── async browse pass per commenter ──────────────────────────────────


X_HOME_URL = "https://x.com/home"


async def _engage_one_commenter(handle: str, max_per_commenter: int,
                                 blocklist: dict, live: bool) -> dict:
    """Visit x.com/<handle>, scroll, judge their recent posts, draft
    goodwill on top max_per_commenter. Returns a summary dict."""
    summary = {"handle": handle, "visited": 0, "judged": 0,
               "drafted": 0, "posts_engaged": [], "errors": []}
    profile_url = f"https://x.com/{handle}"

    try:
        async with open_session() as s:
            h = HumanCDP(s)
            await s.navigate(profile_url, settle_seconds=4.0)

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
                summary["errors"].append("profile_didnt_load")
                return summary

            # A couple of human-paced scrolls so we get more than the top
            # three above-the-fold posts.
            for _ in range(random.randint(2, 4)):
                vw, vh = await h.viewport_size()
                await h.scroll(int(vh * random.uniform(0.8, 1.6)))
                await h.jitter(1.0, 2.5)

            visible = await s.eval_js(X_EXTRACT_FEED_JS, await_promise=False) or []
            # Filter: drop pinned, ads, blocklisted, retweets to oneself,
            # empty previews. Keep posts that are THIS handle's authored
            # content (skip quote-tweets / reposts for now to keep judgments
            # tight).
            self_marker = f"@{handle}".lower()
            fresh = []
            for p in visible:
                url = p.get("url")
                if not url:
                    continue
                if p.get("pinned"):
                    continue
                if is_promoted(p):
                    continue
                if is_blocklisted(url, blocklist):
                    continue
                preview = (p.get("text") or "").strip()
                if not preview:
                    continue
                skip_hit, _ = has_skip_signal(preview)
                if skip_hit:
                    continue
                # Only the commenter's own posts — the profile view also
                # shows their reposts. Filter to URLs that contain /<handle>/.
                if f"/{handle.lower()}/" not in url.lower():
                    continue
                fresh.append(p)

            if not fresh:
                summary["errors"].append("no_candidate_posts")
                return summary

            random.shuffle(fresh)
            for post in fresh[:max(3, max_per_commenter * 2)]:
                if summary["drafted"] >= max_per_commenter:
                    break
                summary["judged"] += 1
                url = post["url"]
                op_display = post.get("author") or ""
                op_handle = author_handle(op_display) or f"@{handle}"
                preview = (post.get("text") or "").strip()

                # The judge is the SAME audience-alignment judge feed uses —
                # treat the commenter's post like any other goodwill candidate.
                verdict = judge_audience_alignment(handle, op_handle,
                                                    op_display, preview)
                _metrics.log_event("audience_judged", platform="x",
                                   mode="commenter_followup",
                                   parent_url=url,
                                   decision=verdict["decision"],
                                   confidence=verdict["confidence"],
                                   reason=verdict["reason"])
                if verdict["decision"] != "yes":
                    continue

                # Draft.
                result = draft_commenter_followup(handle, op_handle, preview)
                if result.get("decision") != "DRAFT":
                    _metrics.log_event("vetoed_brandguard", platform="x",
                                       mode="commenter_followup",
                                       parent_url=url,
                                       reasons=result.get("reasons"))
                    continue
                reply_text = result["draft"].strip()
                _metrics.log_event("drafted", platform="x",
                                   mode="commenter_followup",
                                   parent_url=url, chars=len(reply_text),
                                   word_count=len(reply_text.split()),
                                   length_target=result.get("length_target"),
                                   autofixes=result.get("autofixes"),
                                   confidence=verdict["confidence"])

                if live:
                    try:
                        outbox_id = _outbox.enqueue(
                            platform="x", mode="commenter_followup",
                            parent_url=url, draft_text=reply_text,
                            metadata={
                                "op_handle": op_handle,
                                "op_display": op_display,
                                "judge_confidence": verdict["confidence"],
                                "judge_reason": verdict["reason"],
                                "length_target": result.get("length_target"),
                                "autofixes": result.get("autofixes") or [],
                                "via_commenter_handle": handle,
                            },
                        )
                        summary["drafted"] += 1
                        summary["posts_engaged"].append(url)
                        print(f"        ✓ queued outbox id={outbox_id}  ({reply_text})")
                    except Exception as e:
                        summary["errors"].append(f"queue_failed:{e}")
                else:
                    # Dry-run: composer pre-fill in a new tab.
                    fill = await _fe.fill_composer_in_new_tab(url, reply_text)
                    if fill.get("ok"):
                        summary["drafted"] += 1
                        summary["posts_engaged"].append(url)
                        print(f"        ✓ composer pre-filled  ({reply_text})")
                        # Bring profile tab back to foreground so subsequent
                        # CDP calls aren't throttled (same fix as feed).
                        try:
                            await s.call("Page.bringToFront")
                        except Exception:
                            pass
                    else:
                        summary["errors"].append(
                            f"compose_failed:{fill.get('error')}")
                # Brief inter-draft jitter so we don't stack two enqueues
                # back-to-back.
                await h.jitter(8, 20)

            summary["visited"] = 1
            await h.park_cursor_offscreen()
    except Exception as e:
        summary["errors"].append(f"exception:{type(e).__name__}:{e}")
    return summary


# ─── main ─────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--max-commenters", type=int, default=5,
                   help="Max commenters to engage per run (default 5).")
    p.add_argument("--max-per-commenter", type=int, default=1,
                   help="Max goodwill drafts per commenter (default 1). "
                        "Bumping above 1 risks looking like brigading.")
    p.add_argument("--cooldown-days", type=int, default=14,
                   help="Don't re-engage the same commenter within N days "
                        "(default 14). Audit trail in commenter_engagement_seen.json.")
    p.add_argument("--live", action="store_true",
                   help="Enqueue drafts to outbox (mode=commenter_followup). "
                        "Default: dry-run composer pre-fill.")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    own_handle = extract_x_handle_from_brand() or ""
    if not own_handle:
        print("[commenter] couldn't read X handle from BRAND.md — aborting",
              file=sys.stderr)
        return 2

    eligible = _eligible_handles(args.max_commenters, args.cooldown_days,
                                  own_handle)
    if not eligible:
        print("[commenter] no eligible queued commenters "
              "(empty queue or all in cooldown)")
        return 0

    if not ensure_cdp_chrome():
        print("[commenter] CDP Chrome unreachable on :9222", file=sys.stderr)
        return 2

    blocklist = load_blocklist()
    total_drafted = 0
    for q in eligible:
        handle = q["handle"]
        print(f"\n[commenter] {handle}  (source reply: "
              f"{(q.get('source_reply_url') or '')[:60]})")
        summary = asyncio.run(_engage_one_commenter(
            handle, args.max_per_commenter, blocklist, args.live))
        total_drafted += summary["drafted"]
        _mark_engaged(handle, posts_engaged=summary["posts_engaged"],
                      pass_summary={k: v for k, v in summary.items()
                                    if k != "posts_engaged"})
        if args.verbose:
            print(f"          summary: {summary}")
        # Inter-commenter jitter so we don't bounce profile-to-profile in a
        # robotic rhythm.
        time.sleep(random.uniform(15, 45))

    print(f"\n[commenter] done. {total_drafted} draft(s) across "
          f"{len(eligible)} commenter(s) (live={args.live})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
