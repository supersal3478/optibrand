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

# ─────────────────────────── 2. Hermes Agent ───────────────────────────
step "Restoring vendor/hermes-agent"

HERMES_DIR="$PROJECT_ROOT/vendor/hermes-agent"
if [[ ! -d "$HERMES_DIR/.git" ]]; then
  mkdir -p "$PROJECT_ROOT/vendor"
  git clone --depth 1 https://github.com/NousResearch/hermes-agent "$HERMES_DIR"
  ok "cloned hermes-agent"
else
  ok "hermes-agent already cloned"
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
  "$HERMES_HOME/memory"
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

$(c_yellow 'NEXT STEPS — do these once on this laptop:')

  1. $(c_blue 'Add credentials.') Open the env file and fill it in:
       \$EDITOR $HERMES_HOME/.env
     Minimum to start: AZURE_FOUNDRY_API_KEY + AZURE_FOUNDRY_BASE_URL
     (or ANTHROPIC_API_KEY if using direct Anthropic).

  2. $(c_blue 'Log into X.') Launch the dedicated CDP Chrome:
       start-chrome-cdp
     Then in that Chrome window: navigate to x.com, log in with your account,
     complete 2FA if asked. The login persists in ~/.hermes/state/chrome-cdp/.

  3. $(c_blue 'Log into LinkedIn.') Run:
       lipy login --headed
     This opens a chromium window. Log in normally (incl. 2FA). The session
     cookie is saved in ~/.hermes/state/playwright/linkedin/.

  4. $(c_blue 'Fill in BRAND.md.') Open:
       $PROJECT_ROOT/BRAND.md
     and replace every placeholder. This is the single most load-bearing file
     in the project — every drafted reply is validated against it.

  5. $(c_blue 'Add voice corpus.') Drop your platform exports into:
       $PROJECT_ROOT/corpus/
     See corpus/README.md for formats. Even 30 LinkedIn comments is enough to
     bootstrap; more = better voice fidelity.

  6. $(c_blue 'Set your schedule.') Copy and edit the example:
       cp schedule.example.yaml schedule.yaml
     Then \$EDITOR schedule.yaml.

  7. $(c_blue 'Smoke test.') Verify both pipelines:
       lipy status
       start-chrome-cdp && curl -s http://localhost:9222/json/version

When ready: $(c_blue 'hermes')  (interactive chat)
            $(c_blue 'hermes cron list')  (scheduled jobs)
            $(c_blue 'hermes gateway start')  (autonomous daemon)
EOF
