# engagement-test.py — manual engagement loop

A standalone orchestrator that:

1. Scans your recent X posts (and LinkedIn, when logged in) for unanswered third-party replies
2. Drafts a response in your voice via a direct Azure OpenAI call
3. Opens a new tab in your CDP Chrome with the reply composer **pre-filled**
4. Stops there — clicking submit is your job

Built as a hand-driven engagement tool: you keep the script running in the background; it surfaces drafts as inbound replies arrive; you skim and post (or yank). Designed to be the practical bridge between fully manual engagement and Phase 3+ autonomous mode.

**File:** [`scripts/engagement-test.py`](../scripts/engagement-test.py) (~820 LOC, single file)

**Status:** Validated end-to-end against `@salaicreates` 2026-06-16 — drafted on-voice replies to real third-party comments; composer tabs opened correctly.

---

## Quick usage

```bash
# Single pass — scan once, draft what you can, exit.
./vendor/hermes-agent/.venv/bin/python scripts/engagement-test.py \
    --platforms x --source my-posts --limit 5

# Watch mode — poll every 90s, stop on first iteration that drafts ≥1 reply.
./vendor/hermes-agent/.venv/bin/python scripts/engagement-test.py \
    --platforms x --source my-posts --watch

# Watch forever — poll on interval, never stop until Ctrl-C.
./vendor/hermes-agent/.venv/bin/python scripts/engagement-test.py \
    --platforms x --source my-posts --watch --keep-going

# Reset the "already drafted" state and start over.
./vendor/hermes-agent/.venv/bin/python scripts/engagement-test.py \
    --platforms x --source my-posts --reset-seen --limit 5
```

**Common flags:**

| Flag | Default | Meaning |
|---|---|---|
| `--platforms` | `x,linkedin` | Subset of platforms to scan |
| `--source` | `auto` | `my-posts` (scan profile timeline) / `mentions` (`/notifications/mentions`) / `auto` (try my-posts first) |
| `--limit` | `5` | Max drafts per platform per iteration |
| `--max-age-days` | `14` | Skip your posts older than N days |
| `--watch` | off | Poll on interval (default exit on first draft) |
| `--keep-going` | off | With `--watch`: don't exit on first draft, loop forever |
| `--interval-seconds` | `90` | With `--watch`: seconds between polls |
| `--reset-seen` | off | Clear the dedup state file before running |

---

## Architecture (one diagram)

