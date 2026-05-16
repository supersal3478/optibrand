#!/usr/bin/env python3
"""Validate schedule.yaml against the schema documented in schedule.example.yaml.

Exit 0 if valid. Exit 1 with a list of issues otherwise.

Usage:
    scripts/validate-schedule.py [path/to/schedule.yaml]

If no path given, looks for ./schedule.yaml in the project root.
"""
from __future__ import annotations

import sys
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import yaml
except ImportError:
    sys.stderr.write("pyyaml required: pip install pyyaml\n")
    sys.exit(2)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VALID_PLATFORMS = {"linkedin", "x", "youtube"}


def err(issues: list, msg: str) -> None:
    issues.append(msg)


def validate(path: Path) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors block; warnings only inform."""
    issues: list[str] = []
    warnings: list[str] = []
    if not path.exists():
        return ([f"{path} does not exist. Copy schedule.example.yaml to schedule.yaml first."], [])

    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        return ([f"YAML parse error: {e}"], [])

    if not isinstance(data, dict):
        return (["Top-level must be a mapping."], [])

    tz_name = data.get("timezone", "America/Toronto")
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        err(issues, f"timezone: '{tz_name}' is not a valid IANA name.")
        tz = None

    defaults = data.get("defaults", {}) or {}
    for k, expected_type in [
        ("goodwill_minutes_before", int),
        ("goodwill_count", int),
        ("monitor_hours_after", (int, float)),
        ("inbound_poll_minutes", int),
        ("goodwill_enabled", bool),
    ]:
        if k in defaults and not isinstance(defaults[k], expected_type):
            err(issues, f"defaults.{k}: expected {expected_type}, got {type(defaults[k]).__name__}")

    posts = data.get("posts", []) or []
    if not isinstance(posts, list):
        err(issues, "posts: must be a list.")
        posts = []

    seen_ids = set()
    for i, p in enumerate(posts):
        prefix = f"posts[{i}]"
        if not isinstance(p, dict):
            err(issues, f"{prefix}: must be a mapping.")
            continue
        pid = p.get("id")
        if not pid:
            err(issues, f"{prefix}: missing 'id'.")
        elif pid in seen_ids:
            err(issues, f"{prefix}: duplicate id '{pid}'.")
        else:
            seen_ids.add(pid)

        platform = p.get("platform")
        if platform not in VALID_PLATFORMS:
            err(issues, f"{prefix}.platform: '{platform}' is not one of {sorted(VALID_PLATFORMS)}.")

        t = p.get("time")
        if not t:
            err(issues, f"{prefix}.time: missing.")
        else:
            try:
                datetime.fromisoformat(str(t))
            except ValueError:
                err(issues, f"{prefix}.time: '{t}' is not valid ISO-8601 local datetime.")

        draft = p.get("draft")
        if draft:
            draft_path = (PROJECT_ROOT / draft).resolve()
            if not draft_path.exists():
                warnings.append(f"{prefix}.draft: '{draft}' does not exist yet (will need to exist by post time).")

    recurring = data.get("recurring", {}) or {}
    if "daily_report" in recurring:
        dr = recurring["daily_report"]
        t = dr.get("time")
        if t:
            try:
                dtime.fromisoformat(str(t))
            except ValueError:
                err(issues, f"recurring.daily_report.time: '{t}' not valid HH:MM[:SS].")

    return issues, warnings


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else PROJECT_ROOT / "schedule.yaml"
    issues, warnings = validate(path)
    for w in warnings:
        print(f"warn: {w}")
    if not issues:
        print(f"OK — {path} is valid" + (f" ({len(warnings)} warning(s))" if warnings else "") + ".")
        return 0
    print(f"FAIL — {path} has {len(issues)} error(s):")
    for x in issues:
        print(f"  - {x}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
