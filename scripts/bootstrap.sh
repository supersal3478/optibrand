#!/usr/bin/env bash
# bootstrap.sh — interactive new-laptop walkthrough.
#
# Run this AFTER ./setup.sh has cloned Hermes + installed venvs + symlinked
# skills. This script walks the manual gates (cua-driver, credentials, X login,
# LinkedIn login, BRAND.md fill, voice corpus, schedule.yaml, autonomy arm,
# launchd) and validates each before continuing.
#
# Idempotent. Re-running picks up at the first un-passed gate.
#
# Usage:
#   ./scripts/bootstrap.sh               # interactive
#   ./scripts/bootstrap.sh --dry-run     # show what each gate would check; no prompts
#   ./scripts/bootstrap.sh --skip-x      # skip the X login + cua-driver gates
#   ./scripts/bootstrap.sh --skip-li     # skip the LinkedIn login gate
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_PY="$PROJECT_ROOT/vendor/hermes-agent/.venv/bin/python"
HERMES_BIN="$PROJECT_ROOT/vendor/hermes-agent/.venv/bin/hermes"
LIPY_BIN="$HOME/.local/bin/lipy"

DRY_RUN=0
SKIP_X=0
SKIP_LI=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --skip-x)  SKIP_X=1;  shift ;;
    --skip-li) SKIP_LI=1; shift ;;
    -h|--help) sed -n '1,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

c_red()    { printf "\033[31m%s\033[0m" "$*"; }
c_green()  { printf "\033[32m%s\033[0m" "$*"; }
c_yellow() { printf "\033[33m%s\033[0m" "$*"; }
c_blue()   { printf "\033[34m%s\033[0m" "$*"; }
c_bold()   { printf "\033[1m%s\033[0m" "$*"; }
step()  { printf "\n%s %s\n" "$(c_blue '━━━')" "$(c_bold "$*")"; }
ok()    { printf "  %s %s\n" "$(c_green '✓')" "$*"; }
warn()  { printf "  %s %s\n" "$(c_yellow '⚠')" "$*"; }
die()   { printf "  %s %s\n" "$(c_red '✗')" "$*" >&2; exit 1; }
ask()   { printf "  %s %s " "$(c_yellow '?')" "$*"; }
pause() {
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "  [dry-run] would prompt: $*"
    return
  fi
  ask "$* Press ENTER to continue, Ctrl-C to abort."
  read -r _
}

# ─── 1. Verify setup.sh has been run + ~/.local/bin on PATH ───────
step "1/12  Verify ./setup.sh has been run"
[[ -x "$HERMES_BIN" ]] || die "Hermes venv missing at $HERMES_BIN. Run ./setup.sh first."
ok "Hermes venv present"
[[ -d "$HERMES_HOME/skills" ]] || die "$HERMES_HOME/skills missing. Re-run ./setup.sh."
ok "$HERMES_HOME/skills exists"
[[ -f "$HERMES_HOME/.env" ]] || die "$HERMES_HOME/.env missing. Re-run ./setup.sh."
ok "$HERMES_HOME/.env exists"

# Detect whether ~/.local/bin is on PATH. setup.sh puts `lipy` and
# `start-chrome-cdp` there — if PATH excludes it, every later stage that calls
# `lipy ...` directly will silently `command not found`.
case ":$PATH:" in
  *":$HOME/.local/bin:"*)
    ok "\$HOME/.local/bin is on PATH"
    ;;
  *)
    warn "\$HOME/.local/bin is NOT on PATH — \`lipy\` and \`start-chrome-cdp\` won't resolve."
    # Find the user's shell rc.
    SHELL_RC=""
    case "$(basename "${SHELL:-}")" in
      zsh)  SHELL_RC="$HOME/.zshrc" ;;
      bash) SHELL_RC="$HOME/.bash_profile" ;;
      *)    SHELL_RC="$HOME/.profile" ;;
    esac
    PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
    if [[ $DRY_RUN -eq 0 ]]; then
      ask "Append \`$PATH_LINE\` to $SHELL_RC and source it for this session?"
      read -r ans
      if [[ "$ans" =~ ^[Yy] ]]; then
        # Idempotent append — don't double-add if the rc already has the line.
        if [[ -f "$SHELL_RC" ]] && grep -qF '$HOME/.local/bin' "$SHELL_RC"; then
          ok "$SHELL_RC already references \$HOME/.local/bin (probably commented or in another form)"
        else
          printf '\n# brand-growth-engine — added by bootstrap.sh\n%s\n' "$PATH_LINE" >> "$SHELL_RC"
          ok "appended to $SHELL_RC"
        fi
        # Apply to THIS shell so the subsequent stages work without restarting.
        export PATH="$HOME/.local/bin:$PATH"
        ok "exported PATH for this session"
      else
        warn "Skipping — you'll need to add \`$PATH_LINE\` manually or call lipy by absolute path."
      fi
    fi
    ;;
