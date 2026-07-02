# Engagement Systems — Master Document & Continuation Guide

**Purpose:** complete reference for the X and LinkedIn autonomous engagement
systems — the methodology, the architecture, every script and its status, what
has been tested, and a precise build-plan for what remains. Written so a new
engineer or AI can continue the build without re-deriving context.

**Last updated:** 2026-07-02. **Branch:** `macmini-live`. **Runs on:** the Mac
mini (the always-on machine); this desktop is for editing/review.

> **2026-07-02 session update — READ-SIDE PROTECTION + RECOVERY RAMP (both
> platforms).** The account got view-rate-limited on X; root cause was read
> volume: every inbound check swept the profile + every permalink + back-nav
> (~13 loads / 100+ post views), up to ~19×/day — far past the free-tier view
> budget. New, applied identically to X and LinkedIn: (1) a `recovery:` ramp
> in `cadence.yaml` (weeks 1-2 no outbound + quiet-only inbound → weeks 3-4
> one small session → weeks 5-6 two sessions → normal; reduce-only vs
> baseline); (2) a **page-view budget** — all navigations logged as
> `page_view` events, capped via `caps.yaml <platform>.reads`; (3) a
> **rate-limit circuit breaker** — scrapers detect the throttle page, log
> `rate_limited`, cadence stands down for a jittered cooldown; (4) **cheap
> inbound checks on X** — mentions page only (1 load), full sweep once/day;
> (5) X windows narrowed to 09:00–22:00 (no more 3am sessions). See
> [human-cadence.md](human-cadence.md) § Recovery ramp.

> **▶ Setting this up on a NEW laptop / handing to a new agent?** Start with
> [NEW-MACHINE-RUNBOOK.md](NEW-MACHINE-RUNBOOK.md) — the step-by-step *executable*
> guide to install, log in, arm **both X and LinkedIn on the human-rhythm cadence**,
> and the must-read note on what "run automatically" actually does (it posts
> without asking). This master doc is the *why* / architecture reference behind it.

> **Commit status note.** Everything described here is committed + pushed on
> `macmini-live` (through the LinkedIn goodwill build). After a `git pull` on the
> Mac mini, re-run `./scripts/autonomy-mode.sh` to pick up the cadence crons.
>
> **2026-06-21 session update — LinkedIn goodwill is now UNBLOCKED + quality-gated.**
> `lipy feed` returns real home-feed posts (headed); LinkedIn outbound goodwill is
> wired into `cadence-tick`; `lipy like` exists and `outbox-flush` likes after every
> LinkedIn submit; the flaky inbound comment-scrape and reply/comment nav are
> hardened; and goodwill now runs a **quality judge** (skip promo/low-value/off-brand)
> + a proper outbound **goodwill drafter** before enqueueing (§8.1-note). The whole
> chain is dry-run-proven. What's left is a supervised LIVE goodwill comment+like —
> nothing posts to a real person without explicit per-post approval.

---

## 1. What this is

A personal brand-growth engagement engine for **X (Twitter)** and **LinkedIn**.
Original posts are authored elsewhere; this system does **engagement**:

1. **Outbound goodwill** — comment on (and on X, like) other people's posts.
2. **Inbound replies** — reply to comments people leave on *your* posts.

It runs as Hermes cron jobs driven by a human-rhythm scheduler, drafts in your
voice (LLM + `BRAND.md` + voice profile), validates every draft against
`brand-guard`, queues drafts in a hold-buffer for review, then posts via a real
browser. No platform APIs for the engagement path.

---

## 2. Methodology & philosophy (the "why")

These principles drove every design decision; preserve them.

### 2.1 Stealth = behavior, not tricks
Detection is probabilistic scoring over time. The dominant signal is **behavioral
cadence and volume**, not the mechanics of a click. So the highest-leverage
stealth work is realistic rhythm (Section 2.3), not input spoofing.

### 2.2 Lowest-detection transport: real browser + trusted input + your own IP
- **No residential proxy.** For one person automating their own account from
  home, your real IP is the *best* signal; a proxy looks *worse*.
