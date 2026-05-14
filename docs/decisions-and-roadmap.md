# Decisions & roadmap

Why we made the architectural choices we did, and what's next.

## Key decisions

### 1. Hermes Agent as the backbone

**Alternatives considered:** OpenClaw (more general-purpose, 347K stars), NVIDIA NeMo Agent Toolkit (an observability/instrumentation layer that works WITH frameworks, not the framework itself), building from scratch on the Anthropic / Azure SDK.

**Why Hermes:**
- Released Feb 2026, ~60K stars in two months — fastest-growing OSS agent in 2026.
- Provides everything we'd otherwise build: daemon, cron, memory (SQLite+FTS5), CLI, multi-channel gateway (Telegram/Discord/etc.), 87 built-in skills, pluggable LLM providers.
- Already ships a social-media skill scaffold using `xurl` (the X dev platform team's official CLI). One platform of three is done for us.
- Open-standard skill format (`agentskills.io`) — portable, future-proof.
- MIT license, no telemetry, fully local.

**Cost:** ~99 MB vendored repo. Worth it for what it provides.

### 2. Vendor Hermes rather than installing globally

User preference: "I'd much rather have the code files local here." The cloned repo lives at `vendor/hermes-agent/`, and we run from its venv at `vendor/hermes-agent/.venv/bin/hermes`.

**Why:**
- Full visibility into what runs.
- No system-wide footprint.
- Easy to pin to a specific Hermes version; survives `hermes update` choices.
- The official `curl ... | bash` installer was blocked by safety checks; this path works around that cleanly.

### 3. Azure OpenAI via Hermes' `azure-foundry` provider

**Why Azure (and not Anthropic or OpenRouter directly):**
- The user already has Azure deployments configured for other projects (`gpt-5.1-chat`, `gpt-5.2-chat`, `gpt-5.3-chat`, `gpt-5.4`), with usable keys in `51_AZURE_LLM_DEPLOYMENTS_AND_AGENT_RULES.md`.
- All four deployments verified working via the `/openai/v1/` OpenAI-compatible endpoint, which Hermes' `azure-foundry` plugin uses.
- Existing Azure tenancy = no new billing setup.

**Why `gpt-5.1-chat` as default:**
- Cheapest of the four; accepts custom `temperature`; no minimum-token quirk.
- Fine for most reply drafting and classification work.
- We can switch to 5.4 for high-stakes drafts (`hermes chat --provider azure-foundry -m gpt-5.4 -q "..."`).

**Per-skill recommendations:**

| Skill | Recommended model | Why |
|---|---|---|
| `reply-drafter` | `gpt-5.4` | Voice mimicry benefits from controllable temperature; only 5.4 supports non-1 temp |
| `voice-profile` (training pass) | `gpt-5.3-chat` | Reasoning-style, one-shot quality matters |
| `brand-guard` | `gpt-5.1-chat` | High-volume, rule-driven; cheap + fast |
| `spam-classifier` | `gpt-5.1-chat` | High-volume classification |

### 4. Human emulation built on Playwright (not Selenium / pyppeteer)

**Why Playwright:**
- Modern API, sync + async modes.
- Excellent persistence (`launch_persistent_context`) for the user-data-dir pattern.
- CDP-attach support via `connect_over_cdp` — lets other processes attach to a running browser, which we use for `lipy session`.
- Healthy `playwright-stealth` ecosystem.
- Cross-platform.

### 5. Persistent Chromium profile, not state.json reloads

The early implementation used `launch_persistent_context` per-action with a saved `storage_state.json`. This caused LinkedIn to fire "Remember this device?" prompts every run.

**Switched to:** `launch_persistent_context(user_data_dir=...)` with a single profile directory. The browser's full state (history, IndexedDB, fingerprint, "remember this device" status, language settings) persists between runs. After one successful login + "yes remember this device," subsequent runs don't trigger 2FA.

Profile lives at `~/.hermes/state/playwright/linkedin/profile/`. We also save a portable `state.json` backup for emergencies.

### 6. Long-running session daemon (`lipy session`) with CDP attach

Originally each `lipy` command launched its own Chromium. Pattern: launch → act → close. **Five seconds of useful work per fifteen seconds of overhead, and to LinkedIn it looks like a fresh device on every action.**

**Switched to:** `lipy session` opens a long-running visible Chromium. Other commands attach via CDP (Chrome DevTools Protocol) on port 9222. One warm browser, many actions. The user can also use the browser themselves between agent commands.

This was the single biggest improvement to "human-likeness" — one continuous session vs. boot-act-close per command.

### 7. Navigate-by-click vs direct URL goto

User strongly preferred click-through navigation (no `page.goto(post_url)`). We built `navigate_to_own_post()` to do this via the activity page.

**Discovery:** LinkedIn blocks programmatic clicks on the activity-page navigation buttons. Three click methods (Bezier mouse, plain Playwright click, JS `element.click()`) all fail silently — URL doesn't change, comments don't expand. See [linkedin-engineering.md](linkedin-engineering.md) for the full investigation.

**Pragmatic decision:** Direct URL goto INSIDE the running session, after we've been on the feed/activity page. This is far better than the original "launch fresh, goto, comment, close" pattern, even if it isn't pure click-through. We mitigate with everything else (session warmth, dwell, jitter, rate caps).

We left the click-through code in place — if a user manually navigates to a post in the visible browser, `lipy reply` detects this and skips its own nav entirely. That gives 100% click-through when the user is in the loop, and graceful fallback when not.

### 8. URN cache (activity ↔ ugcPost)

Discovered: LinkedIn uses two URN forms for the same post (`urn:li:activity:X` vs `urn:li:ugcPost:Y`), with different numeric IDs, no client-side mapping.

**Built:** Auto-populated cache at `~/.hermes/state/playwright/linkedin/urn_map.json`. Whenever `lipy comments` or `lipy inbound` runs, we learn the pairing from the page URL + comment URN substrings.

### 9. Default-to-dry-run for write ops

`lipy reply` and `lipy comment` default to dry-run mode. They type into the textbox but do NOT submit. You must pass `--live` explicitly to actually post. Prevents accidental posts from typos.

### 10. Voice rules captured per-feedback

The user is explicit about voice (no em dashes, first name only for addressing, name-tag + one sentence + closing question). We logged these as feedback memory at `~/.claude/projects/.../memory/feedback_voice_rules.md`. When `reply-drafter` and `brand-guard` are wired up, these become enforced rules.

## What's working today (2026-05-14)

✅ Foundation:
- Hermes installed, Azure wired, model verified end-to-end
- Project skills written and linked into `~/.hermes/skills/`
- Long-running browser session with CDP attach
- BRAND.md template + all config files in place
- Documentation (this folder)

✅ LinkedIn — read:
- `lipy posts` (your recent posts) — **post body extraction fixed 2026-05-14** after the activity-page DOM rolled and `componentkey="feed-commentary_*"` was dropped from that view. Now uses `.update-components-text` with two fallbacks (see [linkedin-engineering.md](linkedin-engineering.md))
- `lipy comments` (comments on a single post)
- `lipy inbound` (combined; same post-body fix applies)
- `lipy my-comments` (your outbound history → corpus)

✅ LinkedIn — write:
- `lipy reply --live` (human-emulated reply to a specific comment, posted live one real reply to Farouk Hajjej on 2026-05-11)
- `lipy comment --live` (human-emulated top-level comment on a post — same code path, untested live)
- Both default to `--headed=True`, so a visible Chromium opens on every write

✅ X (Twitter) — read + write proven live (added 2026-05-14):
- **Live test**: drafted and posted "Vadim haha what made you skeptical?" as a reply to @VadimStrizheus's "We will see haha" mention. X returned "Your post was sent" toast + the post-publish Premium upsell modal. Same shape as the May 11 LinkedIn live test to Farouk Hajjej.
- **Architecture**: dedicated Chrome instance at `~/.hermes/state/chrome-cdp/` (separate from user's normal Chrome — Chrome 121+ refuses CDP on default profile), launched with `--remote-debugging-port=9222`. Reads via CDP `Runtime.evaluate` against the DOM (`article[data-testid="tweet"]`, `button[data-testid="reply"]`, etc.); writes via CDP `Input.insertText` for the composer + `Runtime.evaluate` with `userGesture=true` to click submit. Screenshots via CDP `Page.captureScreenshot` (more reliable than macOS screen capture for backgrounded windows).
- **AX-tree read approach was deleted** — Chrome lazily evicts the AX tree for backgrounded SPA web content, too flaky to depend on. The CDP DOM path via [`skills/x-engage/cdp_eval.py`](../skills/x-engage/cdp_eval.py) is deterministic and the only X read path in the project today. AX-tree-reader code lives in git history; do not resurrect without a strong reason. See [x-engineering.md](x-engineering.md) appendix for the diagnostic.
- See [feedback_x_cdp_recipe](../../.claude/projects/-Users-salsmacos-Desktop-projects-brand-growth-engine/memory/feedback_x_cdp_recipe.md) for the exact CDP commands that worked.

✅ Voice corpus:
- 45 unique outbound comments in `corpus/linkedin_comments.jsonl`
- Spans short (~11 ultra-brief), medium (~19 typical), long (~15 substantive)
- 27 replies + 18 top-level

## How a real engagement runs today (manual happy path)

This is the **only** end-to-end flow that works. The autonomous loop does not exist yet.

1. **Read (headless, automated).** `lipy inbound --limit 5` runs in the background — no browser window visible. Returns JSON of your recent posts + comments. ~30s.
2. **Decide (manual).** You (or an LLM in chat) read the JSON, see whether there's a fresh unanswered comment. If not, the flow stops. If yes, you pick the comment URN to reply to.
3. **Draft (manual).** You write the reply text — applying voice rules manually (first name only, no em-dashes, name-tag + one sentence + closing question). `reply-drafter` exists as a SKILL.md but **is not called from any code path today**.
4. **Validate (skipped).** `brand-guard` is not yet called. No automated hard-veto runs. You are the brand guard.
5. **Post (headed, automated).** `lipy reply --live --parent <urn> --text "..."` opens a visible Chromium, navigates to your activity page, falls back to direct URL goto (LinkedIn blocks programmatic click-through from the activity page), finds the parent comment, opens the inline reply box, types character-by-character with Bezier-curve mouse, dwells, clicks Reply. ~75s.
6. **Enforce (skipped).** Caps in [config/caps.yaml](../config/caps.yaml), windows in [config/windows.yaml](../config/windows.yaml), jitter in [config/jitter.yaml](../config/jitter.yaml), blocklist in [config/blocklist.yaml](../config/blocklist.yaml) are all **declarative only** — `lipy reply` does not read them. You are the rate limiter.

Net: today the system saves typing and produces a clean audit trail of one writes. It doesn't yet save deciding, drafting, or guarding. Those are Phase 1+ work.

## What's NOT working yet

❌ Pure click-through navigation from the activity page (LinkedIn blocks it; we fall back to direct URL goto inside the running session)

❌ `voice-profile` distillation (SKILL.md ready, not run yet — needs an LLM call against corpus; `~/.hermes/memories/voice_profile.json` does not exist)

❌ `reply-drafter` integration (SKILL.md ready, no code path calls it — drafting today is manual)

❌ `brand-guard` enforcement (SKILL.md ready, not yet a hard veto in any code path — also would refuse today because [BRAND.md](../BRAND.md) still has unfilled placeholder fields)

✅ X (Twitter) read + write proven live 2026-05-14 — see "What's working today" above. The original `xurl` API plan is deprioritized in favor of `skills/x-engage` (browser + CDP route). X auto-reply approval (per Feb 2026 X policy) still required for **autonomous** mode, gated on `X_AUTO_REPLY_APPROVED` in `~/.hermes/.env`. Manual mode (one reply at a time, user reviews each draft) is working without approval.

❌ YouTube integration (SKILL.md + helper scripts written, OAuth not done)

❌ Autonomous cron mode (`hermes cron` exists, no jobs defined yet)

❌ Activity caps enforcement in `lipy reply` (caps are in `caps.yaml` but only declared; not yet checked before each action)

❌ The 5-minute hold buffer for outbound (config exists, not implemented in code)

❌ `n_comments` count on activity-page posts with zero comments (regex doesn't match when no "X comments" text is present — returns `null`. Cosmetic; doesn't affect functionality.)

## Phased roadmap

### Phase 1 — voice + drafting (next)

Goals: turn the 45 corpus samples into an active voice profile, and have the agent draft replies in your voice.

- [ ] Run `voice-profile` skill against `corpus/linkedin_comments.jsonl` + `BRAND.md`. Outputs `~/.hermes/memories/voice_profile.json`.
- [ ] Wire `reply-drafter` to read the voice profile + call Azure gpt-5.4 with the parent-comment + voice context. Returns DRAFT or REFUSE.
- [ ] Wire `brand-guard` as a hard-veto step inside `reply-drafter`. Reject drafts with em-dashes, sycophantic openers, AI tells, off-limits topics.
- [ ] Build `lipy draft-reply --parent <urn>` — pulls parent, calls reply-drafter, prints the draft for you to review. You then run `lipy reply --live --parent ... --text "<draft>"`.

### Phase 2 — YouTube own-channel moderation

Lowest-ToS-risk platform; legitimate API.

- [ ] Set up Google Cloud OAuth credentials (web console).
- [ ] Run `python skills/youtube-engage/youtube_auth.py` once to bootstrap refresh token.
- [ ] Implement the comment-moderation pass (list new comments, classify with spam-classifier, delete or reply).
- [ ] Dry-run for 7 days.
- [ ] Flip `youtube.live=true` in `caps.yaml`.

### Phase 3 — X via browser (cua-driver read + Hermes browser_cdp write)

Pivoted 2026-05-14 from `xurl` (paid API + Feb 2026 approval) to the
**browser-driven path** so it works today on the user's real Chrome.

- [x] Read path: `skills/x-engage/cdp_eval.py` extracting mentions in JSON via CDP `Runtime.evaluate` against the DOM
- [x] Write path: CDP `Input.insertText` + `Runtime.evaluate` w/ `userGesture: true` on `tweetButtonInline` — proven live with the Vadim reply on 2026-05-14
- [ ] User one-time setup: launch Chrome with `--remote-debugging-port=9222`
      then `/browser connect ws://localhost:9222/devtools/browser` in Hermes
- [ ] Wire `reply-drafter` for X via the SKILL.md flow
- [ ] Manual happy-path demo: read mentions → draft → type into composer → dry-run
- [ ] Live demo: same flow with `--live` flag → real reply posted to a real mention
- [ ] Add `caps.yaml: x.live = true` gate, daily caps enforcement
- [ ] Apply for X auto-reply approval in parallel (kept as a long-running gate
      for autonomous mode per Feb 2026 X policy; not required for human-in-the-loop
      manual replies)
- [ ] Keep `xurl` (Hermes' bundled X API skill) as fallback option if the
      browser route hits sustained issues

### Phase 4 — LinkedIn outbound

Highest-risk. Need everything else stable first.

- [ ] `lipy feed-scan` to find outbound candidates (high-relevance posts from others).
- [ ] Wire `relevance-scorer` skill.
- [ ] `RISK_ACCEPTED.md` signed.
- [ ] Residential proxy ($50–150/mo) configured.
- [ ] Canary at 5 outbound comments/day for 2 weeks before ramping to 25/day.

### Phase 5 — Autonomous orchestration

Hermes cron firing the engagement loop unattended.

- [ ] `hermes cron add` jobs for each platform, with the schedules in `caps.yaml`.
- [ ] 5-minute hold buffer implemented (drafts visible in dashboard for 5 min before auto-post).
- [ ] Daily cost-meter rollups (Azure spend + X API spend).
- [ ] Weekly voice-profile retrain (Sunday 03:00).
- [ ] launchd plist so the Hermes gateway survives reboots.

## Real-world results so far

- **1 live LinkedIn reply** posted via full human emulation (to Farouk Hajjej's question on the Stitch/Gemini post). User watched the Chromium window do the typing in real time. Comment count went 3→4. Reply is up and posted.
- **45 voice samples** in the corpus, ready for distillation.
- **Zero account warnings or restrictions** so far.
- **Zero accidental posts.**

## Things to remember when picking this back up

1. **The session is the unit.** Always start with `lipy session` and run other commands while it's up. Single warm browser is doing 90% of the ban-protection work.
2. **Dry-run is default for writes.** Won't accidentally post.
3. **The URN cache is yours to inspect.** `cat ~/.hermes/state/playwright/linkedin/urn_map.json` if a navigation feels off.
4. **LinkedIn DOM changes.** When a selector breaks, look in [linkedin-engineering.md](linkedin-engineering.md) for the documented patterns, run with `--debug` to dump the live DOM, update selectors. Re-document any changes.
5. **Voice rules go in `BRAND.md`.** The current explicit ones (no em-dashes, first name only) live as feedback memory — they should migrate into `BRAND.md`'s "Off-limits phrasings" section before brand-guard goes live.
6. **The Azure key file is gitignored.** `51_AZURE_LLM_DEPLOYMENTS_AND_AGENT_RULES.md` contains live keys. Don't push to a public remote.