esac

# ─── 2. cua-driver + Accessibility (X read path) ──────────────────
if [[ $SKIP_X -eq 1 ]]; then
  step "2/12  cua-driver gate (SKIPPED via --skip-x)"
else
  step "2/12  cua-driver + Accessibility permission"
  # Auto-install the pinned, universal (Intel + Apple Silicon) binary. cua-driver
  # is a SECONDARY tool — x-engage reads/writes over CDP — so a failure here is a
  # warning, never a hard stop.
  if [[ ! -x "$HOME/.local/bin/cua-driver" ]]; then
    if [[ $DRY_RUN -eq 0 ]]; then
      bash "$PROJECT_ROOT/scripts/install-cua-driver.sh" || warn "cua-driver auto-install failed."
    else
      echo "  [dry-run] would run scripts/install-cua-driver.sh"
    fi
  fi
  if [[ ! -x "$HOME/.local/bin/cua-driver" ]]; then
    warn "cua-driver not installed — x-engage core (CDP) still works; OS conveniences (screenshots, hotkeys) won't."
  else
    CUA_VER="$("$HOME/.local/bin/cua-driver" --version 2>/dev/null | head -1 || echo unknown)"
    ok "cua-driver: $CUA_VER"

    # Validate the Accessibility grant the right way: check_permissions returns
    # JSON like {"accessibility": true, "screen_recording": true}.
    if [[ $DRY_RUN -eq 0 ]]; then
      PERMS="$("$HOME/.local/bin/cua-driver" check_permissions 2>/dev/null || echo '{}')"
      if echo "$PERMS" | grep -q '"accessibility"[[:space:]]*:[[:space:]]*true'; then
        ok "cua-driver Accessibility grant active"
      else
        warn "Accessibility permission not granted (optional, but enables screenshots/hotkeys)."
        echo "    Grant it via: System Settings → Privacy & Security → Accessibility →"
        echo "    drag cua-driver in → toggle on."
        ask "Open the Accessibility panel now? (or just press ENTER to skip)"
        read -r ans
        [[ "$ans" =~ ^[Yy] ]] && open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" || true
        [[ "$ans" =~ ^[Yy] ]] && pause "After toggling cua-driver on,"
      fi
    fi
  fi
fi

# ─── 3. LLM credentials ───────────────────────────────────────────
step "3/12  LLM credentials in $HERMES_HOME/.env"
if ! grep -qE '^(AZURE_FOUNDRY_API_KEY|ANTHROPIC_API_KEY|OPENROUTER_API_KEY)=[^<]' "$HERMES_HOME/.env"; then
  warn "No usable LLM key in $HERMES_HOME/.env (every value still has placeholder)."
  if [[ $DRY_RUN -eq 0 ]]; then
    ask "Open .env in \$EDITOR?"
    read -r ans
    [[ "$ans" =~ ^[Yy] ]] && "${EDITOR:-vi}" "$HERMES_HOME/.env"
    grep -qE '^(AZURE_FOUNDRY_API_KEY|ANTHROPIC_API_KEY|OPENROUTER_API_KEY)=[^<]' "$HERMES_HOME/.env" \
      || die "Still no usable LLM key. Fill in one of AZURE_FOUNDRY_API_KEY / ANTHROPIC_API_KEY / OPENROUTER_API_KEY."
  fi
fi
ok "an API key value is present in .env"

