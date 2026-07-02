# Human-rhythm cadence

Makes the engagement loop act like a person instead of a flat-interval bot. This
is the single highest-leverage stealth lever — realistic *volume and rhythm*
matter more than any input trick, because behavioral cadence is what platforms
score over weeks.

## What it does

- **Outbound (feed goodwill):** 1–2 bursty **sessions** a day at jittered times,
  a handful of comments total with human gaps between them, and occasional
  **skip days**. Not an all-day poll.
- **Inbound (replies on your posts):** an adaptive **decay** — checks densely
  while there's fresh comment activity, tapers over the next hours, then drops to
  a background glance a couple times a day. Anchored to *activity*, not a flat
  24/7 timer (works even though your posts come from another app).

All the existing safety bounds (caps, windows, jitter, hold-buffer, the variance
gate, humanized typing/mouse) still apply underneath — cadence just decides
*when* a human would act.

## How it's built

| Piece | Role |
|---|---|
| `config/cadence.yaml` | The knobs: sessions/day, session length, comments/day, gaps, skip-day prob, inbound decay tiers. Per platform. |
| `scripts/_cadence.py` | Pure logic. Builds a **deterministic** daily plan from a per-machine salt (same plan all day, varies day-to-day), and answers "should I act now?" using the metrics log as state. |
| `scripts/cadence-tick.py` | One cron, every minute. Asks `_cadence`; fires one feed-goodwill browse and/or one inbound check when due. Replaces the old flat crons. |

State lives in the metrics log: today's `queued_outbox` count = budget used;
`cadence_fire` events = when we last acted. No new state files; survives restarts.

## Inspect & tune

```bash
# See a simulated human day (sessions, comment times, decay tiers) — no side effects:
./vendor/hermes-agent/.venv/bin/python scripts/cadence-tick.py --simulate 2026-06-22

# Decide for right now, fire nothing:
./vendor/hermes-agent/.venv/bin/python scripts/cadence-tick.py --dry-run --verbose
```

- **Behavior numbers:** edit `config/cadence.yaml` (e.g. `daily_actions`,
  `sessions_per_day`, `skip_day_prob`). Takes effect next tick.
- **What hours sessions can land in:** `config/windows.yaml`. The X outbound
  window is currently `00:00–23:59`, so sessions can land at 5am. Narrow it
  (e.g. `08:00–22:00`) to keep activity in natural hours.

## Wiring

`autonomy-mode.sh` registers a single `cadence-tick` cron (every minute) plus
`outbox-flush` (every 2 min). It **replaces** the old goodwill-×6 /
inbound-every-10m / commenter-every-15m crons. Re-run `autonomy-mode.sh` to pick
up the change. (`engage-commenter` is no longer auto-scheduled; it can be folded
into the cadence later.)

## Recovery ramp (added 2026-07-02)

After a rate-limit / restriction event, don't switch back on at full volume —
X scores that as a bot resuming. The `recovery:` block in `config/cadence.yaml`
phases activity back in, **identically for X and LinkedIn** (one engine, one
config):

| Stage | Weeks | Outbound | Inbound |
|---|---|---|---|
| 1 | 1–2 | **disabled** | quiet glances only (forced), ≤4 replies/day |
| 2 | 3–4 | 1 session, 30–45 min, 1–2 comments, 40% skip-days | dense relaxed to 30–45 min, ≤6/day |
| 3 | 5–6 | up to 2 sessions, 2–4 comments, 25% skip-days | ≤8/day |
| — | 7+ | normal platform config | normal decay |

Set `recovery.start_date` to the day you re-enable; delete it (or
`enabled: false`) to turn the ramp off. Stage values can only **reduce**
activity relative to a platform's baseline (volume ranges min'd, wait
intervals max'd, skip-day prob max'd), so LinkedIn's lighter defaults are
never raised by a stage.

Three read-side guards run underneath, because **views are what platforms
rate-limit**:

- **View budget** — every page navigation is logged (`page_view` events);
  `caps.yaml <platform>.reads.page_views_per_day` stops all cadence firing
  when spent (X: 60, LinkedIn: 40).
- **Rate-limit circuit breaker** — the X scrapers detect the "rate limit"
  error page and log `rate_limited`; the cadence then stands down for a
  jittered `rate_limit_cooldown_hours`–2× (X: 3h, LinkedIn: 6h) instead of
  refreshing into the limit.
- **Cheap inbound checks (X)** — most checks hit only `/notifications/mentions`
  (1 page load); the expensive full profile sweep (profile + every permalink +
  back-nav ≈ 10–15 loads) runs once per day.

## Defaults (recommended starting point)

- **X outbound:** 1–2 sessions/day, 25–60 min each, 3–8 comments/day, 5–15 min
  gaps, 15% skip-day.
- **X inbound:** dense 12–25 min for 2h of fresh activity → taper 45–90 min for
  10h → quiet 150–240 min background; up to 12 replies/day.
- **LinkedIn:** lighter across the board (1 session, 1–4 comments, 25% skip) —
  higher restriction risk. Inherits the engine once LinkedIn is autonomous.
