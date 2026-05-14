# Architecture

How the pieces fit together.

## The two layers

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Layer 1: Hermes Agent (vendored at vendor/hermes-agent/)               │
│                                                                         │
│  Provides: daemon, cron scheduler, memory (SQLite+FTS5), CLI,           │
│  multi-channel gateway (Telegram/Discord/etc.), 87 built-in skills,     │
│  pluggable LLM providers (we use azure-foundry → gpt-5.1-chat).         │
│                                                                         │
│  Entry point: vendor/hermes-agent/.venv/bin/hermes                      │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ symlinks
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  Layer 2: Our skills (skills/) and CLIs                                 │
│                                                                         │
│  Skills:                                                                │
│    voice-profile, brand-guard, reply-drafter   (pure prompt)            │
│    youtube-engage                              (curl + OAuth)           │
│    linkedin-engage                             (Playwright via lipy)    │
│                                                                         │
│  Configs:                                                               │
│    BRAND.md, config/caps.yaml, config/windows.yaml,                     │
│    config/jitter.yaml, config/blocklist.yaml                            │
│                                                                         │
│  Voice corpus:                                                          │
│    corpus/linkedin_comments.jsonl (45 entries, auto-populated)          │
└─────────────────────────────────────────────────────────────────────────┘
```

Hermes is a fully functional standalone agent — we **add** to it rather than build a new framework on top.

## How Hermes knows about our skills

Skills are linked from `skills/<name>/` into `~/.hermes/skills/<name>/` as symlinks. Hermes' skill loader walks `~/.hermes/skills/` on every start.

```bash
# This is the one-time wiring done in setup
mkdir -p ~/.hermes/skills
for d in skills/*/; do
  ln -snf "$(pwd)/$d" "$HOME/.hermes/skills/$(basename "$d")"
done
```

After linking, `hermes skills list` shows our skills mixed in with Hermes' built-in 87. Slash commands like `/voice-profile`, `/brand-guard`, `/reply-drafter`, `/linkedin-engage`, `/youtube-engage` are available inside a Hermes chat session.

## The skills

### Pure-prompt skills (no code, just instructions)

These skills are `SKILL.md` markdown files that tell the LLM how to perform a task. The LLM reads the SKILL.md when the user invokes the skill and follows the instructions.

| Skill | What it does |
|---|---|
| `voice-profile` | Reads BRAND.md + corpus/, distills into `~/.hermes/memories/voice_profile.json`. Two modes: bootstrap and retrain. |
| `brand-guard` | Hard-veto validator. Reads BRAND.md + voice_profile.json. Returns PASS/FAIL on a drafted reply with structured reasons. **Must be called before any post action.** |
| `reply-drafter` | Generates a reply in the user's voice. Reads BRAND.md + voice_profile.json + parent post/comment. Calls brand-guard. Returns a draft or refusal. |
| `x-engage` | X (Twitter) workflow. Both read and write run against a dedicated CDP Chrome at `~/.hermes/state/chrome-cdp/` (Chrome 121+ refuses CDP on the default profile, so a separate user-data-dir is required). Reads via CDP `Runtime.evaluate` querying `document.querySelectorAll('article[data-testid="tweet"]')`. Writes via `Input.insertText` into the DraftJS composer + `Runtime.evaluate` with `userGesture: true` to click `tweetButtonInline`. The thin CLI is [`cdp_eval.py`](../skills/x-engage/cdp_eval.py); the proven write recipe is inlined in [`skills/x-engage/SKILL.md`](../skills/x-engage/SKILL.md). |

### CLI-wrapper skills (instructions + executable code)

| Skill | What it wraps | Status |
|---|---|---|
| `youtube-engage` | curl + Google Data API v3 OAuth (helper scripts `youtube_auth.py`, `youtube_token.py`) | SKILL.md complete; OAuth bootstrap untested in production |
| `linkedin-engage` | Playwright via the `lipy` CLI we wrote | Manual happy path working: read (headless) + write (headed). No drafting, no brand-guard, no caps enforcement, no cron yet — see [decisions-and-roadmap.md](decisions-and-roadmap.md#how-a-real-engagement-runs-today-manual-happy-path) |
| `x-engage` | A dedicated CDP Chrome at `~/.hermes/state/chrome-cdp/` driven via Chrome DevTools Protocol over WebSocket (`ws://localhost:9222/...`). Both read and write paths use CDP. The `cdp_eval.py` CLI wraps `Runtime.evaluate` / `Page.navigate`; the write recipe additionally uses `Input.insertText` and `Page.captureScreenshot`. | Manual happy path working: read (mentions/posts/replies via DOM scan) + write (reply via CDP recipe). Proven 2026-05-14 with a live reply to @VadimStrizheus. No drafting, no brand-guard, no caps enforcement, no cron yet. See [x-engineering.md](x-engineering.md). |
| `xurl` (X / Twitter API) | The X dev platform's official `xurl` CLI — shipped with Hermes at [`vendor/hermes-agent/skills/social-media/xurl/SKILL.md`](../vendor/hermes-agent/skills/social-media/xurl/SKILL.md) | **Deprioritized** in favor of `x-engage`. Requires ~$100/mo Basic tier + X's Feb 2026 auto-reply approval. Kept as a fallback for the autonomous-cron loop if the browser route hits issues. |

## `lipy` — the LinkedIn CLI

The heart of the LinkedIn engagement layer. Located at [`skills/linkedin-engage/lipy.py`](../skills/linkedin-engage/lipy.py).

**Why a separate CLI** (not pure SKILL.md): LinkedIn has no public API, so all interaction is browser automation. Playwright requires Python code; a pure prompt skill can't drive a browser. We keep the Playwright code in `lipy.py` and expose a clean JSON-over-stdout CLI surface that the SKILL.md describes.

### Internal structure

```
lipy.py
├── _session_running()              ← detect long-running daemon
├── linkedin_session()              ← context manager; attaches via CDP if daemon
│                                     running, else launches fresh browser
├── _check_auth(page)               ← visit /feed/, check login state
├── _stealth(page)                  ← apply playwright-stealth tweaks
│
├── _urn_map_load/save              ← cache for activity-URN ↔ ugcPost-URN
├── _remember_urn(...)              ← called by scrapers to populate the map
│
├── _scrape_posts(page, limit)
├── _scrape_comments(page, post_urn, ...)
├── _scrape_my_comments(page, ...)  ← outbound comment history → voice corpus
├── navigate_to_own_post(page, urn, text_hint=None)
│
├── _find_top_level_comment_textbox(page)
├── _find_inline_reply_textbox(parent_el, page)
├── _find_submit_button(textbox, labels)
│
└── cmd_login / cmd_doctor / cmd_status / cmd_session / cmd_session_stop /
    cmd_posts / cmd_comments / cmd_inbound / cmd_my_comments /
    cmd_reply / cmd_comment
```

### human_actions.py — the emulation primitives

Located at [`skills/linkedin-engage/human_actions.py`](../skills/linkedin-engage/human_actions.py). Used by `cmd_reply` and `cmd_comment` for any action that touches LinkedIn's anti-bot perimeter.

| Primitive | What it does |
|---|---|
| `HumanMouse.move_to(page, x, y)` | Bezier-curve cursor path with ease-in/out, ~600ms per 500px with jitter |
| `human_click(page, element)` | Move-to + mouse-down + small drift + mouse-up at sub-pixel offset |
| `human_type(page, text)` | Per-char delays (70–180ms base), longer at punctuation, occasional "thinking" pauses, 1.2% typo+backspace rate |
| `smooth_scroll(page, pixels)` | Many small wheel events instead of one big jump |
| `read_dwell(text)` | Sleep proportional to reading speed (~250 wpm) |
| `consider_dwell()` | 1.4–3.6s pause modeling "should I post this?" |
| `dwell(lo, hi)` | Generic uniform-random sleep |

## Config files

All in [`config/`](../config/). Read fresh at the start of every job — no daemon restart needed to change them.

### `caps.yaml` — the kill switches

The most important file. Per-platform `live: false` halts that platform instantly. Daily/weekly comment caps enforce ban-safe rates. Includes the 5-minute outbound hold buffer setting and X auto-reply-approval gate.

### `windows.yaml` — when to act

Business-hour activity windows per platform per action type. YouTube moderation is 24/7; LinkedIn/X are weekdays during business hours. Includes timezone (default `America/Los_Angeles`).

### `jitter.yaml` — pacing variance

Min/max delay (in seconds) between actions. Long jitter on outbound (5–25 min on X, 5–20 min on LinkedIn) is the single largest defense against pattern detection. Also typing speed bounds and page-dwell ranges.

### `blocklist.yaml` — who/what to skip

Accounts (handles), keywords, domains, off-topic categories, trusted-handles override list.

## State directories

### `~/.hermes/`

Hermes' home. Generated on first run.
- `config.yaml` — Hermes config (we set `provider: azure-foundry`, `model: gpt-5.1-chat`)
- `.env` — secrets (chmod 600). Contains `AZURE_FOUNDRY_API_KEY`, `AZURE_FOUNDRY_BASE_URL`
- `memories/` — agent memory (markdown + FTS5)
- `state.db` — session SQLite

### `~/.hermes/state/playwright/linkedin/`

The LinkedIn-specific state.
- `profile/` — persistent Chromium user-data-dir (login state, cookies, fingerprint, all browser data). **Don't delete this**; deleting means re-logging in + re-doing 2FA.
- `state.json` — portable session backup (storage_state format)
- `session.pid`, `session.port` — markers for the running `lipy session` daemon
- `urn_map.json` — activity-URN ↔ ugcPost-URN cache (auto-populated by scrapes; survives reboots)

### Project corpus

`corpus/linkedin_comments.jsonl` — outbound comments scraped from your `/in/me/recent-activity/comments/`. Currently 45 entries. Auto-populated by `lipy my-comments --save`.

## Data flow: an inbound reply (current implementation)

```
User starts:           lipy session              ← Chromium opens, daemon writes PID/port
                       ↓
Other terminal:        lipy reply --parent ...   ← Python process
                       ↓
                       linkedin_session()        ← reads session.port, CDP-attaches
                       ↓                           (no new browser launched)
                       _check_auth(page)         ← visit /feed/, check li_at cookie
                       ↓
                       Look up activity URN      ← consult urn_map.json by ugcPost ID
                       in cache
                       ↓
                       navigate_to_own_post(...) ← go to /in/me/recent-activity/all/,
                       ↓                           scroll, find card.
                                                   (LinkedIn blocks click-through;
                                                    falls back to direct URL goto.)
                       ↓
                       _scrape_comments(...)     ← force LazyColumn render via JS
                       ↓                           scrollIntoView, harvest comments
                       Find parent_el by URN
                       ↓
                       Click Reply button        ← inside the parent comment element
                       ↓
                       _find_inline_reply_textbox  ← scoped to parent's sibling
                       ↓                            (distinguishes from top-level
                                                     comment box that shares aria-label)
                       human_click + human_type  ← Bezier curve + char-by-char typing
                       ↓                           ~75s for typical reply
                       consider_dwell()
                       ↓
                       _find_submit_button("reply")
                       ↓                         ← walk up DOM, find the Reply button
                       human_click(submit_btn)   ← submits the reply
                       ↓
                       _scrape_comments again    ← verify the reply landed
                       ↓
                       json.dump(result)         ← {ok, dry_run, target, parent_urn, ...}
```

## Models in use

**Default**: Azure OpenAI `gpt-5.1-chat` via Hermes' `azure-foundry` provider plugin.

Configuration (in `~/.hermes/config.yaml`):
```yaml
provider: azure-foundry
model: gpt-5.1-chat
```

API config (in `~/.hermes/.env`):
```bash
AZURE_FOUNDRY_API_KEY=<from 51_AZURE_*.md>
AZURE_FOUNDRY_BASE_URL=https://072025.openai.azure.com/openai/v1
```

All four available deployments (5.1-chat, 5.2-chat, 5.3-chat, 5.4) verified working on both classic and `/openai/v1/` URL patterns via `/tmp/azure_probe.py`. See [decisions-and-roadmap.md](decisions-and-roadmap.md) for per-skill recommendations.

To switch models for a one-off:
```bash
hermes chat --provider azure-foundry -m gpt-5.4 -q "..."
```

To switch the default (requires also swapping which key/endpoint is active in `~/.hermes/.env`):
```bash
hermes config set model gpt-5.4
# Then edit ~/.hermes/.env to use the sjudieh resource for 5.3/5.4
```
