#!/usr/bin/env python3
"""Build voice_profile.json from the normalized corpus + BRAND.md.

Loads the project's `voice-profile` skill from ~/.hermes/skills/voice-profile/
and dispatches a Hermes session that produces ~/.hermes/memory/voice_profile.json.

The actual voice distillation prompt lives in the skill's SKILL.md — this script
is just the runner. If the skill is missing, fail with a clear pointer.

Usage:
    scripts/voice-train.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
HERMES_BIN = PROJECT_ROOT / "vendor" / "hermes-agent" / ".venv" / "bin" / "hermes"
NORMALIZED = PROJECT_ROOT / "corpus" / "_normalized.jsonl"
BRAND_MD = PROJECT_ROOT / "BRAND.md"
VOICE_OUT = HERMES_HOME / "memory" / "voice_profile.json"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="Just check inputs and exit.")
    args = p.parse_args()

    issues = []
    if not HERMES_BIN.exists():
        issues.append(f"Hermes not installed at {HERMES_BIN}. Run ./setup.sh first.")
    if not BRAND_MD.exists():
        issues.append(f"{BRAND_MD} missing.")
    if not NORMALIZED.exists():
        issues.append(f"{NORMALIZED} missing. Run scripts/ingest-corpus.py first.")
    if not (HERMES_HOME / "skills" / "voice-profile").exists():
        issues.append(f"voice-profile skill not linked into {HERMES_HOME}/skills/. Re-run ./setup.sh.")

    if issues:
        for i in issues:
            print(f"  ✗ {i}", file=sys.stderr)
        return 1

    print(f"✓ Inputs OK")
    print(f"  corpus:  {NORMALIZED}")
    print(f"  brand:   {BRAND_MD}")
    print(f"  output:  {VOICE_OUT}")

    if args.dry_run:
        return 0

    VOICE_OUT.parent.mkdir(parents=True, exist_ok=True)

    # Compose a single Hermes invocation:
    #   "/voice-profile  rebuild  --corpus <path>  --brand <path>  --out <path>"
    # The skill's SKILL.md defines what /voice-profile does. We pass arguments
    # in the prompt as Hermes' skills protocol parses them.
    prompt = (
        f"/voice-profile rebuild. Read the entire normalized corpus at "
        f"{NORMALIZED} and BRAND.md at {BRAND_MD}. Distill a voice profile "
        f"and write it as JSON to {VOICE_OUT}. Include: signature phrases, "
        f"sentence-length distribution, em-dash usage (should be near zero), "
        f"sentence-opener patterns, off-limits topic list, and 10 sample "
        f"sentences that best capture the user's voice. Return only the path "
        f"to the written file."
    )

    cmd = [str(HERMES_BIN), "chat", "-q", prompt]
    print(f"\nrunning: {' '.join(shutil.quote(c) if hasattr(shutil, 'quote') else c for c in cmd)}\n")
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
