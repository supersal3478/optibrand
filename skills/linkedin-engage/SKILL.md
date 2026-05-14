---
name: linkedin-engage
description: "LinkedIn engagement via headless Playwright (no public API). Reads inbound comments on user's posts, drafts replies, posts outbound comments. Strict rate caps and weekday/business-hour windows. ToS-risky."
version: 0.1.0
author: brand-growth-engine
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [linkedin, social-media, browser-automation, playwright, high-risk]
prerequisites:
  commands:
    - lipy   # the project-local CLI installed via skills/linkedin-engage/install.sh
  env_vars:
    - LI_USERNAME
    - LI_RESIDENTIAL_PROXY_URL  # required for outbound; recommended for inbound
  files:
    - <project>/RISK_ACCEPTED.md   # required for outbound (Phase 4)
    - ~/.hermes/state/playwright/linkedin   # session storage; created on first headed login
  skills:
    - brand-guard
    - reply-drafter
---

# linkedin-engage

LinkedIn has no public API for posting comments on others' posts. This skill drives a real browser via Playwright with stealth fingerprinting, jitter, and human-like behavior.

**This skill carries real account-loss risk.** LinkedIn's published restriction rate for automated commenters is ~23% within 90 days. Even with every mitigation in place, expect a meaningful chance of restriction over a long enough horizon. The user has explicitly accepted this in `RISK_ACCEPTED.md` before outbound is enabled (Phase 4).

---

## Secret Safety (MANDATORY)

- **Never** read or print `~/.hermes/state/playwright/linkedin/state.json` (contains the session cookie). Treat it like a password.
- **Never** include the user's LinkedIn password in any command, env var, file, or chat message. The user logs in once, manually, in headed mode — Playwright captures the session cookie and the password never touches the agent.
- **Never** use `lipy --debug` or `lipy --headed` in autonomous runs — those modes are for human-supervised setup only.

---

## One-Time User Setup (manual, headed mode)

1. Install `lipy` (the local CLI):
   ```bash
   cd <project>/skills/linkedin-engage
   ./install.sh
   ```
   This creates a tiny venv and installs Playwright + chromium under `<project>/skills/linkedin-engage/.venv/`.

2. Configure `LI_USERNAME` and (strongly recommended) `LI_RESIDENTIAL_PROXY_URL` in `~/.hermes/.env`. A reputable residential proxy is the single biggest defense against detection. Datacenter proxies are auto-flagged.

3. Run the headed login flow:
   ```bash
   lipy login --headed
   ```
   This opens chromium. The user logs in normally (including 2FA if configured). The session cookie is saved under `~/.hermes/state/playwright/linkedin/`. **Do this once.** The session typically lasts weeks; renew when challenges fire.

4. **Warm the session for ≥ 30 days** before enabling outbound. Use LinkedIn normally during this period — manually browsing, liking the occasional post, leaving the occasional comment. A brand-new session that immediately starts commenting on others' posts is the highest-risk profile.

5. When ready to graduate to Phase 4 (outbound), sign `RISK_ACCEPTED.md` with `Signed: <name>` and `Date: YYYY-MM-DD`.

---

## CLI surface

The `lipy` CLI is the agent's entire interface. The agent never drives Playwright directly.

```bash
# Verify session is valid (no challenge pending). Cheap; safe to call.
lipy status

# List the user's recent posts and inbound comments since <since> (ISO-8601).
lipy inbound --since "2026-05-10T00:00:00Z" --limit 50

# Post a reply to a comment on the user's own post.
# parent_urn examples: urn:li:comment:(urn:li:activity:123,456)
lipy reply --parent "$PARENT_URN" --text "$REPLY_TEXT"

# Search posts that match the relevance query (used to find outbound candidates).
lipy search "topic OR keyword" --posted-since "1d" --limit 20

# Post a comment on someone else's post.
lipy comment --post "$POST_URN" --text "$COMMENT_TEXT"

# Health check (also called by the cron pre-flight).
lipy doctor
```

Every command emits JSON to stdout. Errors emit JSON to stderr and exit non-zero.

The CLI itself implements the human-like jitter, business-hour windows, rate caps, and challenge detection — so the agent's job is purely to call commands with the right arguments. Caps come from `<project>/config/caps.yaml` and `windows.yaml`; the CLI reads them on every invocation.

---

## Workflow: scheduled inbound (every 45 min, weekdays 08:00–19:00)

