# Onboarding — getting brand-growth-engine running on a new laptop

The short version:

```bash
git clone <your-fork-of-this-repo> brand-growth-engine
cd brand-growth-engine
./scripts/preflight.sh # Stage 0: installs Homebrew, Python, Chrome, ripgrep, jq
./setup.sh             # mechanical: clones the pinned Hermes, builds venvs, installs cua-driver + lipy, symlinks skills
./scripts/bootstrap.sh # interactive: walks every remaining manual gate
```

That's it. All three are idempotent — re-running picks up where it left off (`bootstrap.sh` at the first un-passed gate).

## Stage 0 — system-level prereqs (one command)

```bash
./scripts/preflight.sh
```

That installs everything `setup.sh` depends on: Xcode Command Line Tools (if needed it launches the GUI installer and tells you to re-run), Homebrew, Python 3.14, Google Chrome, plus ripgrep and jq. It's safe and idempotent — each step checks before installing.

<details>
<summary>If you'd rather run Stage 0 by hand</summary>

```bash
xcode-select --install                                                               # git + compilers
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"  # Homebrew
brew install python@3.14                                                             # Python 3.11+
brew install --cask google-chrome                                                    # Chrome (X workflow)
brew install ripgrep jq                                                              # optional but useful
```
</details>

> **cua-driver is now automatic.** `setup.sh` runs `scripts/install-cua-driver.sh`, which downloads the pinned, **universal** (Intel + Apple Silicon) binary — no manual download, no architecture guessing. It's a secondary helper (the X read/write path is all CDP), so if the download ever fails the install keeps going.

You also need to bring **two pieces of information** that can't come from git:

- **Your LLM API key** — Azure (`AZURE_FOUNDRY_API_KEY` + `AZURE_FOUNDRY_BASE_URL`), Anthropic, or OpenRouter. bootstrap.sh's stage 3 opens `~/.hermes/.env` in `$EDITOR` and **live-validates the key** with a curl before continuing.
- **Your LinkedIn + X 2FA factor** — your authenticator app or SMS device. bootstrap.sh's stages 5 & 6 open browser windows where you log in manually.

## What each stage does

`bootstrap.sh` walks 12 gates with validation between each:

| # | Stage | What it does | Why manual |
|---|---|---|---|
| 1 | Verify `setup.sh` ran | Hermes venv, `~/.hermes/skills`, `~/.hermes/.env` exist | n/a |
| 2 | cua-driver + Accessibility | Opens the GitHub releases page; pauses until binary is installed; opens System Settings → Accessibility for the grant; validates the grant with a real AX read | macOS UI; no API for grant |
| 3 | LLM credentials | Opens `~/.hermes/.env` in `$EDITOR`; refuses to proceed without a usable key | Keys live in your password manager |
| 4 | BRAND.md filled | Greps for empty `**Name:**` placeholder; refuses if found | Personal content |
| 5 | X login | Launches the CDP Chrome; validates the X SideNav element after you log in | 2FA in your hands |
| 6 | LinkedIn login | Runs `lipy login --headed`; validates with `lipy status` | 2FA in your hands |
| 7 | Voice corpus + profile | Counts corpus records; offers to re-run `ingest-corpus.py` + `voice-train.py` | LLM call |
| 8 | schedule.yaml | Copies `schedule.example.yaml` if missing; opens for edit; validates schema | Personal content |
| 9 | Autonomy arm | Runs `scripts/autonomy-mode.sh` to flip caps to phase 2 + register the three cron jobs | Deliberate switch |
| 10 | launchd persistence | Interpolates `__HOME__` in the plist, copies to `~/Library/LaunchAgents/`, `launchctl load`s it | One-time install |
| 11 | Hermes gateway running | Detects the daemon process | Diagnostic |
| 12 | Final summary | Prints where to watch logs + how to halt | n/a |

Bypass flags (use sparingly):

