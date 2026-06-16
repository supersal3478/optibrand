# Workflows

How to actually use the system day-to-day. Two-tier model: **you** decide what to engage with, **the agent** drafts and types.

## Setup (one-time)

Already done as of 2026-05-11. Documented here for reference / repeat install:

```bash
# 1. Install Hermes from the cloned source
cd ~/Desktop/projects/brand-growth-engine/vendor/hermes-agent
python3 -m venv .venv
.venv/bin/pip install -e .

# 2. Install Playwright + chromium for the LinkedIn skill
cd ~/Desktop/projects/brand-growth-engine/skills/linkedin-engage
./install.sh                          # creates skill-local .venv, downloads Chromium

# 3. Configure Azure OpenAI (Hermes will use it)
python /tmp/setup_hermes_azure.py     # reads 51_AZURE_*.md, writes ~/.hermes/.env
.venv/bin/hermes config set provider azure-foundry
.venv/bin/hermes config set model DeepSeek-V4-Flash   # cheapest; setup/bootstrap does this for you

# 4. Link our skills into Hermes
cd ~/Desktop/projects/brand-growth-engine
mkdir -p ~/.hermes/skills
for d in skills/*/; do
  ln -snf "$(pwd)/$d" "$HOME/.hermes/skills/$(basename "$d")"
done

# 5. Log into LinkedIn (one time; persists in the profile)
cd skills/linkedin-engage
.venv/bin/python lipy.py login --headed     # Chromium opens, you log in + 2FA
```

After this, the persistent Chromium profile at `~/.hermes/state/playwright/linkedin/profile/` holds your cookies, fingerprint, and "remember this device" state. Future runs reuse it without re-login.

## Daily flow

### Start of a work block

Open a dedicated terminal and start the long-running browser session:

```bash
cd ~/Desktop/projects/brand-growth-engine/skills/linkedin-engage
.venv/bin/python lipy.py session
```

A visible Chromium window opens on `linkedin.com/feed/`. **Leave this terminal alone.** You can:
- Use the browser yourself (scroll, browse, click around — anything a normal user does)
- Open more tabs if you want
- Just let it sit there

The terminal will print:
```
╭──────────────────────────────────────────────────────────╮
│ lipy session ready                                       │
│   pid:       12345                                       │
│   cdp port:  9222                                        │
│ Leave this terminal open. ...                            │
╰──────────────────────────────────────────────────────────╯
```

### Read what's happening

In a **second** terminal:

```bash
# What's new on my recent posts?
.venv/bin/python lipy.py inbound --limit 5
```

This returns JSON: your last 5 posts, with their comments. You read the JSON (or pipe it into `jq` / your favorite tool) and decide what to reply to.

### Reply to something

You pick a comment URN from the inbound output. For example, Farouk's comment:

```bash
.venv/bin/python lipy.py reply --headed --live \
  --parent 'urn:li:comment:(urn:li:ugcPost:7444399420177108992,7444496828470865920)' \
  --text "Farouk great question. Here's what's worked for us: ..."
```

The Chromium window (still the one from `lipy session`) navigates to your activity page, finds the post, opens the post detail, clicks Reply under Farouk's comment, types your reply character-by-character with Bezier-curve mouse movement, dwells briefly to "consider," then clicks the Reply button. The whole thing takes ~75 seconds.

**Important flags:**
- Default is `--dry-run` (types but does NOT submit). To actually post, you must pass `--live`.
- `--headed` is on by default (visible). `--headless` works but is more detectable.

### Periodically refresh your voice corpus

Every week or so, run:

```bash
.venv/bin/python lipy.py my-comments --limit 200 --save
```

This scrapes your `/in/me/recent-activity/comments/` history and appends new comments to `corpus/linkedin_comments.jsonl`. Dedup is automatic — re-runs only add new entries.

### End of work block

```bash
.venv/bin/python lipy.py session-stop
```

Or just Ctrl+C the session terminal. The Chromium window closes; PID/port marker files are cleaned up.

## When you have a target but no comment URN

If you want to reply to something on someone else's post (outbound), the comment URN isn't in your scraped data. Two paths:

### Path A: navigate manually, agent types

Best for ad-hoc outbound:

1. In the running session's Chromium window, manually navigate to the target post (search, scroll feed, paste URL from a message, however you'd normally find it).
2. Click the reply button under the comment you want to respond to. The inline reply textbox appears.
3. **In your other terminal**, run `lipy reply` with the comment URN. The script will detect you're already on the post detail page (skips its own navigation) and just handle the typing.

To get the URN, you can scrape the comments first:
```bash
.venv/bin/python lipy.py comments --post 'urn:li:activity:<id>'
```

Or pull it from LinkedIn's URL bar when looking at the comment thread.

### Path B: agent navigates + types

```bash
.venv/bin/python lipy.py reply --headed --live --parent '<comment urn>' --text '...'
```

The agent navigates to the post itself (currently via direct URL goto inside the running session — see [linkedin-engineering.md](linkedin-engineering.md) for why pure click-through is blocked). This works but is slightly more detectable than Path A.

## Reading other people's posts

Currently we only have helpers for reading **your own** posts (`lipy posts`, `lipy inbound`). To engage outbound on others' posts, you either:

1. **Browse manually** in the session browser and run `lipy reply` / `lipy comment` when you're ready.
2. Wait for a future `lipy feed-scan` / `lipy search` helper to be built (on the roadmap).

## Safety overlays

These are baked into the code and configs. You usually don't think about them; they prevent accidents.

| Safety | Where | What it does |
|---|---|---|
| `--dry-run` default | `lipy reply`, `lipy comment` | Types into the box but does NOT submit. Must pass `--live` explicitly. |
| `caps.yaml: <platform>.live: false` | config | Master kill-switch. Set to `false` and the platform's actions are skipped. Currently set to `false` for all three platforms while we're not autonomous yet. |
| Daily/weekly caps | `caps.yaml` | LinkedIn outbound ≤25/day, ≤120/week. X outbound ≤15/day. YouTube replies ≤40/day. |
| Activity windows | `windows.yaml` | LinkedIn weekday business hours only. X 07:00–22:00. YouTube 24/7 for moderation. |
| Jitter | `jitter.yaml` | 5–20 min between outbound LinkedIn actions. Real human spacing. |
| Brand guard (future) | `skills/brand-guard/SKILL.md` | When wired into `reply-drafter`, will hard-veto any draft that contains em-dashes, sycophantic openers, AI tells, off-limits topics from BRAND.md. |
| Hold buffer (future) | `caps.yaml: hold_buffer_outbound_seconds: 300` | Once we're autonomous, drafts will sit in a queue for 5 min before posting; you can yank from your phone. |

## Real LinkedIn workflow we proved (2026-05-11)

End-to-end, this is what worked:

```
1. lipy session                             # Chromium opens
2. lipy posts --limit 5                     # got URNs for last 5 posts
3. lipy inbound --limit 5                   # got all comments on those posts
4. (read output, decided to reply to Farouk's question on post 4)
5. Drafted reply text, iterated with user feedback (no em dashes, first name only)
6. lipy reply --headed --live \             # ~75s end-to-end
   --parent 'urn:li:comment:(urn:li:ugcPost:7444399420177108992,7444496828470865920)' \
   --text "great question. What's worked for us: ..."
7. lipy comments --post <activity URN>      # verified — comment count went 3→4
8. lipy my-comments --limit 100 --save      # 51 voice samples → 45 deduped saved
```

The Farouk reply is live on LinkedIn right now.

---

# X (Twitter) day-to-day flow

Different transport from LinkedIn (Chrome DevTools Protocol against a dedicated Chrome instance, not Playwright), same logical shape.

## Setup (one-time per machine)

```bash
# 1. Install the Monterey-compatible cua-driver Rust port — see docs/x-engineering.md
~/.local/bin/cua-driver check_permissions
# expect: {"accessibility": true, "screen_recording": true}

# 2. The CDP Chrome's persistent profile directory
mkdir -p "$HOME/.hermes/state/chrome-cdp"
```

## Start of a work block

Launch the dedicated CDP Chrome (separate window from your normal Chrome; coexists fine). Leave it running while you work; you don't need to relaunch unless it's been days.