- **Trusted input matters.** Web pages can read `event.isTrusted`. Real hardware
  input and **CDP `Input.dispatch*`** events are `isTrusted: true`; JS-synthesized
  events (`element.click()`, a naive extension content script) are `false` and
  detectable. So:
  - **X** drives a real Chrome over **CDP** (`Input.dispatchMouseEvent` /
    `Input.insertText`) → trusted.
  - **LinkedIn** drives Playwright via `lipy`; Playwright's `mouse`/`keyboard`
    also route through CDP under the hood → trusted.
  - A pure browser-extension content-script approach was considered and rejected
    for writes (untrusted events) unless it uses `chrome.debugger` (which itself
    is a signal).
- **Micro-humanization** (already built): Bezier mouse paths, per-character typing
  with ~1.2% typo+backspace, reading dwell, a "should I comment?" pause, eased
  scrolling. X: `skills/x-engage/x_human.py`. LinkedIn: `skills/linkedin-engage/human_actions.py`.

### 2.3 Human rhythm (the macro-cadence) — the key insight
Flat-interval polling (every 10/15 min, fixed daily times) is a robotic
signature. A human instead: logs in for **1–2 bursty sessions/day** at varying
times, comments on a handful of posts with human gaps, **skips some days**, and
checks comments on a fresh post **densely right after, then with decay**, not
forever. This is implemented by the cadence engine (Section 4 / 5.3). It is the
single biggest stealth improvement and applies to both platforms.

### 2.4 Risk asymmetry — start with inbound
Replying on **your own** posts looks like normal owner activity (low risk).
Outbound goodwill on **strangers** is the classic automation signature and
carries essentially all the risk — LinkedIn's *published* restriction rate for
automated commenters is **~23% within 90 days**. Therefore: inbound first;
goodwill is gated, lighter, and deliberately last.

### 2.5 Safety rails (keep all of them)
`config/caps.yaml` (live kill-switches + daily/weekly caps), `config/windows.yaml`
(active hours), `config/jitter.yaml`, the **hold-buffer** (drafts queue for
review before posting; flip `x.live`/`linkedin.live` to `false` to cancel all),
`brand-guard` (hard veto), and a fatigue/variance gate. The cadence sits *on top*
of these — it never bypasses them.

---

## 3. Architecture & data flow

```
DISCOVER            DRAFT                 GATE        QUEUE            SUBMIT
(read feed/        (LLM + voice +        (brand-     (outbox.jsonl,   (humanized
 mentions)   ───▶   length rules)  ───▶   guard) ─▶   hold buffer) ─▶  browser) ─▶ (X: +like)
   │                                                      │
   └── cadence-tick decides WHEN a human would do this ───┘   outbox-flush drains it
```

- **Transport differs per platform; everything else is shared.**
  - **X:** Chrome DevTools Protocol against a dedicated logged-in Chrome on
    `:9222` (`skills/x-engage/`). Reads + writes both over CDP.
  - **LinkedIn:** the `lipy` Playwright CLI (`skills/linkedin-engage/lipy.py`),
    invoked as a subprocess; persistent profile under
    `~/.hermes/state/playwright/linkedin/`.
- **Shared spine (platform-agnostic, key off a `platform` arg):**
  `scripts/_outbox.py`, `_caps.py`, `_metrics.py`, `_drafter.py`, `_cadence.py`,
  `activity-report.py`. All already accept `"x"` and `"linkedin"`.

---

## 4. Configuration reference

| File | What |
|---|---|
| `config/caps.yaml` | `phase`, per-platform `live` kill-switch, daily/weekly caps, `requires_approval_flag`, hold-buffer seconds, `llm` (DeepSeek-V4-Flash default / Pro escalation). |
| `config/windows.yaml` | Per-platform `inbound`/`outbound` active hours + weekdays. **X is currently 24/7** (widen/narrow here to bound cadence session hours). LinkedIn inbound Mon–Fri 08:00–19:00, outbound 09:00–17:00. |
| `config/jitter.yaml` | Per-platform/mode randomized inter-action delays. |
| `config/blocklist.yaml` | Accounts/keywords/domains to never engage. |
| `config/cadence.yaml` | **NEW.** Human-rhythm knobs per platform: `sessions_per_day`, `session_minutes`, `daily_actions`, `gap_minutes`, `skip_day_prob`; inbound `fresh_window_hours`/`dense_minutes`/`taper_*`/`quiet_minutes`. |
| `~/.hermes/.env` | Secrets (chmod 600). `AZURE_FOUNDRY_API_KEY/BASE_URL/MODEL`, `LI_USERNAME`, etc. Key is also committed in `.env.example` (private repo). |
| `BRAND.md` | Voice/values/off-limits. Read on every draft. `**Name:**` is used to skip your own comments. |

