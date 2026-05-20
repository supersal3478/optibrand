# Status & Autonomy Readiness — 2026-05-20

Single-document snapshot of where the brand-growth-engine project is after the 2026-05-20 voice-bootstrap session. Two parts:

1. **What we did today** (the session log).
2. **Are we ready to run autonomously every 30 min?** — honest gap list + concrete plan to close it, including the specific cron schedule for the 15-minute-offset cadence Sal asked for.

---

## Part 1 — Session log: 2026-05-20

Goal entering the session: stand up a brand profile, a voice profile, and a ghostwriter trained on Sal's own LinkedIn comments and X replies.

### What was already in place (before today)

- Hermes Agent vendored at `vendor/hermes-agent/` and installed in its own venv (Python 3.14.4).
- All six project skills written and symlinked into `~/.hermes/skills/`: `voice-profile`, `brand-guard`, `reply-drafter`, `linkedin-engage`, `x-engage`, `youtube-engage`. Confirmed visible via `hermes skills list`.
- `lipy` CLI installed at `~/.local/bin/lipy`, Playwright LinkedIn session warmed (~9 days old at session start).
- Dedicated CDP Chrome for X at `~/.hermes/state/chrome-cdp/`, running on port 9222, logged in as `@salaicreates`.
- 45 LinkedIn outbound comments already in `corpus/linkedin_comments.jsonl` (from a prior `lipy my-comments` run).
- `corpus/_normalized.jsonl` had 45 normalized records.
- `BRAND.md` was the unfilled template (all placeholders empty).
- No `voice_profile.json` anywhere. No X corpus.

### What we did this session

1. **Verified CDP Chrome login** — opened a new tab at `x.com/salaicreates/with_replies`, confirmed title `"Posts with replies by Sal AI 🏆 🇨🇦 (@salaicreates) / X"`. CDP Chrome is logged in.

2. **Scraped LinkedIn comments** via `lipy my-comments --limit 100 --save --headed`. Result: **44 visible, 43 dupes, 1 net new** — total now 46 in `corpus/linkedin_comments.jsonl`. LinkedIn's own `/in/me/recent-activity/comments/` page does not surface beyond ~46 items, so the scraper's 33-scroll budget hit a ceiling that's external to us. Going deeper requires the LinkedIn data export (24h wait) or per-post scraping (rejected as too risky for this session).

3. **Scraped X via `scripts/scrape-x-corpus.py --handle salaicreates --limit 50`.** First run: 3 replies + 52 posts. **Mistake made:** re-ran with `--limit 400` to look for more replies, which clobbered the better first run (script was using `write_text`, not append). Second run: 2 replies + 32 posts. Net loss of ~20 X records.

4. **Patched `scripts/scrape-x-corpus.py`** to load existing records on start, dedupe by URL, and append. New `--limit N` semantics: N *new* records per run, not total. So future re-runs accumulate instead of clobber.

5. **Re-ran `scripts/ingest-corpus.py`** — normalized everything into 77 records: 46 `linkedin_comment`, 30 `x_post`, 1 `x_reply` (the empty-text one was dropped by the normalizer).

6. **Decided strategy with Sal:** accept current corpus (versus waiting 24h for LinkedIn data export). Use `x_posts.jsonl` as voice signal for X since the account is genuinely post-heavy.

7. **Drafted and saved `BRAND.md`** (108 lines, full file). Sal confirmed three load-bearing choices:
   - Stated voice rules win over observed habits (first name only, zero em dashes, no credential parens).
   - Positioning: *"I help operators ship AI automation that actually runs."*
   - Primary audience: operators and agency owners building AI automation for clients.

   The brand is consistent across LinkedIn / X / YouTube. The **voice** differs: LinkedIn = YouTube (warm, longer, emoji-OK), X is distinct (tighter, drier, fewer emojis, no hashtags in replies).

8. **Generated `~/.hermes/memories/voice_profile.json`** (204 lines, 8.3 KB). Highlights:
   - `confidence: medium` — above the 20-record floor, below the 200-record target.
   - `corpus_breakdown`: 46 LI / 30 X-post / 1 X-reply.
   - `platform_specific` has four sections: `linkedin`, `x_replies`, `x_originals` (marked `agent_should_imitate: false` — that's Sal's own posting template), `youtube` (marked `agent_should_imitate: "use_linkedin_profile"`).
   - `brand_alignment.brand_md_hash` = `sha256:e3bc52…39c39e67` so `brand-guard` can detect when `BRAND.md` is edited.
   - Honest caveats: only 1 actual X reply observed; X reply voice is *inferred* from X-originals' brevity dialed down with LinkedIn's warmth dialed down.

9. **Saved memory** at `~/.claude/projects/-Users-…-brand-growth-engine/memory/project_voice_profile_status.md` and indexed it in `MEMORY.md`.

10. **Created symlink** at `/voice_profile.json` (project root) → `~/.hermes/memories/voice_profile.json`. Added to `.gitignore` so it doesn't get committed.

### Files created or modified this session

