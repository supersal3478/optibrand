#!/usr/bin/env bash
# Brand Growth Engine — one-command bootstrap for a fresh macOS laptop.
#
# Idempotent. Re-runnable. Doesn't touch your home dir beyond:
#   ~/.hermes/                    (skills, state, logs, .env)
#   ~/.local/bin/{lipy,start-chrome-cdp}   (CLI wrappers)
#
# What it does:
#   1. Verifies prerequisites (Chrome, python3 >= 3.11, git)
#   2. Restores vendor/hermes-agent (clones + installs venv)
#   3. Installs lipy (skills/linkedin-engage/.venv + ~/.local/bin/lipy)
#   4. Symlinks the start-chrome-cdp shim into ~/.local/bin/
#   5. Creates ~/.hermes/{skills,state,logs,reports}
#   6. Symlinks every <project>/skills/<name>/ → ~/.hermes/skills/<name>
#   7. Seeds ~/.hermes/.env from .env.example if absent
#   8. Prints next steps (logins + BRAND.md + corpus)
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
LOCAL_BIN="$HOME/.local/bin"

c_red()    { printf "\033[31m%s\033[0m" "$*"; }
c_green()  { printf "\033[32m%s\033[0m" "$*"; }
c_yellow() { printf "\033[33m%s\033[0m" "$*"; }
c_blue()   { printf "\033[34m%s\033[0m" "$*"; }

step() { printf "\n%s %s\n" "$(c_blue '→')" "$*"; }
ok()   { printf "  %s %s\n" "$(c_green '✓')" "$*"; }
warn() { printf "  %s %s\n" "$(c_yellow '⚠')" "$*"; }
die()  { printf "  %s %s\n" "$(c_red '✗')" "$*" >&2; exit 1; }

# ─────────────────────────── 1. Prerequisites ───────────────────────────
step "Checking prerequisites"

if [[ "$(uname)" != "Darwin" ]]; then
  warn "Not macOS (\$(uname) == $(uname)). Some skills (cua-driver, Chrome paths) won't work."
fi

command -v git >/dev/null || die "git not found. Install with: xcode-select --install"
ok "git found"

PY_BIN=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PY_VER="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    if [[ "$PY_VER" =~ ^3\.(1[1-9]|[2-9][0-9])$ ]]; then
      PY_BIN="$(command -v "$candidate")"
      ok "python: $PY_BIN ($PY_VER)"
      break
    fi
  fi
done
[[ -n "$PY_BIN" ]] || die "Python 3.11+ not found. Install via: brew install python@3.14"

CHROME_BIN='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
if [[ -x "$CHROME_BIN" ]]; then
  ok "Chrome found at $CHROME_BIN"
else
  warn "Chrome not at $CHROME_BIN — install from https://google.com/chrome before running x-engage."
fi

# cua-driver is a SECONDARY tool (OS conveniences only; the X path is all CDP).
# Auto-install it so a fresh laptop needs zero manual downloads. Never fatal.
step "Installing cua-driver (X helper; OS conveniences only)"
bash "$PROJECT_ROOT/scripts/install-cua-driver.sh" || warn "cua-driver install skipped/failed — x-engage core path still works over CDP."

# ─────────────────────────── 2. Hermes Agent ───────────────────────────
step "Restoring vendor/hermes-agent"

# Pinned to the exact commit this project was built and tested against, so a
# fresh clone gets the identical Hermes — not whatever upstream HEAD happens to
# be today. To bump: set HERMES_REF to a newer tag/commit and re-run setup.sh.
HERMES_REF="${HERMES_REF:-d62808c37383ea44777229ee99a2c4cfe28d2783}"
HERMES_DIR="$PROJECT_ROOT/vendor/hermes-agent"
if [[ ! -d "$HERMES_DIR/.git" ]]; then
  mkdir -p "$PROJECT_ROOT/vendor"
  git clone https://github.com/NousResearch/hermes-agent "$HERMES_DIR"
  git -C "$HERMES_DIR" checkout --quiet "$HERMES_REF"
  ok "cloned hermes-agent @ ${HERMES_REF:0:12}"
