# X engineering notes

Everything we learned about X.com automation on macOS Monterey while building [`skills/x-engage/`](../skills/x-engage/). The first half is the **proven working pattern**. The second half is **rejected approaches** — engineering knowledge we paid for in time, kept here so we don't pay for it again.

Last updated: 2026-05-14 (after live proof: a reply to @VadimStrizheus posted via the CDP recipe below).

---

## TL;DR

- **Transport: Chrome DevTools Protocol (CDP) over WebSocket.** One transport, both reads and writes.
- **Chrome: a dedicated instance** launched with `--remote-debugging-port=9222 --user-data-dir="$HOME/.hermes/state/chrome-cdp"`. Coexists with the user's normal Chrome. Chrome 121+ refuses CDP on the default user-data-dir, so the separate profile is required.
- **Reading:** `Runtime.evaluate` against the DOM with `document.querySelectorAll('article[data-testid="tweet"]')` and the per-tweet `data-testid` attributes X exposes for its own QA.
- **Typing into the composer:** `Input.insertText` (CDP method, not DOM `execCommand`). Mandatory for X's DraftJS composer — anything else either fails to insert or fails to flip the submit button to enabled.
- **Clicking submit:** `Runtime.evaluate` with `userGesture: true` on `button[data-testid="tweetButtonInline"]`. The `userGesture` flag is required; without it React rejects the click as automated.
- **Verifying:** `Page.captureScreenshot` via CDP (not macOS screen capture, which can return blank for backgrounded windows).
- **Python prerequisite:** the `websockets` module. The Hermes venv at `/Users/salsmacos/Desktop/projects/brand-growth-engine/vendor/hermes-agent/.venv/bin/python` has it.

---

## Installing cua-driver on Monterey

`cua-driver` is used only for OS-level things — `list_apps`, `list_windows`, screenshots of the foreground app, and the `hotkey` tool for `Cmd+6` tab switching in the user's *normal* Chrome. It is NOT used for clicking inside X (see "Rejected approaches" below).

The official Hermes installer (`hermes computer-use install`) downloads `cua-driver-0.1.9-darwin-x86_64.tar.gz`, which is the **Swift** binary requiring macOS 14+'s Swift runtime. On Monterey it crashes with `Library not loaded: '/usr/lib/swift/libswiftObservation.dylib'`.

Use the **Rust port** instead:

```bash
mkdir -p /tmp/cua-rs-install && cd /tmp/cua-rs-install
curl -fsSL "https://github.com/trycua/cua/releases/download/cua-driver-rs-v0.1.3/cua-driver-rs-0.1.3-darwin-x86_64.tar.gz" -o cua.tar.gz
tar -xzf cua.tar.gz
mkdir -p ~/.local/bin
cp cua-driver-rs-0.1.3-darwin-x86_64/cua-driver ~/.local/bin/cua-driver
chmod +x ~/.local/bin/cua-driver

# Verify
~/.local/bin/cua-driver --version          # cua-driver 0.1.3
~/.local/bin/cua-driver check_permissions  # {accessibility: true, screen_recording: true}
```

Grant macOS Accessibility + Screen Recording permissions once when prompted by the first run.

---

## DOM identifiers (stable, X exposes these for its own QA)

| Element | Selector |
|---|---|
| Tweet / post container | `article[data-testid="tweet"]` |
| Tweet author block (name + handle + date) | `[data-testid="User-Name"]` |
| Tweet body text | `[data-testid="tweetText"]` |
| Tweet permalink anchor | `a[href*="/status/"]` |
| Reply button inside a tweet | `button[data-testid="reply"]` |
| Repost button | `button[data-testid="retweet"]` / `button[data-testid="unretweet"]` (state) |
| Like button | `button[data-testid="like"]` / `button[data-testid="unlike"]` |
| Inline reply composer (DraftJS contenteditable) | `div[data-testid="tweetTextarea_0"]` |
| Inline reply submit button | `button[data-testid="tweetButtonInline"]` |
| Modal reply submit button | `button[data-testid="tweetButton"]` |
| Logged-in indicator | `[data-testid="AccountSwitcher_Button"]` |
| Notifications sidebar link | `a[data-testid="AppTabBar_Notifications_Link"]` |
| Mentions sub-tab | `a[role="tab"][href$="/mentions"]` (text content "Mentions") |
| Compose tweet button (sidebar) | `a[data-testid="SideNav_NewTweet_Button"]` |
| Rate-limit / "Try again later" modal | text content search for "rate limit" or "try again later" |
| Premium upsell modal (appears after successful reply) | text content "Want more people to see your reply?" |

Reply count, like count, and repost count are exposed via the button's `aria-label` (e.g. `"3 Replies. Reply"`, `"12 Likes. Liked"`). Read with `element.getAttribute("aria-label")` and regex out the count.

