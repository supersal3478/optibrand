#!/usr/bin/env python3
"""Build voice_profile.json from the normalized corpus + BRAND.md.

Dispatches a one-shot `hermes chat -q --skills voice-profile` session that
reads the corpus + BRAND.md and writes ~/.hermes/memories/voice_profile.json.
This is the canonical Hermes path — the skill's SKILL.md owns the distillation
prompt; this script is just a runner with input validation.

If the LLM run fails to produce a well-formed file within `--timeout` seconds,
exits non-zero so callers (setup.sh, cron) can react.

Usage:
    scripts/voice-train.py              # bootstrap
    scripts/voice-train.py --retrain    # weights original 2x over sent_replies
    scripts/voice-train.py --dry-run    # just check inputs and exit
    scripts/voice-train.py --timeout 600
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
HERMES_BIN = PROJECT_ROOT / "vendor" / "hermes-agent" / ".venv" / "bin" / "hermes"
NORMALIZED = PROJECT_ROOT / "corpus" / "_normalized.jsonl"
BRAND_MD = PROJECT_ROOT / "BRAND.md"
VOICE_OUT = HERMES_HOME / "memories" / "voice_profile.json"
VOICE_BAK = HERMES_HOME / "memories" / "voice_profile.json.bak"


def check_inputs(retrain: bool) -> list[str]:
    issues: list[str] = []
    if not HERMES_BIN.exists():
        issues.append(f"Hermes not installed at {HERMES_BIN}. Run ./setup.sh first.")
    if not BRAND_MD.exists():
        issues.append(f"{BRAND_MD} missing.")
    else:
        brand_text = BRAND_MD.read_text()
        # Refuse on the obvious unfilled template (empty placeholders).
        if "**Name:**\n" in brand_text or "**Headline (1 sentence):**\n" in brand_text:
            issues.append(f"{BRAND_MD} still has empty template placeholders. Fill it in first.")
    if not NORMALIZED.exists():
        issues.append(f"{NORMALIZED} missing. Run scripts/ingest-corpus.py first.")
    elif sum(1 for _ in NORMALIZED.open()) < 5:
        issues.append(f"{NORMALIZED} has fewer than 5 records — drop corpus exports into corpus/ first.")
    if not (HERMES_HOME / "skills" / "voice-profile").exists():
        issues.append(f"voice-profile skill not linked into {HERMES_HOME}/skills/. Re-run ./setup.sh.")
    if retrain and not VOICE_OUT.exists():
        issues.append(f"--retrain requested but {VOICE_OUT} doesn't exist yet. Run without --retrain first.")
    return issues


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="Just check inputs and exit.")
    p.add_argument("--retrain", action="store_true",
                   help="Retrain mode — weights original corpus 2x over sent_replies.jsonl.")
    p.add_argument("--timeout", type=int, default=600,
                   help="Seconds to wait for the LLM session to finish (default 600).")
    p.add_argument("--model", default="DeepSeek-V4-Pro",
                   help="Model deployment for the distillation pass. Defaults to "
                        "DeepSeek-V4-Pro — this is a one-shot, quality-critical job, "
                        "so it's worth escalating off the cheap Flash default. Pass an "
                        "empty string to use the Hermes-configured default instead.")
    args = p.parse_args()

    issues = check_inputs(args.retrain)
    if issues:
        for i in issues:
            print(f"  ✗ {i}", file=sys.stderr)
        return 1

    print(f"✓ Inputs OK")
    print(f"  corpus:  {NORMALIZED}  ({sum(1 for _ in NORMALIZED.open())} records)")
    print(f"  brand:   {BRAND_MD}    ({BRAND_MD.stat().st_size} bytes)")
    print(f"  output:  {VOICE_OUT}")
    print(f"  mode:    {'retrain' if args.retrain else 'bootstrap'}")

    if args.dry_run:
        return 0

    VOICE_OUT.parent.mkdir(parents=True, exist_ok=True)

    # Back up prior profile in retrain mode.
    if args.retrain and VOICE_OUT.exists():
        VOICE_BAK.write_bytes(VOICE_OUT.read_bytes())
        print(f"  backed up prior to {VOICE_BAK}")

    mode = "retrain" if args.retrain else "bootstrap"
    prompt = (
        f"Run the voice-profile skill in {mode} mode. "
        f"Read every record in {NORMALIZED} and the full contents of {BRAND_MD}. "
        f"Follow the schema and workflow in your SKILL.md. "
        f"Write the resulting JSON to {VOICE_OUT}. "
        f"After writing, print exactly one line: VOICE_PROFILE_WRITTEN={VOICE_OUT}"
    )

    cmd = [str(HERMES_BIN), "chat", "--skills", "voice-profile"]
    if args.model:
        # Escalate this one-shot, quality-critical pass off the Flash default.
        cmd += ["--provider", "azure-foundry", "-m", args.model]
    cmd += ["-q", prompt]
    print(f"\nrunning: {' '.join(cmd[:4])} ...")
    if args.model:
        print(f"  model: {args.model} (override the Hermes default for this pass)")
    print(f"(this may take 60–180 seconds — the LLM is reading {NORMALIZED.stat().st_size:,} bytes of corpus)\n")

    pre_mtime = VOICE_OUT.stat().st_mtime if VOICE_OUT.exists() else 0
    try:
        result = subprocess.run(cmd, timeout=args.timeout, capture_output=False)
    except subprocess.TimeoutExpired:
        print(f"\n✗ Hermes session timed out after {args.timeout}s.", file=sys.stderr)
        return 2

    # Validate output.
    if not VOICE_OUT.exists():
        print(f"\n✗ {VOICE_OUT} was not written. Check the Hermes output above.", file=sys.stderr)
        return 3
    if VOICE_OUT.stat().st_mtime <= pre_mtime:
        print(f"\n✗ {VOICE_OUT} exists but was not updated by this run.", file=sys.stderr)
        return 4
    try:
        prof = json.loads(VOICE_OUT.read_text())
        if "tone" not in prof or "platform_specific" not in prof:
            print(f"\n✗ {VOICE_OUT} is JSON but missing required keys (tone, platform_specific).", file=sys.stderr)
            return 5
    except json.JSONDecodeError as e:
        print(f"\n✗ {VOICE_OUT} is not valid JSON: {e}", file=sys.stderr)
        return 6

    print(f"\n✓ voice profile written: {VOICE_OUT}")
    print(f"  confidence: {prof.get('confidence', 'unknown')}")
    print(f"  records:    {prof.get('corpus_record_count', '?')}")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