```
┌─────────────────────────────────────────────────────────────────────┐
│  engagement-test.py                                                 │
│  ──────────────────                                                 │
│                                                                     │
│  1. Scan source (X profile timeline OR /notifications/mentions)     │
│     │                                                               │
│     ▼                                                               │
│  2. Filter:                                                         │
│       • pinned-post DOM detection                                   │
│       • blocklist file (~/.hermes/state/engagement_blocklist.json)  │
│       • max-age-days cap on parent post                             │
│       • self-authored thread continuations                          │
│       • already-seen URLs (~/.hermes/state/engagement_seen.json)    │
│     │                                                               │
│     ▼                                                               │
│  3. For each surviving third-party reply:                           │
│     │                                                               │
│     ├──► Build prompt from BRAND.md + voice_profile.json (~3K chars)│
│     │                                                               │
│     ├──► POST /chat/completions to Azure DeepSeek-V4-Flash (~10s)   │
│     │                                                               │
│     ├──► Inline brand-guard:                                        │
│     │     · em-dash / en-dash    → auto-strip (replace with ', ')   │
│     │     · hashtag              → REFUSE                           │
│     │     · sycophantic opener   → REFUSE                           │
│     │                                                               │
│     ├──► Open new tab in CDP Chrome via /json/new?<reply_url>       │
│     │                                                               │
│     ├──► Poll for tweetTextarea_0 composer selector (up to 25s)     │
│     │                                                               │
│     └──► Input.insertText with the cleaned draft. Leave tab open.   │
│                                                                     │
│  4. Print summary + persist seen.json                               │
│     ─ if --watch: sleep, repeat                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Why bypass `hermes chat`?

The schedule-tick.py orchestrator drives drafts through `hermes chat --skills reply-drafter,brand-guard -q '<prompt>'`. That works, but each call boots a full Hermes agent loop with all tools available → ~7+ minutes per draft on this hardware. Not viable for an interactive engagement test.

engagement-test.py skips the agent and calls `/chat/completions` on Azure directly:

```python
httpx.post(
    f"{cfg['base_url']}/chat/completions",
    headers={"api-key": cfg["api_key"], "Content-Type": "application/json"},
    json={
        "model": cfg["model"],  # DeepSeek-V4-Flash by default
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": 400,
        "temperature": 0.7,
    },
    timeout=90,
)
```

Result: **~5–15 seconds per draft** instead of 7+ minutes. The voice context (BRAND.md + voice_profile.json) is inlined into the system prompt instead of being loaded as skills.

This is a deliberate shortcut for the test phase. For production-grade engagement, the schedule-tick.py path is the correct one once we can tolerate the latency or pre-warm the agent.

---

## The three-layer defense against engaging with the wrong post

Drafting a reply on the wrong content (e.g., your pinned post from 2023) is the most embarrassing failure mode. engagement-test.py blocks this three independent ways — any one blocks:

### Layer 1: pinned-flag DOM detection

X marks pinned posts with a `[data-testid="socialContext"]` element whose innerText contains `"Pinned"`. The extraction JS pulls this:

```js
const ctx = a.querySelector('[data-testid="socialContext"]');
return { /* ... */ pinned: /pinned/i.test(ctx ? ctx.innerText : '') };
```

If `pinned: true`, the post is skipped. **Failure mode:** X redesigns the pinned indicator and removes `socialContext`. Then layers 2 and 3 catch it.

### Layer 2: hard URL blocklist

`~/.hermes/state/engagement_blocklist.json`:

```json
{
  "x": [
    "https://x.com/salaicreates/status/1709080757610926388"
  ],
  "linkedin": []
}
```

Edited by hand. Any post URL listed here is unconditionally skipped, regardless of age or pinned status. Reload-on-each-pass: edits take effect on the next iteration with no restart.

### Layer 3: max-age cutoff on the parent post

`--max-age-days 14` (default): skip any parent post whose `<time datetime="...">` is older than the cutoff. Catches:

- Pinned posts that aren't marked as such (e.g., evergreen viral posts you re-up regularly)
- The accidental case where someone replies to one of your old posts and it shows up
- Any blocklist mistake

---

## Other safety filters

| Filter | Source | Purpose |
|---|---|---|
| **Self-authored replies** | author field contains `@<your-handle>` | Don't draft replies to your own thread continuations |
| **Already-drafted** | `~/.hermes/state/engagement_seen.json` | Don't redraft the same comment across iterations |
| **No third-party replies** | post had only self-replies after filtering | Naturally results in zero drafts; skip the post |

The `seen.json` schema is per-platform → URL → status:

```json
{
  "x": {
    "https://x.com/Kevin/status/123": {"ts": "2026-06-16T22:36:00Z", "status": "drafted"},
    "https://x.com/X/status/456":      {"ts": "2026-06-16T22:36:05Z", "status": "refused"}
  }
}
```

`--reset-seen` wipes this. Without `--reset-seen`, comments stay marked indefinitely. To re-draft a specific URL, edit the file by hand and remove its entry.

---

## Voice context construction

`_brand_voice_context()` builds a ~3K-char block that goes into every system prompt:

```
=== BRAND.md (truncated) ===
# Brand Guide
...
**Name:** Sal AI
**Handle:** @salaicreates (X), in/sal-ai (LinkedIn)
**Positioning:** I help operators ship AI automation that actually runs.
...

