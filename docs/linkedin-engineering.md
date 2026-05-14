# LinkedIn engineering notes

Everything we learned about LinkedIn's DOM, navigation, and bot detection while building this. Pin this; it's the kind of knowledge that's painful to rediscover.

## TL;DR

- LinkedIn DOM strategy is **per-view**, not site-wide. Post-detail pages still use `data-testid` / `componentkey` / `aria-label` attributes. The **activity-page view** (`/in/me/recent-activity/all/`) rolled back to *named* class wrappers (`.update-components-text`, `.feed-shared-update-v2__description`) sometime between 2026-05-11 and 2026-05-14 — `componentkey` and `data-testid` are both **gone** from cards in that view. Always check both selector strategies per view.
- LinkedIn uses **TWO different URN forms for the same post**: `urn:li:activity:X` and `urn:li:ugcPost:Y`. The numeric IDs are different and there's no client-side mapping. We cache the pair as we scrape.
- Comments are wrapped in `replaceableComment_<comment_urn>` componentkeys.
- The comments section is a `LazyColumn` and requires JavaScript-driven `scrollIntoView` to render — `mouse.wheel` is not enough.
- The activity page's "View N comments" / "Comment" buttons are **deliberately broken for programmatic clicks** (likely a bot-protection mechanism). Three click methods all fail silently. Direct URL goto is the only path to a post detail page from there.
- The inline reply textbox shares the same `aria-label` as the top-level comment textbox. They're distinguished by DOM position only (the inline reply is a sibling of the parent comment).
- LinkedIn's contenteditable does **not** submit on Cmd+Enter. You have to click the actual submit button.
- LinkedIn auto-prepends the @-mention of the parent comment's author when you click Reply.

## DOM patterns

### Posts on the activity feed

```
<div data-urn="urn:li:activity:7444761787033346048"> ← outer card
    ↓ contains
  <div class="update-components-text">              ← post body lives here (2026-05-14)
    <span dir="ltr">...post commentary...</span>    ← also matchable as span[dir="ltr"]
  </div>
  <!-- Reshared posts (if any) appear in their own .update-components-text wrapper
       BELOW the original commentary. query_selector returns the first match, which
       is the user's own post body. Use query_selector_all if you need both. -->
  <a href="/in/<your-handle>"> ← author link (your own profile)
  <a href="/analytics/post-summary/urn:li:activity:<X>/"> ← the ONLY anchor
                                                            containing the activity URN
  <button aria-label="2 comments on Sal AI's post"> ← BLOCKED by bot detection
  <button aria-label="Comment">                     ← BLOCKED by bot detection
  <button>Like</button>
  <button>Repost</button>
  <button>Send</button>
</div>
```

**Note on the DOM regression (2026-05-14):** an earlier version of this doc claimed
the activity-page view had **no** `componentkey^="feed-commentary_"` body wrapper.
That was correct in spirit but missed the fact that the body still exists — under
class names, not attributes. Live DOM dump 2026-05-14 found **zero** `componentkey`
and **zero** `data-testid` attributes anywhere inside an activity-page card. The
working selectors for the post body on this view are now (in priority order):

1. `.update-components-text` — direct hit, body only
2. `.feed-shared-update-v2__description` — wider wrapper, same content
3. `[componentkey^="feed-commentary_"]` — legacy; kept as fallback in case LinkedIn rolls back