**Model policy:** default `DeepSeek-V4-Flash` (cheapest); escalate to
`DeepSeek-V4-Pro` for judgment-heavy calls (voice distillation, high-visibility
outbound). Same key/endpoint, only the model name changes. Via the
`/openai/v1` shim. See `51_AZURE_LLM_DEPLOYMENTS_AND_AGENT_RULES.md`.

---

## 5. Script catalog & status

Legend: ✅ built+tested · 🟡 built, partially tested · 🔴 built but blocked/not working · ⬜ not built

### 5.1 Shared spine
| Script | Role | Status |
|---|---|---|
| `scripts/_outbox.py` | Hold-buffer queue (`~/.hermes/state/outbox.jsonl`). enqueue/mature/mark_submitted/cancel. Platform-agnostic. | ✅ |
| `scripts/_caps.py` | `is_live` / `in_window` / `under_cap` / `hold_buffer_seconds` — read caps/windows/jitter by platform. | ✅ |
| `scripts/_metrics.py` | Append-only event log (`~/.hermes/logs/engagement_metrics.jsonl`) + today counters. Events: drafted, queued_outbox, submitted, liked, vetoed_brandguard, skipped_*, cadence_fire, session_logged_out, etc. | ✅ |
| `scripts/_drafter.py` | Length-target distribution + punctuation/length autofixes. | ✅ |
| `scripts/_cadence.py` | **NEW.** Human-rhythm engine: deterministic daily plan (per-machine salt), `should_act_outbound`, `inbound_due` (decay), `describe_day`. Pure logic; state from metrics. | ✅ (simulate + dry-run verified) |
| `scripts/cadence-tick.py` | **NEW.** One-cron driver (every minute). Fires one goodwill + one inbound action when a human would. `--simulate`, `--dry-run`. X **and** LinkedIn now do outbound+inbound (per-platform OUTBOUND command map). | ✅ X · ✅ LinkedIn (outbound + inbound) |
| `scripts/outbox-flush.py` | Drains mature outbox items, re-checks kill-switches, submits. X via CDP+`x_human` (+like every reply); LinkedIn via `_submit_one_li` → `lipy reply` (inbound) / `lipy comment` (goodwill), **+`lipy like` after every LI submit** (`_like_li_post`, non-fatal). Writes `heartbeat.json`. | ✅ X · ✅ LI inbound (live-proven) · ✅ LI goodwill submit path (feed unblocked; live post pending approval) |
| `scripts/activity-report.py` | Ledger + daily summary + liveness/heartbeat, from outbox+metrics. `--date/--days/--csv/--save`. | ✅ |

### 5.2 X engagement (CDP) — all built & working
| Script | Role | Status |
|---|---|---|
| `skills/x-engage/_cdp.py` | CDP session wrapper (now has `open_timeout`/`close_timeout`). | ✅ |
| `skills/x-engage/x_human.py` | Humanized CDP (mouse/typing/scroll/dwell). | ✅ |
| `skills/x-engage/cdp_eval.py`, `start-chrome-cdp.sh` | DOM eval + CDP Chrome bring-up. | ✅ |
| `skills/x-engage/publish.py` | Scheduled original posts. **Known dead-end:** raw DOM `.focus()` doesn't land text; not used by this engagement tool. | 🔴 (irrelevant) |
| `skills/x-engage/reply.py`, `fetch-comments.py` | X reply / comment read primitives. | ✅ |
| `scripts/feed-engagement.py` | X goodwill on the home feed + self-thread. | ✅ (live-proven: drafts + enqueues) |
| `scripts/inbound-engagement.py` | Replies to comments on your posts (X via CDP; LinkedIn via `lipy`). | ✅ X · ✅ LI inbound |
| `scripts/engage-commenter.py` | Engage your commenters' own audiences (X only). No longer auto-scheduled. | 🟡 X-only |
| `scripts/schedule-tick.py` | Scheduled-post orchestrator + 24h monitor (audit-log idempotent; tail-read + rotation + crash guard added). | ✅ |