# Live-validate the key. Presence in .env doesn't mean it works — typos,
# expired keys, and wrong base URLs all pass presence checks. One curl
# catches all three. Skip the test in --dry-run (no network calls).
if [[ $DRY_RUN -eq 0 ]]; then
  # Pull the relevant values out of .env without sourcing (.env may contain
  # shell-syntax-hostile content like unescaped # or spaces).
  AZURE_KEY=$(grep -E '^AZURE_FOUNDRY_API_KEY=' "$HERMES_HOME/.env" | sed 's/^[^=]*=//; s/^"\(.*\)"$/\1/')
  AZURE_URL=$(grep -E '^AZURE_FOUNDRY_BASE_URL=' "$HERMES_HOME/.env" | sed 's/^[^=]*=//; s/^"\(.*\)"$/\1/')
  ANTHROPIC_KEY=$(grep -E '^ANTHROPIC_API_KEY=' "$HERMES_HOME/.env" | sed 's/^[^=]*=//; s/^"\(.*\)"$/\1/')
  OPENROUTER_KEY=$(grep -E '^OPENROUTER_API_KEY=' "$HERMES_HOME/.env" | sed 's/^[^=]*=//; s/^"\(.*\)"$/\1/')

  KEY_TEST_PASSED=0
  if [[ -n "$AZURE_KEY" && "$AZURE_KEY" != "<your-azure-key>" && -n "$AZURE_URL" ]]; then
    # Azure /openai/v1/ list-models endpoint — 200 with valid key, 401 with bad key.
    HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -m 8 \
      -H "api-key: $AZURE_KEY" \
      "${AZURE_URL%/}/models" 2>/dev/null || echo "000")
    if [[ "$HTTP_STATUS" == "200" ]]; then
      ok "Azure key validates (HTTP 200 from $AZURE_URL/models)"
      KEY_TEST_PASSED=1
    else
      warn "Azure key returned HTTP $HTTP_STATUS — key, URL, or deployment is wrong."
    fi
  fi
  if [[ $KEY_TEST_PASSED -eq 0 && -n "$ANTHROPIC_KEY" && "$ANTHROPIC_KEY" != "sk-ant-<...>" ]]; then
    HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -m 8 \
      -H "x-api-key: $ANTHROPIC_KEY" \
      -H "anthropic-version: 2023-06-01" \
      "https://api.anthropic.com/v1/models" 2>/dev/null || echo "000")
    if [[ "$HTTP_STATUS" == "200" ]]; then
      ok "Anthropic key validates (HTTP 200 from api.anthropic.com/v1/models)"
      KEY_TEST_PASSED=1
    else
      warn "Anthropic key returned HTTP $HTTP_STATUS."
    fi
  fi
  if [[ $KEY_TEST_PASSED -eq 0 && -n "$OPENROUTER_KEY" && "$OPENROUTER_KEY" != "sk-or-<...>" ]]; then
    HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -m 8 \
      -H "Authorization: Bearer $OPENROUTER_KEY" \
      "https://openrouter.ai/api/v1/models" 2>/dev/null || echo "000")
    if [[ "$HTTP_STATUS" == "200" ]]; then
      ok "OpenRouter key validates (HTTP 200 from openrouter.ai/api/v1/models)"
      KEY_TEST_PASSED=1
    else
      warn "OpenRouter key returned HTTP $HTTP_STATUS."
    fi
  fi
  if [[ $KEY_TEST_PASSED -eq 0 ]]; then
    die "No LLM key validated via live curl. Fix the value in $HERMES_HOME/.env and re-run."
  fi

  # When using the Azure provider, make the cheapest deployment the default so
  # every skill/cron tick runs on it unless explicitly escalated. Best-effort.
  if [[ -n "$AZURE_KEY" && "$AZURE_KEY" != "<your-azure-key>" ]]; then
    AZURE_MODEL=$(grep -E '^AZURE_FOUNDRY_MODEL=' "$HERMES_HOME/.env" | sed 's/^[^=]*=//; s/^"\(.*\)"$/\1/')
    AZURE_MODEL="${AZURE_MODEL:-DeepSeek-V4-Flash}"
    if "$HERMES_BIN" config set model "$AZURE_MODEL" >/dev/null 2>&1; then
      ok "default model → $AZURE_MODEL (cheapest; escalate to DeepSeek-V4-Pro where judgment matters)"
    else
      warn "couldn't set default model automatically — run: $HERMES_BIN config set model $AZURE_MODEL"
    fi
  fi
fi

# ─── 4. BRAND.md filled ──────────────────────────────────────────
step "4/12  BRAND.md filled"
if grep -q '^\*\*Name:\*\*$' "$PROJECT_ROOT/BRAND.md" 2>/dev/null; then
  die "BRAND.md still has empty template placeholders. Open $PROJECT_ROOT/BRAND.md and fill it in. This file is the single most load-bearing config — every drafted reply is validated against it."
fi
ok "BRAND.md has content (no empty Name placeholder)"

# ─── 5. X login via CDP Chrome ────────────────────────────────────
if [[ $SKIP_X -eq 1 ]]; then
  step "5/12  X login (SKIPPED via --skip-x)"