The fix is in `_scrape_posts` at [`lipy.py:741`](../skills/linkedin-engage/lipy.py#L741).

### Posts on the post-detail page (after navigation)

```
<div componentkey="feed-commentary_<uuid>">
  <span data-testid="expandable-text-box">
    <!-- post body text -->
  </span>
</div>

<!-- Comments LazyColumn -->
<div data-testid="...TOAQ-commentList..."
     data-component-type="LazyColumn"
     componentkey="...TOAQ-pagedCommentsContainer...">
   <!-- comments don't render until LazyColumn is in viewport -->
</div>

<!-- Each comment, once rendered -->
<div componentkey="replaceableComment_urn:li:comment:(urn:li:ugcPost:X,Y)">
  <a href="https://www.linkedin.com/in/<handle>/">
    <svg aria-label="View <Author Name>'s profile"> ← aria-label lives on the SVG
                                                       inside the <a>, not on <a>
  </a>
  <span data-testid="expandable-text-box">
    <!-- the comment text -->
  </span>
  ...
  <button>Reply</button> ← inside the parent comment
</div>

<!-- After clicking Reply on a specific comment, the inline reply box appears as
     a SIBLING of the parent comment (not inside it) -->
<div ...>
  <div contenteditable="true" role="textbox"
       aria-label="Text editor for creating comment">
    <!-- SAME aria-label as the top-level box — distinguish by DOM position -->
  </div>
  <button>Reply</button> ← the SUBMIT button for the inline reply
</div>
```

### The user's outbound comment history (`/in/me/recent-activity/comments/`)

Totally different rendering from post-detail pages. **No `data-testid`s, no `componentkey`s** in this view. The user's comment text is buried inside a generic `<div>` tree.

Extraction strategy: split `card.inner_text()` on newlines, find the **last** line that is exactly a time label (`1mo`, `3d`, etc.), and the comment text follows. Stop at action-bar tokens (`Like`, `Reply`, `Repost`, `Send`).

```python
time_re = re.compile(r"^\d+\s*(?:mo|[dhmwy])$", re.I)
lines = [ln.strip() for ln in card.inner_text().split("\n") if ln.strip()]
last_time_idx = max(i for i, ln in enumerate(lines) if time_re.match(ln))
comment_lines = []
STOP = {"Like", "Reply", "Repost", "Send", "Comment", "…more"}
for ln in lines[last_time_idx + 1:]:
    if ln in STOP or re.fullmatch(r"\d+", ln):
        break
    comment_lines.append(ln)
comment_text = " ".join(comment_lines)
```

## The URN problem

LinkedIn has at least two URN forms that refer to the same post:

- `urn:li:activity:7444761787033346048` — used by data-urn on activity-page cards, used in `/feed/update/` URLs
- `urn:li:ugcPost:7444761725255512064` — used inside `replaceableComment_<...>` componentkeys, used in `/feed/update/` URLs interchangeably

**The numeric IDs are NOT equal and have no client-side mapping.**

Both URN forms work in the URL pattern `linkedin.com/feed/update/<urn>/`. So you can navigate to a post via either form, but you can't translate between them without observing the same post in both contexts.

### Our solution: the URN cache

Stored at `~/.hermes/state/playwright/linkedin/urn_map.json`. Populated automatically:

- `_scrape_posts` learns activity URN + post text.
- `_scrape_comments` learns the activity URN (from the page URL) + ugcPost URN (from comment URNs) → links them.

After running `lipy comments` or `lipy inbound` on a post, that post's URN pair is cached. Future `lipy reply` calls can translate ugcPost → activity URN for navigation.

## The LazyColumn problem

Comments on the post-detail page live inside a `LazyColumn` component. By default they don't render — only the container exists. Triggering render requires bringing the container into the viewport.

`mouse.wheel(0, large_number)` is **not enough.** What works:

```python
page.evaluate("""() => {
    const sel = '[componentkey*="commentsSectionAnchor"], '
              + '[componentkey*="pagedCommentsContainer"]';
    const el = document.querySelector(sel);
    if (el) el.scrollIntoView({behavior: 'instant', block: 'center'});
}""")
```

JS-driven `scrollIntoView` triggers the IntersectionObserver hidden inside LazyColumn, which loads + renders the comments. Then wait 3–4 seconds.

## The reply-textbox problem (CRITICAL)

When the user clicks Reply on a specific comment, LinkedIn renders an **inline reply textbox** below that comment. The textbox has:

```html
<div contenteditable="true" role="textbox"
     aria-label="Text editor for creating comment">  ← SAME aria-label
                                                       as the top-level box!
```

If you find textboxes by aria-label and pick the first one, you grab the **top-level** "Add a comment" box at the top of the page — NOT the inline reply box you just opened. Your reply gets typed in the wrong place.

### How to distinguish

The inline reply box is a **sibling** (or descendant) of the parent comment element. Use DOM position, not aria-label:

```python
def _find_inline_reply_textbox(parent_el, page, timeout_ms=10_000):
    # 1. Try inside the parent comment.
    tb = parent_el.query_selector('div[contenteditable="true"][role="textbox"]')
    if tb and tb.is_visible():
        return tb
    # 2. Walk forward through siblings of the parent.
    tb = parent_el.evaluate_handle("""e => {
        let n = e.nextElementSibling;
        while (n) {
            const t = n.querySelector('div[contenteditable="true"][role="textbox"]');
            if (t && t.offsetParent !== null) return t;
            n = n.nextElementSibling;
        }
        return null;
    }""").as_element()
    return tb
```

We learned this the hard way — the first live reply attempt typed into the wrong box and didn't submit. Diagnostic dump revealed both textboxes had identical aria-labels, only DOM position differed.

## The submit problem

LinkedIn's contenteditable does **not** submit on Cmd+Enter (or Ctrl+Enter on Linux/Windows). You have to actually click the submit button.

The submit button appears (or becomes enabled) after the textbox has text. To find it:

```python
def _find_submit_button(textbox, label_options):
    # Walk up the DOM from the textbox looking for a button whose text matches.
    return textbox.evaluate_handle("""(el, labels) => {
        let n = el;
        while (n && n !== document.body) {
            for (const b of n.querySelectorAll('button')) {
                if (b.disabled) continue;
                const t = (b.innerText || '').trim().toLowerCase();
                if (labels.includes(t)) return b;
            }
            n = n.parentElement;
        }
        return null;
    }""", [s.lower() for s in label_options]).as_element()
```

Use `("reply",)` for inline replies, `("comment", "post")` for top-level comments.

## Bot detection: what's blocked, what isn't

We tested extensively. The picture:

### Works (programmatic clicks succeed)

- Clicking the **Reply** button inside a specific comment thread (the small Reply button next to a comment's Like/Reply controls)
- Clicking the blue **Reply / Comment** submit button after typing
- Clicking **Load more comments** / **Show previous comments** buttons
- Focusing/clicking the comment textbox (the contenteditable div)
- Clicking the **Comment** action button on the post-detail page

### Blocked (programmatic clicks register but do nothing)

- The **"View N comments on X's post"** button on activity-page cards
- The **"Comment"** action button on activity-page cards
- (Possibly other activity-page navigation buttons — not exhaustively tested)

### What we tried that all failed

For the blocked buttons, we tried:

1. **Bezier-curve mouse movement + click** (`human_click` via `human_actions.py`) — Bezier paths, sub-pixel jitter, real CDP mouse events with `isTrusted: true`
2. **Plain Playwright `.click()`** (the standard automation primitive)
3. **JavaScript `element.click()`** dispatched via `clickable.evaluate("e => e.click()")`

All three return without error. URL doesn't change. Comments don't expand. Nothing happens.

### Interpretation

LinkedIn appears to have **selective bot protection**. They don't broadly block automation (lots of legit accessibility tools depend on click events working) — they specifically lock down the buttons that scrapers most want, the ones that would navigate from "list of posts" to "post detail." Probably a JS handler that checks for some pre-condition (recent mouse-move trajectory, pointer event sequence, gesture state, CDP-connection detection — we can't tell from the outside) before allowing the SPA router to fire.

### Implications

- We **cannot** rely on click-through from the activity page. Direct URL goto is the only way to reach post detail from there.
- We mitigate by doing the goto **inside a long-running session that's been browsing** (feed → activity → post detail). The pattern resembles a real user opening a tab from a shared link or from search.
- For maximum safety, you can **manually navigate** to a post in the visible browser, and run `lipy reply` after. The script detects you're already on the post and skips its own navigation.

## Activity-page anchors: where they go

A full enumeration of `<a>` tags inside a single activity-page card (from a real diagnostic dump):

| Href | Goes to | Useful for nav? |
|---|---|---|
| `https://www.linkedin.com/in/<your-handle>?miniProfileUrn=...` | Your profile | ❌ (it's YOU) |
| `/in/<your-handle>/` | Your profile | ❌ |
| `/search/results/all/?keywords=%23AIAutomation&origin=HASH_TAG_FROM_FEED` | Search for a hashtag | ❌ |
| `/analytics/post-summary/urn:li:activity:<X>/` | Analytics for the post | ❌ (analytics, not detail) |

**No anchor to `/feed/update/<urn>/`.** The only path to post detail from the activity page is via a button click — and those are blocked. This is the design constraint that drives our use of direct URL navigation.

## Human emulation: what's worth doing

In order of how much it matters for ban-avoidance:

1. **Daily/weekly caps and pacing.** A user posting 25 thoughtful comments/day looks different from a bot posting 200/hour. The single biggest signal.
2. **Persistent profile.** Same browser fingerprint across sessions, "remember this device" state, real cookies. Fresh profile per action is a huge tell.
3. **Long-running session.** One warm Chromium for many actions, not boot-act-close per command.
4. **Activity windows.** Only act during business hours on weekdays (LinkedIn). 24/7 activity is suspicious.
5. **Long jitter on outbound.** 5–20 minutes between outbound comments. The exact ranges are in `jitter.yaml`.
6. **Realistic typing.** Per-character delays 70–180ms base, longer at punctuation, occasional "thinking" pauses, low typo rate. Cmd+V'ing a full string is a tell.
7. **Bezier-curve mouse movement** to elements before clicking. Better than teleporting.
8. **Read-dwell.** Spend time on a post proportional to its length before commenting. Real users read.
9. **Coming from feed.** Visit `/feed/` before navigating elsewhere. The HTTP referer + JS history matters.

The first 4 give you 80% of the protection. The rest are diminishing returns but stack.

## Things that probably aren't worth fighting

- **CDP-detection bypass.** There are public methods to detect a CDP-connected browser (timing of `Console.log` interception, certain runtime properties). Fighting these is cat-and-mouse forever.
- **Fingerprint randomization across runs.** Real users have ONE consistent fingerprint per device. Randomizing each run is itself a stronger signal of automation.
- **JavaScript-level event isTrusted spoofing.** Already `true` via CDP. Pursuing more doesn't help.

## Stable selectors (as of 2026-05-14)

These have survived several LinkedIn UI rolls. Document changes here when they break.

| Element | Selector | View |
|---|---|---|
| Activity-page post card | `[data-urn^="urn:li:activity:"]` | activity page |
| Post body on activity page | `.update-components-text` → `.feed-shared-update-v2__description` → `[componentkey^="feed-commentary_"]` (fallback) | activity page (changed 2026-05-14) |
| Post body on post-detail | `[componentkey^="feed-commentary_"]` | post-detail page |
| Single rendered comment | `[componentkey^="replaceableComment_urn:li:comment:"]` | post-detail page |
| Comments LazyColumn anchor | `[componentkey*="commentsSectionAnchor"], [componentkey*="pagedCommentsContainer"]` | post-detail page |
| Comment text body | `[data-testid="expandable-text-box"]` | post-detail page only; activity-page my-comments view has no `data-testid` at all |
| Comment input (top-level or inline) | `div[contenteditable="true"][role="textbox"]` (with `aria-label="Text editor for creating comment"`) | post-detail page |
| Author link | `a[href*="/in/"]` (aria-label lives on inner `<svg>` not the `<a>`) | post-detail page |
| Submit button (Reply, Comment) | walk up DOM from textbox, find button with matching innerText | post-detail page |
