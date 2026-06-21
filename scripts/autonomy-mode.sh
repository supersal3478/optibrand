#!/usr/bin/env bash
# autonomy-mode.sh — flip the brand-growth-engine into autonomous polling.
#
# What this does (in order, idempotent):
#   1. Validates required artifacts exist: BRAND.md filled, voice_profile.json,
#      lipy session warmed, CDP Chrome logged into X.
#   2. Flips config/caps.yaml to phase 2 + live: true for LinkedIn and X.
#      (Original is backed up to caps.yaml.bak on first run.)
#   3. Increases hold_buffer_inbound_seconds to 1800 (30-min review queue)
#      unless caller passes --no-hold.
#   4. Decides the X approval gate based on --x-path={cdp,api}.
#   5. Creates a minimal schedule.yaml if absent.
#   6. Registers two Hermes cron jobs:
#        li-inbound: minute 0,30 every hour
#        x-inbound:  minute 15,45 every hour
#      (Same platform never back-to-back; one platform runs every 15 min
#       during business hours per config/windows.yaml.)
#   7. Prints next steps for starting the gateway daemon + persisting it
#      via launchd.
#
# Re-runnable. Will not duplicate cron jobs; updates them in place.
#
# Usage:
#   ./scripts/autonomy-mode.sh                 # default: CDP path for X, hold-buffer ON
#   ./scripts/autonomy-mode.sh --x-path api    # use official X API (requires approval)
#   ./scripts/autonomy-mode.sh --no-hold       # post immediately, no review queue
#   ./scripts/autonomy-mode.sh --dry-run       # show what it would do, no changes
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_BIN="$PROJECT_ROOT/vendor/hermes-agent/.venv/bin/hermes"
CAPS="$PROJECT_ROOT/config/caps.yaml"
SCHEDULE="$PROJECT_ROOT/schedule.yaml"

X_PATH="cdp"
HOLD_BUFFER="1800"
DRY_RUN="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --x-path) X_PATH="$2"; shift 2 ;;
    --no-hold) HOLD_BUFFER="0"; shift ;;
    --dry-run) DRY_RUN="1"; shift ;;
    -h|--help) sed -n '1,30p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

c_red()    { printf "\033[31m%s\033[0m" "$*"; }
c_green()  { printf "\033[32m%s\033[0m" "$*"; }
c_yellow() { printf "\033[33m%s\033[0m" "$*"; }
c_blue()   { printf "\033[34m%s\033[0m" "$*"; }
step() { printf "\n%s %s\n" "$(c_blue '→')" "$*"; }
ok()   { printf "  %s %s\n" "$(c_green '✓')" "$*"; }
warn() { printf "  %s %s\n" "$(c_yellow '⚠')" "$*"; }
die()  { printf "  %s %s\n" "$(c_red '✗')" "$*" >&2; exit 1; }
run()  { if [[ "$DRY_RUN" == "1" ]]; then printf "  %s would run: %s\n" "$(c_yellow 'DRY')" "$*"; else eval "$@"; fi; }

# ─── 1. Validate artifacts ───────────────────────────────────────────────
step "Validating preconditions"

[[ -x "$HERMES_BIN" ]] || die "Hermes not installed. Run ./setup.sh first."
ok "hermes at $HERMES_BIN"

if grep -q '^\*\*Name:\*\*$' "$PROJECT_ROOT/BRAND.md" 2>/dev/null; then
  die "BRAND.md still has empty template placeholders. Fill it in first."
fi
ok "BRAND.md filled"

VOICE_PROFILE="$HERMES_HOME/memories/voice_profile.json"
if [[ ! -f "$VOICE_PROFILE" ]]; then
  die "Voice profile missing at $VOICE_PROFILE. Run ./scripts/voice-train.py first."
fi
ok "voice profile present"

# LinkedIn is OPTIONAL — a lapsed/absent session must not block X. If lipy is
# warmed we arm the LinkedIn inbound cadence too; otherwise we skip it (X-only).
LI_READY=0
if "$HOME/.local/bin/lipy" status 2>/dev/null | grep -q '"profile_present": true'; then
  LI_READY=1
  ok "LinkedIn Playwright session warmed (LinkedIn inbound will be armed)"
