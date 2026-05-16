#!/usr/bin/env bash
# Idempotently launch the dedicated CDP Chrome for x-engage.
# Safe to run anytime: if CDP is already up on 9222, exits 0 without relaunching.
set -euo pipefail

PORT=9222
PROFILE="$HOME/.hermes/state/chrome-cdp"
LOG="/tmp/chrome-cdp.log"
CHROME='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

if curl -s -m 2 "http://localhost:$PORT/json/version" >/dev/null 2>&1; then
  echo "CDP Chrome already up on :$PORT"
  exit 0
fi

if [[ ! -x "$CHROME" ]]; then
  echo "ERROR: Chrome not at $CHROME" >&2
  exit 1
fi

mkdir -p "$PROFILE"
"$CHROME" --remote-debugging-port="$PORT" --user-data-dir="$PROFILE" >"$LOG" 2>&1 &
disown

for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 1
  if curl -s -m 2 "http://localhost:$PORT/json/version" >/dev/null 2>&1; then
    echo "CDP Chrome up on :$PORT  (log: $LOG)"
    exit 0
  fi
done

echo "ERROR: Chrome didn't expose CDP on :$PORT within 10s. Check $LOG" >&2
exit 1