---

## Login + 2FA

The user logs in once, manually, in the CDP Chrome window:

1. Launch the CDP Chrome (see [`skills/x-engage/SKILL.md`](../skills/x-engage/SKILL.md) launch command).
2. The first launch shows Chrome's Welcome / "Sign in to Google" flow. **Skip or close it.**
3. Navigate to `https://x.com` in that window.
4. Click "Sign in," enter credentials, complete 2FA if prompted.
5. The login persists in `~/.hermes/state/chrome-cdp/Default/Cookies`. Subsequent launches don't require re-login (cookies last weeks-to-months).

The agent must not handle the login.

---

## Rate-limit and challenge handling

| Signal | Detection | Response |
|---|---|---|
| Rate-limit modal | `Runtime.evaluate` finds page text matching `/rate limit\|try again later/i` | Halt all writes for 24h. Set `caps.yaml: x.live = false`. Log CRITICAL |
| Account suspended/restricted | URL navigates to `/account/access`, `/i/suspended`, `/i/restricted` | Full halt, notify user via the messaging gateway |
| Captcha (Arkose) | `Runtime.evaluate` finds `iframe[src*="arkoselabs.com"]` | **Halt. Surface to user. Never solve it.** User solves manually |
| Login required | URL navigates to `/i/flow/login` after a Page.navigate | Notify user, halt platform until they re-log in |

The skill never attempts to solve a challenge. User-only intervention.

---

## Process tree (what's actually running)

After a normal day's workflow setup:

```
Google Chrome (pid A)                       ← user's everyday Chrome
└─ ~/Library/Application Support/Google/Chrome/   (default profile)

Google Chrome (pid B)                       ← CDP Chrome
├─ --remote-debugging-port=9222
└─ --user-data-dir=~/.hermes/state/chrome-cdp/    (dedicated profile, X login)
   └─ listens on localhost:9222 (CDP WebSocket)

~/.local/bin/cua-driver                     ← OS-level utility (Rust port)
   used for: list_apps, list_windows, hotkey, screenshot (foreground apps only)
```

The two Chrome instances are independent. Quitting one does not affect the other. They share no state — including extensions, bookmarks, history, or saved passwords.

---

## What's IN the project for X (after dead-code cleanup 2026-05-14)

```
skills/x-engage/
├── SKILL.md          ← runbook with proven write recipe
└── cdp_eval.py       ← thin CDP CLI: --expr / --navigate
```

The only Python module needed is `websockets` (16.0). Available via the Hermes venv: `/Users/salsmacos/Desktop/projects/brand-growth-engine/vendor/hermes-agent/.venv/bin/python`.

There is no `xipy.py` wrapper today. The proven recipes live inline in the SKILL.md. If the workflow grows complex enough to need a CLI wrapper, build one then — don't pre-build.

---

# Appendix: rejected approaches

Engineering knowledge from approaches we explored that didn't work. Recorded so we don't re-explore them.

## A. cua-driver coordinate clicks (rejected — Monterey)

Sending `cua-driver call click {x, y}` to Chrome's pid posts CGEvent mouse events at the OS level. On macOS Monterey 12.x:
- Chrome's **chrome layer** (tab strip, address bar, OS menu bar) responds correctly.
- Chrome's **renderer process** for web content silently drops them. React handlers never fire.

The reason: Chrome 95+ checks whether incoming mouse events came from a trusted source. The trusted-input envelope that cua-driver uses on macOS 14+ (Sonoma) does not exist on 12.x. On Monterey, Chrome receives clicks without that envelope and rejects them at the renderer security boundary.

Empirically verified 2026-05-14 with `debug_image_out`: the crosshair lands pixel-perfect on the intended element (X.com sidebar "Home" link), but no navigation occurs. Multiple coordinate attempts; none triggered any X.com web-content action.

**Don't retry.** This is OS-version-locked. The only fix is a macOS upgrade.

## B. AX-tree reads of X web content (rejected — flaky)

Chrome exposes some web content to the macOS Accessibility API. We tried using `cua-driver call get_window_state` to pull the AX tree and parse mentions out of it.

Sometimes this worked beautifully — one call returned 700+ elements including author handles, reply counts, post text. The early `parse_x_ax.py` (since deleted) extracted clean records.

Other times — including in the same session, on the same page — the AX tree contained only browser chrome and macOS menus, with zero web content. Chrome appears to lazily evict the AX tree for backgrounded SPA web content. Bringing the page to front + scrolling sometimes refreshed it; sometimes did not.

**Verdict: too flaky to depend on.** DOM via CDP is deterministic — every visible tweet appears in `querySelectorAll('article[data-testid="tweet"]')`, every time.

If we ever wanted to use AX as a *backup* read path, the parser code is in git history. Don't resurrect without a strong reason.

