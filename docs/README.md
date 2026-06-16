# brand-growth-engine — docs

This folder documents what's been built, how it fits together, what we've learned, and what's next.

Last updated: 2026-05-14

## What this project is

A locally-hosted personal brand-management agent for LinkedIn, X, and YouTube, built on [Hermes Agent](https://github.com/NousResearch/hermes-agent) (vendored at [`vendor/hermes-agent/`](../vendor/hermes-agent/)). The agent reads your engagement (comments on your posts, your past outbound comments), can draft replies in your voice (future), and can post them with human-emulated typing through a real Chromium browser.

## How the workflow actually works today (2026-05-14)

**Both LinkedIn and X manual happy paths are working end-to-end.** Live writes proven on both platforms. The autonomous-cron loop and the drafter/brand-guard wiring are still pending — those move "decide" and "draft" out of the manual column.

### LinkedIn manual happy path

Transport: Playwright via the [`lipy.py`](../skills/linkedin-engage/lipy.py) CLI.

1. **Pre-flight (one-time per session, ~5s).** Open a terminal and run `lipy session` to start a long-running Chromium with your LinkedIn login. Leave that terminal open.
2. **Read inbound (~30s).** `lipy inbound --limit 5` returns JSON of your 5 most recent posts plus their comments. Headless — no window pops.
3. **Decide (manual).** Pick a comment URN worth responding to.
4. **Draft (manual).** Apply voice rules: first name only, no em-dashes, name-tag + one sentence + closing question.
5. **Write (~75s).** `lipy reply --live --parent <urn> --text "..."` opens a visible Chromium, navigates, clicks Reply, types char-by-char with Bezier-curve mouse, clicks submit.
6. **Verify.** `lipy reply` re-scrapes after submit to confirm the comment count incremented.

Proven 2026-05-11 with a live reply to Farouk Hajjej.

### X manual happy path

Transport: Chrome DevTools Protocol against a dedicated Chrome instance at `~/.hermes/state/chrome-cdp/`.

1. **Pre-flight (one-time per session, ~5s).** Launch the dedicated CDP Chrome:
   ```bash
   '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
     --remote-debugging-port=9222 \
     --user-data-dir="$HOME/.hermes/state/chrome-cdp" &
   disown
   ```
   X login persists in that profile. On first run only, log in once.
2. **Read inbound (~5s).** Navigate to `x.com/notifications/mentions` via CDP, then a `Runtime.evaluate` queries `document.querySelectorAll('article[data-testid="tweet"]')` and returns structured JSON of every mention.
3. **Decide (manual).** Pick a tweet worth responding to.
4. **Draft (manual).** Same voice rules as LinkedIn.
5. **Write (~10s).** Navigate to the tweet detail URL, focus the composer (`div[data-testid="tweetTextarea_0"]`), `Input.insertText` for typing, `Runtime.evaluate` with `userGesture: true` to click `button[data-testid="tweetButtonInline"]`.
6. **Verify.** `Page.captureScreenshot` confirms the "Your post was sent" toast + Premium upsell modal.

Proven 2026-05-14 with a live reply to @VadimStrizheus.

### What's still manual

- **Decide:** which comment / mention to engage with (rule + spam filtering coming via `spam-classifier`).
- **Draft:** writing the reply text (coming via `reply-drafter`, which will read voice_profile.json + BRAND.md).
- **Guard:** veto-checking the draft (coming via `brand-guard`).
- **Enforce:** caps / windows / jitter / hold-buffer (declared in [config/](../config/) but not read by the write commands).

When those four are wired in (Phase 1+), the only manual step left is the high-level decision "engage with this topic today, skip that one."

## Current status (Phase 0 → Phase 1)

✅ **Foundation complete:**
- Hermes Agent v0.13.0 installed locally at [`vendor/hermes-agent/.venv/`](../vendor/hermes-agent/.venv/)
- Azure OpenAI (DeepSeek-V4-Flash default, DeepSeek-V4-Pro for high-judgment calls) wired up via Hermes' `azure-foundry` provider
- 5 project skills written and linked into `~/.hermes/skills/`
- Long-running browser session daemon (`lipy session`) with CDP attach
- Human-emulation primitives (Bezier mouse, realistic typing, dwell)
- URN map cache for click-through navigation
- 45 voice training samples scraped from the user's own LinkedIn comment history

✅ **LinkedIn manual happy path proven (read + write):**
- Scrape posts, comments, and your own outbound comments via `lipy` (Playwright)
- Post a reply with full human emulation — live reply to Farouk Hajjej, 2026-05-11

✅ **X manual happy path proven (read + write):**
- Read mentions/posts/replies via CDP `Runtime.evaluate` against the DOM — structured JSON in one call
- Type via CDP `Input.insertText` (DraftJS-compatible), submit via `Runtime.evaluate` with `userGesture: true`
- Live reply to @VadimStrizheus posted 2026-05-14
- Runs in a dedicated CDP Chrome at `~/.hermes/state/chrome-cdp/`, separate from the user's normal Chrome (Chrome 121+ refuses CDP on the default profile)

🔧 **Fixed 2026-05-14:** LinkedIn activity-page DOM rolled — `componentkey="feed-commentary_*"` was dropped. Selector chain at [lipy.py:741](../skills/linkedin-engage/lipy.py#L741) updated. See [linkedin-engineering.md](linkedin-engineering.md).

🧹 **Cleaned 2026-05-14:** dead code from approaches that didn't pan out — `skills/x-engage/parse_x_ax.py` (AX-tree reader, replaced by CDP DOM) and `/Applications/CuaDriver.app` (broken Swift binary, replaced by Rust port at `~/.local/bin/cua-driver`). Engineering knowledge preserved in [x-engineering.md](x-engineering.md) appendix.

⏳ **Not yet wired (still manual or missing entirely):**
- `voice-profile` skill activation (distill voice from corpus → `voice_profile.json` — never run)
- `reply-drafter` skill integration (SKILL.md exists, no code path calls it)
- `brand-guard` enforcement (SKILL.md exists, never called — drafts are not veto-checked)
- [BRAND.md](../BRAND.md) still has unfilled placeholders — anything that *did* call brand-guard would refuse with `brand-md-not-configured`
- Caps / windows / jitter / blocklist enforcement inside `lipy` and the X recipe (declared in YAML; not yet read at action time)
- 5-minute outbound hold buffer (declared, not implemented)
- X auto-reply approval (per Feb 2026 X policy) — required before any autonomous cron-driven X writes; manual happy path doesn't need it
- YouTube integration via Google Data API v3 — OAuth bootstrap not done
- Phased autonomous rollout (no `hermes cron` jobs defined)

## Read me in this order

| If you want to… | Read |
|---|---|
| Understand what's built and how to use it | [architecture.md](architecture.md) |
| Get hands-on day-to-day | [workflows.md](workflows.md) |
| Look up a specific `lipy` command | [lipy-reference.md](lipy-reference.md) |
| Understand what we learned about LinkedIn's DOM and bot detection | [linkedin-engineering.md](linkedin-engineering.md) |
| Understand the X workflow + macOS Monterey + Chrome CDP findings | [x-engineering.md](x-engineering.md) |
| Know why we made the architectural choices we did + what's next | [decisions-and-roadmap.md](decisions-and-roadmap.md) |

## Quick start

### LinkedIn

```bash
# 1. Start the long-running browser session
cd ~/Desktop/projects/brand-growth-engine/skills/linkedin-engage
.venv/bin/python lipy.py session            # leave this terminal open

# 2. In another terminal: read your recent posts and comments
.venv/bin/python lipy.py posts --limit 5
.venv/bin/python lipy.py inbound --limit 5

# 3. Refresh your voice corpus from your own outbound comments
.venv/bin/python lipy.py my-comments --limit 100 --save

# 4. Reply to a comment (dry-run by default; pass --live to actually submit)
.venv/bin/python lipy.py reply --headed --live \
  --parent '<comment URN>' \
  --text 'Your reply text'

# 5. When done
.venv/bin/python lipy.py session-stop       # or Ctrl+C the session terminal
```

### X (Twitter)

```bash
# 1. Launch the dedicated CDP Chrome (separate from your normal Chrome)
mkdir -p "$HOME/.hermes/state/chrome-cdp"
'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.hermes/state/chrome-cdp" &
disown
# First run only: navigate to x.com in that window and log in once.

# 2. Verify CDP is live
curl -s http://localhost:9222/json/version

# 3. Read mentions (navigate + DOM scan)
HERMES_PY=~/Desktop/projects/brand-growth-engine/vendor/hermes-agent/.venv/bin/python
CDP=~/Desktop/projects/brand-growth-engine/skills/x-engage/cdp_eval.py
$HERMES_PY $CDP --navigate 'https://x.com/notifications/mentions'
sleep 3
$HERMES_PY $CDP --expr 'JSON.stringify([...document.querySelectorAll("article[data-testid=tweet]")].map(a => ({
  author: (a.querySelector("[data-testid=User-Name]") || {}).innerText,
  text: (a.querySelector("[data-testid=tweetText]") || {}).innerText,
  url: (a.querySelector("a[href*=\"/status/\"]") || {}).href,
  reply_aria: (a.querySelector("button[data-testid=reply]") || {}).getAttribute("aria-label")
})))'

# 4. Reply to a tweet: see the proven recipe in skills/x-engage/SKILL.md
#    (navigate to the tweet detail URL, focus composer, Input.insertText, click tweetButtonInline)
```

## Top-level project layout

```
brand-growth-engine/
├── BRAND.md                  # User-maintained voice guide — read by every draft
├── README.md                 # Project intro
├── HERMES_ENV.md             # ~/.hermes/.env template
├── docs/                     # This folder
├── config/                   # caps.yaml, windows.yaml, jitter.yaml, blocklist.yaml
├── corpus/                   # Voice training data (jsonl)
│   ├── README.md
│   └── linkedin_comments.jsonl   # 45 outbound comments (auto-populated)
├── skills/                   # Project-specific Hermes skills
│   ├── voice-profile/        # (pure prompt) distill voice from corpus
│   ├── brand-guard/          # (pure prompt) hard-veto validator
│   ├── reply-drafter/        # (pure prompt) generate replies in voice
│   ├── youtube-engage/       # YouTube Data API v3
│   │   ├── SKILL.md
│   │   ├── youtube_auth.py   # one-time OAuth bootstrap
│   │   └── youtube_token.py  # runtime token helper
│   ├── linkedin-engage/      # Playwright-driven LinkedIn
│   │   ├── SKILL.md
│   │   ├── install.sh
│   │   ├── lipy.py           # the CLI — every LinkedIn action
│   │   └── human_actions.py  # mouse curves, typing, dwell
│   └── x-engage/             # CDP-driven X
│       ├── SKILL.md          # runbook with the proven write recipe
│       └── cdp_eval.py       # thin CDP CLI (--expr / --navigate)
├── vendor/
│   └── hermes-agent/         # cloned Hermes (~99 MB)
└── 51_AZURE_*.md             # live API keys (gitignored)
```

## The key state directories outside the project

- `~/.hermes/.env` — API keys for Hermes (Azure Foundry creds, etc.)
- `~/.hermes/state/playwright/linkedin/profile/` — persistent Chromium profile for LinkedIn (login state, cookies, fingerprint)
- `~/.hermes/state/playwright/linkedin/urn_map.json` — activity-URN ↔ ugcPost-URN cache for LinkedIn
- `~/.hermes/state/chrome-cdp/` — persistent Chrome user-data-dir for X (login state, cookies, fingerprint)
- `~/.local/bin/cua-driver` — Rust-port cua-driver binary (Monterey-compatible; macOS Accessibility + Screen Recording permissions granted once)

## A note on philosophy

This project deliberately keeps the human and the agent in different lanes:

- **The human** decides what to engage with, navigates LinkedIn naturally, and approves drafts.
- **The agent** drafts in your learned voice, validates against your brand guide, types with human emulation, posts under strict rate caps.

The agent never replaces your judgment about what to engage with. It scales the typing, the voice consistency, and the audit trail — not the decision-making.