else
  step "5/12  X login (Chrome DevTools Protocol session)"
  if [[ $DRY_RUN -eq 0 ]]; then
    bash "$PROJECT_ROOT/skills/x-engage/start-chrome-cdp.sh"
    if curl -s -m 2 http://localhost:9222/json/version >/dev/null 2>&1; then
      ok "CDP Chrome up on :9222"
    else
      die "CDP Chrome didn't come up. Check /tmp/chrome-cdp.log"
    fi

    # Check whether the saved profile is already logged in.
    LOGGED_IN="$("$HERMES_PY" "$PROJECT_ROOT/skills/x-engage/cdp_eval.py" \
      --expr 'JSON.stringify({li: !!document.querySelector("[data-testid=SideNav_AccountSwitcher_Button]")})' 2>/dev/null || echo '{"li":false}')"
    if echo "$LOGGED_IN" | grep -q '"li":true'; then
      ok "X session already logged in"
    else
      warn "Not logged in on X. The CDP Chrome window is open — log in there now (incl. 2FA)."
      pause "After you see your X home feed,"
      LOGGED_IN="$("$HERMES_PY" "$PROJECT_ROOT/skills/x-engage/cdp_eval.py" \
        --expr 'JSON.stringify({li: !!document.querySelector("[data-testid=SideNav_AccountSwitcher_Button]")})' 2>/dev/null || echo '{"li":false}')"
      echo "$LOGGED_IN" | grep -q '"li":true' || die "Still not detected as logged in on X."
      ok "X session logged in"
    fi
  fi
fi

# ─── 6. LinkedIn login via lipy ───────────────────────────────────
if [[ $SKIP_LI -eq 1 ]]; then
  step "6/12  LinkedIn login (SKIPPED via --skip-li)"
else
  step "6/12  LinkedIn login (Playwright session)"
  [[ -x "$LIPY_BIN" ]] || die "lipy not at $LIPY_BIN. Re-run ./setup.sh."
  if "$LIPY_BIN" status 2>/dev/null | grep -q '"profile_present": true'; then
    ok "LinkedIn Playwright session warmed"
  else
    if [[ $DRY_RUN -eq 0 ]]; then
      warn "No LinkedIn session yet."
      ask "Launch 'lipy login --headed' now? (A Chromium window will open.)"
      read -r ans
      if [[ "$ans" =~ ^[Yy] ]]; then
        "$LIPY_BIN" login --headed
      else
        die "Run '$LIPY_BIN login --headed' manually, then re-run bootstrap.sh."
      fi
      "$LIPY_BIN" status 2>/dev/null | grep -q '"profile_present": true' \
        || die "LinkedIn session still not warmed after login attempt."
      ok "LinkedIn session warmed"
    fi
  fi
fi

# ─── 7. Voice corpus + voice profile ──────────────────────────────
step "7/12  Voice corpus + voice profile"
NORMALIZED="$PROJECT_ROOT/corpus/_normalized.jsonl"
if [[ ! -f "$NORMALIZED" ]] || [[ "$(wc -l < "$NORMALIZED" | tr -d ' ')" -lt 5 ]]; then
  warn "$NORMALIZED has <5 records."
  if [[ -f "$PROJECT_ROOT/corpus/linkedin_comments.jsonl" ]] || [[ -f "$PROJECT_ROOT/corpus/x_posts.jsonl" ]]; then
    if [[ $DRY_RUN -eq 0 ]]; then
      ask "Run scripts/ingest-corpus.py to (re)build _normalized.jsonl?"
      read -r ans
      [[ "$ans" =~ ^[Yy] ]] && "$HERMES_PY" "$PROJECT_ROOT/scripts/ingest-corpus.py" || true
    fi
  else
    warn "No raw corpus files in $PROJECT_ROOT/corpus/ either. See corpus/README.md for export instructions."
  fi
fi
[[ -f "$NORMALIZED" ]] && ok "$NORMALIZED has $(wc -l < "$NORMALIZED" | tr -d ' ') records"

VOICE="$HERMES_HOME/memories/voice_profile.json"
if [[ -f "$VOICE" ]]; then
  ok "voice_profile.json present"
else
  warn "voice_profile.json missing."
  if [[ $DRY_RUN -eq 0 ]]; then
    ask "Run scripts/voice-train.py now? (LLM call; 1–3 minutes.)"
    read -r ans
    [[ "$ans" =~ ^[Yy] ]] && "$HERMES_PY" "$PROJECT_ROOT/scripts/voice-train.py" || true
    [[ -f "$VOICE" ]] || warn "voice_profile.json still missing — autonomy-mode.sh will fail without it."
  fi
fi

