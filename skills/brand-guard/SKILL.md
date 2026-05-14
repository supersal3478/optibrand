---
name: brand-guard
description: "Hard-veto validator for any drafted reply or comment. Reads BRAND.md + voice_profile.json. Returns pass/fail and reasons. MUST be called before any post action across all platforms."
version: 0.1.0
author: brand-growth-engine
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [brand, safety, content-validation, gating]
prerequisites:
  files:
    - <project>/BRAND.md
    - ~/.hermes/memories/voice_profile.json
---

# brand-guard

Validates a drafted reply or comment against the user's brand guide. Returns a single decision: **PASS** or **FAIL** with reasons. **No draft is posted on any platform unless brand-guard returns PASS.**

This is a hard veto. The agent must not post on a FAIL even if the user's prompt seems to authorize it.

---

## When to invoke

Before every post/reply/comment action on any platform. Specifically:
- Before `xurl post`, `xurl reply`
- Before any LinkedIn post action
- Before YouTube `comments.insert`
- (Not needed for deletions/moderation — only for content the agent authors)

---

## Inputs

- `draft_text`: the text the agent intends to post (required)
- `target_kind`: `"own_post_reply" | "outbound_comment" | "yt_mod_reply" | "x_inbound_reply" | "li_inbound_reply"` (required)
- `platform`: `"x" | "linkedin" | "youtube"` (required)
- `parent_text`: the post or comment being replied to, if applicable (recommended)

---

## Output

A JSON object:

```json
{
  "decision": "PASS" | "FAIL",
  "reasons": [
    {"rule": "rule-id", "severity": "hard|soft", "detail": "..."},
    ...
  ],
  "suggested_revision": "..."   // only if FAIL with soft-fix-possible reasons
}
```

A draft is `PASS` only if `reasons` contains zero `severity: "hard"` entries.

---

## Validation rules

Run all checks. Each rule has an ID, a severity (hard or soft), and clear pass/fail criteria.

### Hard-veto rules (any one triggers FAIL)

| Rule ID | Description |
|---|---|
| `competitor-named` | Mentions a competitor by name (read competitors list from BRAND.md "Off-limits topics" + any explicit competitor list). |
| `unsourced-claim` | Makes a numeric/statistical claim ("studies show 73%...") without that claim being in the input `parent_text`. |
| `invented-link` | Contains a URL that wasn't in `parent_text` or BRAND.md. The agent must never invent links. |
| `outcome-promise` | Promises a specific outcome ("you will get X clients", "this will 10x your reach", "guaranteed"). |
| `lazy-quote-opening` | First sentence quotes the parent verbatim. |
| `off-limits-topic` | Touches any topic listed under "Off-limits topics" in BRAND.md. |
| `off-limits-phrasing` | Contains any phrase from BRAND.md's "Off-limits phrasings" or `voice_profile.brand_alignment.off_limits_phrases`. |
| `sycophantic-opening` | Opens with "Great point!", "Absolutely love this", "What a thought-provoking post", "This is gold", or similar. |
| `forbidden-ai-tells` | Contains "delve", "tapestry", "underscores", "in today's fast-paced world", "in conclusion", or any phrase the BRAND.md lists. |
| `em-dash-overuse` | More than 1 em-dash in the draft (configurable in BRAND.md). |
| `cta-where-forbidden` | Contains a CTA when target_kind is `own_post_reply`, `yt_mod_reply`, or any reply to a "serious-topic" parent (condolence, crisis, illness, death). |
| `length-violation` | LinkedIn comment > 700 chars; X reply > 280 chars; YouTube reply > 1500 chars. |
| `excess-tagging` | Tags more than 1 person on LinkedIn, more than 2 on X. |
| `voice-drift-severe` | None of the draft's signature patterns match `voice_profile.structure.typical_opening_patterns` AND draft length deviates > 3× from `voice_profile.platform_specific.<platform>.avg_chars`. |

### Soft rules (PASS but recorded; if 3+ soft fails, escalate to FAIL)

| Rule ID | Description |
|---|---|
| `agreement-only` | Draft only agrees, doesn't extend with concrete counter-point or experience. |
| `filler-question` | Ends with a generic "Thoughts?" or "What do you think?" |
| `vague-opener` | Opens with "Interesting", "Actually", or other low-information starts. |
| `cta-missing` | target_kind is outbound + BRAND.md requires CTA + draft has none. |

---

## Workflow

1. Load BRAND.md (project root) and `~/.hermes/memories/voice_profile.json`.
2. Verify the BRAND.md hash matches `voice_profile.brand_alignment.brand_md_hash`. If not, warn — voice profile is stale.
3. Run every hard-veto rule against the draft. Collect all hits, don't short-circuit.
4. Run soft rules. Count.
5. Decide:
   - Any hard hit → FAIL
   - 3+ soft hits → FAIL
   - Else → PASS
6. If FAIL and at least one fixable reason (e.g., just `cta-missing` or `filler-question`), generate `suggested_revision` by rewriting the draft to fix those issues. Re-run brand-guard on the revision. If revision passes, return PASS with a note that the draft was auto-revised.
7. Return the JSON object.

---

## Logging

Every brand-guard call must be logged to `~/.hermes/logs/brand_guard.jsonl` (append-only):

```json
{"ts": "...", "platform": "...", "target_kind": "...", "draft_hash": "sha256:...", "decision": "...", "reasons": [...], "auto_revised": false}
```

This log is the auditable record of what was checked and why anything was rejected — critical for post-incident review if a bad reply slips through.

---

## Failure modes

- **BRAND.md unfilled** (placeholders still present): refuse — return FAIL with reason `brand-md-not-configured`.
- **voice_profile.json missing**: still run all BRAND.md-derived rules; note in output `"voice_profile_present": false`. The agent should not post until voice-profile has run at least once.

---

## Notes

- This skill is the single point of enforcement. Do not duplicate brand checks in `reply-drafter` — let the drafter draft, let brand-guard veto.
- The skill is intentionally pessimistic. It's better to reject a borderline draft than to post a bad one.
- Soft-rule thresholds and the em-dash limit can be tuned by editing this SKILL.md (no code change needed).