## C. The user's REAL Chrome with CDP (rejected — Chrome refuses)

The original ask was "use my real Chrome where I'm already logged in into x." We tried:

```bash
open -a "Google Chrome" --args --remote-debugging-port=9222
```

Chrome's stdout log immediately said:

```
DevTools remote debugging requires a non-default data directory.
Specify this using --user-data-dir.
```

This is a Chrome 121+ security feature. The default user-data-dir is privileged (the user's normal browsing session); Chrome refuses to expose CDP on it to prevent any local process from snooping. The only path to CDP is a separate user-data-dir.

We accepted the compromise: a dedicated CDP Chrome at `~/.hermes/state/chrome-cdp/`. Cost: logging into X once on that profile. Benefit: full CDP reliability from there on.

## D. DOM `execCommand("insertText")` (rejected — DraftJS doesn't update)

```javascript
composer.focus();
document.execCommand("insertText", false, "Vadim haha what made you skeptical?");
```

DOM updated correctly — `composer.innerText` read back the typed text. But X's React handler did not fire, so the **submit button stayed disabled.** X tracks composer state in React internal state, not from DOM events. `execCommand` doesn't trigger the events DraftJS listens for.

## E. `paste` event with `DataTransfer` (rejected — wrong text inserted)

```javascript
const dt = new DataTransfer();
dt.setData("text/plain", "Vadim haha what made you skeptical?");
composer.dispatchEvent(new ClipboardEvent("paste", {clipboardData: dt, bubbles: true}));
```

This sometimes flipped React state correctly (submit button enabled) but the text inserted as a blank `\n` instead of the intended string. Hypothesis: DraftJS's paste handler reads from a real clipboard, and synthesized ClipboardEvents don't carry data through the same path.

## F. CDP `Input.insertText` (✅ ACCEPTED — the working path)

Replace both D and E with:

```python
await ws.send(json.dumps({
    "id": N,
    "method": "Input.insertText",
    "params": {"text": "Vadim haha what made you skeptical?"},
}))
```

This is Chrome's protocol-level "insert text into the currently focused element" command. Simulates real OS-level text input — exactly what a human typing produces. DraftJS handles it correctly; React state updates; submit button enables.

Pre-focus the composer first with a small `Runtime.evaluate` that calls `composer.focus()`. Otherwise `Input.insertText` will insert into whatever currently has browser focus, often nothing useful.

## G. `element.click()` without `userGesture` (rejected — React rejects)

```javascript
document.querySelector('button[data-testid="tweetButtonInline"]').click();
```

Inside `Runtime.evaluate` WITHOUT `userGesture: true`, this no-ops. X's React onClick handler checks whether the click was user-initiated and rejects automated clicks.

Fix: pass `userGesture: true` to `Runtime.evaluate`. CDP then tags the resulting click as user-initiated; React accepts it.

## H. cua-driver `browser_eval` tool (deferred — hung in testing)

cua-driver has its own `browser_eval` tool that wraps CDP. We tried it; the call hung indefinitely (15+ seconds with no response). Our roll-your-own `cdp_eval.py` using the Hermes venv's `websockets` module works reliably. We didn't dig into why cua-driver's wrapper hung — possibly a tab-matching heuristic issue.

If we ever switch back to cua-driver's wrapper, the test cycle is: `cua-driver call browser_eval --json '{"cdp_port": 9222, "expression": "1+1"}'` — should return `2` in under a second.

## I. macOS screenshot of backgrounded CDP Chrome (rejected — blank)

`cua-driver call screenshot` and `cua-driver call get_window_state --capture-mode vision` both work fine on foreground Chrome windows, but for backgrounded windows (like our CDP Chrome usually is, since the user is in VS Code or their main Chrome), they often return blank or stale state.

`Page.captureScreenshot` via CDP captures the actual rendered page state regardless of which window is foreground. Use this for visual verification of write operations.

---

# Glossary

- **CDP**: Chrome DevTools Protocol. The same protocol Chrome's DevTools panel uses. Spoken over a WebSocket on `localhost:9222/devtools/...` when Chrome is launched with `--remote-debugging-port=9222`.
- **`userGesture`**: a CDP parameter on `Runtime.evaluate` that tags the call as user-initiated. Required for clicks on React-controlled buttons.
- **DraftJS**: Facebook's rich-text editor framework. X's tweet composer uses it. Why we need `Input.insertText` instead of DOM mutations.
- **`Input.insertText`**: CDP protocol method that inserts text into the currently focused element as if typed via keyboard.
- **`Page.captureScreenshot`**: CDP method that captures a PNG of the current page render. Works regardless of which window is foreground.
- **Trusted-input envelope**: macOS 14+ mechanism that lets processes synthesize OS input events that downstream apps treat as real user input. Absent on Monterey 12.x — why cua-driver clicks don't work on X.