| Path | Change |
|---|---|
| `BRAND.md` | Filled from template (108 lines) |
| `corpus/linkedin_comments.jsonl` | 45 → 46 records |
| `corpus/x_replies.jsonl` | 0 → 1 useful record |
| `corpus/x_posts.jsonl` | 0 → 30 records |
| `corpus/_normalized.jsonl` | 45 → 77 records |
| `scripts/scrape-x-corpus.py` | Patched to append+dedupe (was clobbering) |
| `~/.hermes/memories/voice_profile.json` | Created (204 lines) |
| `voice_profile.json` (symlink at root) | Created |
| `.gitignore` | Added `voice_profile.json` entry |
| `~/.claude/…/memory/project_voice_profile_status.md` | Created (cross-session memory) |
| `~/.claude/…/memory/MEMORY.md` | Indexed new memory |

---

## Part 2 — Are we ready to run autonomously every 30 min?

**Short answer: no, but the gap is small and concrete.** The skills, the brand profile, the voice profile, the LinkedIn session, the CDP Chrome, and the Hermes platform with cron support are all in place. What's missing is glue and gating decisions that have to be made before any cron tick is allowed to post anything.

### What's already true

- `hermes cron` subcommands exist (`add`, `list`, `pause`, `tick`, etc.) — verified.
- `hermes gateway start` is the daemon — exists.
- Reply-drafter and brand-guard skills are installed and visible to Hermes.
- LinkedIn `lipy` has `inbound` (read), `reply` (post on a comment), and `comment` (top-level post) commands.
- X `x-engage` has the proven CDP recipe (writes were live-tested 2026-05-14 on `@VadimStrizheus`).
- All four config files exist and are reasonable: `caps.yaml`, `windows.yaml`, `jitter.yaml`, `blocklist.yaml`.
- BRAND.md and voice_profile.json exist and are wired by hash.

### What blocks autonomous run today

These are the hard gates. Each one must be resolved before the first autonomous tick is safe to fire.

#### 1. `caps.yaml` has everything live: false (the master kill switch)

Current state:

```yaml
phase: 0
youtube:  { live: false, ... }
x:        { live: false, ..., inbound: { requires_approval_flag: true } }
linkedin: { live: false, ... }
```

Phase 0 means "scaffold only — nothing posts." For the inbound-polling cadence Sal wants, this needs to become at least:

```yaml
phase: 2
x:        { live: true, ... }
linkedin: { live: true, ... }
```

The phased rollout in [README.md](../README.md) calls Phase 2 *"Inbound replies on own X + LinkedIn posts"* — exactly the cadence Sal asked for. Skipping Phase 1 (YouTube) is fine since Sal explicitly deprioritized YouTube.

**This is a deliberate decision, not a config typo.** The kill switch exists for a reason. Sal's autonomy preference (memory) means we can flip it, but the choice should be conscious.

#### 2. X auto-reply approval gate