else
  warn "LinkedIn session not warmed — arming X only. To enable LinkedIn: lipy login --headed, then re-run."
fi

if ! curl -s -m 2 http://localhost:9222/json/version >/dev/null 2>&1; then
  warn "CDP Chrome not running. Will need 'start-chrome-cdp' before cron ticks fire."
else
  ok "CDP Chrome running on :9222"
fi

# ─── 2. Flip caps.yaml ─────────────────────────────────────────────────
step "Flipping config/caps.yaml to phase 2 (LinkedIn + X live)"

if [[ ! -f "$CAPS.bak" ]]; then
  run "cp '$CAPS' '$CAPS.bak'"
  ok "backed up to $CAPS.bak"
fi

# Use Python for surgical YAML edits — sed-on-YAML is fragile.
run "$PROJECT_ROOT/vendor/hermes-agent/.venv/bin/python - <<PYEOF
import re, sys
from pathlib import Path
caps = Path('$CAPS')
t = caps.read_text()
t = re.sub(r'^phase:\s*\d+', 'phase: 2', t, count=1, flags=re.M)
# Flip linkedin.live and x.live to true (only the section-level live, not nested)
t = re.sub(r'(^x:\s*\n\s*live:)\s*false', r'\1 true', t, flags=re.M)
t = re.sub(r'(^linkedin:\s*\n\s*live:)\s*false', r'\1 true', t, flags=re.M)
# Update hold buffer
t = re.sub(r'^hold_buffer_inbound_seconds:\s*\d+', 'hold_buffer_inbound_seconds: $HOLD_BUFFER', t, flags=re.M)
# X approval gate
if '$X_PATH' == 'cdp':
    t = re.sub(r'(\s+)requires_approval_flag:\s*true', r'\\1requires_approval_flag: false  # CDP path, not API', t)
caps.write_text(t)
print('caps.yaml updated')
PYEOF"
ok "caps.yaml flipped (phase=2, linkedin.live=true, x.live=true, hold_buffer=${HOLD_BUFFER}s)"

