---
name: x-engage
description: "X (Twitter) engagement via a dedicated CDP-controlled Chrome instance. One read+write transport: Chrome DevTools Protocol against a Chrome launched with --remote-debugging-port=9222 on a separate user-data-dir. Proven live on 2026-05-14 with a reply to @VadimStrizheus."
version: 0.2.0
author: brand-growth-engine
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [x, twitter, social-media, browser-automation, cdp]
prerequisites:
  commands:
    - cua-driver   # ~/.local/bin/cua-driver; app/window inventory + screenshots only
    - start-chrome-cdp   # ~/.local/bin/start-chrome-cdp; brings up the dedicated CDP Chrome
  files:
    - <project>/BRAND.md
    - <project>/skills/x-engage/cdp_eval.py
  python:
    - <project>/vendor/hermes-agent/.venv/bin/python  # provides the websockets library cdp_eval needs
  skills:
    - brand-guard
    - reply-drafter
---

# x-engage

X (Twitter) workflow driven via Chrome DevTools Protocol against a dedicated Chrome instance with the X login. Same logical shape as `linkedin-engage` (`lipy.py`) but the transport is CDP over WebSocket instead of Playwright.

**Proven live 2026-05-14:** drafted and posted "Vadim haha what made you skeptical?" as a reply to @VadimStrizheus's "We will see haha" mention on the user's real `@salaicreates` X account. The CDP recipe in [`feedback_x_cdp_recipe`](../../.claude/projects/-Users-salsmacos-Desktop-projects-brand-growth-engine/memory/feedback_x_cdp_recipe.md) captures every command verbatim.

---

## Architecture (one diagram)

```
┌──────────────────────────────────────────────────────────────────┐
│  User's normal Chrome (default user-data-dir)                    │
│   • everyday browsing, X reading, everything else                │
│   • NO debug port, NO automation flag — completely untouched     │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│  CDP Chrome (user-data-dir = ~/.hermes/state/chrome-cdp/)        │
│   • launched with --remote-debugging-port=9222                   │
│   • logged into X as @salaicreates (one-time on first launch)    │
│   • profile persists across sessions, login survives             │
│   • yellow banner: "Chrome is being controlled by automated      │
│     test software" — visible only to user, not in posts          │
└────────────────────────────┬─────────────────────────────────────┘
                             │ WebSocket on ws://localhost:9222/...
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  cdp_eval.py + ad-hoc Hermes-venv Python                         │
│   • Page.navigate          → switch URL inside CDP Chrome        │
│   • Runtime.evaluate       → read DOM, click buttons             │
│   • Input.insertText       → type into composer (DraftJS-safe)   │
│   • Page.captureScreenshot → visual verification                 │
└──────────────────────────────────────────────────────────────────┘
```

There is NO AX-tree path, NO `cua-driver browser_eval`, NO Playwright Chromium. Just CDP. Reads and writes are the same transport. cua-driver is kept only for OS-level conveniences (`list_apps`, `list_windows`, screenshots of foreground windows) — never for clicking inside X.

---

## Why not...

- **The X API (`xurl`)?** Costs ~$100/mo for Basic tier, and X's Feb 2026 policy requires explicit prior written approval for automated replies (multi-week wait). The browser+CDP route avoids both for manual engagement; the X auto-reply approval is still required before flipping to autonomous-cron mode.
- **The user's real Chrome with CDP?** Impossible. Chrome 121+ refuses CDP on the default `user-data-dir` for security ("DevTools remote debugging requires a non-default data directory"). The separate profile at `~/.hermes/state/chrome-cdp/` is the workaround.
- **AX tree reads of X content?** Tried this; doesn't work reliably. Chrome lazily evicts AX exposure for backgrounded SPA web content. You get rich data sometimes, empty trees other times. DOM via CDP is deterministic. See [docs/x-engineering.md](../../docs/x-engineering.md) "Rejected approaches."
- **cua-driver coordinate clicks?** Tried this; doesn't work on macOS Monterey 12.x. CGEvent-synthesized clicks lack the trusted-input envelope (a macOS 14+ feature), so Chrome's renderer silently drops them for web content. Only browser-chrome elements (tab strip, menus) respond.
- **Playwright Chromium like `lipy`?** Would work. Skipped because CDP against Chrome.app is one less moving part and uses the same browser binary the user trusts.

---

## Secret Safety (MANDATORY)

