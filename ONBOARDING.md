# Onboarding — getting brand-growth-engine running on a new laptop

You just cloned this repo onto a fresh Mac. This file walks you through what to do, in order. Everything is designed so you can paste blocks one at a time.

## 0. Prerequisites

You need three things installed system-wide before `setup.sh` can run:

| Thing | Why | How |
|---|---|---|
| **macOS 12+** | Everything's tested here. Monterey 12.7 is the floor. | — |
| **Python 3.11+** | Hermes Agent needs ≥ 3.11; 3.14 recommended. | `brew install python@3.14` |
| **Google Chrome** | The X workflow runs against Chrome via DevTools Protocol. | https://google.com/chrome |
| **git** | To clone vendored upstream Hermes. | `xcode-select --install` |

Optional but useful: `brew install ripgrep jq` (Hermes uses ripgrep for code search; jq is handy for inspecting state).

## 1. Clone and bootstrap

```bash
git clone <your-fork-of-this-repo> brand-growth-engine
cd brand-growth-engine
./setup.sh
```

`setup.sh` is idempotent — safe to re-run any time. It:

- clones `vendor/hermes-agent/` and installs its venv,
- installs the `lipy` CLI (LinkedIn Playwright wrapper),
- symlinks `start-chrome-cdp` into `~/.local/bin/`,
- creates state dirs under `~/.hermes/`,
- symlinks every `skills/*/` into `~/.hermes/skills/`,
- seeds `~/.hermes/.env` from `.env.example`.

Make sure `~/.local/bin/` is on your `PATH`. Add to your shell rc if not:
```bash
export PATH="$HOME/.local/bin:$PATH"
```

## 2. Add credentials

```bash
$EDITOR ~/.hermes/.env
```

Minimum to run:
- **LLM**: `AZURE_FOUNDRY_API_KEY` + `AZURE_FOUNDRY_BASE_URL` (or `ANTHROPIC_API_KEY` if you switch to direct Anthropic).
- **LinkedIn**: `LI_USERNAME` (your email).
- **Time zone**: `BGE_TIMEZONE` defaults to `America/Toronto`.

Optional for later phases: YouTube OAuth, X auto-reply approval flag, LinkedIn residential proxy.

## 3. Log into the platforms

This step is unavoidable manual work — the agent never handles your passwords. You do this once per laptop.

### 3a. X (Twitter)

```bash
start-chrome-cdp
```

A Chrome window opens with a yellow "controlled by automated test software" banner. **In that window:**

1. Go to https://x.com
2. Click Sign In, log in with your account.
3. Complete 2FA if asked.
4. Stay on x.com/home for a few seconds (cookies persist).

The login is saved in `~/.hermes/state/chrome-cdp/`. You won't need to repeat this for weeks unless the session expires.

Verify:
```bash
HERMES_PY=./vendor/hermes-agent/.venv/bin/python
$HERMES_PY skills/x-engage/cdp_eval.py \
  --expr 'JSON.stringify({logged_in: !!document.querySelector("[data-testid=SideNav_AccountSwitcher_Button]")})'
```
Expect `{"logged_in":true}`.

### 3b. LinkedIn

```bash
lipy login --headed
```

A separate chromium window opens. Log in normally (including 2FA). Don't browse anything — close the window once you're at your feed.

The session cookie is saved in `~/.hermes/state/playwright/linkedin/`. Verify:
```bash
lipy status
```
Expect `{"ok": true, "profile_present": true, ...}`.

### 3c. YouTube (only when you're ready for Phase 1)

Skip unless you're enabling YouTube moderation:
```bash
python skills/youtube-engage/youtube_auth.py
```
Follow the OAuth flow. Token saved to `~/.hermes/state/youtube_token.json`.

## 4. Fill in BRAND.md

```bash
$EDITOR BRAND.md
```

This file is the single most load-bearing config in the project. Every drafted reply is validated against it by `brand-guard` (hard veto). Replace every placeholder. Take 20 minutes — it's the difference between drafts that sound like you and drafts that get rejected.

## 5. Drop voice corpus

The agent learns your voice from your past writing. The more you provide, the closer drafts will sound to you. See [corpus/README.md](corpus/README.md) for formats.

**Fastest path** (LinkedIn data export):

1. LinkedIn → Settings & Privacy → Data privacy → Get a copy of your data → "The works"
2. Wait 24 hours for the email.
3. Unzip; copy `Comments.csv` and `Shares.csv` into `corpus/`.
4. `python scripts/ingest-corpus.py` (converts CSVs to the expected JSONL).

**Fastest path** (X — your replies + own posts):

```bash
python scripts/scrape-x-corpus.py --handle <your-handle> --limit 200
```

(Both scripts are scaffolded; they may need a manual pass on first run. See script-level comments.)

Even 30 records of each is enough to bootstrap.

## 6. Build your voice profile

```bash
python scripts/voice-train.py
```

Reads `corpus/*.jsonl` + `BRAND.md`, emits `~/.hermes/memory/voice_profile.json`. Re-runs weekly on cron.

## 7. Set your schedule

```bash
cp schedule.example.yaml schedule.yaml
$EDITOR schedule.yaml
```

`schedule.yaml` is the single source of truth for what you're posting and when. The autonomous daemon reads it every minute. See comments in the example file for the schema.

## 8. Smoke test (no posts)

Verify both pipelines without posting anything:

```bash
# X
$HERMES_PY skills/x-engage/cdp_eval.py \
  --navigate 'https://x.com/notifications/mentions'
sleep 5
$HERMES_PY skills/x-engage/cdp_eval.py \
  --expr 'JSON.stringify([...document.querySelectorAll("article[data-testid=tweet]")].slice(0,5).map(a => ({author:(a.querySelector("[data-testid=User-Name]")||{}).innerText, text:((a.querySelector("[data-testid=tweetText]")||{}).innerText||"").slice(0,120)})))'

# LinkedIn
lipy inbound --since "2026-04-01T00:00:00Z" --limit 5
```

Both should emit real JSON about your account. If either fails, see the troubleshooting table at the bottom.

## 9. Start the daemon

When you're confident:

```bash
hermes gateway start
```

This is the autonomous loop. It reads `schedule.yaml`, fires pre-post engagement, monitors after-post comments, runs the daily report. Watch with:

```bash
hermes logs --follow
```

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
| `~/.hermes/logs/audit.jsonl` | Every action the agent takes — one line per action |
| `~/.hermes/reports/` | Daily reports |
| `~/.hermes/memory/voice_profile.json` | Distilled voice from corpus |

## What to do when you're not sure

1. Check `~/.hermes/logs/audit.jsonl` — the last 20 lines tell you what just happened.
2. Run `lipy doctor` and `hermes doctor` for diagnostics.
3. Flip `config/caps.yaml: linkedin.live: false` (or `x.live: false`) to halt that platform within one tick.
4. Open `BRAND.md` and re-read it — most "weird draft" problems trace back to underspecified brand rules.
