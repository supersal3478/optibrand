#!/usr/bin/env bash
# Installs the local `lipy` CLI into a venv inside this skill folder.
# Re-runnable; idempotent.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"
BIN="$HOME/.local/bin"

if [[ ! -d "$VENV" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet \
  "playwright>=1.48.0" \
  "playwright-stealth>=1.0.6" \
  "pyyaml>=6.0.2" \
  "rich>=13.9.0"

# Install chromium for Playwright
"$VENV/bin/playwright" install chromium

# Install a wrapper at ~/.local/bin/lipy that execs the venv python with lipy.py.
# Symlinking lipy.py directly leaves it running under system python (per the
# `#!/usr/bin/env python3` shebang), which doesn't have Playwright installed —
# lipy.py imports playwright lazily and returns playwright_not_installed.
mkdir -p "$BIN"
cat > "$BIN/lipy" <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/python" "$HERE/lipy.py" "\$@"
EOF
chmod +x "$BIN/lipy"
chmod +x "$HERE/lipy.py"

echo "OK — lipy installed at $BIN/lipy"
echo "    venv: $VENV"
echo "    next: lipy login --headed"
