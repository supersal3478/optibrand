#!/usr/bin/env bash
# preflight.sh — Stage 0 for a brand-new Mac. Installs the system-level
# prerequisites that setup.sh expects: Homebrew, Python 3.11+, Chrome, and the
# handy CLIs (ripgrep, jq).
#
# Safe + idempotent: every step checks before installing. Re-run anytime.
#
# After this finishes:
#   ./setup.sh
#   ./scripts/bootstrap.sh
set -euo pipefail

c_green()  { printf "\033[32m%s\033[0m" "$*"; }
c_yellow() { printf "\033[33m%s\033[0m" "$*"; }
c_blue()   { printf "\033[34m%s\033[0m" "$*"; }
c_red()    { printf "\033[31m%s\033[0m" "$*"; }
step() { printf "\n%s %s\n" "$(c_blue '→')" "$*"; }
ok()   { printf "  %s %s\n" "$(c_green '✓')" "$*"; }
warn() { printf "  %s %s\n" "$(c_yellow '⚠')" "$*"; }
die()  { printf "  %s %s\n" "$(c_red '✗')" "$*" >&2; exit 1; }

[[ "$(uname)" == "Darwin" ]] || die "preflight.sh is macOS-only (saw $(uname))."

# ─── 1. Xcode Command Line Tools (provides git, compilers) ───────────────
step "Xcode Command Line Tools"
if xcode-select -p >/dev/null 2>&1; then
  ok "already installed"
else
  warn "not installed — launching the installer (a GUI dialog will appear)."
  xcode-select --install || true
  echo "    Finish the dialog, then re-run ./scripts/preflight.sh."
  die "waiting on Xcode Command Line Tools install."
fi

# ─── 2. Homebrew ─────────────────────────────────────────────────────────
step "Homebrew"
if command -v brew >/dev/null 2>&1; then
  ok "brew at $(command -v brew)"
else
  warn "installing Homebrew (you'll be prompted for your password)…"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # Put brew on PATH for the rest of THIS script (Apple Silicon vs Intel prefix).
  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
  command -v brew >/dev/null 2>&1 || die "Homebrew install did not put brew on PATH — open a new terminal and re-run."
  ok "brew installed"
fi

# ─── 3. Python 3.11+ ─────────────────────────────────────────────────────
step "Python 3.11+"
PY_OK=0
for c in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    v="$("$c" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 0.0)"
    [[ "$v" =~ ^3\.(1[1-9]|[2-9][0-9])$ ]] && { ok "$c ($v)"; PY_OK=1; break; }
  fi
done
if [[ $PY_OK -eq 0 ]]; then
  warn "installing python@3.14 via brew…"
  brew install python@3.14
  ok "python@3.14 installed"
fi

# ─── 4. Google Chrome ────────────────────────────────────────────────────
step "Google Chrome"
if [[ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]]; then
  ok "Chrome present"
else
  warn "installing Google Chrome via brew cask…"
  brew install --cask google-chrome || warn "Chrome cask install failed — download manually from https://google.com/chrome"
  [[ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]] && ok "Chrome installed" || warn "Chrome still missing — install it before running x-engage."
fi

# ─── 5. Handy CLIs (optional but used by Hermes/skills) ──────────────────
step "ripgrep + jq"
for tool in ripgrep jq; do
  bin="$tool"; [[ "$tool" == ripgrep ]] && bin="rg"
  if command -v "$bin" >/dev/null 2>&1; then
    ok "$tool present"
  else
    brew install "$tool" >/dev/null 2>&1 && ok "$tool installed" || warn "$tool install failed (non-fatal)."
  fi
done

cat <<EOF

$(c_green '════════════════════════════════════════════════════════════════')
$(c_green '  Preflight complete — system prerequisites are in place.')
$(c_green '════════════════════════════════════════════════════════════════')

Next:
  $(c_blue './setup.sh')              # clones Hermes, builds venvs, installs cua-driver + lipy
  $(c_blue './scripts/bootstrap.sh')  # interactive: logins, BRAND.md, voice, autonomy arm

You'll also need (these can't come from git):
  • An LLM API key (Azure / Anthropic / OpenRouter)
  • Your X + LinkedIn 2FA device for the manual logins
EOF