```bash
./scripts/bootstrap.sh --dry-run     # show what each gate would check; no prompts
./scripts/bootstrap.sh --skip-x      # skip cua-driver + X login (e.g., a LinkedIn-only laptop)
./scripts/bootstrap.sh --skip-li     # skip LinkedIn login (e.g., an X-only laptop)
```

## Start the agent

Bootstrap registers the cron jobs (stage 9) and, if you accepted stage 10, installs the launchd plist that starts the gateway daemon and relaunches it on every reboot. The cron jobs only fire while that daemon is running. To start it by hand (or if you skipped launchd):

```bash
./vendor/hermes-agent/.venv/bin/hermes gateway start
```

Confirm it's alive: `pgrep -f "hermes.*gateway"` (or `launchctl list | grep brandgrowthengine` if you used launchd). To halt everything instantly without stopping the daemon, flip `config/caps.yaml` → `x.live` / `linkedin.live` to `false` (re-read every tick).

## How the loop runs after bootstrap

`autonomy-mode.sh` (stage 9) registers three Hermes cron jobs:

| Job | Schedule | What |
|---|---|---|
| `schedule-tick` | `* * * * *` | Runs `scripts/schedule-tick.py`. Reads `schedule.yaml` + `caps.yaml` + `windows.yaml`. Decides what to do this minute: publish a scheduled post, poll a post's comments (24h monitor window), or post a queued reply (after the hold buffer). |
| `daily-report` | `55 23 * * *` | Renders `~/.hermes/reports/YYYY-MM-DD.md` from the audit log. |
| `voice-retrain` | `0 2 * * 0` | Re-runs `scripts/voice-train.py --retrain` weekly. |

A scheduled post in `schedule.yaml` is the unit of work:

```yaml
posts:
  - id: 2026-05-25-li-01
    platform: linkedin
    time: "2026-05-25T09:00:00"
    draft: drafts/2026-05-25-li-01.md
```

At 09:00 the orchestrator publishes the draft. For the next 24h it polls comments on that specific post every 15 min, drafts replies via `reply-drafter` + `brand-guard`, queues them for 30 min (the hold buffer) in `~/.hermes/queue/`, then posts. To yank a draft before it goes live: `rm ~/.hermes/queue/<draft-id>.json`.

## Daily report

Every day at 23:55 local time, a report lands at:

```
~/.hermes/reports/YYYY-MM-DD.md
```

It summarizes:

- Posts published (by platform)
- Inbound comments received + how many you replied to
- Outbound engagements made (goodwill passes)
- Brand-guard rejections (and why)
- Cost burn (per provider)
- Rate-limit signals

## Migrating from your old laptop (optional)

The git clone carries your committed content: BRAND.md, the corpus, configs, skills. It does **not** carry your `~/.hermes/` runtime state, which lives outside the repo. `bootstrap.sh` regenerates the parts that matter (X + LinkedIn logins, voice profile), so a clean install is fully supported — you don't have to copy anything.

But if you want continuity rather than a fresh start, copy `~/.hermes/` from the old Mac before wiping it:

```bash
# On the OLD machine — archive the runtime state:
tar -czf hermes-state.tgz -C "$HOME" .hermes
# Move hermes-state.tgz to the new machine, then there:
tar -xzf hermes-state.tgz -C "$HOME"
```

What that preserves: your logged-in browser profiles (skip both manual logins), `~/.hermes/.env` (your keys), `memories/voice_profile.json`, `memories/sent_replies.jsonl` (so the weekly voice-retrain keeps its history), the audit log, and `state/scheduled-posts/` (so in-flight posts keep their monitor windows). Re-run `./setup.sh` and `./scripts/bootstrap.sh` afterward — they'll detect the existing state and skip the gates it satisfies.

