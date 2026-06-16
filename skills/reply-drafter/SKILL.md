---
name: reply-drafter
description: "Generate a reply or comment in the user's voice. Reads voice_profile.json + BRAND.md + the parent post/comment. Calls brand-guard. Produces a draft ready to post (or a refusal)."
version: 0.1.0
author: brand-growth-engine
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [content-creation, brand, drafting]
prerequisites:
  files:
    - <project>/BRAND.md
    - ~/.hermes/memories/voice_profile.json
  skills:
    - brand-guard
---

# reply-drafter

Generates a single reply or outbound comment in the user's voice, validates it via `brand-guard`, and returns either the draft (PASS) or a refusal explaining why no acceptable draft could be produced.

**Never posts. Never sends.** Posting is the responsibility of the platform skill (xurl, youtube-engage, linkedin-engage). The drafter only produces text + a PASS/FAIL.

---

## When to invoke

Whenever the agent needs to author a reply or outbound comment. Always invoked before any platform skill's `post` / `reply` action.

---

## Inputs

- `parent_text`: the text being replied to (required)
- `parent_author`: handle/name of the parent's author (recommended — used to gauge tone match)
- `parent_meta`: extra context — `{platform, post_url, posted_at, parent_engagement: {likes, replies}}` (optional)
- `target_kind`: `"own_post_reply" | "outbound_comment" | "yt_mod_reply"` (required)
- `platform`: `"x" | "linkedin" | "youtube"` (required)
- `intent_hint`: the user's high-level intent, if known: `"add_value" | "share_experience" | "challenge_respectfully" | "agree_and_extend"` (optional)

---

## Workflow

1. **Load context**: read `BRAND.md`, `~/.hermes/memories/voice_profile.json`, and `config/blocklist.yaml` (skip if parent_text matches any blocklist keyword/domain — return refusal `"reason": "blocklisted-input"`).

2. **Decide engagement angle**: pick exactly one of:
   - `agree_and_extend` — agree with the parent's main point, then add a concrete experience or counter-example.
   - `challenge_respectfully` — disagree on a specific claim with a concrete counter-point. Never tone-policing; only substance.
   - `share_experience` — relevant first-person anecdote that connects to the parent's point.
   - `clarifying_question` — only when something genuinely needs clarification, not as filler.

   Bias against `agree_and_extend` for outbound comments (BRAND.md says "I add a specific concrete experience or counter-point. I do not just agree.").

3. **Draft v1** with the LLM. Prompt structure:

   ```
   You are drafting a {platform} {target_kind} in this user's voice.

   THEIR VOICE PROFILE (always follow):
   <voice_profile.json>

   THEIR BRAND GUIDE (always follow; off-limits is hard):
   <BRAND.md>

   PARENT POST/COMMENT (what we're replying to):
   {parent_text}
   Author: {parent_author}
   Platform: {platform}

   ENGAGEMENT ANGLE: {chosen_angle}

   PLATFORM CONSTRAINTS:
   - X: ≤ 280 chars, no link unless from parent.
   - LinkedIn: ≤ 700 chars, no more than 1 person tagged, weekday-business-hour tone.
   - YouTube: ≤ 1500 chars, conversational.

   Output ONLY the reply text. No quotes around it. No preamble.
   No em-dashes used as stylistic crutch (max 1).
   Do not open with "Great point!", "Absolutely love this", or any sycophantic opener.
   Do not invent statistics. Do not invent URLs. Do not promise outcomes.
   ```

4. **Brand-guard the draft** by calling the `brand-guard` skill with `{draft_text, target_kind, platform, parent_text}`.

5. **Branch on result**:
   - **PASS** → return draft.
   - **FAIL with `suggested_revision`** → use the revision (it has already been re-validated). Return revision.
   - **FAIL without `suggested_revision`** → re-draft once with the FAIL reasons added to the prompt as constraints. Re-run brand-guard.
   - If the second draft also FAILs → return refusal: `{decision: "REFUSE", reasons: [...]}`. Do not draft a third time. Brand-guard exists to stop us, not to be brute-forced.

6. **Return** one of:
   ```json
   {"decision": "DRAFT", "draft": "<text>", "angle": "...", "model": "...", "tokens_in": N, "tokens_out": N, "brand_guard": {...}}
   ```
   ```json
   {"decision": "REFUSE", "reasons": [...], "attempts": [{"draft": "...", "guard": {...}}, ...]}
   ```

---

## Model selection

By default use **`DeepSeek-V4-Flash`** — the cheapest deployment and the configured default in `~/.hermes/config.yaml`. Escalate to **`DeepSeek-V4-Pro`** only if BOTH:
- `parent_engagement.likes` >= 500 (i.e., a high-visibility post where reply quality matters), AND
- `target_kind` is `outbound_comment`.

To escalate for a single draft, run it through a per-call override rather than changing config:
`hermes chat --provider azure-foundry -m DeepSeek-V4-Pro -q "..."`.

This caps cost. Inbound replies and YouTube moderation always stay on Flash. Record which model produced the draft in the `model` field of the return value.

---

## Special cases

- **Serious-topic parent** (death, illness, crisis): omit any CTA, prefer short and sincere. Use `engagement_style: agrees_then_extends` or `share_experience`. Brand-guard's `cta-where-forbidden` rule will catch CTAs anyway.
- **Spam-classified parent** (per spam-classifier or blocklist): refuse — no reply at all.
- **Parent in another language**: reply in the same language. If unsure, reply in English.
- **Sarcastic parent**: do not engage on the sarcasm. Address the underlying point.

---

## Logging

Append every draft attempt and outcome to `~/.hermes/memories/sent_replies.jsonl` (this also feeds `voice-profile` retrain):

```json
{"ts": "...", "platform": "...", "target_kind": "...", "parent_id": "...",
 "angle": "...", "draft": "...", "decision": "DRAFT|REFUSE",
 "guard_reasons": [...], "user_edited": null, "posted_at": null}
```

`user_edited` and `posted_at` are filled in later by the dashboard/cron job once we know the outcome.

---

## Notes

- **Never** include the agent's own commentary in the output ("Here's a reply for you:..." is a bug).
- **Never** include hashtags unless the user's voice profile explicitly uses them (most professional brands don't).
- The drafter is allowed two attempts. If both fail brand-guard, that's a signal — surface it. Repeated refusals on a particular kind of parent can mean BRAND.md is too strict for the engagement scope.
