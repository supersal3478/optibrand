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

## Defaults (recommended starting point)

- **X outbound:** 1–2 sessions/day, 25–60 min each, 3–8 comments/day, 5–15 min
  gaps, 15% skip-day.
- **X inbound:** dense 12–25 min for 2h of fresh activity → taper 45–90 min for
  10h → quiet 150–240 min background; up to 12 replies/day.
- **LinkedIn:** lighter across the board (1 session, 1–4 comments, 25% skip) —
  higher restriction risk. Inherits the engine once LinkedIn is autonomous.