Two things that can't be copied and must be redone on the new machine: the macOS **Accessibility grant** for cua-driver (a per-machine UI permission) and any **launchd plist** (paths are baked in at install time — bootstrap stage 10 rewrites it).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `setup.sh` fails on `pip install -e hermes-agent` | Python 3.11+ not found | `brew install python@3.14`, then re-run |
| `cua-driver check_permissions` shows `accessibility: false` | Accessibility permission not granted | System Settings → Privacy & Security → Accessibility → add `cua-driver` + toggle on |
| `setup.sh` printed "cua-driver install failed" | Network blip or release moved | Re-run `./scripts/install-cua-driver.sh`; core X path works over CDP regardless |
| `voice-train.py` reports `BRAND.md still has empty template placeholders` | BRAND.md gate not passed | `$EDITOR BRAND.md` and replace placeholders |
| `voice-train.py` succeeds but the JSON is missing keys | LLM didn't follow the schema | Re-run; if persistent, switch model in `~/.hermes/.env` |
| `autonomy-mode.sh` says voice profile missing | Wrote to wrong path (singular `memory/`) | Move to `~/.hermes/memories/voice_profile.json` (plural — Hermes convention) |
| `schedule-tick.py` runs but never publishes | `caps.yaml: <platform>.live: false` or outside `windows.yaml` | Tail `~/.hermes/logs/audit.jsonl` — the `publish_skip` reason names the gate |
| Cron jobs don't fire | Gateway not running | `hermes gateway start` or load the launchd plist |
| Cron ticks happen but nothing posts | Hold buffer not yet expired | Check `~/.hermes/queue/`; the entry's `hold_until_ts` says when |
| `start-chrome-cdp` says "Chrome didn't expose CDP" | Existing Chrome instance with same `--user-data-dir` | Quit all Chrome windows, retry |
| `lipy status` returns `auth_required` | LinkedIn session expired | `lipy login --headed` again |
| `lipy inbound` returns `playwright_not_installed` | Old symlink from pre-fix install | `cd skills/linkedin-engage && ./install.sh` (writes the wrapper now) |
| CDP `logged_in: false` after fresh login | x.com session cookie didn't persist | Log in again, then close Chrome via dock (not via the X tab) so cookies flush |
| `hermes doctor` warns about ripgrep | Hermes' PATH detection bug; harmless | Ignore unless skills actually fail |

## Where things live

| Path | What |
|---|---|
| `<project>/BRAND.md` | Your voice/values/off-limits — read on every draft |
| `<project>/schedule.yaml` | What you're posting and when — single source of truth |
| `<project>/corpus/` | Your past posts and comments — voice training data |
| `<project>/drafts/` | Markdown drafts referenced from schedule.yaml |
| `<project>/skills/` | Project skills; symlinked into `~/.hermes/skills/` |
| `<project>/config/caps.yaml` | Daily/weekly caps + per-platform kill switches |
| `~/.hermes/.env` | Credentials (chmod 600) |
| `~/.hermes/state/` | Browser profiles, session cookies, OAuth tokens |
| `~/.hermes/state/scheduled-posts/` | Per-post state (published URN, comment-seen sets) |
| `~/.hermes/queue/` | Drafts pending posting (held for `hold_buffer_inbound_seconds`) |
| `~/.hermes/logs/audit.jsonl` | Every action the agent takes — one line per action |
| `~/.hermes/logs/x-screenshots/` | Per-action screenshots from `x-engage/publish.py` + `reply.py` |
| `~/.hermes/reports/` | Daily reports |
| `~/.hermes/memories/voice_profile.json` | Distilled voice from corpus |

## What to do when you're not sure

1. Check `~/.hermes/logs/audit.jsonl` — the last 20 lines tell you what just happened.
2. Run `lipy doctor` and `hermes doctor` for diagnostics.
3. Flip `config/caps.yaml: linkedin.live: false` (or `x.live: false`) to halt that platform within one tick.
4. Open `BRAND.md` and re-read it — most "weird draft" problems trace back to underspecified brand rules.
5. To yank a queued draft before it posts: `rm ~/.hermes/queue/<draft-id>.json`.