`caps.yaml` line: `x: inbound: requires_approval_flag: true`. This gates on the `X_AUTO_REPLY_APPROVED` env var (per X's Feb 2026 policy for the official API). The CDP path technically bypasses the API requirement, but the gate is still in code as a safety check.

Two options:
- Set `X_AUTO_REPLY_APPROVED=true` in `~/.hermes/.env` and accept that the official-API rules technically apply if X ever audits.
- Change the gate to `requires_approval_flag: false` since we're using CDP, not the API.

The second is more honest about what we're actually doing. The first is more conservative if there's any chance of switching to API later.

#### 3. `windows.yaml` timezone is wrong

```yaml
timezone: America/Los_Angeles
```

`schedule.example.yaml` says `America/Toronto` — that's Sal's actual timezone. With LA set, the configured `08:00–19:00` LinkedIn inbound window is actually `11:00–22:00` Toronto. Probably not what Sal wants. **Fix:** change `windows.yaml` to `America/Toronto`.

#### 4. No actual `schedule.yaml` exists

`schedule.example.yaml` is the template (gitignored as `schedule.yaml` per `.gitignore`). For pure inbound polling without scheduled outbound posts, `schedule.yaml` is technically not required — but creating an empty/minimal one makes the daemon happier and gives a place to add posts later.

#### 5. Hold-buffer config for the first runs

Current:
```yaml
hold_buffer_outbound_seconds: 300   # 5-min hold on outbound
hold_buffer_inbound_seconds: 0      # inbound posts immediately
```

For the first ~7 days of autonomous operation, inbound replies should NOT post immediately. They should queue in `~/.hermes/queue/` (or the dashboard) for Sal to review and approve. This catches voice-drift and brand-guard misfires before they hit your account. Recommend `hold_buffer_inbound_seconds: 1800` (30 min) for the first week.

#### 6. The Hermes gateway daemon isn't running

`lipy status` shows `daemon_running: false`. For autonomous cron ticks to fire, `hermes gateway start` needs to be invoked once. On a Mac, the launchd plist or a simple "start at login" entry persists it across reboots. The MacBook-lid-close issue (memory) still applies — closing the lid kills the process. `pmset` config or dedicating an always-plugged Mac is the answer.

#### 7. First-tick verification has never been done end-to-end

We have not yet observed a single cron-fired prompt that:
1. Reads inbound comments on Sal's own LinkedIn posts,
2. Drafts a reply via `reply-drafter`,
3. Passes through `brand-guard`,
4. Queues (or posts) via `lipy reply`,
5. Logs to `~/.hermes/memories/sent_replies.jsonl`.

This loop is *designed* in the SKILL.md files but has never run start-to-finish. The first time we run it should be a manual `hermes cron run <job>` to inspect output, before letting cron tick on its own.

### Once those are resolved — the cron cadence Sal asked for

Sal asked for: LinkedIn every 30 min, X every 30 min, **offset 15 min so something runs every 15 min but the same platform isn't hit twice in a row.**

That's two cron jobs:

```bash
# LinkedIn inbound: minute 0 and 30 of every hour
hermes cron add \
  --name li-inbound \
  --schedule "0,30 * * * *" \
  --prompt "Run the linkedin-engage inbound pass. Check for new comments on my recent LinkedIn posts since the last run. For each, draft a reply via reply-drafter, validate via brand-guard, and queue (do not post). Respect windows.yaml — skip if outside the linkedin inbound window. Apply jitter.yaml between actions. Update sent_replies.jsonl."

# X inbound: minute 15 and 45 of every hour
hermes cron add \
  --name x-inbound \
  --schedule "15,45 * * * *" \
  --prompt "Run the x-engage inbound pass. Check for new replies/mentions on my recent X posts since the last run via the CDP Chrome at ~/.hermes/state/chrome-cdp/. For each, draft a reply via reply-drafter, validate via brand-guard, and queue (do not post). Respect windows.yaml — skip if outside the x inbound window. Apply jitter.yaml between actions. Update sent_replies.jsonl."
```

Resulting timeline (during business hours per `windows.yaml`):

| Minute | Action |
|---|---|
| :00 | LI inbound poll |
| :15 | X inbound poll |
| :30 | LI inbound poll |
| :45 | X inbound poll |

Each tick respects `windows.yaml` (won't fire outside business hours) and `jitter.yaml` (adds randomized 2–8 min delay before any actual action). With the hold-buffer recommendation above, nothing actually posts in week 1 — everything queues for review.

### Concrete next-step checklist (in order)

To go from current state → autonomous polling with the cadence Sal asked for:

1. **Fix `windows.yaml` timezone** to `America/Toronto`. *(2-min edit, no blast radius.)*
2. **Decide hold-buffer policy.** Recommend `hold_buffer_inbound_seconds: 1800` for week 1, then drop to 0 once Sal has approved ≥ 80% of queued drafts.
3. **Decide X approval gate.** Set `requires_approval_flag: false` (CDP path) or set `X_AUTO_REPLY_APPROVED=true` env var.
4. **Flip `caps.yaml` to phase 2** and `linkedin.live: true`, `x.live: true`.
5. **Create minimal `schedule.yaml`** (copy from example, leave `posts: []` for now).
6. **Start Hermes gateway** (`hermes gateway start`) and add a launchd plist or `pmset` config to keep it alive.
7. **Manually run each cron job once** via `hermes cron run li-inbound` and `hermes cron run x-inbound` — inspect the queued drafts in the dashboard. Verify brand-guard catches obvious bad drafts.
8. **Add both cron jobs** with the schedules above. Watch the first 24 hours of ticks closely.
9. **Daily-report skill** (cron at 23:55) writes a summary to `~/.hermes/reports/YYYY-MM-DD.md` — review every morning for the first week.
10. **Drop hold buffer to 0** once Sal is comfortable. Optionally flip to phase 3+ for outbound engagement (more invasive, separate risk decision).

### Risks Sal has already accepted (per memory)

- LinkedIn 23% account-restriction rate within 90 days for automated commenters (this risk applies to *outbound*; inbound replies on Sal's own posts are much lower risk).
- Voice drift over time as Sal unconsciously matches the agent.
- MacBook lid-close killing the daemon.

### Risks that have NOT been resolved

- **One-X-reply problem:** voice_profile's X reply tone is inferred, not observed. First batch of X replies should be hand-reviewed even after general autonomy is granted.
- **LLM-as-orchestrator drift:** cron prompts trigger an LLM session that follows SKILL.md instructions in natural language. There is no deterministic Python controller. If an LLM tick skips a step (e.g., forgets to call brand-guard), nothing in the system catches it. Recommend adding a simple post-tick audit script that verifies every entry in `sent_replies.jsonl` has a `brand_guard` block; if not, alert.

---

**Bottom line:** the artifacts are ready (BRAND.md, voice_profile.json, skills, sessions, CDP Chrome). The autonomy gap is ~10 concrete steps, mostly config flips and one manual end-to-end dry-run. Realistically half a day of work, gated on Sal's comfort with each switch-flip.