### 5.3 LinkedIn engagement (lipy / Playwright)
| Script / command | Role | Status |
|---|---|---|
| `skills/linkedin-engage/lipy.py` | The LinkedIn driver. Commands: `login`, `doctor`, `status`, `session`, `posts`, `comments`, `comment`, `reply`, `publish`, `my-comments`, **`feed` (NEW)**. | mixed (below) |
| `lipy reply --parent <comment-urn> --text … --live` | Reply to a comment. | ✅ **live-proven** (posted to a real comment) |
| `lipy comment --post <activity-urn> --text … --live` | Top-level comment on a post (= goodwill submit). | ✅ (primitive exists; used by goodwill) |
| `lipy posts` / `lipy inbound` | Read your posts / comments on them. | ✅ reads work; ✅ comment-scrape **hardened** (retry the LazyColumn scroll up to 4× w/ growing dwell + wheel nudge) |
| `lipy feed` **(NEW)** | Scrape home feed for posts to goodwill-comment on. | ✅ **returns posts** — headed + per-card scrollIntoView; URN from inline-comment keys or the control-menu Embed/Report href; author from the control-menu aria-label (§8.1) |
| `lipy like` **(NEW)** | Like/react to a post (human-emulated). Default `--dry-run`; idempotent. | ✅ dry-run proven; live like pending per-post approval |
| `skills/linkedin-engage/human_actions.py` | LinkedIn humanization. | ✅ |
| `scripts/li-feed-engagement.py` **(NEW)** | LinkedIn goodwill orchestrator: `lipy feed` → **`judge_goodwill_post`** (quality gate) → **`draft_goodwill`** (outbound framing) → enqueue (mode=goodwill). | ✅ end-to-end dry-run proven; judge skips promo/off-brand, drafts additive on-topic (§8.1-note). Blocklist bug fixed. |

### 5.4 Orchestration / ops
| File | Role | Status |
|---|---|---|
| `scripts/autonomy-mode.sh` | One-time arm: flips caps live, registers cron. Now registers `cadence-tick` (X) + conditional `cadence-tick-li` (LinkedIn, only if `lipy` warmed) + `outbox-flush` + `daily-report`(→activity-report) + `voice-retrain`. LinkedIn gate is non-fatal. | ✅ |
| `scripts/start.sh` | One-command start: self-heal venv → CDP Chrome + X login → arm-if-needed → start gateway. | ✅ |
| `scripts/preflight.sh`, `setup.sh`, `scripts/bootstrap.sh`, `scripts/install-cua-driver.sh` | Fresh-laptop install chain (pinned Hermes, universal cua-driver, etc.). | ✅ |

---

