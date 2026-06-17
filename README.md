# brand-growth-engine

Personal brand-management agent for LinkedIn, X, and YouTube — built on [Hermes Agent](https://github.com/NousResearch/hermes-agent) (Nous Research). The Hermes source is vendored at [vendor/hermes-agent/](vendor/hermes-agent/) so you can read all the code that runs on your machine.

## What this is

A set of Hermes skills + a brand guide + per-platform configs that, together, instruct the agent to:

- Reply to comments on your own posts (LinkedIn, X, YouTube) in your voice
- Comment outbound on others' posts (LinkedIn, X) for brand growth
- Moderate spam/toxic comments on your YouTube channel (delete or hold)
- Validate every drafted reply against your brand guide before posting
- Learn your voice from your past posts and re-train weekly

## Status

Phase 0 scaffold. Hermes is installed locally; skills are written; nothing posts yet. All the safety gates (brand-guard, caps, kill-switches) are wired up.

## Install (new laptop, three commands)

Full step-by-step is in [ONBOARDING.md](ONBOARDING.md). The short version, on a freshly unboxed Mac:

```bash
git clone <your-fork-of-this-repo> brand-growth-engine
cd brand-growth-engine
./scripts/preflight.sh    # Stage 0: Homebrew, Python 3.14, Chrome, ripgrep, jq
./setup.sh                # clones the pinned Hermes, builds venvs, installs cua-driver + lipy, symlinks skills
./scripts/bootstrap.sh    # interactive: LLM key, BRAND.md, X + LinkedIn logins, voice profile, autonomy arm
```

Everything is idempotent — re-run any of the three and it picks up where it left off. The only things you bring by hand: an LLM API key and your X/LinkedIn 2FA device.

## Layout

```
brand-growth-engine/
├── BRAND.md                 # YOU FILL IN. Voice/values/off-limits/CTAs. Read on every draft.
├── README.md                # this file
├── ONBOARDING.md            # the full new-laptop walkthrough
├── setup.sh                 # mechanical install (Hermes, venvs, cua-driver, lipy, symlinks)
├── schedule.example.yaml    # template → copy to schedule.yaml (gitignored)
├── .env.example             # template → copied to ~/.hermes/.env by setup.sh
├── config/
│   ├── caps.yaml            # daily/weekly caps + per-platform live kill-switches
│   ├── windows.yaml         # business-hour activity windows
│   ├── jitter.yaml          # human-like delay jitter
│   ├── blocklist.yaml       # accounts/keywords/domains to never engage
│   └── launchd/             # com.brandgrowthengine.hermes.plist (persistent daemon)
├── corpus/                  # YOUR EXPORTS. Voice training data (jsonl).
│   └── README.md            # how to export from each platform
├── scripts/                 # operational scripts (run via the Hermes venv python)
│   ├── preflight.sh         # Stage 0: brew, python, Chrome
│   ├── install-cua-driver.sh# auto-installs the pinned, universal cua-driver
│   ├── bootstrap.sh         # interactive 12-gate new-laptop walkthrough
│   ├── autonomy-mode.sh     # flips caps to phase 2 + registers cron jobs
│   ├── schedule-tick.py     # per-minute orchestrator (publish/monitor/reply)
│   ├── engagement-test.py   # manual engagement loop — scan, draft, pre-fill composer (docs: docs/engagement-test.md)
│   ├── voice-train.py       # builds ~/.hermes/memories/voice_profile.json
│   ├── ingest-corpus.py     # normalizes corpus/*.jsonl → _normalized.jsonl
│   └── daily-report.py      # renders the daily report
├── skills/                  # OUR SKILLS, symlinked into ~/.hermes/skills/
│   ├── voice-profile/       # distill voice from corpus + BRAND.md
│   ├── brand-guard/         # hard-veto validator on every draft
│   ├── reply-drafter/       # generate replies in voice
│   ├── x-engage/            # X reads + writes over Chrome DevTools Protocol (CDP)
│   ├── youtube-engage/      # YouTube Data API v3 (curl + OAuth)
│   └── linkedin-engage/     # Playwright-based `lipy` CLI; ToS-risky
└── vendor/
    └── hermes-agent/        # cloned NousResearch/hermes-agent, pinned (gitignored; ~230 MB)
        └── .venv/           # Hermes' venv with all deps installed
```

X is covered by Hermes' built-in `xurl` skill at [vendor/hermes-agent/skills/social-media/xurl/](vendor/hermes-agent/skills/social-media/xurl/). We use it as-is.

## How it runs

Hermes is a complete agent platform — daemon, cron, memory (SQLite + FTS5), skill loader, dashboard, multi-channel gateway. We don't reinvent any of that. Our project contributes only:

1. **Skills** — what the agent can do
2. **BRAND.md** — what voice it should do it in
3. **Configs** — caps, windows, blocklist that the skills read

The agent runs as a daemon (`hermes gateway start`). Cron jobs (set up via `hermes cron`) fire on schedule with prompts like *"Run the YouTube moderation pass"* or *"Find outbound LinkedIn candidates for the next hour."* Inside those jobs, the LLM uses our skills (which it learns from each `SKILL.md`) to do the work.

## Getting started

> `./setup.sh` already did the mechanical work below — cloned the pinned Hermes, built the venv, ran `hermes doctor`, and symlinked every skill into `~/.hermes/skills/`. The steps here are the **reference / manual fallback**, plus the things only you can supply (model key, BRAND.md, corpus, credentials). On a fresh laptop, prefer `./scripts/bootstrap.sh`, which walks all of this interactively with validation.

### Verify the local install (optional)

```bash
./vendor/hermes-agent/.venv/bin/hermes doctor
./vendor/hermes-agent/.venv/bin/hermes skills list
```

`doctor` should show `✓` on Python, packages, directory structure. Some `⚠` items (Telegram, Discord) are optional. If ripgrep shows missing despite being on PATH, that's a benign Hermes-internal PATH detection issue. `skills list` should show the six project skills.

### Configure a model

Hermes works with many providers. Three reasonable choices:

**Option A — Anthropic direct (highest reply quality, recommended).**
Get an API key at https://console.anthropic.com. Then:

```bash
mkdir -p ~/.hermes
cat > ~/.hermes/.env <<'EOF'
ANTHROPIC_API_KEY=sk-ant-...
EOF
chmod 600 ~/.hermes/.env

./vendor/hermes-agent/.venv/bin/hermes model anthropic claude-sonnet-4-6
```

**Option B — OpenRouter (single key, hundreds of models).**
Get a key at https://openrouter.ai. Same pattern but `OPENROUTER_API_KEY=...` and `hermes model openrouter anthropic/claude-sonnet-4-6`.

**Option C — Nous Portal** (built-in, lowest friction): `hermes login` and follow the flow.

### Fill in BRAND.md

This is the most important file in the project. Every drafted reply is validated against it. Open [BRAND.md](BRAND.md) and replace every placeholder.

### Drop voice training data into corpus/

See [corpus/README.md](corpus/README.md) for export instructions per platform. Even partial corpus (say, 30 LinkedIn comments) is enough to bootstrap; more = better voice fidelity.

### Per-platform credentials

Add to `~/.hermes/.env`:

```bash
# X (Twitter) — for the xurl skill. xurl is installed separately:
#   curl -fsSL https://raw.githubusercontent.com/xdevplatform/xurl/main/install.sh | bash
# Then: xurl auth oauth2 --app brand-growth-engine
# (See vendor/hermes-agent/skills/social-media/xurl/SKILL.md for full flow.)
# X_AUTO_REPLY_APPROVED is the gate for autonomous replies.
X_AUTO_REPLY_APPROVED=false

# YouTube
YT_CHANNEL_ID=UC...
YT_OAUTH_CLIENT_ID=...
YT_OAUTH_CLIENT_SECRET=...
# Bootstrap with: python skills/youtube-engage/youtube_auth.py

# LinkedIn (Playwright session — no API)
LI_USERNAME=you@example.com
LI_RESIDENTIAL_PROXY_URL=http://user:pass@proxy.example.com:8080
# Bootstrap with: cd skills/linkedin-engage && ./install.sh && lipy login --headed
```

### Run

The interactive way:

```bash
./vendor/hermes-agent/.venv/bin/hermes
```

Then in the chat: `/youtube-engage` or `/voice-profile` to load the relevant skill, and tell it what to do.

The autonomous way (Phase 1+):

```bash
./vendor/hermes-agent/.venv/bin/hermes cron add \
  --name yt-mod-pass \
  --schedule "*/30 * * * *" \
  --prompt "Run the youtube-engage moderation pass for new comments since the last run."
./vendor/hermes-agent/.venv/bin/hermes gateway start
```

(`hermes cron` and `hermes gateway` are real subcommands — see `hermes <cmd> --help`.)

## Manual engagement loop (Phase-2-supervised)

For the in-between phase — you're posting on X/LinkedIn manually, and you want
help drafting replies in your voice without going fully autonomous —
[`scripts/engagement-test.py`](scripts/engagement-test.py) scans your recent
posts for unanswered third-party replies, drafts responses via direct Azure
DeepSeek-V4-Flash (~10s/draft), and opens a new tab in your CDP Chrome with
the composer pre-filled. You review and click Reply yourself.

```bash
# One pass — scan, draft, exit.
./vendor/hermes-agent/.venv/bin/python scripts/engagement-test.py \
    --platforms x --source my-posts

# Watch — keep polling every 90s, stop when something gets drafted.
./vendor/hermes-agent/.venv/bin/python scripts/engagement-test.py \
    --platforms x --source my-posts --watch
```

Full architecture, configuration, and troubleshooting in
[`docs/engagement-test.md`](docs/engagement-test.md). Three-layer defense
against engaging with the wrong post (pinned-flag detection + URL blocklist +
max-age cutoff), inline brand-guard with em-dash auto-strip, and back-to-profile
navigation between posts so the browser doesn't look stuck.

## Phased rollout (gates)

Each phase has go/no-go criteria. Don't flip `caps.yaml: phase` higher until they're met.

| Phase | What's enabled | Go criteria |
|---|---|---|
| 0 | Foundation | Hermes runs, skills listed, BRAND.md filled, voice profile generated |
| 1 | YouTube own-channel moderation | Phase 0 + dry-run review for 7 days, 95% spam-classifier agreement |
| 2 | Inbound replies on own X + LinkedIn posts | Phase 1 clean 14 days; X auto-reply approval received; LinkedIn session warmed 30 days |
| 3 | Outbound on X | Phase 2 clean 21 days; relevance-scorer precision ≥ 0.8; cost-meter under budget |
| 4 | Outbound on LinkedIn | Phase 3 clean; `RISK_ACCEPTED.md` signed; residential proxy stable |

## Hard rules baked into the skills

- LinkedIn outbound is gated on `RISK_ACCEPTED.md` being present and signed (Phase 4).
- X auto-reply is gated on `X_AUTO_REPLY_APPROVED=true` in `~/.hermes/.env` — required by X's Feb 2026 policy.
- Every platform has `live: false` in `caps.yaml` — flip to halt instantly.
- Every drafted reply is run through `brand-guard` (hard veto) before any post.
- Outbound has a 5-min hold buffer — drafts surface in the dashboard before posting; you can yank.

## Risks (read these)

1. **LinkedIn**: published 23% account-restriction rate within 90 days for automated commenters. Even with stealth + proxy + jitter + caps, expect a meaningful chance of restriction over time. Decide whether your main account is acceptable to risk.
2. **X**: automated replies require explicit prior written approval from X (Feb 2026 policy). Apply at https://developer.x.com — can take weeks.
3. **MacBook lid-close**: kills the daemon unless `pmset` is configured. Cleanest answer: dedicate an always-plugged Mac.
4. **Voice drift**: if the agent's replies engage well, you may unconsciously imitate it. Mitigation is built in — `voice-profile` weights original corpus 2× over agent-generated replies.
5. **YouTube quota**: Data API v3 default is 10K units/day. A viral video can exhaust it; the skill degrades gracefully.

## Useful Hermes commands

| Command | Purpose |
|---|---|
| `hermes` | Interactive chat |
| `hermes doctor` | Diagnose config/dep issues |
| `hermes skills list` | Show what skills are available |
| `hermes cron list` / `hermes cron add` | Manage scheduled jobs |
| `hermes status` | Status of all components |
| `hermes logs --follow` | Tail the agent log |
| `hermes dashboard` | Web dashboard (works best on Linux/WSL2) |
| `hermes config set` | Change a config value |

## Plan reference

The full plan with phased rollout, data model, and risk register lives at `~/.claude/plans/okay-as-claude-coe-federated-candle.md`.
