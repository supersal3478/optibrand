# Operations & Changes — 2026-06-21 session

Full record of the work done in this session, plus how to run the system. This
is both a changelog and an operations runbook. The branch is `macmini-live`.

---

## TL;DR — what changed

| Area | Change |
|---|---|
| **Start** | New `scripts/start.sh` — ONE command brings the whole system live (heal venv → Chrome+login → arm → gateway). Self-heals the move-breakage below. |
| **Observability** | New `scripts/activity-report.py` — chronological ledger of every comment/reply/like + daily counts + a liveness/heartbeat line. The old `daily-report.py` read the wrong log and showed nothing. |
| **Heartbeat** | `outbox-flush.py` now writes `~/.hermes/state/heartbeat.json` every run (and a `session_logged_out` event) so a **silent stop becomes visible**. |
| **Stability** | `_cdp.py` websocket connect timeout (no more infinite hang); `schedule-tick.py` audit-log tail-read + rotation + crash guard. |
| **Daily report** | `autonomy-mode.sh` repointed the 23:55 cron from the broken `daily-report.py` to `activity-report.py --save`. |
| **Move fix** | Found + fixed that moving the project folder bricked the Hermes venv (baked-in absolute paths). `start.sh` now self-heals it. |

Earlier in the same thread (already on this branch via `main`): fresh-laptop
install hardening (`preflight.sh`, auto cua-driver, pinned Hermes), the
DeepSeek-V4 Flash/Pro model policy, the committed Azure key (standalone repo),
and the explicit "start the agent" step.

---

## 1. How the X engagement system works

This is an **engagement & growth tool**, not a publisher. Original posts come
from a different app. Its two jobs:

