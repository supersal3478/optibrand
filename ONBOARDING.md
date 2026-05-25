# Onboarding — getting brand-growth-engine running on a new laptop

The short version:

```bash
git clone <your-fork-of-this-repo> brand-growth-engine
cd brand-growth-engine
./setup.sh             # mechanical: clones Hermes, installs venvs, symlinks skills
./scripts/bootstrap.sh # interactive: walks every remaining manual gate
```

That's it. `bootstrap.sh` is idempotent — re-running picks up at the first un-passed gate.

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

## Prerequisites before `setup.sh`

Three things have to be on the system already:

| Thing | How |
|---|---|
| macOS 12+ (Monterey or later) | — |
| Python 3.11+ | `brew install python@3.14` |
| Google Chrome | https://google.com/chrome |
| git | `xcode-select --install` |

Optional but useful: `brew install ripgrep jq`.

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

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `setup.sh` fails on `pip install -e hermes-agent` | Python 3.11+ not found | `brew install python@3.14`, then re-run |
| `cua-driver` returns empty arrays for every read | Accessibility permission not granted | System Settings → Privacy & Security → Accessibility → add `cua-driver` + toggle on |
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