# ─── 8. schedule.yaml ─────────────────────────────────────────────
step "8/12  schedule.yaml"
SCHEDULE="$PROJECT_ROOT/schedule.yaml"
if [[ ! -f "$SCHEDULE" ]]; then
  warn "schedule.yaml missing."
  if [[ $DRY_RUN -eq 0 ]]; then
    cp "$PROJECT_ROOT/schedule.example.yaml" "$SCHEDULE"
    ok "copied schedule.example.yaml → schedule.yaml"
    ask "Open in \$EDITOR to edit?"
    read -r ans
    [[ "$ans" =~ ^[Yy] ]] && "${EDITOR:-vi}" "$SCHEDULE" || true
  fi
fi
if [[ -f "$SCHEDULE" ]]; then
  "$HERMES_PY" "$PROJECT_ROOT/scripts/validate-schedule.py" "$SCHEDULE" || die "schedule.yaml failed validation."
  ok "schedule.yaml valid"
fi

# ─── 9. Autonomy mode (cron arm) ──────────────────────────────────
step "9/12  Arm autonomy mode (registers schedule-tick + daily-report + voice-retrain cron jobs)"
if [[ $DRY_RUN -eq 0 ]]; then
  ask "Run scripts/autonomy-mode.sh now?"
  read -r ans
  [[ "$ans" =~ ^[Yy] ]] && "$PROJECT_ROOT/scripts/autonomy-mode.sh" || true
fi

# ─── 10. launchd persistence ──────────────────────────────────────
step "10/12 Persistent daemon via launchd"
PLIST_SRC="$PROJECT_ROOT/config/launchd/com.brandgrowthengine.hermes.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.brandgrowthengine.hermes.plist"
if launchctl list 2>/dev/null | grep -q brandgrowthengine; then
  ok "launchd job already loaded"
else
  if [[ $DRY_RUN -eq 0 ]]; then
    ask "Install + load the launchd plist now? (keeps the gateway alive across reboots)"
    read -r ans
    if [[ "$ans" =~ ^[Yy] ]]; then
      mkdir -p "$(dirname "$PLIST_DST")"
      # Substitute BOTH tokens: __HOME__ (user's home) and __PROJECT_ROOT__ (where
      # this clone lives). The plist runs under launchd which doesn't expand env
      # vars in ProgramArguments — so we must bake absolute paths in here.
      sed -e "s|__HOME__|$HOME|g" \
          -e "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
          "$PLIST_SRC" > "$PLIST_DST"
      # Belt-and-suspenders: refuse to load a plist that still has either token.
      if grep -q '__HOME__\|__PROJECT_ROOT__' "$PLIST_DST"; then
        die "plist token substitution failed — $PLIST_DST still contains __HOME__ or __PROJECT_ROOT__"
      fi
      launchctl load "$PLIST_DST"
      ok "installed + loaded $PLIST_DST"
      ok "  HOME         = $HOME"
      ok "  PROJECT_ROOT = $PROJECT_ROOT"
    fi
  fi
fi

# ─── 11. Hermes gateway running ───────────────────────────────────
step "11/12 Hermes gateway running"
if pgrep -f "hermes.*gateway" >/dev/null 2>&1; then
  ok "Hermes gateway process detected"
else
  warn "No gateway process. Either launchd hasn't loaded yet, or you need to start it manually:"
  echo "    $HERMES_BIN gateway start"
fi

# ─── 12. Final summary ─────────────────────────────────────────────
step "12/12 Done"
cat <<EOF

$(c_green '════════════════════════════════════════════════════════════════')
$(c_green '  Bootstrap walkthrough complete.')
$(c_green '════════════════════════════════════════════════════════════════')

$(c_yellow 'START THE AGENT (this is what makes the cron loop actually run):')

  $(c_blue "$HERMES_BIN gateway start")

  If you installed the launchd plist (stage 10), it already started and will
  relaunch on every reboot — check with: launchctl list | grep brandgrowthengine
  Confirm it's alive either way: pgrep -f "hermes.*gateway"

What to check from here:

  • Audit log:      tail -f $HERMES_HOME/logs/audit.jsonl
  • Daily report:   $HERMES_HOME/reports/\$(date +%F).md
  • Halt instantly: edit config/caps.yaml → set linkedin.live or x.live to false
  • Yank a draft:  rm $HERMES_HOME/queue/<draft-id>.json

Smoke-test the orchestrator (no posting):
  $HERMES_PY scripts/schedule-tick.py --dry-run --verbose

Schedule a test post:
  1. Add a post 5 minutes in the future to schedule.yaml
  2. Pre-write the draft markdown into drafts/<id>.md
  3. Watch $HERMES_HOME/logs/audit.jsonl for publish → monitor_poll → inbound_reply_*

EOF
