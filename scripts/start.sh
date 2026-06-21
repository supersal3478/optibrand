#!/usr/bin/env bash
# start.sh — ONE command to bring the whole X engagement system live.
#
# Idempotent and self-healing. Run it anytime; it does, in order:
#   1. Heal the venv if the project folder was moved. Moving/copying a checkout
#      breaks Hermes because the venv bakes an ABSOLUTE path into its console
#      scripts and its editable-install finder. This repairs both in place.
#   2. Bring up the dedicated CDP Chrome and confirm you're logged into X
#      (everything depends on this — it stops with a clear message if not).
#   3. Arm autonomy (caps live + cron jobs) the first time, via autonomy-mode.sh.
#      On later runs it sees the jobs already registered and skips re-arming.
#   4. Start the Hermes gateway daemon — the process that fires the cron jobs.
#   5. Print how to watch and how to stop.
#
# After this, it runs itself. For reboot-persistence, install the launchd plist
# (bootstrap.sh stage 10) — then you don't even run this.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
VENV="$PROJECT_ROOT/vendor/hermes-agent/.venv"
HERMES_PY="$VENV/bin/python"
HERMES_BIN="$VENV/bin/hermes"

c_red()   { printf "\033[31m%s\033[0m" "$*"; }
c_green() { printf "\033[32m%s\033[0m" "$*"; }
c_yellow(){ printf "\033[33m%s\033[0m" "$*"; }
c_blue()  { printf "\033[34m%s\033[0m" "$*"; }
step() { printf "\n%s %s\n" "$(c_blue '━━━')" "$*"; }
ok()   { printf "  %s %s\n" "$(c_green '✓')" "$*"; }
warn() { printf "  %s %s\n" "$(c_yellow '⚠')" "$*"; }
die()  { printf "  %s %s\n" "$(c_red '✗')" "$*" >&2; exit 1; }

# ─── 1. Heal the venv if the folder was moved ─────────────────────────────
step "1/4  Hermes launcher"
[[ -x "$HERMES_BIN" ]] || die "No Hermes venv at $VENV. Run ./setup.sh first."
if "$HERMES_BIN" --version >/dev/null 2>&1; then
  ok "Hermes launcher works ($("$HERMES_BIN" --version 2>/dev/null | head -1))"
else
  warn "Hermes launcher broken — the project folder was moved. Self-healing the venv…"
  real_py="$(ls "$VENV"/bin/python3.* 2>/dev/null | grep -v config | head -1)"
  [[ -n "$real_py" ]] || die "No python in $VENV/bin. Re-run ./setup.sh."
  # (a) Repair console-script shebangs that point at the old absolute path.
  for f in "$VENV"/bin/*; do
    [[ -f "$f" ]] || continue
    if head -1 "$f" 2>/dev/null | grep -qE '^#!.*/\.venv/bin/python'; then
      sed -i '' "1s|^#!.*|#!$real_py|" "$f"
    fi
  done
  # (b) Repair the editable-install finder's baked project path.
  finder="$(ls "$VENV"/lib/python3.*/site-packages/__editable___hermes_agent_*_finder.py 2>/dev/null | head -1)"
  if [[ -n "$finder" ]]; then
    "$HERMES_PY" - "$finder" "$PROJECT_ROOT/vendor/hermes-agent" <<'PYEOF'
import re, sys
finder, correct = sys.argv[1], sys.argv[2]
t = open(finder).read()
m = re.search(r"(/[^\"',\s]+)/vendor/hermes-agent", t)
if m and m.group(1) + "/vendor/hermes-agent" != correct:
    t = t.replace(m.group(1), correct.rsplit("/vendor/hermes-agent", 1)[0])
    open(finder, "w").write(t)
PYEOF
  fi
  "$HERMES_BIN" --version >/dev/null 2>&1 \
    && ok "venv healed — Hermes launcher works again" \
    || die "Could not heal the venv automatically. Re-run ./setup.sh."
fi

# ─── 2. CDP Chrome + X login ──────────────────────────────────────────────
step "2/4  CDP Chrome + X login"
bash "$PROJECT_ROOT/skills/x-engage/start-chrome-cdp.sh" >/dev/null 2>&1 || true
if ! curl -s -m 3 http://localhost:9222/json/version >/dev/null 2>&1; then
  die "CDP Chrome didn't come up. Check /tmp/chrome-cdp.log"
fi
"$HERMES_PY" "$PROJECT_ROOT/skills/x-engage/cdp_eval.py" --navigate 'https://x.com/home' >/dev/null 2>&1 || true
sleep 3
LOGGED="$("$HERMES_PY" "$PROJECT_ROOT/skills/x-engage/cdp_eval.py" --tab-url-substring x.com \
  --expr 'JSON.stringify({li:!!document.querySelector("[data-testid=SideNav_AccountSwitcher_Button]")})' \
  2>/dev/null || echo '{"li":false}')"
if echo "$LOGGED" | grep -q '"li":true'; then
  ok "X session logged in"
else
  die "Not logged into X. A Chrome window is open — log in (incl. 2FA), then re-run ./scripts/start.sh"
fi

# ─── 3. Arm autonomy (first run only) ─────────────────────────────────────
step "3/4  Autonomy (caps live + cron jobs)"
if "$HERMES_BIN" cron list 2>/dev/null | grep -q "outbox-flush"; then
  ok "cron jobs already registered (already armed)"
else
  warn "not armed yet — running autonomy-mode.sh to register the cron jobs…"
  "$PROJECT_ROOT/scripts/autonomy-mode.sh"
fi

# ─── 4. Start the gateway daemon ──────────────────────────────────────────
step "4/4  Hermes gateway (fires the cron jobs)"
if pgrep -f "hermes.*gateway" >/dev/null 2>&1; then
  ok "gateway already running"
elif launchctl list 2>/dev/null | grep -q brandgrowthengine; then
  ok "gateway managed by launchd (persistent across reboots)"
else
  mkdir -p "$HERMES_HOME/logs"
  nohup "$HERMES_BIN" gateway run >"$HERMES_HOME/logs/gateway.out" 2>&1 &
  disown
  sleep 3
  if pgrep -f "hermes.*gateway" >/dev/null 2>&1; then
    ok "gateway started (log: $HERMES_HOME/logs/gateway.out)"
  else
    warn "gateway may not have started — check $HERMES_HOME/logs/gateway.out"
  fi
fi

cat <<EOF

$(c_green '════════════════════════════════════════════════════════════════')
$(c_green '  System is live. It now runs itself — no further commands needed.')
$(c_green '════════════════════════════════════════════════════════════════')

  Watch what it does:
    $HERMES_PY scripts/activity-report.py

  Stop instantly (cancels queued drafts on the next tick):
    edit config/caps.yaml → set x.live: false

  Full stop:
    $HERMES_BIN gateway stop   (or: pkill -f "hermes.*gateway")

  Survive reboots (one-time): install the launchd plist — see ONBOARDING.md.
EOF
