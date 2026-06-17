# feed-engagement.py — outbound goodwill + self-thread continuations

A second-stage engagement orchestrator that complements
[engagement-test.py](engagement-test.md). Where engagement-test handles
**inbound** (third-party replies on your posts), this script handles **outbound**
on two surfaces:

1. **Goodwill on the X home feed** — scan `x.com/home`, score posts by niche
   keywords, draft outbound comments on the top candidates, open a CDP Chrome
   tab per draft with the composer pre-filled. Designed to fire 30 min before
   each of your scheduled posts so your audience is warmed up when yours drops.

2. **Self-thread continuations** — find your most recent post (default within
   the last 60 min), draft a follow-up comment that extends your own thread
   in your own voice, open a composer tab on that post. Designed to fire 5–10
   min after you've posted manually.

Like `engagement-test.py`: **doesn't post anything.** Composer pre-filled,
submit click is yours.

**File:** [`scripts/feed-engagement.py`](../scripts/feed-engagement.py) (~750 LOC, single file)

---

## Quick usage

```bash
# Both modes, default limits — goodwill first, then self-thread.
./vendor/hermes-agent/.venv/bin/python scripts/feed-engagement.py

# Just goodwill (e.g., 30 min before posting), 5 drafts
./vendor/hermes-agent/.venv/bin/python scripts/feed-engagement.py \
    --mode goodwill --limit 5

# Just self-thread (e.g., 5 min after you posted), target last 30 min
./vendor/hermes-agent/.venv/bin/python scripts/feed-engagement.py \
    --mode self-thread --within-minutes 30

# Watch mode — poll every 10 min until first draft, then exit
./vendor/hermes-agent/.venv/bin/python scripts/feed-engagement.py \
    --mode goodwill --watch --interval-seconds 600

# Reset state, ignore any "already drafted" memory
./vendor/hermes-agent/.venv/bin/python scripts/feed-engagement.py \
    --reset-seen --mode goodwill --limit 3
```

**Common flags:**

| Flag | Default | Meaning |
|---|---|---|
| `--mode` | `both` | `goodwill` / `self-thread` / `both` |
| `--limit` | `5` | Max goodwill drafts per pass (self-thread is always 1) |
| `--min-score` | `1` | Min niche-keyword matches required for a feed post to qualify |
| `--scroll-passes` | `5` | Times to scroll the home feed before extracting (lazy-load) |
| `--within-minutes` | `60` | Self-thread: only target posts within last N min |
| `--watch` | off | Loop on interval |
| `--keep-going` | off | With `--watch`: don't exit on first draft |
| `--interval-seconds` | `300` | With `--watch`: seconds between polls |
| `--reset-seen` | off | Wipe `feed_engagement_seen.json` |

---

## Goodwill mode

### What it does

1. Open `x.com/home` in the existing CDP Chrome tab
2. Scroll the home feed N times (default 5) to load more posts
3. Extract all visible posts (~30–50 typical)
4. Filter out:
   - Your own posts (`@<handle>` appears in author field)
   - Promoted posts / ads
   - Posts in the URL blocklist
   - Posts already drafted-against (seen state)
   - Posts containing skip-signal keywords (crypto pump, self-help, drama, politics)
   - Posts scoring below `--min-score` niche-keyword matches
5. Sort surviving candidates by score descending
6. For the top `--limit` candidates:
   - Extract OP's first name + handle from author field
   - Draft a goodwill comment via direct Azure `/chat/completions` (~10s)
   - Run inline brand-guard
   - Open a new CDP tab at the post URL, poll for composer, insert text
   - Leave tab open

### Niche keyword scoring

The script ships a hardcoded keyword list pulled from BRAND.md positioning
+ audience sections. A post's score = count of these keywords appearing in
its text (case-insensitive substring match):

```
# Tools / stack
claude code, claude-code, anthropic, mcp, browser-use, openclaw,
hermes-agent, agentic, computer-use, ...

# Concepts
ai automation, workflow automation, agentic workflow, ai agents,
production ai, ship ai, infrastructure, operators, agency owner, ...

# Adjacent
playwright, cdp, chrome devtools, cron, daemon, openai, azure openai,
deepseek, model context protocol, ...
```

A post scoring **2** ("MCP for agentic workflows in production") ranks above
one scoring **1** ("just shipped a new feature"). Default `--min-score 1`.
Bump to `--min-score 2` for tighter targeting; drop to `0` (with caution) to
engage with anything (you probably don't want this — it's spammy).

### Skip-signal keywords

Hard refusal — posts containing any of these are skipped regardless of
niche score:

```
crypto pump:    $btc, $eth, $sol, memecoin, next 100x, to the moon
self-help:      morning routine, 5am club, grindset, sigma male, manifest
job seeking:    resume review, looking for a job, open to work
politics:       biden, trump, kamala, maga
drama:          drama, exposed, called out, feud
```

Edit `SKIP_KEYWORDS` in the script to tune.

### Goodwill prompt

Sent to Azure DeepSeek-V4-Flash with the voice context block:

```
You are Sal AI (@<handle>). You're leaving a goodwill comment on someone
ELSE's X post (an outbound engagement to build relationship with another
operator in your space). Match the voice/tone/vocabulary distilled below.
Open with the OP's first name only when natural. ADD a concrete experience,
counter-point, or share a relevant anecdote — do NOT just agree.
No em-dash. No hashtags. Never open with sycophantic opener. ≤280 chars.
Output ONLY the comment text.
```

---

## Self-thread mode

### What it does

1. Navigate to your profile timeline (`x.com/<handle>`)
2. Scroll a few times to ensure recent posts are loaded
3. Find the most recent post (skip pinned + blocklist) within `--within-minutes`
4. Skip if you've already drafted a continuation for it (seen state)
5. Draft a follow-up comment via Azure with a different system prompt (below)
6. Open a new tab at that post URL with the composer pre-filled

### Self-thread prompt

```
You are Sal AI (@<handle>). You just posted the following on X. Now draft a
follow-up comment on your own post — a thread continuation that extends YOUR
OWN argument with one more concrete point, an example, or a related observation.
This is your own voice continuing your own thread (the way you naturally extend
threads manually).
Do NOT repeat or paraphrase your original. Add something new.
No em-dash. No hashtags. ≤280 chars.
Output ONLY the follow-up comment.
```

The model gets your original post as context — it's expected to extend
naturally, not repeat or summarize.

### Why this mode exists

You already do this manually — most of your X posts have a self-authored
thread continuation that adds one more concrete point. The orchestrator
drafts that for you so you can review-and-post in 30 seconds instead of
writing it from scratch.

---

## Three-layer defense against engaging with the wrong content

Same defenses as engagement-test.py:

| Layer | Source | Catches |
|---|---|---|
| 1 | DOM `socialContext` "Pinned" detection | Pinned posts in profile scrape (self-thread mode only) |
| 2 | `~/.hermes/state/engagement_blocklist.json` (shared file) | Hard URL list — known-bad posts you never want engagement on |
| 3 | Skip-signal keywords + (self-thread) `--within-minutes` cutoff | Off-target content; stale posts |

Plus mode-specific filters:
- **Goodwill:** your own posts in feed, promoted posts, niche-score below threshold
- **Self-thread:** posts older than `--within-minutes`

---

## State files (separate from engagement-test.py)

| Path | Purpose |
|---|---|
| `~/.hermes/state/feed_engagement_seen.json` | This script's dedup state. Keys: `x_goodwill`, `x_self_thread` |
| `~/.hermes/state/engagement_blocklist.json` | Shared with engagement-test.py |
| `~/.hermes/memories/voice_profile.json` | Read for voice context |
| `<repo>/BRAND.md` | Read for niche keywords + voice rules |

