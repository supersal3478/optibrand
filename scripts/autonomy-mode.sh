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

if ! "$HOME/.local/bin/lipy" status 2>/dev/null | grep -q '"profile_present": true'; then
  die "LinkedIn session not warmed. Run: lipy login --headed"
fi
ok "LinkedIn Playwright session warmed"

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
step "Registering Hermes cron jobs (15-min offset cadence)"

LI_PROMPT="Run the linkedin-engage inbound pass. Use lipy inbound to fetch new comments on my recent LinkedIn posts since the last run. For each new comment: draft a reply via reply-drafter (it will load voice_profile.json + BRAND.md), validate via brand-guard, and queue (do NOT post directly — let the hold buffer in caps.yaml apply). Respect config/windows.yaml — skip the entire pass if outside the linkedin inbound window. Apply config/jitter.yaml delays between actions. Append every outcome to ~/.hermes/memories/sent_replies.jsonl."

X_PROMPT="Run the x-engage inbound pass. Ensure the CDP Chrome at :9222 is up (start-chrome-cdp if not). Use cdp_eval.py to read x.com/notifications/mentions and the user's recent post threads for new replies/mentions since the last run. For each new item: draft a reply via reply-drafter, validate via brand-guard, and queue (do NOT post directly). Respect config/windows.yaml. Apply config/jitter.yaml. Append every outcome to ~/.hermes/memories/sent_replies.jsonl."

# Remove existing jobs if present, then add fresh.
run "$HERMES_BIN cron remove li-inbound 2>/dev/null || true"
run "$HERMES_BIN cron remove x-inbound 2>/dev/null || true"

run "$HERMES_BIN cron add --name li-inbound --schedule '0,30 * * * *' --prompt '$LI_PROMPT'"
ok "li-inbound: minute 0,30 every hour"

run "$HERMES_BIN cron add --name x-inbound --schedule '15,45 * * * *' --prompt '$X_PROMPT'"
ok "x-inbound: minute 15,45 every hour"

# ─── 5. Summary ─────────────────────────────────────────────────
cat <<EOF

$(c_green '════════════════════════════════════════════════════════════════')
$(c_green '  Autonomy mode armed.')
$(c_green '════════════════════════════════════════════════════════════════')

Configuration applied:
  • phase=2, linkedin.live=true, x.live=true
  • hold_buffer_inbound_seconds=${HOLD_BUFFER} ($([ "$HOLD_BUFFER" = "0" ] && echo 'immediate post' || echo 'review queue'))
  • X path: ${X_PATH}
  • Two cron jobs registered (li-inbound 0,30; x-inbound 15,45)

$(c_yellow 'NEXT STEPS:')

  1. $(c_blue 'Smoke-test each job manually before letting cron tick:')
       hermes cron run li-inbound
       hermes cron run x-inbound
     Inspect the drafts that land in the review queue.

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