1. **Outbound feed engagement** — comment on (and like) others' posts in the niche.
2. **Inbound replies** — reply to comments people leave on your posts (it reads
   `x.com/notifications/mentions`, so it catches comments on posts made by *any*
   app, as long as it's the same X account).

Everything drives a dedicated Chrome over the **Chrome DevTools Protocol (CDP)**
on port 9222, logged into your X account. No X API. Every keystroke/click/scroll
routes through `x_human.py` (humanized typing, curved mouse, eased scroll) so it
doesn't fingerprint as a bot.

```
launchd (optional)  →  Hermes gateway (daemon)  →  fires cron jobs:
  every 2 min  → outbox-flush.py        (posts queued comments + a like on each)
  every 10 min → inbound-engagement.py  (reply to your commenters)
  every 15 min → engage-commenter.py    (engage your commenters' audiences)
  6×/day       → feed-engagement.py     (goodwill on the home feed)
  23:55 daily  → activity-report.py     (writes the day's report)
```

Drafts pass `brand-guard`, then land in `~/.hermes/state/outbox.jsonl` and mature
for a hold buffer (30 min inbound / 5 min outbound) before `outbox-flush` submits
them. Flip `config/caps.yaml: x.live: false` to cancel everything on the next tick.

---

## 2. Running it

### The single command (routine start)

```bash
./scripts/start.sh
```
Idempotent. It (1) heals the venv if the folder moved, (2) brings up the CDP
Chrome and confirms X login, (3) arms autonomy the first time (skips if already
armed), (4) starts the gateway. After this it runs itself; logging + heartbeat +
the daily report are automatic.

### First-time on a machine (or after pulling new code)

```bash
git pull                       # get the latest macmini-live
./scripts/autonomy-mode.sh     # (re)register the cron jobs — REQUIRED to pick up
                               #  the new activity-report cron after a pull
./scripts/start.sh             # bring it live
```

> Why `autonomy-mode.sh` after a pull: `start.sh` skips re-arming when cron jobs
> already exist, so it won't refresh a changed cron (like the new daily report).
> Run `autonomy-mode.sh` once after pulling to refresh, then `start.sh` is your
> single command from then on.

### Persistence across reboots (optional, one-time)

Install the launchd plist (ONBOARDING.md stage 10). Then the gateway auto-starts
on boot and survives crashes — you don't run anything; it's always on.

---

## 3. Operating day-to-day

```bash
# What did it do today? (counts + full ledger + liveness)
./vendor/hermes-agent/.venv/bin/python scripts/activity-report.py

# A specific day, a week, or a CSV export of the ledger:
... scripts/activity-report.py --date 2026-06-17
... scripts/activity-report.py --days 7
... scripts/activity-report.py --csv /tmp/ledger.csv

# Stop instantly (cancels queued drafts next tick, no restart):
#   edit config/caps.yaml → x.live: false
# Full stop:
./vendor/hermes-agent/.venv/bin/hermes gateway stop
```

The report's **"Is it running?"** section is the silent-stop detector: it shows
the last heartbeat, whether Chrome is up, and whether the X session is still
logged in. If it says `LOGGED OUT ⚠️`, the loop is alive but blind — log back in.

---

## 4. What was built / fixed this session (detail)

### 4.1 `scripts/start.sh` (new) — the single start command
Heals the venv, ensures Chrome + X login, arms autonomy on first run, starts the
gateway, prints watch/stop help. Idempotent and self-healing.

### 4.2 `scripts/activity-report.py` (new) — observability
Reads the **real** logs — `~/.hermes/state/outbox.jsonl` (full comment text,
target post, author, status, timestamps) joined with
`~/.hermes/logs/engagement_metrics.jsonl` (the `liked` events) — and produces:
- a **ledger**: every interaction with time, type, author, the text left, the
  post URL, whether it was liked, and status;
- a **summary**: comments on feed, replies to your posts, commenter follow-ups,
  likes given, brand-guard vetoes, skips by reason, failures;
- a **liveness line** from the heartbeat.
Markdown to stdout, `--save` to `~/.hermes/reports/activity-<date>.md`, `--csv`.

Why it was needed: the old `daily-report.py` reads `~/.hermes/logs/audit.jsonl`,
which only the *scheduler* writes — not the engagement pipeline. So it never
showed actual comments/likes/replies. `activity-report.py` reads the right files.

### 4.3 Heartbeat + logout detection — `outbox-flush.py`
On every run (every 2 min) it writes `~/.hermes/state/heartbeat.json`
(`{ts, chrome_up, logged_in, last_counts}`) and, only when actually logged out,
a `session_logged_out` metric event. It does NOT log a per-run metric event (that
would bloat the metrics log ~720 lines/day) — the JSON sentinel is the source.

### 4.4 Stability hardening
- **`skills/x-engage/_cdp.py`**: `websockets.connect(...)` now has
  `open_timeout=15, close_timeout=5`. Previously a wedged tab could hang a cron
  tick forever.
- **`scripts/schedule-tick.py`**: the audit log is now **tail-read** (last ~2 MB)
  instead of whole-file every minute (the #1 slow-death risk), **auto-rotates**
  past 25 MB (archives + keeps the recent tail), and `main()` has a **crash guard**
  so a bad tick logs and exits cleanly instead of throwing an unhandled traceback.

### 4.5 Daily report rewiring — `scripts/autonomy-mode.sh`
The 23:55 `daily-report` cron now runs `activity-report.py --save` instead of the
broken `daily-report.py`. (Requires re-running `autonomy-mode.sh` to take effect.)

### 4.6 The folder-move gotcha (important)
Moving the project folder (here: into `projects/main/`) **bricks the Hermes
venv** — the venv bakes the project's absolute path into (a) every console-script
shebang and (b) the editable-install finder. Symptom: `hermes` dies with
`No module named 'hermes_cli'`. Fixed on this desktop; `start.sh` now self-heals
it automatically. **Takeaway: prefer not to move the folder; if you must,
`start.sh` (or re-running `setup.sh`) repairs it.**

---

## 5. Verified by testing this session

- **Read path** works: logged into X, can navigate/read the feed.
- **Humanized write path works** (the one the whole engagement pipeline uses):
  `x_human.type_into` filled the composer and enabled Post — confirmed live.
- **Orchestrator + outbox** dry-runs are clean; kill-switches honored.
- **Heartbeat + report** verified live: heartbeat wrote `chrome_up: true,
  logged_in: true`; the report rendered the ledger + counts + liveness correctly.
- **Known dead-end, not used by you:** `publish.py` (scheduled *original* posts)
  uses a raw DOM-focus that the X composer ignores, so its text never lands and it
  falsely reports success. Irrelevant to this tool (you don't publish originals
  here) — left as-is. If ever needed, route it through `x_human.type_into`.

---

## 6. Deferred (lower-risk, not done)

- Stale-lock window in `outbox-flush.py` (15 min) — tiny double-post chance under
  a long hang. Could shorten or make PID-aware.
- `engagement_metrics.jsonl` slow growth — `iter_events` reads from the file head;
  could get a tail optimization like the audit log eventually.
- `autonomy-mode.sh` requires a warmed LinkedIn (`lipy`) session and will `die`
  without one — a friction point for an X-only setup; relax if LinkedIn is unused.

---

## 7. Files touched this session

| File | Type | What |
|---|---|---|
| `scripts/start.sh` | new | single start command + venv self-heal |
| `scripts/activity-report.py` | new | ledger + summary + liveness report |
| `scripts/outbox-flush.py` | edit | heartbeat + logout detection |
| `scripts/schedule-tick.py` | edit | audit tail-read + rotation + crash guard |
| `skills/x-engage/_cdp.py` | edit | websocket connect timeouts |
| `scripts/autonomy-mode.sh` | edit | daily report → activity-report.py --save |
| `docs/operations-and-changes-2026-06-21.md` | new | this document |