The two scripts use **separate seen-state files** so a goodwill comment URL
doesn't accidentally prevent inbound-engagement-test from drafting on it later
(though they wouldn't conflict on real data because the URL types are different
— goodwill URLs are others' posts, inbound URLs are reply tweets).

---

## Schedule integration (planned, not yet wired)

User's posting schedule (SGT):

| Platform | Times |
|---|---|
| X | 02:00, 07:00, 09:00, 16:00, 18:00, 22:00 |
| LinkedIn | 17:00, 23:00 |

Goodwill should fire **30 min before each**:

| SGT | Mac mini Toronto (EDT, summer) |
|---|---|
| 01:30 | 13:30 (prev day) |
| 06:30 | 18:30 (prev day) |
| 08:30 | 20:30 (prev day) |
| 15:30 | 03:30 |
| 17:30 | 05:30 |
| 21:30 | 09:30 |

Plus LinkedIn goodwill at 16:30 + 22:30 SGT.

Self-thread should fire **5 min after each post** (gives the user time to
post manually before the agent tries to continue):

| SGT post | SGT self-thread fire |
|---|---|
| 02:00 | 02:05 |
| 07:00 | 07:05 |
| ... | ... |

Registering Hermes cron jobs for these is a follow-up step. Current code is
manually runnable — register via `hermes cron add` once you've watched a few
goodwill passes and are happy with the drafts.

---

## Configuration knobs

To tune behavior without code changes:

| Knob | Where | Notes |
|---|---|---|
| Niche keywords | `NICHE_KEYWORDS` constant in script | Edit list directly |
| Skip-signal keywords | `SKIP_KEYWORDS` constant in script | Edit list directly |
| Voice context | `BRAND.md` + `voice_profile.json` | Edit BRAND.md; re-run `voice-train.py` to regen profile |
| Daily draft cap (per pass) | `--limit N` | No per-day cap yet (TODO) |
| Polling cadence | `--interval-seconds N` | Default 300 (5 min) |
| Min niche score | `--min-score N` | Default 1; bump for tighter targeting |
| How aggressively to scroll | `--scroll-passes N` | Default 5 |

---

## Failure modes and recovery

| Symptom | Cause | Fix |
|---|---|---|
| `[x] CDP Chrome unreachable on :9222` | Chrome not running | Script auto-runs `start-chrome-cdp`; wait |
| `[x] nothing visible in feed` | Logged out OR algorithm-empty feed | Verify login (`cdp_eval.py --expr 'JSON.stringify({logged_in: !!document.querySelector("[data-testid=SideNav_AccountSwitcher_Button]")})'`). If logged-in, your home feed is just not rendering — refresh manually, try again. |
| `no goodwill candidates in current feed snapshot` | Niche-score threshold filtered all posts | Drop `--min-score` to 0 (carefully) OR add keywords to `NICHE_KEYWORDS` |
| `no fresh post within last Nm to continue` | Self-thread mode: you haven't posted recently | Bump `--within-minutes` or wait until you post |
| Same post visited every iteration | Profile or feed scroll didn't load more articles | Bump `--scroll-passes` |
| `composer_not_found` after 25s | Tweet detail SPA didn't render composer (logged-out / restricted account / deleted tweet) | Skip and move on |
| Drafts feel generic | NICHE_KEYWORDS too broad → matches off-niche posts | Tighten the list or raise `--min-score` |
| Drafts feel off-voice | X reply corpus is thin | Add more X replies to `corpus/x_replies.jsonl`, re-run `voice-train.py` |
| Brand-guard refuses every draft | DeepSeek keeps using hashtags / sycophantic openers | Em-dashes auto-strip; for the others, tune the system prompt or accept refusals — they're correct |

---

## Failure modes specific to goodwill

- **Feed is highly noisy** — X's algorithm is a black box. If your home feed is
  full of politics / drama / off-niche stuff, the niche-score filter will
  reject everything. Spend a few days following more operators in your
  space, then re-run.
- **Goodwill candidate is from an off-target account** — score-based filtering
  catches keyword matches, but not author quality. A crypto bro tweeting
  "my AI automation pipeline 100x'd" would score high on `ai automation`
  but is off-target. Manual tuning of `SKIP_KEYWORDS` is the current answer;
  a relevance-scorer skill is the long-term answer (see roadmap).

## Failure modes specific to self-thread

- **Drafted continuation paraphrases your original** — system prompt explicitly
  says "do NOT repeat or paraphrase", but LLMs sometimes ignore. Watch for
  this; if it happens often, tighten the prompt.
- **Multiple continuations** — script writes only ONE continuation per post
  (dedup via seen state). If you want a 2-deep thread, you'd need to either
  unset the seen entry or extend the script.

---

## Relation to engagement-test.py and schedule-tick.py

| | `engagement-test.py` | `feed-engagement.py` | `schedule-tick.py` |
|---|---|---|---|
| Direction | INBOUND (replies on your posts) | OUTBOUND (you commenting elsewhere) | Both (production-grade) |
| Goodwill | No | Yes | Yes (via `goodwill_minutes_before` in schedule.yaml) |
| Self-thread | No | Yes (`--mode self-thread`) | No |
| Posting | Pre-fills composer, you submit | Pre-fills composer, you submit | Posts via `--live` |
| LLM path | Direct Azure (~10s) | Direct Azure (~10s) | `hermes chat --skills` (~7+ min) |
| Schedule | Manual + watch mode | Manual + watch mode (cron is the planned next step) | Cron-driven (every minute) |
| Use case | Reply curation | Outbound engagement | Hands-off Phase 5+ |

All three are independent. Run them concurrently if you want.

---

## What's intentionally NOT in this script (yet)

- **No daily-cap awareness.** Doesn't read `caps.yaml: x.outbound.comments_per_day`. Watch your rate limits manually for now.
- **No relevance-scorer LLM call.** Topic scoring is plain keyword count, not LLM-graded. A `relevance-scorer` skill is on the Phase 3 roadmap; once it exists, swap the keyword scorer for it.
- **No LinkedIn.** LinkedIn goodwill needs a `lipy comment --headed-draft` mode (parallel to the X composer-pre-fill behavior); not built yet.
- **No "scroll until I find N candidates" mode.** Currently does `--scroll-passes N` fixed scrolls then extracts. A smarter version would scroll until at least `--limit` candidates qualify.
- **No "comment vs reply" detection.** Treats every post URL as a top-level comment target. Should be fine since we're commenting on top-level posts, not replies-of-replies.

---

## What I want to add next (suggestions for future me)

1. **Hermes cron registration script** — `scripts/install-goodwill-cron.sh` that registers the 8 daily fire times based on the user's schedule, converted to the mac's local TZ.
2. **`--linkedin` flag** that adds LinkedIn goodwill once `lipy comment --headed-draft` is implemented.
3. **Daily cap awareness** — read `~/.hermes/state/feed_engagement_seen.json`, count entries with today's UTC date, refuse to draft more once `caps.yaml: x.outbound.comments_per_day` is hit.
4. **Relevance-scorer integration** — once that skill exists, replace `topic_score()` with a real LLM call.
5. **Quiet-hours respect** — read `windows.yaml: x.outbound`, refuse to run outside the configured window. (Probably overkill for the manual-test phase; matters for cron mode.)
6. **Cost meter** — count tokens/dollars per session, print at exit.