=== voice_profile.json (distilled) ===
tone: {"register": "conversational", "warmth": 0.65, ...}
signature_phrases: ["own your infrastructure", "the leverage is", "what's worked for us", ...]
preferred_words: ["operators", "leverage", "concrete", "workflows", "infrastructure", ...]
```

BRAND.md is truncated to the first 2200 chars (covers Identity, Audiences, and most of Voice). voice_profile.json's distilled fields (top 15 signature phrases, top 20 preferred words, tone object) are dumped as JSON.

The system prompt then says:

> You are Sal AI (@salaicreates). You're drafting a reply to a comment on one of your social posts.
> Match the voice/tone/vocabulary distilled below from BRAND.md and your voice profile.
> Write ONE reply — concrete, warm, direct, no abstractions. No em-dash (—). No hashtags.
> Never open with "Great question", "Absolutely", "Thanks for sharing", "I love this", or any sycophantic opener.
> Length: under {280|600} characters.
> Output ONLY the reply text — no JSON, no quotes, no preamble.

X length cap = 280; LinkedIn = 600.

---

## Inline brand-guard

Even with the prompt rules, DeepSeek-V4-Flash routinely uses em-dashes. Rather than throw the whole draft away, we **post-process**:

| Rule | Action |
|---|---|
| em-dash (—) or en-dash (–) | **auto-strip** — replace ` — ` and `—` with `, ` |
| hashtag (#) | **REFUSE** (signals lazy / off-brand) |
| sycophantic opener | **REFUSE** — exact list: "great question", "absolutely", "thanks for sharing", "i love this", "this is amazing" |

The auto-strip is enough for em-dashes because the LLM uses them mainly as soft parentheticals — a comma works equivalently. Hashtags and openers are harder failures (they imply the LLM is generating off-brand content) and warrant a full refusal so you can see the rejected draft.

When refused, the output shows:

```
Refused draft: This is amazing! Less work, more grind — love it.
SKIP — [{'rule': "sycophantic opener 'this is amazing'"}, {'rule': 'contains hashtag'}]
```

---

## CDP Chrome flow

Inbound discovery and composer pre-fill both run through the dedicated CDP Chrome (port 9222) launched by `scripts/x-engage/start-chrome-cdp.sh`. The script auto-relaunches Chrome if `:9222` is unreachable.

### Reading posts (profile scrape)

`_navigate_and_extract(url, scroll_passes=N)`:

1. Find an existing `x.com` tab via `http://localhost:9222/json`
2. `Page.navigate` to `url`
3. Poll `document.querySelectorAll("article[data-testid='tweet']").length` every 2s until ≥1 article or `max_wait_s` (default 25s) elapses
4. **Scroll** `scroll_passes` times (default 4): `window.scrollBy(0, window.innerHeight * 1.5)` with a 1.8s wait between. Stops early if the article count stops growing.
5. Run the extraction JS, return the array

Without the scroll loop, only the first 2–3 articles render above-the-fold; you miss recent posts and the script appears to re-check the same single post forever.

### Opening a composer tab

`fill_composer_in_new_tab(tweet_url, text)`:

1. `PUT http://localhost:9222/json/new?<tweet_url>` opens a new tab
2. Wait 3s for initial nav
3. Poll for `div[data-testid="tweetTextarea_0"]` to exist (X's inline composer doesn't render until the tweet detail SPA hydrates — up to 25s)
4. `Runtime.evaluate` `.focus()` on the composer with `userGesture=true`
5. `Input.insertText` with the cleaned draft (NOT `execCommand` / paste — those silently fail on DraftJS)
6. Return — leave the tab open

Each draft = one new tab. You end up with N tabs each pre-filled with one reply. Review, click reply, or close.

### "Back to profile" navigation

After fetching replies for a given post, `navigate_chrome_to(profile_url)` swaps the existing tab back to `https://x.com/<handle>`. Without this, Chrome sits on the last visited post URL for 90s until the next iteration, which looks "stuck" even when the script is correctly sleeping.

Each iteration in watch mode now visibly cycles: profile → post 1 → profile → post 2 → profile → … → sleep.

---

## Watch mode

`--watch` puts the single-pass logic in a `while True` loop with `time.sleep(args.interval_seconds)` between iterations. Two completion modes:

- **Default (stop-on-first-draft):** exit cleanly on the first iteration that drafts ≥1 reply. Useful when you want the script to ping you the moment someone replies and then get out of the way.
- **`--keep-going`:** loop forever; drafts accumulate in Chrome tabs. Ctrl-C exits cleanly with a session summary (`N iterations, M drafts this session`).

`engagement_seen.json` dedupes across iterations: once a comment URL has been drafted (or refused), subsequent iterations skip it. So watch-mode never opens the same composer tab twice.

---

## State and configuration files

| Path | Purpose | Editable by hand? |
|---|---|---|
| `~/.hermes/.env` | Azure base URL + API key + default model | yes (bootstrap stage 3 opens it) |
| `<repo>/BRAND.md` | Identity, voice rules, positioning — read on every draft | yes |
| `~/.hermes/memories/voice_profile.json` | Distilled tone/phrases/words from your corpus | regenerated by `scripts/voice-train.py` |
| `~/.hermes/state/engagement_seen.json` | Per-platform dedup state (drafted/refused URLs) | yes (remove entries to re-draft) |
| `~/.hermes/state/engagement_blocklist.json` | Hard URL blocklist (layer 3 defense) | yes |

---

## Output reference

A clean run looks like this:

```
=== X (Twitter) inbound ===
[x] blocklist active: 1 URL(s) in engagement_blocklist.json
[x] scanning @salaicreates's posts from the last 14 days ...
[x]   scroll #1: 4 articles loaded
[x]   scroll #2: 6 articles loaded
[x]   scroll #3: 8 articles loaded
[x]   scroll #4: 8 articles loaded
[x]   skipped 1 pinned post(s)
[x]   post 1/4 (1 replies, 0d ago): https://x.com/.../status/206...
[x]     2 third-party reply/replies queued
[x]     ↩ back to profile
...
[x]   post 4/4 (1 replies, 1d ago): https://x.com/.../status/206...
[x]     no third-party replies on this post (all self or already seen)
[x]     ↩ back to profile
[x] skipped 2 self-authored reply/thread continuation(s)
[x] 2 candidate item(s) collected

[x #1] Kevin Szabo | @KevinSzabo14 | · | Oct 8, 2023
        Reply URL: https://x.com/KevinSzabo14/status/...
        On post:   https://x.com/salaicreates/status/...
        Reply text: Less work more grind
        … LLM returned in 2.5s (HTTP 200)
        Draft:     Less work more grind is a vibe. If that means shipping faster
                   with fewer wasted cycles, I'm in.
        ✓ new tab open with composer pre-filled — review and submit in Chrome
```

The status lines (`scroll #N`, `↩ back to profile`, `2 third-party reply/replies queued`) are emitted via `print(..., flush=True)` so they stream in real time under `PYTHONUNBUFFERED=1`. Without unbuffered mode, stdout buffers and the file looks empty until the script exits.

---

## Failure modes and recovery

| Symptom | Cause | Fix |
|---|---|---|
| `[x] CDP Chrome unreachable on :9222` | Chrome not running | Script auto-runs `start-chrome-cdp` — wait. If it stays unreachable, run it by hand: `start-chrome-cdp` |
| `[x] (still 0 articles after 25s ...)` | X SPA failed to render any tweets | Usually transient — next iteration retries. Persistent: check that the CDP Chrome is logged in (`cdp_eval.py --expr 'JSON.stringify({logged_in: !!document.querySelector("[data-testid=SideNav_AccountSwitcher_Button]")})'`) |
| `composer_not_found` after 25s | The reply target's tweet detail SPA never rendered the composer | Usually X served a different layout (logged-out / restricted account / deleted tweet). Skip and move on. |
| `[li] not logged in` | Playwright session expired | `lipy login --headed` |
| `subprocess.TimeoutExpired` on `hermes chat` | (legacy path — no longer used) | Direct Azure path replaced this; this error shouldn't appear with current code |
| Empty `*.output` file from background task | Python stdout buffered when piped | Add `PYTHONUNBUFFERED=1` to the invocation |
| Same post visited every iteration | Profile scroll didn't load more articles | Bump `scroll_passes` (currently 4) or check that you actually have more recent posts within the 14-day window |
| Drafts feel off-voice | X reply corpus is thin (`corpus/x_replies.jsonl`) | Paste more of your past X replies into `corpus/x_replies.jsonl` (one JSON object per line) and re-run `scripts/voice-train.py` |
| Brand-guard refuses every draft | LLM stuck on em-dashes + hashtags | Most em-dashes auto-strip now; if hashtags keep appearing, edit the system prompt in `draft_reply()` to be more emphatic |
| Browser visibly opens many tabs and gets cluttered | Working as designed | Close stale tabs in CDP Chrome anytime; they don't affect script state. `--limit N` caps each iteration. |

