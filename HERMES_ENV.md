# `~/.hermes/.env` — keys this project needs

Hermes reads `~/.hermes/.env` (NOT a project-local `.env`). This file is a template/checklist for what you'll add as you progress through phases.

```bash
# ───────── Phase 0 (foundation) — currently configured ─────────
# Using Azure OpenAI via Hermes' azure-foundry provider plugin.
# Default deployment: gpt-5.1-chat on the 072025 resource.
# Credentials live in 51_AZURE_LLM_DEPLOYMENTS_AND_AGENT_RULES.md (gitignored).
# Setup script: /tmp/setup_hermes_azure.py (re-run if keys rotate).

AZURE_FOUNDRY_API_KEY=<from 51_AZURE_*.md, AZURE_API_KEY field>
AZURE_FOUNDRY_BASE_URL=https://072025.openai.azure.com/openai/v1

# To switch to gpt-5.4 / gpt-5.3-chat (sjudieh resource), comment out the
# 072025 block and uncomment the sjudieh block (in ~/.hermes/.env):
#   AZURE_FOUNDRY_API_KEY=<AZURE_API_KEY_GPT_5_4 from doc>
#   AZURE_FOUNDRY_BASE_URL=https://sjudieh-3891-resource.openai.azure.com/openai/v1
# Then: hermes config set model gpt-5.4   (or whichever deployment)

# Per-call alternative: hermes chat --provider azure-foundry -m gpt-5.4 -q "..."
# This overrides the default without changing config or .env.

# Alternative providers (not currently configured):
# ANTHROPIC_API_KEY=sk-ant-...
# OPENROUTER_API_KEY=sk-or-...

# ───────── Phase 1 (YouTube own-channel moderation) ─────────
YT_CHANNEL_ID=UC...                     # find at youtube.com/account_advanced
YT_OAUTH_CLIENT_ID=...
YT_OAUTH_CLIENT_SECRET=...
# After setting these, run once outside the agent:
#   python brand-growth-engine/skills/youtube-engage/youtube_auth.py
# That writes ~/.hermes/state/youtube_token.json (the refresh token).

# ───────── Phase 2 (inbound replies on own posts) ─────────

# X / Twitter — install xurl separately (see vendor/hermes-agent/skills/social-media/xurl/SKILL.md)
# After installing xurl, complete OAuth manually:
#   xurl auth oauth2 --app brand-growth-engine
# Then set the env var below to true ONLY after you have explicit prior written
# approval from X for automated replies (per Feb 2026 X policy):
X_AUTO_REPLY_APPROVED=false

# LinkedIn — Playwright-driven (no API). Username only here; password never stored.
LI_USERNAME=you@example.com

# ───────── Phase 4 (LinkedIn outbound — highest risk) ─────────
LI_RESIDENTIAL_PROXY_URL=http://user:pass@proxy.example.com:8080
# Datacenter proxies are auto-flagged. Budget ~$50–150/mo for a reputable
# residential provider (e.g., Bright Data, Oxylabs, Smartproxy).

# ───────── Operational caps (read by skills) ─────────
ANTHROPIC_DAILY_CAP_USD=5
X_API_DAILY_CAP_USD=2
HERMES_LOG_LEVEL=info
```

## File permissions

```bash
chmod 600 ~/.hermes/.env
```

Anyone who reads this file can run actions on your behalf. Keep it `600`.

## What is NOT here

- **No LinkedIn password.** Login is once, in a browser, headed mode (`lipy login --headed`). Playwright captures the session cookie; the password never enters the env.
- **No xurl tokens.** Stored in `~/.xurl` by xurl itself, encrypted/refresh-handled by the official CLI. Never read into agent context.
- **No YouTube refresh token.** Stored in `~/.hermes/state/youtube_token.json` after the OAuth bootstrap. The runtime helper mints short-lived access tokens from it.