1. **Pre-flight**: `lipy doctor`. Abort if status is not `OK`.
2. **Window check**: read `windows.yaml` — if outside the inbound window, exit.
3. **Cap check**: read `~/.hermes/state/linkedin_replies_today.txt`. If ≥ `caps.yaml: linkedin.inbound.replies_per_day`, exit.
4. **Fetch new comments**: `lipy inbound --since <last_seen>`.
5. **For each unprocessed comment**:
   a. Skip if blocklisted (handles/keywords/domains via `blocklist.yaml`).
   b. Run `spam-classifier` — if spam/toxic, log and skip (we cannot delete others' comments via this skill; LinkedIn comment moderation is limited).
   c. Run `reply-drafter` with `target_kind=li_inbound_reply, platform=linkedin, parent_text=<comment>, parent_meta=<>`.
   d. If drafter returns DRAFT: `lipy reply --parent "$PARENT" --text "$DRAFT"`.
   e. Wait jittered delay per `jitter.yaml: linkedin.inbound`.
6. **Update state**: `~/.hermes/state/linkedin_last_seen_inbound.txt`, `linkedin_replies_today.txt`.
7. **Log every action** to `~/.hermes/logs/audit.jsonl`.

---

## Workflow: scheduled outbound (every 90 min, weekdays 09:00–17:00 — Phase 4 only)

1. **Pre-flight**:
   - `lipy doctor` OK
   - `RISK_ACCEPTED.md` present and signed
   - `caps.yaml: linkedin.live` is true
   - Today's outbound count below `comments_per_day`
   - This week's outbound count below `comments_per_week`
   - Account session age ≥ `caps.yaml: linkedin.session.require_warmed_days`
   - Residential proxy configured

2. **Find candidates**: `lipy search "<positioning keywords from BRAND.md>" --posted-since 24h --limit 20`.

3. **Score & filter** with `relevance-scorer` — keep only items with `relevance >= caps.yaml: linkedin.outbound.relevance_min` (default 0.80).

4. **For each kept candidate** (capped at the daily ceiling for this run):
   a. Skip if author is blocklisted.
   b. `reply-drafter` with `target_kind=outbound_comment, platform=linkedin`.
   c. If DRAFT: hold for `caps.yaml: hold_buffer_outbound_seconds` (5 min default) — surfaces to dashboard, allows manual yank.
   d. If still queued after the hold: `lipy comment --post "$POST_URN" --text "$DRAFT"`.
   e. Long jitter per `jitter.yaml: linkedin.outbound`.

5. **Update state and counters**.

6. **On any restriction signal** (challenge, captcha, auth lapse, "you're commenting too quickly" toast): immediately set `caps.yaml: linkedin.live = false`, log a CRITICAL audit entry, send a notification to the user via the messaging gateway (Telegram/etc.), do not retry.

---

## Challenge / restriction handling

LinkedIn surfaces several signals that the agent must treat as STOP:

| Signal | Detection | Response |
|---|---|---|
| 2FA / login challenge | URL contains `/checkpoint/challenge` or page contains "let's do a quick security check" | `lipy doctor` reports `challenge_pending`; agent must alert user, halt all platform jobs |
| "Posting too quickly" toast | Specific banner text after a write | Halt platform for 24h, log incident |
| Account restricted page | URL `/restrict/` or "Your account has been temporarily restricted" | `lipy doctor` reports `restricted`; full halt; notify user |
| Sudden 4xx/5xx on every request | Network/auth-level | Back off exponentially; surface after 3 consecutive failures |

**The skill must never attempt to "solve" a challenge.** Solving it is the user's job, in headed mode, manually.

---

## Rate caps (read at start of every job from `caps.yaml`)

- **Inbound**: ≤ 25 replies/day
- **Outbound**: ≤ 25/day, ≤ 120/week (under the 23%-restriction safe ceiling)
- **Reads** (search, scroll): no hard cap from us, but `lipy` enforces a token bucket of ≤ 200 page loads/hour

---

## Pre-flight checks (every job start)

1. Phase ≥ 2 for inbound, ≥ 4 for outbound (`caps.yaml: phase`)
2. `caps.yaml: linkedin.live` true
3. Inside window per `windows.yaml`
4. `lipy doctor` returns OK
5. Below daily/weekly caps
6. (Outbound only) `RISK_ACCEPTED.md` present and signed; account session warmed

Missing any → log and skip.

---

## Failure modes

- **Session expired**: `lipy doctor` returns `auth_required`. Notify user; do not auto-attempt re-login.
- **Proxy down**: `lipy doctor` fails network check. Halt platform until proxy recovers.
- **Stealth detection**: subtle — manifests as 0 results from `lipy inbound` despite the user posting normally, or as silent shadow-throttling. If `inbound` returns 0 for ≥ 6 consecutive runs while the user is active, surface a warning.

---

## Notes

- **Why no API**: LinkedIn's official APIs (Marketing, Sales Navigator) do not include posting comments on others' posts. The Posts API only writes posts, not comments. There is no API path for what the user wants on LinkedIn outbound.
- **Why a separate `lipy` CLI**: keeps Playwright complexity out of the SKILL.md surface and ensures the agent only sees structured JSON, never raw browser state. The CLI also localizes the rate-limiting and human-jitter logic in one place.
- **Cookie longevity**: LinkedIn sessions can last weeks. When `lipy doctor` reports `session_age` > 30 days, it's healthy.
- **The user can revoke at any time** by deleting `~/.hermes/state/playwright/linkedin/state.json` and changing their LinkedIn password.