## 6. State & logs (where to look)
- `~/.hermes/state/outbox.jsonl` — the queue + ledger (full draft text, target, author, status, timestamps).
- `~/.hermes/logs/engagement_metrics.jsonl` — event timeline (the report's data source).
- `~/.hermes/state/heartbeat.json` — liveness (chrome up? logged in? last run) — silent-stop detector.
- `~/.hermes/state/engagement_seen.json`, `li_feed_seen.json`, `commenter_queue.jsonl` — dedupe/queues.
- `~/.hermes/state/cadence_salt` — per-machine daily-plan seed.
- `~/.hermes/state/playwright/linkedin/` — LinkedIn session profile.
- `~/.hermes/state/chrome-cdp/` — X CDP Chrome profile.
- Watch it all: `python scripts/activity-report.py`.

---

## 7. What's TESTED (with evidence) vs REMAINING

### Tested & working
- **X cadence:** `--simulate` produced realistic days (skip days, 1–2 sessions, clustered comments); `--dry-run` decisions read real config/caps/metrics. ✅
- **X goodwill draft path:** live — browsed feed, LLM-judged a post, drafted in voice, brand-guard autofix, queued to outbox (then canceled). ✅
- **X posting + heartbeat:** earlier in the session — humanized typing lands text; heartbeat writes logged-in state. ✅
- **LinkedIn inbound — full chain, LIVE:** `lipy` repaired → read a real comment → drafted in voice → enqueued correctly (`platform=linkedin`, `parent_url`=comment URN) → **`lipy reply --live` posted a real reply** to John H. Lee's comment. ✅

### Done this session (2026-06-21)
- **LinkedIn goodwill feed discovery (`lipy feed`)** — ✅ unblocked. Returns real
  posts headed (urn+author+text); `li-feed-engagement` dry-run drafts end-to-end.
- **LinkedIn outbound wired into cadence** — ✅ §8.4. `cadence-tick --platform linkedin`
  now fires `li-feed-engagement` on the human-rhythm engine.
- **LinkedIn liking** — ✅ §8.2. `lipy like` added; `outbox-flush` likes after every
  LI submit. Dry-run proven; live like pending approval.
- **Inbound comment-scrape flakiness** — ✅ §8.3. `_scrape_comments` retries the
  LazyColumn hydration; `lipy inbound --limit 3` now reliably returns threads.
- **Reply/comment nav fragility** — ✅ §8.3. Direct-URL permalink is now primary
  (feed pre-context for referer); the flaky click-through is deprecated.

- **Goodwill DRAFT QUALITY** — ✅ DONE (§8.1-note). `li-feed-engagement` now runs
  `judge_goodwill_post` (fail-closed; skips promo/low-value/off-brand) before
  `draft_goodwill` (outbound additive framing). Dry-run proven: motivational post →
  judge NO (skipped); software/AI post → judge YES → additive draft.

### Remaining / not working
- **Live goodwill comment + live like** — untested end-to-end (outward → needs
  per-post approval). Discovery → judge → draft → outbox → submit + like are all in
  place and dry-run-proven; only the final supervised live post is pending.
- **LinkedIn `engage-commenter`** — ⬜ X-only.

---

## 8. REMAINING BUILD — continuation guide for the next engineer/AI

### 8.1 LinkedIn goodwill feed scraper (the big one) — ✅ RESOLVED 2026-06-21
**How it was solved** (`_scrape_feed` in `lipy.py`):
- Run **headed** (`lipy feed` now defaults to `--headed`; `--headless` to override).
  The home feed only renders to a non-headless session.
- The feed is the virtualized container
  `[componentkey="container-update-list_mainFeed-lazy-container"]`; its **direct
  children are the post cards**. Off-screen cards are *dehydrated*, so walk cards
  and `scrollIntoView({block:'center'})` **each** one, waiting ~1–1.8s, before
  reading it.
- **Author** comes from the control-menu button's `aria-label` — `"Open control
  menu for post by <Author>"` (EN) / `"Buka menu kawalan untuk paparan oleh
  <Author>"` (MS). Present on every real card, language-agnostic.
- **Body**: `[componentkey^="feed-commentary_"]` or `[data-testid="expandable-text-box"]`
  (the old `.update-components-*` BEM classes are gone).
- **URN** is *not* on the wrapper. Cheap path: regex `urn:li:(activity|ugcPost):\d+`
  over the card HTML (present when the card has inline comments / fired tracking).
  Authoritative fallback: open the **control menu** and read the **Embed/Report**
  item href (`?targetUrn=` / `?entityUrn=urn:li:share:…`), language-agnostic.
- **Promoted** filter: EN+MS — `Promoted|Sponsored|Dipromosikan|Ditaja`.
- Proven: `lipy feed --limit 4` returns 4 posts; `li-feed-engagement` dry-run drafts
  end-to-end. Also fixed a blocklist bug (`_blocked_terms` iterated the `handles`
  dict → "x" as a substring blocked nearly every post).

> **§8.1-note — goodwill draft quality: ✅ DONE 2026-06-21.** Originally the reused
> inbound `draft_reply` produced a dismissive `"No thanks, I'll pass"` on a spammy
> promo. Fixed in `li-feed-engagement.py`: each post is now gated by
> `judge_goodwill_post` (LLM, **fail-closed** → 'no' on any error; requires
> `decision=yes` AND `confidence ≥ JUDGE_MIN_CONFIDENCE` (0.55)) which skips
> promo / lead-magnets / engagement-bait / low-value / off-brand, and is permissive
> on substantive tech/AI/operator posts. Only yes-verdicts reach `draft_goodwill`,
> an outbound "add a concrete point, don't just agree" drafter (vs the inbound
> framing). Skips log `skipped_judge`. Tune the bar via `JUDGE_MIN_CONFIDENCE` and
> the judge's NO-list. Mirrors X's `judge_audience_alignment` + `draft_goodwill_comment`.

**Original problem (for reference; confirmed via DOM probes 2026-06-21):** `lipy
feed` navigated to `linkedin.com/feed/` and found **zero** post cards in
**headless** mode.
Findings:
- The page loads (URL/title correct) but renders only the sidebar; the feed is a
  **lazy/virtualized container**: `[componentkey="container-update-list_mainFeed-lazy-container"]`.
- `[data-urn]`, `[data-id]`, `div.feed-shared-update-v2`, `.update-components-actor__title`,
  and `a[href*="urn:li:activity:"]` all returned **0** even after aggressive
  scrolling. `[componentkey]` returned 182 elements but they were comments
  (`replaceableComment_…`), translations, and UUIDs — **not** extractable post
  cards with activity URNs.
- The user's LinkedIn UI is in **Malay** → never rely on visible-text selectors;
  use attributes only.
- Contrast: the **recent-activity** page (`/in/me/recent-activity/all/`) *does*
  render `[data-urn^="urn:li:activity:"]` headless — that's why `lipy posts`/
  `inbound` work. The **home feed** is gated harder.

**Recommended approach (next attempt), in order:**
1. **Headed mode.** Run the feed scrape with a visible browser (`lipy feed` →
   pass `--headed`; `_scrape_feed` already accepts the flag). LinkedIn renders
   the real feed far more reliably to a non-headless, warmed session. The
   supervised reply ran headed and worked. Make `li-feed-engagement` invoke
   `lipy feed --headed`, and consider running the whole LinkedIn path through the
   long-running `lipy session` daemon (CDP-attached, warm context) rather than a
   cold per-call launch — the docs note cold single-shot looks suspicious.
2. **Find the real card selector in headed mode.** Re-run the probe pattern in
   `b92x2jzx7`/`bvseqlpoi` (see git history / this doc) but headed: enumerate
   `[componentkey]` values and the lazy container's children; look for the
   per-post wrapper. Feed post URNs on current LinkedIn are most reliably pulled
   from the post's overflow-menu/permalink or a `data-*` on the update wrapper
   once it actually renders. Extract `urn:li:activity:\d+`, the author
   (`.update-components-actor__*` or the actor link), and the body
   (`.update-components-text` / `.feed-shared-update-v2__description`).
3. **Scroll-into-view per item** (LazyColumn): don't just `mouse.wheel`; for each
   candidate, JS `scrollIntoView({block:'center'})` then wait ~1–2s for hydration
   (this is the same trick `_scrape_comments` uses, see lipy.py around the
   `scrollIntoView` call — reuse it).
4. **Fallback discovery without the home feed** (if the feed stays hostile):
   curated-source goodwill — comment on posts from a hand-listed set of
   people/companies, or a hashtag/search results page. This sidesteps the
   most-gated surface and is lower-risk anyway. Would need a small
   `lipy posts --profile <urn>` or `lipy search` (neither exists yet).

**Once feed discovery returns posts:** the rest is done — `li-feed-engagement.py`
already drafts + enqueues (mode=goodwill), `outbox-flush._submit_one_li` already
submits goodwill via `lipy comment --post`, and you just flip on LinkedIn
outbound in `cadence-tick.py` (currently it `skip`s outbound for non-X — search
`"no goodwill/feed path for"`).

### 8.2 LinkedIn liking — ✅ BUILT 2026-06-21
`lipy like --post <urn>` (`cmd_like`): feed pre-context → direct nav → human-click
the reaction toggle → verify the state flip. Button found language-independently
(`_find_like_button`: aria-label has `reaction`/`reaksi`, not the `…menu` trigger;
first match = post action bar). Default `--dry-run`; idempotent (`_reaction_is_set`
checks "no reaction"/"tiada"). `outbox-flush._submit_one_li` calls `_like_li_post`
after every successful LI submit (goodwill → the post; inbound → parent post from
the comment URN), non-fatal, mirroring X. Dry-run proven; live like pending approval.

### 8.3 LinkedIn read reliability — ✅ HARDENED 2026-06-21
- **Inbound comment-scrape:** `_scrape_comments` now retries the comments-anchor
  `scrollIntoView` up to 4× with a growing dwell + wheel nudge, breaking as soon
  as a `replaceableComment_` wrapper renders. `lipy inbound --limit 3` reliably
  returns threads now (was hit-or-miss).
- **Reply/comment nav:** direct-to-permalink is now the **primary** path (feed
  pre-context for referer realism); the flaky click-through (`navigate_to_own_post`)
  is deprecated/unused. cmd_comment also goes direct (goodwill targets strangers'
  posts, which never appear on the own-activity page).

### 8.4 Cadence for LinkedIn outbound — ✅ WIRED 2026-06-21
`cadence-tick.py` now drives outbound via a per-platform `OUTBOUND` command map:
`x`→`feed-engagement.py`, `linkedin`→`li-feed-engagement.py --max-comments 1
--limit 5 --live` (600s timeout). `_cadence.should_act_outbound` reads
`cadence.yaml: linkedin.outbound`. The `cadence-tick-li` cron (autonomy-mode.sh)
already passes `--platform linkedin`. Firing only DRAFTS into the outbox;
`outbox-flush` posts later under caps + per-post approval. **Note:** with §8.1-note
unresolved, goodwill draft quality is weak — keep `linkedin.live: false` (or rely
on the hold-buffer + per-post approval) until the goodwill judge is built.

### 8.5 Lower-priority deferred items
- `outbox-flush` stale-lock window (15 min) — make PID-aware to remove a tiny
  double-post risk.
- `engagement_metrics.jsonl` grows unbounded — give `_metrics.iter_events` a
  tail-read like `schedule-tick`'s audit log got.
- `engage-commenter.py` LinkedIn variant — not built.

---

## 9. Operational runbook (Mac mini)

```bash
git pull                       # get latest macmini-live
./scripts/autonomy-mode.sh     # REQUIRED after a pull — swaps to cadence crons;
                               #   arms LinkedIn inbound only if `lipy status` shows logged in
./scripts/start.sh             # routine start (self-heals venv, Chrome+login, gateway)
```
- Watch: `./vendor/hermes-agent/.venv/bin/python scripts/activity-report.py`
- Halt a platform instantly: `config/caps.yaml` → `x.live`/`linkedin.live: false`.
- Inspect a simulated cadence day: `scripts/cadence-tick.py --simulate <YYYY-MM-DD>`.
- LinkedIn login (if `lipy status` not logged in): `lipy login --headed`.

---

## 10. Critical gotchas (read before touching anything)

1. **Folder moves break the venvs.** Moving the project dir bricks Hermes *and*
   `lipy` (absolute paths baked into console-script shebangs + the editable
   finder + the `~/.local/bin/lipy` wrapper). `start.sh` self-heals Hermes;
   `skills/linkedin-engage/install.sh` (or rewriting the wrapper paths) fixes
   `lipy`. Prefer not to move the folder.
2. **Two separate venvs.** Hermes venv (`vendor/hermes-agent/.venv`) runs the
   scripts but has **no Playwright**; `lipy` runs in
   `skills/linkedin-engage/.venv` (has Playwright). Run lipy DOM probes with the
   *lipy* python, not the Hermes one.
3. **LinkedIn home feed ≠ recent-activity page.** Headless renders the latter,
   not the former (§8.1). Test feed work **headed**.
4. **Malay UI** on this account — attribute selectors only, never visible text.
5. **`live` is outward.** Any `--live` LinkedIn/X post hits a real account; a
   guardrail will (correctly) require explicit per-post approval for replies to
   real people.
6. **The cadence gates DRAFTING, not posting directly.** Drafts mature in the
   hold-buffer; `outbox-flush` posts them. Visible cadence ≈ draft cadence +
   hold buffer. LinkedIn outbound is currently NOT cadence-fired (§8.4).
