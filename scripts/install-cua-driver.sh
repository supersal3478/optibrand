#!/usr/bin/env bash
# install-cua-driver.sh — fetch the Monterey-compatible Rust cua-driver and
# install it at ~/.local/bin/cua-driver.
#
# cua-driver is a SECONDARY tool here: x-engage does all its X reads and writes
# over the Chrome DevTools Protocol (CDP). cua-driver is used only for OS-level
# conveniences (list_apps, list_windows, foreground screenshots, the Cmd+6
# hotkey). The install never blocks the core loop — but having it makes the
# x-engage skill fully functional.
#
# Why pinned + universal:
#   • Pinned to v0.1.3 — the version this project was built and tested against.
#     Newer 0.3–0.5 releases changed the CLI; don't auto-upgrade blindly.
#   • The "universal" macOS tarball runs on BOTH Intel and Apple Silicon, so the
#     same command works on any Mac you unbox.
#
# Idempotent: if the right version is already installed, it exits 0 without
# re-downloading. Override the version with CUA_DRIVER_VERSION=0.x.y.
set -euo pipefail

CUA_VERSION="${CUA_DRIVER_VERSION:-0.1.3}"
LOCAL_BIN="$HOME/.local/bin"
DEST="$LOCAL_BIN/cua-driver"
ASSET="cua-driver-rs-${CUA_VERSION}-darwin-universal.tar.gz"
URL="https://github.com/trycua/cua/releases/download/cua-driver-rs-v${CUA_VERSION}/${ASSET}"

c_green()  { printf "\033[32m%s\033[0m" "$*"; }
c_yellow() { printf "\033[33m%s\033[0m" "$*"; }
c_red()    { printf "\033[31m%s\033[0m" "$*"; }
ok()   { printf "  %s %s\n" "$(c_green '✓')" "$*"; }
warn() { printf "  %s %s\n" "$(c_yellow '⚠')" "$*"; }
die()  { printf "  %s %s\n" "$(c_red '✗')" "$*" >&2; exit 1; }

if [[ "$(uname)" != "Darwin" ]]; then
  warn "cua-driver install is macOS-only; skipping on $(uname)."
  exit 0
fi

# Already installed at the pinned version? Nothing to do.
if [[ -x "$DEST" ]] && "$DEST" --version 2>/dev/null | grep -q "$CUA_VERSION"; then
  ok "cua-driver $CUA_VERSION already installed at $DEST"
  exit 0
fi

mkdir -p "$LOCAL_BIN"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

printf "  downloading %s\n" "$ASSET"
if ! curl -fsSL "$URL" -o "$TMP/cua.tar.gz"; then
  die "download failed: $URL  (check network, or set CUA_DRIVER_VERSION to an available release)"
fi

tar -xzf "$TMP/cua.tar.gz" -C "$TMP"

# Find the extracted binary without hard-coding the directory name (it includes
# the version + platform, e.g. cua-driver-rs-0.1.3-darwin-universal/).
BIN_SRC="$(find "$TMP" -type f -name cua-driver -perm -u+x 2>/dev/null | head -1)"
[[ -z "$BIN_SRC" ]] && BIN_SRC="$(find "$TMP" -type f -name cua-driver 2>/dev/null | head -1)"
[[ -n "$BIN_SRC" ]] || die "no 'cua-driver' binary inside $ASSET"

install -m 0755 "$BIN_SRC" "$DEST"
INSTALLED_VER="$("$DEST" --version 2>/dev/null | head -1 || echo unknown)"
ok "installed: $INSTALLED_VER → $DEST"

# Report (don't enforce) the permission state. The grant is a macOS UI action
# that can't be scripted; bootstrap.sh walks the user through it.
PERMS="$("$DEST" check_permissions 2>/dev/null || echo '{}')"
if echo "$PERMS" | grep -q '"accessibility"[[:space:]]*:[[:space:]]*true'; then
  ok "Accessibility permission already granted"
else
  warn "Accessibility not yet granted — grant it in System Settings → Privacy & Security → Accessibility."
  warn "  (bootstrap.sh stage 2 opens the panel and validates the grant for you.)"
fi
