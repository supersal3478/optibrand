# `lipy` CLI reference

Every command, every flag.

`lipy` lives at [`skills/linkedin-engage/lipy.py`](../skills/linkedin-engage/lipy.py). It's symlinked to `~/.local/bin/lipy` (not on your PATH by default — use the full path or the venv binary).

**Invoke as:** `cd ~/Desktop/projects/brand-growth-engine/skills/linkedin-engage && .venv/bin/python lipy.py <command>`

All commands emit JSON to stdout. Errors emit JSON to stderr and exit non-zero.

---

## `lipy session`

Start a long-running Chromium window. Other `lipy` commands attach via CDP (Chrome DevTools Protocol) instead of launching fresh browsers. Foreground command — leave the terminal open until you're done.

```bash
.venv/bin/python lipy.py session [--port PORT]
```

| Flag | Default | Purpose |
|---|---|---|
| `--port` | 9222 | CDP port for inter-process attachment |

**Behavior:**
- Launches Chromium with the persistent profile, navigates to `linkedin.com/feed/`
- Prints PID + port banner to stderr
- Writes `session.pid` and `session.port` marker files
- Blocks until Ctrl+C or until the browser window is closed
- Cleans up marker files on exit

**Why:** The persistent profile + CDP attach pattern means one warm browser session for many actions, vs. launching fresh chromium per command. Vastly more human-like to LinkedIn, and ~5s per command instead of ~15s.

---

## `lipy session-stop`

Stop the running daemon.

```bash
.venv/bin/python lipy.py session-stop
```

Reads the PID file, sends SIGTERM, cleans up markers. Returns `{"ok": true, "stopped_pid": <pid>}`.

---

## `lipy login [--headed]`

One-time interactive LinkedIn login. **Always requires `--headed`** (browser must be visible so you can type password / complete 2FA).

```bash
.venv/bin/python lipy.py login --headed
```

**Behavior:**
- Launches Chromium (headed)
- Navigates to `linkedin.com/login`
- Polls every 1s for ANY of the following success signals (up to 15 min):
  - Page URL contains `/feed`, `/in/`, `/notifications`, `/messaging`, or `/jobs`
  - `li_at` auth cookie is set
- On success: saves to the persistent profile + writes a portable `state.json` backup
- On timeout (15 min): exits with error

**Why dual signal:** Some flows redirect to onboarding/welcome pages after login. Detecting the cookie is more reliable than detecting the URL.

**Re-run:** When the session goes stale (typically every 4–8 weeks, or after a security event). The persistent profile usually keeps you logged in for weeks.

---

## `lipy doctor`

Health check. Verifies the persistent profile exists, attempts a quick auth check, reports state.

```bash
.venv/bin/python lipy.py doctor
```

Output:
```json
{
  "ok": true,
  "session_present": true,
  "session_age_days": 0.0,
  "proxy_configured": false,
  "username_configured": false,
  "checks": [{"name": "auth", "status": "OK"}],
  "auth_reason": "ok",
  "landing_url": "https://www.linkedin.com/feed/"
}
```

A FAIL on `auth` usually means you need to re-run `lipy login --headed`.

---

## `lipy status`

Brief status without launching a browser (cheap; safe to call frequently).

```bash
.venv/bin/python lipy.py status
```

Reports: profile presence, age, whether the daemon is running, its PID and CDP port.

---

## `lipy posts --limit N [--headed]`

Scrape your own recent posts from `linkedin.com/in/me/recent-activity/all/`.

```bash
.venv/bin/python lipy.py posts --limit 5
```

| Flag | Default | Purpose |
|---|---|---|
| `--limit` | 5 | Max posts to return |
| `--headed` | false | Visible browser (only matters if no daemon) |

Each post:
```json
{
  "urn": "urn:li:activity:7444761787033346048",
  "url": "https://www.linkedin.com/feed/update/urn:li:activity:7444761787033346048/",
  "text": "<post body, normalized whitespace, up to 1500 chars>",
  "n_comments": 2,
  "n_impressions": 600
}
```

**Side effect:** populates the URN map cache with (activity URN → post text).

---

## `lipy comments --post URN [--max-load N] [--headed]`

Read all comments on a single post.

```bash
.venv/bin/python lipy.py comments --post 'urn:li:activity:7444399469581803520'
```

| Flag | Default | Purpose |
|---|---|---|
| `--post` | required | URN of the post (activity OR ugcPost form) |
| `--max-load` | 10 | Max clicks on "Load more comments" |
| `--headed` | false | Visible browser |

Each comment:
```json
{
  "urn": "urn:li:comment:(urn:li:ugcPost:7444399420177108992,7444496828470865920)",
  "author_name": "Farouk Hajjej",
  "author_handle": "faroukhajjej",
  "author_url": "https://www.linkedin.com/in/faroukhajjej/",
  "text": "Your point about Stitch...",
  "posted_label": "1mo"
}
```

**Side effect:** populates the URN map cache with (activity URN ↔ ugcPost URN).

---

## `lipy inbound --limit N [--headed]`

Convenience: combines `posts` + `comments`. Returns your last N posts each with their comments inline.

```bash
.venv/bin/python lipy.py inbound --limit 5
```