else
  # Make sure an existing clone is on the pinned ref (no-op if already there).
  if git -C "$HERMES_DIR" rev-parse --verify --quiet "$HERMES_REF^{commit}" >/dev/null; then
    git -C "$HERMES_DIR" checkout --quiet "$HERMES_REF" 2>/dev/null || true
  fi
  ok "hermes-agent already cloned (@ $(git -C "$HERMES_DIR" rev-parse --short HEAD))"
fi

if [[ ! -d "$HERMES_DIR/.venv" ]]; then
  "$PY_BIN" -m venv "$HERMES_DIR/.venv"
fi
"$HERMES_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$HERMES_DIR/.venv/bin/pip" install --quiet -e "$HERMES_DIR"
"$HERMES_DIR/.venv/bin/pip" install --quiet websockets pyyaml
ok "hermes-agent venv ready (with websockets + pyyaml)"

# ─────────────────────────── 3. lipy ───────────────────────────
step "Installing lipy (LinkedIn CLI)"
bash "$PROJECT_ROOT/skills/linkedin-engage/install.sh"

# ─────────────────────────── 4. start-chrome-cdp ───────────────────────────
step "Linking start-chrome-cdp"
mkdir -p "$LOCAL_BIN"
ln -sf "$PROJECT_ROOT/skills/x-engage/start-chrome-cdp.sh" "$LOCAL_BIN/start-chrome-cdp"
ok "$LOCAL_BIN/start-chrome-cdp → skills/x-engage/start-chrome-cdp.sh"

# ─────────────────────────── 5. Hermes state dirs ───────────────────────────
step "Creating Hermes state directories"
mkdir -p \
  "$HERMES_HOME" \
  "$HERMES_HOME/skills" \
  "$HERMES_HOME/state" \
  "$HERMES_HOME/state/chrome-cdp" \
  "$HERMES_HOME/state/playwright/linkedin" \
  "$HERMES_HOME/logs" \
  "$HERMES_HOME/reports" \
  "$HERMES_HOME/memories"
ok "state dirs under $HERMES_HOME"

# ─────────────────────────── 6. Skill symlinks ───────────────────────────
step "Symlinking project skills into ~/.hermes/skills/"
for d in "$PROJECT_ROOT"/skills/*/; do
  name="$(basename "$d")"
  ln -sfn "$d" "$HERMES_HOME/skills/$name"
  ok "  $name"
done

# ─────────────────────────── 7. ~/.hermes/.env seed ───────────────────────────
step "Seeding ~/.hermes/.env"
if [[ ! -f "$HERMES_HOME/.env" ]]; then
  cp "$PROJECT_ROOT/.env.example" "$HERMES_HOME/.env"
  chmod 600 "$HERMES_HOME/.env"
  warn "Created $HERMES_HOME/.env from .env.example — FILL IN YOUR KEYS before running the agent."
else
  ok "$HERMES_HOME/.env already exists, not overwriting"
fi

# ─────────────────────────── 8. Hermes doctor ───────────────────────────
step "Running hermes doctor"
"$HERMES_DIR/.venv/bin/hermes" doctor 2>&1 | sed 's/^/  /' || warn "hermes doctor reported issues (some warnings are expected)"

# ─────────────────────────── Next steps ───────────────────────────
cat <<EOF

$(c_green '════════════════════════════════════════════════════════════════')
$(c_green '  Bootstrap complete.')
$(c_green '════════════════════════════════════════════════════════════════')

$(c_yellow 'NEXT STEP — one command from here:')

  $(c_blue './scripts/bootstrap.sh')

This walks every remaining manual gate (cua-driver + Accessibility, LLM
credentials, BRAND.md fill check, X login, LinkedIn login, voice profile,
schedule.yaml, autonomy arm, launchd persistence) with validation between
each. Idempotent — safe to re-run if you stop part-way.

If you'd rather do it by hand, see ONBOARDING.md for the full step-by-step.
EOF
