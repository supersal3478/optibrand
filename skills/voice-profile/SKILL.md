---
name: voice-profile
description: "Distill the user's writing voice from their post/comment history and BRAND.md, and produce a voice_profile.json that downstream skills (reply-drafter, brand-guard) read."
version: 0.1.0
author: brand-growth-engine
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [brand, voice, training, content-creation]
prerequisites:
  files:
    - <project>/BRAND.md
    - <project>/corpus/  (one or more .jsonl files; can be partial)
---

# voice-profile

Reads the user's BRAND.md and the corpus of their past LinkedIn posts/comments, X replies, and YouTube comments, and writes a structured voice profile JSON the agent uses on every reply.

This skill runs in two modes:
- **bootstrap** — first run, distills from scratch
- **retrain** — periodic re-distillation that weights the original corpus 2× over recent agent-generated replies (defends against voice drift)

The output lives at `~/.hermes/memories/voice_profile.json` and is read on every draft.

---

## When to invoke

- Once at Phase 0 setup, after the user has dropped exports into `corpus/`.
- Weekly via cron (Sun 03:00) using the `retrain` mode.
- On demand when the user runs `/voice-profile` or asks "update my voice profile."

---

## Inputs

The agent should locate these files (paths relative to the project root, which it can discover by walking up from CWD):

- `BRAND.md` — the user's authored voice/brand guide (always loaded).
- `corpus/linkedin_posts.jsonl` — original posts.
- `corpus/linkedin_comments.jsonl` — comments the user wrote on **others'** posts (highest voice signal).
- `corpus/x_replies.jsonl` — user's replies on X.
- `corpus/youtube_comments.jsonl` — user's replies in YouTube threads.

Skip files that don't exist. If the total record count is < 20, proceed but mark `confidence: "low"` in the output.

---

## Output schema

Write `~/.hermes/memories/voice_profile.json`:

```json
{
  "version": "0.1.0",
  "generated_at": "ISO-8601 UTC",
  "corpus_record_count": <int>,
  "confidence": "low|medium|high",
  "tone": {
    "register": "casual|conversational|professional|authoritative",
    "warmth": 0.0,
    "directness": 0.0,
    "humor_present": true,
    "humor_style": "dry|playful|sardonic|absent"
  },
  "vocabulary": {
    "signature_phrases": ["...", "..."],
    "preferred_words": ["..."],
    "avoid_phrases_observed": ["..."]
  },
  "structure": {
    "avg_sentence_length_words": <int>,
    "avg_reply_length_chars": <int>,
    "uses_lists_in_replies": true,
    "uses_questions_in_replies": true,
    "typical_opening_patterns": ["...", "..."],
    "typical_closing_patterns": ["...", "..."]
  },
  "engagement_style": {
    "agrees_then_extends": 0.0,
    "challenges_with_concrete_counter": 0.0,
    "shares_personal_anecdote": 0.0,
    "asks_clarifying_question": 0.0
  },
  "platform_specific": {
    "linkedin":  {"avg_chars": <int>, "tone_shift": "..."},
    "x":         {"avg_chars": <int>, "tone_shift": "..."},
    "youtube":   {"avg_chars": <int>, "tone_shift": "..."}
  },
  "brand_alignment": {
    "brand_md_hash": "sha256:...",
    "off_limits_phrases": ["..."],
    "required_ctas": {
      "linkedin_outbound": "...",
      "x_outbound": "..."
    }
  }
}
```

The numeric fields (warmth, directness, agrees_then_extends, etc.) are 0.0–1.0 estimates from analyzing the corpus.

---

## Bootstrap workflow

1. Resolve the project root (look for `BRAND.md` walking up from CWD; ask user if not found).
2. Read `BRAND.md`. Compute its sha256 → `brand_alignment.brand_md_hash`.
3. Read each `corpus/*.jsonl` that exists. Concatenate into a single sample list, **tagged by source platform**.
4. Sample up to 200 records (prefer comments-on-others over original posts — they're a stronger voice signal).
5. Send the sample + BRAND.md to the LLM with this distillation prompt structure:

   ```
   You are distilling a writing voice profile from real samples authored by the user.

   BRAND.MD (their explicit guidance):
   <full BRAND.md>

   SAMPLES (each item is {platform, text}):
   <samples>

   Output a JSON object matching the schema below. Every numeric field must be a calibrated estimate, not a guess. If you cannot estimate, use null. Quote signature_phrases verbatim from the samples — do not invent.
   <schema>
   ```

6. Validate the JSON parses and matches the schema.
7. Add `off_limits_phrases` from BRAND.md's "Off-limits phrasings" section (verbatim).
8. Write to `~/.hermes/memories/voice_profile.json`.
9. Print a one-line summary: `voice profile written: confidence=<c>, samples=<n>, signature_phrases=<count>`.

---

## Retrain workflow

1. Run bootstrap, BUT:
2. Add a second corpus source: `~/.hermes/memories/sent_replies.jsonl` (replies the agent has posted on the user's behalf, with a `user_edited: true|false` flag).
3. Weight the original corpus **2×** over `sent_replies.jsonl` when sampling. Drop sent replies where `user_edited: true` AND the edit ratio > 0.5 (treats heavy edits as voice rejection).
4. Compare new profile to previous via `~/.hermes/memories/voice_profile.json.bak` (keep the prior as backup).
5. If `confidence` decreased OR `signature_phrases` Jaccard similarity to prior < 0.5: stop, print warning, do not overwrite. Surface to user via dashboard.
6. Otherwise overwrite and back up the prior.

---

## Failure modes

- **Empty corpus**: Write a profile that defers entirely to BRAND.md. Set `confidence: "low"`. Mark in output: `"corpus_status": "empty — agent will rely on BRAND.md only"`.
- **BRAND.md missing or unfilled**: Refuse to run. Print: `"BRAND.md required before voice-profile can run."`
- **LLM returns invalid JSON**: Retry once with explicit schema reminder. If it fails again, surface the error and exit non-zero.
- **Schema drift detected** in retrain (see step 5): preserve prior, surface warning.

---

## Notes

- This skill produces a profile, it does not produce content. `reply-drafter` reads this profile.
- Privacy: `corpus/` may contain handles of people the user engaged. The LLM call will see this — make sure the model provider's data policy is acceptable to the user. (Anthropic and OpenRouter do not train on API data by default.)
- Model: distillation is a one-shot, quality-critical, reasoning-heavy pass that runs only weekly, so it's worth escalating to **`DeepSeek-V4-Pro`** rather than the Flash default. `scripts/voice-train.py` passes `-m DeepSeek-V4-Pro` automatically (override with `--model`).
- Cost: a one-time bootstrap with 200 samples + BRAND.md is ~30K input tokens — a few cents on DeepSeek pricing even on Pro. Retrain weekly is the same.