# ─── 3. schedule.yaml ─────────────────────────────────────────────────
step "Ensuring schedule.yaml exists"
if [[ ! -f "$SCHEDULE" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    warn "would create minimal schedule.yaml"
  else
    cat > "$SCHEDULE" <<YAMLEOF
# Minimal schedule.yaml created by autonomy-mode.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ).
# Add post entries under 'posts:' when you want the agent to publish + warm + monitor.
timezone: America/Toronto

defaults:
  goodwill_minutes_before: 30
  goodwill_count: 10
  monitor_hours_after: 24
  inbound_poll_minutes: 15
  goodwill_enabled: true

posts: []

recurring:
  daily_report:
    enabled: true
    time: "23:55"
  voice_retrain:
    enabled: true
    weekday: sunday
    time: "02:00"
YAMLEOF
    ok "created minimal $SCHEDULE"
  fi
else
  ok "$SCHEDULE already exists"
fi

# ─── 4. Cron jobs ─────────────────────────────────────────────────
step "Registering Hermes cron jobs (schedule-tick + daily-report + voice-retrain)"

# The schedule-tick orchestrator is the single source of truth for what runs
# when. It reads schedule.yaml every minute and decides: publish, monitor-poll,
# inbound-reply-post (after hold-buffer flush), daily report, weekly retrain.
# All caps/windows enforcement happens inside the tick — primitives stay dumb.
TICK_PROMPT="Run scripts/schedule-tick.py from $PROJECT_ROOT. It is a Python orchestrator (not an LLM prompt) — invoke it as a subprocess: cd $PROJECT_ROOT && $PROJECT_ROOT/vendor/hermes-agent/.venv/bin/python scripts/schedule-tick.py. Do NOT modify the prompt; the tick handles everything."

REPORT_PROMPT="Run scripts/activity-report.py from $PROJECT_ROOT. Invoke as: $PROJECT_ROOT/vendor/hermes-agent/.venv/bin/python $PROJECT_ROOT/scripts/activity-report.py --save. Writes ~/.hermes/reports/activity-YYYY-MM-DD.md (comments left, replies, likes, vetoes, skips, liveness + full ledger) from the real engagement logs (outbox.jsonl + engagement_metrics.jsonl)."

RETRAIN_PROMPT="Run scripts/voice-train.py --retrain from $PROJECT_ROOT. Invoke as: $PROJECT_ROOT/vendor/hermes-agent/.venv/bin/python $PROJECT_ROOT/scripts/voice-train.py --retrain. Weights original corpus 2x over agent-generated sent_replies.jsonl."

# Remove old jobs if present, then add fresh. The schedule-tick job replaces
# both li-inbound (0,30) and x-inbound (15,45) — schedule.yaml now drives
# everything via per-minute ticks.
#
# IMPORTANT: hermes cron remove takes IDs, not names. The old shell-loop
# remove-by-name silently failed, creating duplicates on every re-arm
# (caught 2026-06-17 — found 3× schedule-tick / daily-report / voice-retrain
# after two arming runs). Use a Python helper to look up IDs by name and
# remove them. Idempotent regardless of how many duplicates accumulated.
remove_by_name() {
  $PROJECT_ROOT/vendor/hermes-agent/.venv/bin/python - "$1" "$HERMES_BIN" <<'PYHELPER'
import subprocess, sys
target, hermes = sys.argv[1], sys.argv[2]
r = subprocess.run([hermes, 'cron', 'list'], capture_output=True, text=True)
current_id = None
ids_to_remove = []
for line in r.stdout.splitlines():
    s = line.strip()
    if s.endswith("[active]") or s.endswith("[paused]"):
        current_id = s.split()[0]
    elif current_id and "Name:" in s:
        name = s.split("Name:", 1)[1].strip()
        if name == target:
            ids_to_remove.append(current_id)
        current_id = None
for jid in ids_to_remove:
    subprocess.run([hermes, 'cron', 'remove', jid], capture_output=True)
if ids_to_remove:
    print(f"removed {len(ids_to_remove)} job(s) named '{target}'")
PYHELPER
}

run "remove_by_name li-inbound"
run "remove_by_name x-inbound"
run "remove_by_name schedule-tick"
run "remove_by_name daily-report"
run "remove_by_name voice-retrain"

run "$HERMES_BIN cron add --name schedule-tick '* * * * *' '$TICK_PROMPT'"
ok "schedule-tick: every minute (reads schedule.yaml + caps.yaml + windows.yaml)"

run "$HERMES_BIN cron add --name daily-report '55 23 * * *' '$REPORT_PROMPT'"
ok "daily-report: 23:55 every day"

run "$HERMES_BIN cron add --name voice-retrain '0 2 * * 0' '$RETRAIN_PROMPT'"
ok "voice-retrain: Sunday 02:00"

# ─── 4b. Phase-2 X engagement — human-rhythm cadence ──────────────────
#
# A SINGLE cadence-tick (every minute) decides when a human would actually act:
# 1-2 bursty feed-goodwill sessions a day at jittered times, and inbound replies
# that cluster after your posts get comments and then decay. This REPLACES the
# old flat crons (goodwill ×6, inbound every 10m, commenter every 15m), which
# were a robotic detection signal. Behavior knobs: config/cadence.yaml; session
# hours come from config/windows.yaml. Inspect a day: cadence-tick.py --simulate.
CADENCE_PROMPT="Subprocess this exact command and return its stdout: cd $PROJECT_ROOT && $PROJECT_ROOT/vendor/hermes-agent/.venv/bin/python $PROJECT_ROOT/scripts/cadence-tick.py --platform x"

CADENCE_LI_PROMPT="Subprocess this exact command and return its stdout: cd $PROJECT_ROOT && $PROJECT_ROOT/vendor/hermes-agent/.venv/bin/python $PROJECT_ROOT/scripts/cadence-tick.py --platform linkedin"

FLUSH_PROMPT="Subprocess this exact command and return its stdout: cd $PROJECT_ROOT && $PROJECT_ROOT/vendor/hermes-agent/.venv/bin/python $PROJECT_ROOT/scripts/outbox-flush.py --batch 1"

# Remove ALL prior engagement cron entries (flat-cron names from earlier runs)
# so re-arming cleanly swaps to the cadence model.
for J in \
    goodwill-0130 goodwill-0630 goodwill-0830 goodwill-1530 goodwill-1730 goodwill-2130 \
    goodwill-0330 goodwill-0530 goodwill-0930 goodwill-1330 goodwill-1830 goodwill-2030 \
    inbound-monitor engage-commenter cadence-tick cadence-tick-li outbox-flush; do
  run "remove_by_name $J"
done

# Cadence (X) — one tick/minute; fires goodwill + inbound ONLY when a human would.
run "$HERMES_BIN cron add --name cadence-tick '* * * * *' '$CADENCE_PROMPT'"
ok "cadence-tick: every minute (X human-rhythm sessions + inbound decay)"

# Cadence (LinkedIn) — inbound replies only (no LinkedIn goodwill/feed path yet).
# Armed only if lipy is logged in.
if [[ "${LI_READY:-0}" == "1" ]]; then
  run "$HERMES_BIN cron add --name cadence-tick-li '* * * * *' '$CADENCE_LI_PROMPT'"
  ok "cadence-tick-li: every minute (LinkedIn feed goodwill + inbound replies, human decay)"
else
  warn "cadence-tick-li: SKIPPED (LinkedIn not warmed — lipy login --headed, then re-run)"
fi

# Outbox flush — submits + likes matured drafts. Every 2 min.
run "$HERMES_BIN cron add --name outbox-flush '*/2 * * * *' '$FLUSH_PROMPT'"
ok "outbox-flush: every 2 min (humanly submits + likes from outbox)"

# NOTE: engage-commenter (engaging your commenters' own audiences) is no longer
# auto-scheduled — it can be folded into the cadence later if you want it back.

# ─── 5. Summary ─────────────────────────────────────────────────
cat <<EOF

$(c_green '════════════════════════════════════════════════════════════════')
$(c_green '  Autonomy mode armed.')
$(c_green '════════════════════════════════════════════════════════════════')

Configuration applied:
  • phase=2, linkedin.live=true, x.live=true
  • hold_buffer_inbound_seconds=${HOLD_BUFFER} ($([ "$HOLD_BUFFER" = "0" ] && echo 'immediate post' || echo 'review queue'))
  • X path: ${X_PATH}
  • Cron jobs registered:
      schedule-tick  — every minute  (legacy orchestrator)
      daily-report   — 23:55 daily
      voice-retrain  — Sunday 02:00
      goodwill x6    — SGT 01:30 / 06:30 / 08:30 / 15:30 / 17:30 / 21:30 (T-30 before each SGT post)
      inbound-mon    — every 10 min  (replies to YOUR comments)
      eng-commenter  — every 15 min  (goodwill follow-up on commenters)
      outbox-flush   — every 2 min   (humanly submits + likes from outbox)

$(c_yellow 'NEXT STEPS:')

  1. $(c_blue 'Smoke-test the tick manually before letting cron drive it:')
       $PROJECT_ROOT/vendor/hermes-agent/.venv/bin/python $PROJECT_ROOT/scripts/schedule-tick.py --dry-run --verbose
     Inspect any drafts that land in ~/.hermes/queue/.

  2. $(c_blue 'Start the gateway daemon:')
       hermes gateway start
     Watch with: hermes logs --follow

  3. $(c_blue 'Persist the daemon across reboots:')
       cp config/launchd/com.brandgrowthengine.hermes.plist ~/Library/LaunchAgents/
       launchctl load ~/Library/LaunchAgents/com.brandgrowthengine.hermes.plist
     Edit the plist first to set absolute paths if HERMES_HOME differs.

  4. $(c_blue 'Keep the Mac awake on lid close:')
       sudo pmset -a sleep 0          # never sleep on AC
       sudo pmset -a disablesleep 1   # never sleep even with lid closed
     (Cleaner: dedicate an always-plugged Mac.)

To halt instantly: edit config/caps.yaml → set linkedin.live or x.live to false.
Caps are re-read every tick, no restart needed.
EOF