Same flags as `posts`. Output: an array of `{urn, url, text, n_comments, n_impressions, comments: [...]}` records.

This is the canonical "what's happening on my posts" query.

---

## `lipy my-comments --limit N [--save] [--headed] [--debug]`

Scrape **your outbound** comment history from `linkedin.com/in/me/recent-activity/comments/`. The highest-value voice training data.

```bash
.venv/bin/python lipy.py my-comments --limit 100 --save
```

| Flag | Default | Purpose |
|---|---|---|
| `--limit` | 50 | Max comments to return |
| `--save` | false | Append new entries to `corpus/linkedin_comments.jsonl` (deduped) |
| `--headed` | false | Visible browser (only matters if no daemon) |
| `--debug` | false | Dump DOM samples for selector tuning |

Each entry:
```json
{
  "parent_urn": "urn:li:activity:...",
  "parent_url": "https://www.linkedin.com/feed/update/...",
  "parent_author_name": "Omar Zaibak",
  "parent_author_handle": "omarzaibak",
  "is_reply_to_comment": false,
  "comment_text": "being an entrepreneur is like a job x 100x...",
  "posted_label": "1d"
}
```

**Dedup is automatic** — same comment text won't be re-added even on repeat runs.

---

## `lipy reply --parent URN --text "..." [--live] [--headed/--headless] [--post-hint TEXT]`

**Write op.** Reply to a specific comment with full human emulation.

```bash
.venv/bin/python lipy.py reply --headed --live \
  --parent 'urn:li:comment:(urn:li:ugcPost:<id>,<comment-id>)' \
  --text 'Your reply text here'
```

| Flag | Default | Purpose |
|---|---|---|
| `--parent` | required | URN of the comment you're replying to |
| `--text` | required | The reply text (LinkedIn auto-prepends a name-tag — see below) |
| `--live` | false | **Required to actually submit.** Without it, types but doesn't post (dry-run). |
| `--dry-run` | true | Default behavior — explicit flag, not usually needed |
| `--headed` | true | Visible browser (recommended for writes) |
| `--headless` | — | Inverts `--headed` |
| `--post-hint` | none | Manual fallback for click-through nav (first words of the post text). Used only when the URN cache is empty. |

### What it does in order:

1. Attaches via CDP if `lipy session` running; else launches fresh persistent context
2. Verifies auth (`_check_auth`)
3. Looks up the activity URN from the URN cache (or uses `--post-hint` for matching)
4. Tries to navigate to the post by clicking through `/in/me/recent-activity/all/` — **this currently falls back to direct URL goto on LinkedIn's activity page because LinkedIn blocks programmatic click-through there** (see [linkedin-engineering.md](linkedin-engineering.md))
5. Reads the post (read-dwell proportional to length)
6. Forces the comments LazyColumn to render via JS scrollIntoView
7. Finds the parent comment by URN
8. Reads the parent comment (read-dwell)
9. Bezier-curve mouse to the Reply button under the parent → human-click
10. Finds the inline reply textbox (scoped to a sibling of the parent comment — distinguishes from the top-level comment box that shares the same aria-label)
11. Human-clicks the textbox to focus
12. Human-types the text character-by-character (~30s for 250 chars)
13. Consider-dwell (~2.5s)
14. **If `--live`**: finds and human-clicks the Reply submit button. Otherwise stops here and returns dry-run result.

### Output on success:
```json
{"ok": true, "dry_run": false, "target": "inline_reply",
 "parent_urn": "...", "submitted": true}
```

### Important behavior: LinkedIn auto-tags the parent author

When you click "Reply" under Farouk Hajjej's comment, LinkedIn pre-fills the textbox with `Farouk Hajjej ` (as an @-mention pill). Whatever you pass in `--text` is appended after that. So if `--text "great question. Here's what..."`, the posted reply reads `Farouk Hajjej great question. Here's what...`.

**Don't include the addressee's name in `--text`** — LinkedIn handles it. The @-mention also gives the recipient a notification and links their name to their profile.

---

## `lipy comment --post URN --text "..." [--live] [--headed]`

**Write op.** Post a top-level comment on a post.

```bash
.venv/bin/python lipy.py comment --headed --live \
  --post 'urn:li:activity:<id>' \
  --text 'Your comment here'
```

Flags identical to `lipy reply` except:
- `--post` instead of `--parent` (post URN, not comment URN)
- No `--post-hint` (uses `--post` directly for navigation)

### What it does

Similar to `reply` but targets the top-level comment box on the post (the `[aria-label="Text editor for creating comment"]` element above the comments list, NOT the inline reply box). Clicks the "Comment" submit button.

---

## Notes for all write ops

- **Default is dry-run.** You will not accidentally post.
- **Cmd+Enter doesn't work** in LinkedIn's contenteditable — we use the actual submit button.
- **Activity windows / caps are NOT enforced inside `lipy reply` / `lipy comment` today.** They're declared in config but enforcement comes when we wire them into Hermes cron jobs (the autonomous mode). Until then, you're the rate limiter.
- **brand-guard is NOT yet called automatically.** Coming when `reply-drafter` is wired up.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Operation failed — see JSON error in output |
| 2 | Critical failure (Playwright not installed, etc.) — see stderr |