---

## What's intentionally NOT in this script

- **No publishing.** Doesn't post anything. The CDP Chrome composer is pre-filled but never `.click()`'d on the Reply button. Compare to `skills/x-engage/reply.py --live` which does post.
- **No outbound goodwill.** Only handles inbound on your own posts. Outbound discovery (`relevance-scorer`) is a separate concern handled by schedule-tick.py in Phase 3+.
- **No "did I already manually reply?" detection.** Currently dedupes only via `seen.json` (what the *script* has drafted). If you reply to a comment outside the script, it's still in seen.json from the prior iteration (or it isn't — and the script will try to draft again next iteration). To prevent that: add the comment URL to `engagement_blocklist.json` after you reply manually.
- **No daily cap enforcement.** `caps.yaml: x.inbound.replies_per_day` is consulted by schedule-tick.py but ignored here. Don't run watch-mode all day with `--keep-going` without thinking about your rate limits.

These are all valid follow-ups. The script is intentionally minimal and dependency-light so it can demonstrate the engagement loop end-to-end without dragging in the full schedule-tick.py orchestration surface.

---

## Relation to schedule-tick.py

`scripts/schedule-tick.py` is the production-grade orchestrator (publish + monitor + reply, cron-driven). engagement-test.py is its **monitor-only sibling**, deliberately decoupled, optimized for the "I post manually; I want help drafting replies" workflow. Key differences:

| | `schedule-tick.py` | `engagement-test.py` |
|---|---|---|
| Discovery | Requires post URN/URL in state (only fires after the agent published) | Scrapes your profile timeline — no upfront knowledge needed |
| Drafter | `hermes chat --skills reply-drafter,brand-guard` (~7 min) | Direct Azure call (~10s) |
| Brand-guard | Full skill via Hermes | Inline (em-dash auto-strip, hashtag refuse, opener refuse) |
| Posting | Posts via `lipy reply` / `x-engage/reply.py --live` | Pre-fills composer, leaves submit to user |
| Schedule | Every-minute cron job, driven by `schedule.yaml` | One-shot OR `--watch` interval (default 90s) |
| Use case | Hands-off Phase 2+ autonomous mode | Manual / supervised engagement |

You can run both. They use different state files and don't conflict.

---

## Future work

- **LinkedIn headed-draft mode.** Currently LinkedIn drafts print to terminal for copy-paste. Add `lipy draft --parent X --text Y --headed` that opens a Chromium window with the comment composer pre-filled, parallel to the X behavior.
- **Already-replied-by-me detection.** Walk the reply tree on each candidate post and skip third-party replies that have `@salaicreates` nested under them. Would need DOM expansion of "Show replies" buttons.
- **Daily cap awareness.** Read `config/caps.yaml: x.inbound.replies_per_day`, count drafts in `audit.jsonl`, refuse to draft more once the cap is hit.
- **Cost meter.** Per-call token + dollar tracking. Roll up into a session summary at exit.
- **Schedule integration.** Optionally consult `schedule.yaml` to know which posts are in their 24h monitor window vs. which are older.
- **Promote to production.** When watch-mode has been stable for a few days, swap the `fill_composer_in_new_tab` step for `reply.py --live` and register as a cron job — full autonomous engagement.