- Cookies in `~/.hermes/state/chrome-cdp/Default/Cookies` contain session credentials. Never read, copy, print, or transmit them.
- Never use `--verbose` on cua-driver. Never log raw HTTP responses (they can contain bearer tokens).
- The X login is set up once by the user, manually, in the CDP Chrome window. The agent must not handle credentials.

---

## One-time user setup

User runs these once, outside the agent session. The agent verifies via the checks listed but does not run them.

1. Install `cua-driver` (Rust port, Monterey-compatible). See [docs/x-engineering.md](../../docs/x-engineering.md#installing-cua-driver-on-monterey).
2. Launch the dedicated CDP Chrome (also do this each time you start the workflow):
   ```bash
   start-chrome-cdp
   ```
   Idempotent shim at [`skills/x-engage/start-chrome-cdp.sh`](start-chrome-cdp.sh), symlinked into `~/.local/bin/`. If CDP is already up on 9222 it exits 0 without relaunching, otherwise it launches Chrome with the right flags and waits up to 10s for the debug port to come up. The raw equivalent:
   ```bash
   mkdir -p "$HOME/.hermes/state/chrome-cdp"
   '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
     --remote-debugging-port=9222 \
     --user-data-dir="$HOME/.hermes/state/chrome-cdp" &
   disown
   ```
3. **First launch only:** in that new Chrome window, navigate to `x.com`, click "Sign in," log in as `@salaicreates`. Complete 2FA if prompted. The login persists in the user-data-dir.
4. Verify CDP is up:
   ```bash
   curl -s http://localhost:9222/json/version
   ```
   Expect JSON with `webSocketDebuggerUrl`. If you see it, CDP is live.

---

## CLI: `cdp_eval.py`

Located at [`skills/x-engage/cdp_eval.py`](cdp_eval.py). The single utility that wraps CDP `Runtime.evaluate` and `Page.navigate` for ad-hoc use. Invoke via the Hermes venv's Python (where `websockets` is installed):

```bash
# Resolve from the project root (set this once in your shell or use $PWD if you're inside the repo)
BGE_ROOT="${BGE_ROOT:-$PWD}"
HERMES_PY="$BGE_ROOT/vendor/hermes-agent/.venv/bin/python"
CDP="$BGE_ROOT/skills/x-engage/cdp_eval.py"

# Read login state + page metadata
$HERMES_PY $CDP --expr 'JSON.stringify({url: window.location.href, title: document.title, logged_in: !!document.querySelector("[data-testid=SideNav_AccountSwitcher_Button]")})'

# Navigate
$HERMES_PY $CDP --navigate 'https://x.com/notifications/mentions'

# Extract all visible mentions/replies on current page
$HERMES_PY $CDP --expr 'JSON.stringify([...document.querySelectorAll("article[data-testid=tweet]")].map(a => ({
  author: (a.querySelector("[data-testid=User-Name]") || {}).innerText,
  text: (a.querySelector("[data-testid=tweetText]") || {}).innerText,
  url: (a.querySelector("a[href*=\"/status/\"]") || {}).href,
  reply_aria: (a.querySelector("button[data-testid=reply]") || {}).getAttribute("aria-label"),
  like_aria: (a.querySelector("button[data-testid=like], button[data-testid=unlike]") || {}).getAttribute("aria-label"),
})))'
```

For tasks that need multiple CDP commands sharing state in one process (typing + clicking submit), write an inline Python block using the Hermes venv's `websockets` module. See "The proven write recipe" below.

---

## The proven write recipe

Inline Python (uses Hermes venv) that posts a reply to a specific tweet. Each step is required; skipping any breaks the chain:

```python
import asyncio, json, urllib.request, websockets

# 1. Find the right tab via CDP HTTP API
def get_tab(url_substring):
    with urllib.request.urlopen("http://localhost:9222/json", timeout=5) as r:
        for t in json.load(r):
            if t.get("type") == "page" and url_substring in (t.get("url") or ""):
                return t
    raise SystemExit("tab not found")

async def main():
    # 2. Navigate to the target tweet's detail page (NOT the Mentions list view —
    # clicking reply from the list sometimes navigates to /compose/post without
    # the in-reply-to context; the tweet detail page is reliable)
    tab = get_tab("x.com")
    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=20_000_000) as ws:
        nxt = [0]
        async def call(method, params=None):
            nxt[0] += 1; mid = nxt[0]
            await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            while True:
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                if data.get("id") == mid: return data

        await call("Page.navigate", {"url": "https://x.com/VadimStrizheus/status/2051435314808254862"})
        await asyncio.sleep(5)  # SPA render

        # 3. Focus the inline reply composer (DraftJS contenteditable)
        await call("Runtime.evaluate", {
            "expression": "document.querySelector('div[data-testid=\"tweetTextarea_0\"]').focus()",
            "userGesture": True,
        })

        # 4. Type via CDP Input.insertText — NOT execCommand/paste. This is the OS-level
        # input simulation that DraftJS handles correctly. Anything else either fails to
        # insert or fails to enable the submit button.
        await call("Input.insertText", {"text": "Vadim haha what made you skeptical?"})

        # 5. Click submit with userGesture=true. Required — without the flag, X's React
        # handler rejects the click as automated and silently no-ops.
        await call("Runtime.evaluate", {
            "expression": "document.querySelector('button[data-testid=\"tweetButtonInline\"]').click()",
            "userGesture": True,
        })

        await asyncio.sleep(5)  # let X process

        # 6. Verify via CDP screenshot — NOT cua-driver screenshot. The CDP Chrome window
        # may be backgrounded; macOS screen capture of backgrounded windows can return
        # blank/stale state. Page.captureScreenshot reads the actual rendered page.
        import base64
        shot = await call("Page.captureScreenshot", {"format": "png"})
        open("/tmp/x_verify.png", "wb").write(base64.b64decode(shot["result"]["data"]))

asyncio.run(main())
```

**Confirmations the reply landed:**

- X displays a blue toast at the bottom: **"Your post was sent."**
- X opens its **"Want more people to see your reply? Subscribe to Premium..."** modal (only appears after a successful publish).
- The inline composer (`tweetTextarea_0`) resets to empty.

If the toast/modal don't appear, the click failed. Common causes: composer text is empty, the wrong composer was focused, the user is no longer logged in (login expired).

---

## Stable CDP/DOM selectors (as of 2026-05-14)

| Element | Selector |
|---|---|
| Tweet / post container | `article[data-testid="tweet"]` |
| Tweet author block | `[data-testid="User-Name"]` |
| Tweet body text | `[data-testid="tweetText"]` |
| Reply button (inside a tweet) | `button[data-testid="reply"]` |
| Repost button | `button[data-testid="retweet"]` / `button[data-testid="unretweet"]` |
| Like button | `button[data-testid="like"]` / `button[data-testid="unlike"]` |
| Inline reply composer | `div[data-testid="tweetTextarea_0"]` |
| Inline reply submit | `button[data-testid="tweetButtonInline"]` |
| Modal reply submit | `button[data-testid="tweetButton"]` |
| AccountSwitcher (logged-in indicator) | `[data-testid="SideNav_AccountSwitcher_Button"]` |
| Tweet permalink anchor | `a[href*="/status/"]` (use to get tweet ID) |

X's data-testid attributes are stable — their internal QA depends on them. If they roll, see [docs/x-engineering.md](../../docs/x-engineering.md) for the diagnostic + update procedure.

---

## Workflow: manual happy path (proven 2026-05-14)

This is the only end-to-end flow that works today. The autonomous loop is Phase 3+.

1. **Pre-flight (user, ~30s):**
   - Launch CDP Chrome with the command above
   - `curl -s http://localhost:9222/json/version` returns JSON

2. **Read inbound (~5s):**
   - `cdp_eval.py --navigate 'https://x.com/notifications/mentions'`
   - Sleep a few seconds for SPA load
   - `cdp_eval.py --expr '...'` with the article-extraction JS (see CLI example above)
   - Returns JSON: every mention with author, text, URL, counts

3. **Decide (manual):** the user (or an LLM in chat) reads the JSON, picks a mention worth responding to. If nothing fresh, the workflow stops.

4. **Draft (manual today, automated when reply-drafter is wired):** apply voice rules (first name only, no em-dashes, name-tag + one sentence + closing question).

5. **Brand-guard (skipped today, hard veto when wired):** the draft passes through `skills/brand-guard/` for em-dash / sycophantic-opener / off-limits check.

6. **Type into composer (CDP, ~2s):** the inline Python recipe above. Navigate to the tweet detail URL → focus composer → `Input.insertText` → STOP here for dry-run.

7. **Verify draft visually (CDP screenshot):** `Page.captureScreenshot` to `/tmp/`. User reviews.

8. **Submit (CDP, ~1s):** `Runtime.evaluate` with `userGesture=true` on `tweetButtonInline.click()`.

9. **Confirm (CDP, ~5s):** screenshot again, look for "Your post was sent" toast + Premium upsell modal. Log to `~/.hermes/logs/audit.jsonl`.

Total time per reply: ~15-30 seconds end-to-end, dominated by SPA load + the user's review.

---

## Workflow: scheduled mentions pass (Phase 3+, autonomous)

Same shape as `linkedin-engage`'s inbound flow. Pre-flight gates that must all pass:

1. `caps.yaml: phase` >= 3 for X inbound
2. `caps.yaml: x.live` is `true`
3. `X_AUTO_REPLY_APPROVED=true` in `~/.hermes/.env` (required by X's Feb 2026 policy regardless of transport)
4. CDP Chrome is up: `curl -s http://localhost:9222/json/version` returns JSON
5. Active session: a `Runtime.evaluate` for `[data-testid="SideNav_AccountSwitcher_Button"]` returns the user's profile
6. Inside `windows.yaml: x.inbound`
7. Today's reply count below `caps.yaml: x.inbound.replies_per_day`

Then, for each unprocessed mention (not in `~/.hermes/state/x_processed.jsonl`):

- Skip if blocklisted per `blocklist.yaml`
- Run `spam-classifier`; skip if spam/toxic
- Run `reply-drafter` → returns DRAFT or REFUSE
- Hold for `caps.yaml: hold_buffer_outbound_seconds` (5 min default), dashboard-visible — user can yank
- If still queued: execute the proven write recipe (above)
- Verify via screenshot for "Your post was sent" toast presence
- Append to `x_processed.jsonl`, increment `x_replies_today.txt`
- Long jitter per `jitter.yaml: x.inbound`

---

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| `curl http://localhost:9222/json/version` returns nothing | CDP Chrome not running OR launched with default user-data-dir | Re-run the launch command. Check `/tmp/chrome-cdp.log` for `"requires a non-default data directory"` — if so, the `--user-data-dir` arg got dropped |
| Login page appears on x.com | Session expired (rare; Chrome cookies last weeks) | User re-logs in once in the CDP Chrome window |
| `Input.insertText` succeeds but `tweetButtonInline.disabled` stays true | Composer wasn't focused first, OR wrong tweetTextarea index | `Runtime.evaluate` `document.querySelector('div[data-testid="tweetTextarea_0"]').focus()` first, then re-`Input.insertText` |
| Click on `tweetButtonInline` does nothing | `userGesture` flag was missing | Re-call `Runtime.evaluate` with `userGesture: true` |
| Page screenshot shows blank content | macOS screen capture on backgrounded Chrome | Use `Page.captureScreenshot` via CDP instead — captures actual page state |
| URL ends up on `/compose/post` with blank page after clicking reply | X lost the in-reply-to context (happens from list views) | Don't click reply from `/notifications/mentions`. Navigate to the tweet detail URL first (`https://x.com/<author>/status/<id>`), then click reply there |
| "Try again later" / rate-limit modal | X throttled the account | Halt all writes for 24h. Set `caps.yaml: x.live = false`. Investigate before resuming |
| Captcha (Arkose) iframe appears | X challenge | **Do not solve it.** Halt, surface to user, user solves it manually in the visible window |

---

## Notes

- **Browser process lifecycle:** the CDP Chrome can stay running indefinitely. The yellow "controlled by automated test software" banner is cosmetic. When you want to use Chrome normally, just use your other Chrome instance — they coexist.
- **Persisted login:** `~/.hermes/state/chrome-cdp/` holds cookies, history, fingerprint. Survives restarts of the Chrome instance. Treat the directory like a password — delete it to force a re-login.
- **Why no Playwright wrapper:** we could mirror `lipy.py`'s structure exactly. The reason we don't: CDP-direct (cdp_eval + inline Python recipes) is fewer moving parts than Playwright-managed-Chromium, and we get the same operations. If this skill grows complex enough that the SKILL.md isn't enough orientation, write `xipy.py` as a thin wrapper at that time. Don't pre-build it.
- **Hermes integration:** Hermes ships a `browser_cdp` tool ([`tools/browser_cdp_tool.py`](../../vendor/hermes-agent/tools/browser_cdp_tool.py)) which does the same thing as our inline recipes but through Hermes' tool-call interface. When this skill graduates to autonomous-cron mode, the cron job invokes `browser_cdp` from inside Hermes' agent loop. The CDP commands stay identical.
