# New-Machine Runbook — set up & run X + LinkedIn engagement (the "new algorithm")

**Who this is for:** a fresh engineer **or a new Claude/AI agent** on a *different*
laptop who has only this git repo and needs to get the autonomous X + LinkedIn
engagement system running, on the **human-rhythm cadence** ("the new realistic
algorithm"), without re-deriving context.

**Read order:** this file is the *executable* guide (do these steps). For the
*why* / architecture / every script's status, read
[ENGAGEMENT-SYSTEMS-MASTER.md](ENGAGEMENT-SYSTEMS-MASTER.md) (the master doc) and
[human-cadence.md](human-cadence.md). When something here says "the cadence" or
"goodwill" or "lipy", the **Glossary** at the bottom defines it exactly.

**Branch:** `macmini-live`. **Platform:** macOS only (the X path needs mac Chrome
+ CDP). **Account UI note:** the LinkedIn account's UI is in **Malay** — all
LinkedIn selectors are attribute-based for that reason; don't "fix" them to
English text.

---

## 0. Mental model (10 lines — read before touching anything)

```
DISCOVER ──▶ DRAFT ──▶ GATE ──▶ QUEUE ──▶ FLUSH ──▶ (LIKE)
(read feed/  (LLM in    (brand-  (outbox  (humanized   (+react)
 mentions)   your voice) guard +  hold-    real browser
                         judge)   buffer)  posts it)
        ▲                                   ▲
        └── the CADENCE decides WHEN a human would do this (per platform) ──┘
```

- The system does **engagement**, not original posting. Two modes per platform:
  **inbound** (reply to comments on *your* posts — low risk) and **outbound
  goodwill** (comment on *strangers'* posts — higher risk).
- **The "new algorithm" = the human-rhythm cadence.** Instead of polling every N
  minutes, it acts in **1–2 bursty sessions/day at jittered times, with human
  gaps, skip-days, and inbound replies that cluster after your posts then decay.**
  It now drives **BOTH** X and LinkedIn.
- Transport: **X** = Chrome DevTools Protocol (CDP) on `:9222`. **LinkedIn** = the
  `lipy` Playwright CLI. Everything between draft and queue is shared.
- **Live posting is gated only by `config/caps.yaml` → `<platform>.live`.** When
  that's `true` and the crons are armed, **the system posts on its own** (see §4 —
  this is the one thing people get wrong).

---

## 1. First-time setup on the new laptop

Everything in `~/` and both Python venvs are **deliberately NOT in git** (they bake
in absolute paths / are huge / are secrets / are logged-in sessions). So a `git
pull` alone does nothing. Run the installer.

### 1.1 Prerequisites
- macOS, Google **Chrome** installed, **python3 ≥ 3.11**, **git**.
- Clone the repo to a **stable, permanent path** and **do not move it afterward**
  (moving the folder re-breaks both venvs and the `lipy` wrapper — see §6).

### 1.2 Run the installer (does ~90% automatically)
```bash
cd <repo>
./setup.sh
```
`setup.sh` is idempotent and: restores `vendor/hermes-agent` (clones Hermes pinned
to the tested commit + builds its venv), builds the **lipy** venv
(`skills/linkedin-engage/.venv`, has Playwright), installs the `~/.local/bin/lipy`
wrapper **rewritten for this machine's path**, creates `~/.hermes/{skills,state,
logs,reports}`, symlinks skills into `~/.hermes/skills/`, and seeds `~/.hermes/.env`
from `.env.example`.

> There are **TWO venvs** and they are not interchangeable:
> - `vendor/hermes-agent/.venv` — runs the `scripts/*.py` orchestrators. **No Playwright.**
> - `skills/linkedin-engage/.venv` — runs `lipy` / any LinkedIn DOM work. **Has Playwright.**
> Always run lipy / DOM probes with the **lipy** venv, not the Hermes one.

### 1.3 Secrets (`~/.hermes/.env`, chmod 600)
The Azure LLM key travels in-repo (committed in `51_AZURE_LLM_DEPLOYMENTS_AND_AGENT_RULES.md`),
so drafting works out of the box. Open `~/.hermes/.env` and confirm/fill:
`AZURE_FOUNDRY_API_KEY`, `AZURE_FOUNDRY_BASE_URL`, `AZURE_FOUNDRY_MODEL`,
`LI_USERNAME`, `BGE_TIMEZONE` (e.g. `America/Toronto`, must match
`config/windows.yaml: timezone`).

### 1.4 The two logins (the only genuinely manual, un-automatable steps)
The logged-in browser sessions are per-device and never copied (a warm, locally
established session **is** the stealth premise). On the new laptop:
```bash
lipy login --headed          # LinkedIn: log in IN THE VISIBLE WINDOW, clear 2FA
lipy status                  # expect: {"ok": true, "profile_present": true, ...}
lipy doctor                  # expect: auth OK, landing_url .../feed/
```
For **X**: bring up the CDP Chrome and log into x.com once (the bootstrap gate and
`start.sh` handle Chrome bring-up — `./scripts/start.sh` then complete the X login
in that Chrome window).

### 1.5 Guided gate-check (recommended)
```bash
./scripts/bootstrap.sh        # idempotent; stops at the first un-passed gate
                              # (Hermes, venvs, X login, LinkedIn login, BRAND.md, autonomy)
./scripts/bootstrap.sh --dry-run   # show what each gate checks without doing anything
```

At this point the machine is **installed** but **not yet acting**. Nothing posts
until you arm it (§3) — and even then only if `live: true` (§4).

---

## 2. The cadence ("new algorithm") — already configured for BOTH platforms

You do not need to build anything; the cadence is implemented and configured for X
**and** LinkedIn. You only tune numbers and inspect.

| Knob file | What it controls |
|---|---|
| `config/cadence.yaml` | **The rhythm.** Per platform: `sessions_per_day`, `session_minutes`, `daily_actions` (comments/day), `gap_minutes`, `skip_day_prob`; inbound decay tiers (`fresh`/`taper`/`quiet`). |
| `config/windows.yaml` | **What hours/days sessions may land in.** X = 24/7. LinkedIn = Mon–Fri, inbound 08:00–19:00, outbound 09:00–17:00. Times are in `timezone:` (local). |
| `config/caps.yaml` | **Hard safety bounds + the live kill-switch.** Daily/weekly caps, hold-buffer seconds, and `<platform>.live`. |
| `config/jitter.yaml`, `config/blocklist.yaml` | Inter-action delays; accounts/keywords never to engage. |

Current cadence (defaults, tune in `cadence.yaml`):
- **X outbound:** 1–2 sessions/day, 3–8 comments/day, 5–15 min gaps, 15% skip-day.
- **X inbound:** dense 12–25 min for ~2 h of fresh activity → taper 45–90 min → quiet 150–240 min; up to 12 replies/day.
- **LinkedIn outbound:** 1 session/day, 1–4 comments, 8–20 min gaps, 25% skip-day (lighter — higher restriction risk).
- **LinkedIn inbound:** dense 15–30 min → taper 60–120 min → quiet 180–300 min; up to 8 replies/day.

**Inspect before arming (no side effects):**
```bash
HP=./vendor/hermes-agent/.venv/bin/python
$HP scripts/cadence-tick.py --simulate 2026-06-23 --platform x         # a simulated X day
$HP scripts/cadence-tick.py --simulate 2026-06-23 --platform linkedin  # a simulated LI day
$HP scripts/cadence-tick.py --dry-run --verbose --platform x           # decide for NOW, fire nothing
$HP scripts/cadence-tick.py --dry-run --verbose --platform linkedin
```
A dry-run prints lines like `[outbound] skip — outside outbound window` or
`[inbound] CHECK — quiet tier`. That's the cadence deciding; it fires nothing.

---

## 3. Arm both X and LinkedIn

```bash
./scripts/autonomy-mode.sh     # REQUIRED after any git pull, and to arm a fresh machine
```
This one command:
1. Flips `config/caps.yaml` → `phase: 2`, `x.live: true`, `linkedin.live: true`
   (original backed up to `caps.yaml.bak`).
2. Registers the cron jobs (every minute / every 2 min):
   - **`cadence-tick`** → drives **X** outbound + inbound on the cadence.
   - **`cadence-tick-li`** → drives **LinkedIn** outbound goodwill + inbound on the
     cadence. **Armed only if `lipy status` shows a warmed, logged-in session** —
     so do §1.4 first, or LinkedIn is skipped (non-fatal; X still arms).
   - **`outbox-flush`** → drains matured drafts and actually posts them.
   - **`daily-report`** → activity ledger.

Then start the runtime:
```bash
./scripts/start.sh             # self-heals venv, brings up CDP Chrome + X login, starts gateway
```

**Routine restart on an already-set-up machine** is just:
`git pull && ./scripts/autonomy-mode.sh && ./scripts/start.sh`.

---

## 4. ⚠️ READ THIS: "automatic" means it posts without asking

This is the single most important operational fact, and it resolves the tension
between "run it automatically" and the earlier "ask me before each live post":

- **There is NO per-post human approval step in autonomous mode.** Once armed
  (§3) with `live: true`, the cadence drafts → the draft sits in the **hold
  buffer** for `hold_buffer_outbound_seconds` (default **300 s = 5 min**) → then
  `outbox-flush` **posts it to the real account automatically**, re-checking only
  `live` / window / caps at that moment.
- The hold buffer is a **5-minute yank window**, not an approval gate: a human
  *can* cancel in those 5 minutes, but nobody is *asked*.
- The "stop and get explicit per-post approval" behavior used during development
  was a **manual discipline of the operator**, not something the code enforces.

**So choose your posture deliberately:**

| Posture | How | Result |
|---|---|---|
| **Fully autonomous** (what you asked for) | `live: true` for both (autonomy-mode sets this), crons armed | The system browses, drafts, **and posts** X + LinkedIn on the human cadence, on its own. |
| **Staged / review-first** (recommended for the first few days on a new machine) | Run `autonomy-mode.sh`, then set `config/caps.yaml` → `x.live: false` and/or `linkedin.live: false` | The cadence still **drafts into the outbox** so you can read exactly what it *would* post (`scripts/activity-report.py`), but `outbox-flush` cancels instead of posting. Flip `live: true` when you trust it. |

Notes:
- `phase:` in caps.yaml is **advisory only** (informational; not a hard gate). The
  real on/off switch is `<platform>.live`.
- LinkedIn outbound is documented (SKILL.md) to require a signed
  **`RISK_ACCEPTED.md`** at repo root (LinkedIn restricts ~23% of automated
  commenters within 90 days). It is **not** code-enforced, but create + sign it
  before enabling LinkedIn goodwill so the intent is on record:
  ```
  RISK_ACCEPTED.md  →  "Signed: <name>" / "Date: YYYY-MM-DD"
  ```
- **Outbound goodwill drafts are quality-gated.** `li-feed-engagement` (LinkedIn)
  and `feed-engagement` (X) run an LLM **judge** before drafting that skips
  promo / low-value / off-brand posts, and a goodwill drafter that adds a real
  point (never a dismissive or sycophantic reply). Tune via the judge's
  confidence bar (`JUDGE_MIN_CONFIDENCE` in `li-feed-engagement.py`).

---

## 5. Verify it's actually running, and watch what it does
```bash
HP=./vendor/hermes-agent/.venv/bin/python
$HP scripts/activity-report.py            # ledger + daily summary + liveness/heartbeat
$HP scripts/activity-report.py --days 3   # last 3 days
```
Other signals:
- `~/.hermes/state/heartbeat.json` — is Chrome up? logged in? last run? (silent-stop detector)
- `~/.hermes/state/outbox.jsonl` — the queue + full draft text + status (queued/submitted/canceled)
- `~/.hermes/logs/engagement_metrics.jsonl` — event timeline (drafted, queued, submitted, liked, skipped_judge, …)
- `<lipy venv>/bin/python` `lipy feed --limit 3` — sanity-check LinkedIn feed discovery returns posts (headed).

---

## 6. Halt & safety

- **Halt a platform instantly:** edit `config/caps.yaml` → `x.live: false` and/or
  `linkedin.live: false`. Caps are re-read every job tick (≤ 1 min); the next
  `outbox-flush` cancels instead of posting. No restart needed.
- **Cancel one queued draft:** `python scripts/_outbox.py cancel <id>` (ids show in
  `activity-report.py` / `outbox.jsonl`).
- **Stop all crons:** re-run `autonomy-mode.sh` logic in reverse, or remove the
  Hermes crons (`<hermes> cron remove --name cadence-tick` etc.).

---

## 7. Critical gotchas (don't get caught by these)

1. **Folder moves break the venvs + the lipy wrapper** (absolute paths baked in).
   Clone once to a permanent path; don't move it. If you must, re-run `setup.sh`.
2. **Two venvs** — Hermes (no Playwright) for `scripts/*.py`; lipy (Playwright) for
   LinkedIn. Don't cross them.
3. **The LinkedIn home feed is gated against *headless* scraping.** `lipy feed`
   runs **headed** by default; keep it that way (a visible Chromium window will
   open during LinkedIn sessions — that's expected). The recent-activity page
   (used by inbound) renders headless; the home feed (goodwill) does not.
4. **LinkedIn UI is Malay** — selectors are attribute-based on purpose; never
   switch them to visible English text.
5. **`live` is outward and autonomous** — see §4. Arming + `live: true` = it posts
   by itself. Start staged if unsure.
6. **The cadence gates DRAFTING, not the final post.** Visible posting cadence ≈
   draft cadence + the hold buffer. LinkedIn goodwill now fires on the cadence
   (this was wired in the 2026-06-21 build).

---

## 8. Glossary (so the terms in the docs mean exactly one thing)

- **cadence / "the new algorithm"** — the human-rhythm engine (`scripts/_cadence.py`
  + `scripts/cadence-tick.py`, knobs in `config/cadence.yaml`). Decides *when* a
  human would act: bursty sessions, gaps, skip-days, inbound decay. Replaces the
  old flat every-N-minutes crons.
- **inbound** — replying to comments people leave on *your* posts (low risk).
- **outbound goodwill** — commenting on *strangers'* posts found in the feed
  (higher risk; quality-judged before drafting; X also likes).
- **lipy** — the LinkedIn driver CLI (`skills/linkedin-engage/lipy.py`, Playwright).
  Commands used here: `login`, `status`, `doctor`, `feed`, `inbound`, `comment`,
  `reply`, `like`, `session`.
- **CDP** — Chrome DevTools Protocol; how the X path drives a real logged-in Chrome
  on `:9222` with trusted input events.
- **outbox / hold-buffer** — `~/.hermes/state/outbox.jsonl`. Drafts queue here and
  "mature" for `hold_buffer_*_seconds` before `outbox-flush` posts them.
- **brand-guard** — hard veto on a draft (em-dash/hashtag/sycophancy/off-limits);
  rules in `BRAND.md`. Inline in the drafters.
- **judge** — the pre-draft LLM gate for outbound goodwill (skip promo/low-value/
  off-brand). `judge_goodwill_post` (LinkedIn) / `judge_audience_alignment` (X).
- **autonomy-mode** — `scripts/autonomy-mode.sh`: flips caps live + registers the
  cadence/flush/report crons. Run after every `git pull`.
- **start.sh** — routine bring-up: heals venv, starts CDP Chrome + X login, gateway.

---

*Last updated 2026-06-22. If a command here disagrees with the code, the code
wins — re-derive from the master doc and update this file.*