```bash
'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.hermes/state/chrome-cdp" &
disown
```

**First launch only**: in that Chrome window, navigate to `x.com`, click "Sign in," log in as `@salaicreates`. Complete any 2FA. Login persists in the user-data-dir for future sessions.

Verify CDP is live:

```bash
curl -s http://localhost:9222/json/version | python3 -m json.tool
# Expect JSON with "webSocketDebuggerUrl"
```

## Read inbound (Mentions)

```bash
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
```

Returns JSON with one record per mention. Pick the one worth responding to.

## Reply to a mention

The proven recipe (use the Hermes venv's Python so `websockets` is available):

```python
import asyncio, json, base64, urllib.request, websockets

TWEET_URL = "https://x.com/<author>/status/<id>"   # the tweet you're replying to
REPLY_TEXT = "<first name> <one sentence with substance> <closing question>"

def get_x_tab():
    with urllib.request.urlopen("http://localhost:9222/json", timeout=5) as r:
        for t in json.load(r):
            if t.get("type") == "page" and "x.com" in (t.get("url") or ""):
                return t

async def main():
    tab = get_x_tab()
    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=20_000_000) as ws:
        nxt = [0]
        async def call(m, p=None):
            nxt[0] += 1; mid = nxt[0]
            await ws.send(json.dumps({"id": mid, "method": m, "params": p or {}}))
            while True:
                d = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                if d.get("id") == mid: return d

        # Navigate to the tweet detail page (don't try replying from /notifications/mentions list)
        await call("Page.navigate", {"url": TWEET_URL})
        await asyncio.sleep(5)

        # Focus the composer
        await call("Runtime.evaluate", {
            "expression": "document.querySelector('div[data-testid=\"tweetTextarea_0\"]').focus()",
            "userGesture": True,
        })

        # Type via CDP Input.insertText (DraftJS-safe; execCommand/paste won't work)
        await call("Input.insertText", {"text": REPLY_TEXT})

        # Dry-run screenshot — review before submit
        shot = await call("Page.captureScreenshot", {"format": "png"})
        open("/tmp/x_dry_run.png", "wb").write(base64.b64decode(shot["result"]["data"]))
        print("dry-run screenshot at /tmp/x_dry_run.png — review, then continue")

        # ===== SUBMIT (uncomment when ready to go live) =====
        # await call("Runtime.evaluate", {
        #     "expression": "document.querySelector('button[data-testid=\"tweetButtonInline\"]').click()",
        #     "userGesture": True,
        # })
        # await asyncio.sleep(5)
        # shot2 = await call("Page.captureScreenshot", {"format": "png"})
        # open("/tmp/x_after_submit.png", "wb").write(base64.b64decode(shot2["result"]["data"]))
        # Expect to see X's "Your post was sent" toast + Premium upsell modal

asyncio.run(main())
```

## Real X workflow we proved (2026-05-14)

```
1. Launch CDP Chrome with --remote-debugging-port=9222 --user-data-dir=~/.hermes/state/chrome-cdp
2. curl -s http://localhost:9222/json/version  → CDP responding
3. cdp_eval.py --navigate 'https://x.com/notifications/mentions'
4. cdp_eval.py --expr 'JSON.stringify([...document.querySelectorAll(...)])'
   → returned Vadim Strizheus + Grok as the 2 visible mentions
5. (decided to reply to Vadim; Grok is automated spam, skip)
6. Drafted "Vadim haha what made you skeptical?" applying voice rules
7. Inline Python recipe (above) navigated to Vadim's tweet detail URL
8. Input.insertText placed draft into composer
9. Page.captureScreenshot confirmed draft visible
10. Runtime.evaluate click on tweetButtonInline with userGesture=true
11. Page.captureScreenshot confirmed "Your post was sent" toast + Premium upsell modal
```

The Vadim reply is live on X right now.

## End of a work block

The CDP Chrome can stay running indefinitely. To stop it: Cmd+Q on that Chrome window, or `pkill -f "remote-debugging-port=9222"` from a terminal. The user-data-dir persists, so next launch picks up your X login automatically.
