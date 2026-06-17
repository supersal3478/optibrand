"""_drafter — shared draft-stylization helpers.

Both feed-engagement.py and inbound-engagement.py call into the same Azure
chat-completions endpoint to draft replies; they need to apply the same
stylistic rules to each draft:

  • Random length target per draft (50% ≤7w, 25% ≤15w, 25% ≤25w).
  • Strip trailing period — humans on X almost always omit it on short
    replies; keeping it reads as faintly AI.
  • Hard-truncate to the word cap if the LLM blew past it (X has a 280-char
    hard limit, our word caps are tighter for style).

Single source so a rule change lives in one place. Both drafters import:

    from _drafter import (
        pick_length_target, apply_length_and_punctuation_fixes,
    )
"""
from __future__ import annotations

import random


def pick_length_target() -> tuple[str, str, int]:
    """Random per-draft length category. User-spec distribution:
      50% → 7 words or less (ultra-terse — the dominant style)
      25% → 15 words or less (short)
      25% → 25 words or less (concise but substantive)

    Returns (label, prompt-instruction, max_words)."""
    roll = random.random()
    if roll < 0.50:
        return ("7w",
                "Length: 7 WORDS OR LESS. Ultra-terse. One short clause. "
                "DO NOT end with a period.",
                7)
    if roll < 0.75:
        return ("15w",
                "Length: 15 WORDS OR LESS. Short and direct. "
                "Cut every adjective and filler. DO NOT end with a period.",
                15)
    return ("25w",
            "Length: 25 WORDS OR LESS. Concise but substantive. "
            "DO NOT end with a period.",
            25)


def strip_trailing_period(text: str) -> str:
    """Strip a single trailing period/full-stop only. Trailing ! and ? are
    fine. Ellipses ('...' / '…') are intentional."""
    if not text:
        return text
    stripped = text.rstrip()
    while stripped.endswith("."):
        if stripped.endswith("...") or stripped.endswith("…."):
            break
        stripped = stripped[:-1].rstrip()
    return stripped


def enforce_word_cap(text: str, max_words: int) -> tuple[str, bool]:
    """If text exceeds max_words, truncate to fit. Returns (text, was_within_cap)."""
    words = text.split()
    if len(words) <= max_words:
        return text, True
    return " ".join(words[:max_words]), False


def apply_length_and_punctuation_fixes(result: dict, max_words: int) -> None:
    """In-place post-processing on a drafter result dict.

    Expects a result with `decision` and `draft` fields (the shape both feed
    and inbound drafters produce). No-op if decision != 'DRAFT' or draft empty.

    Appends to result['autofixes'] for each fix applied, so logs can audit
    how often the LLM needed correcting.
    """
    if result.get("decision") != "DRAFT":
        return
    draft = (result.get("draft") or "").strip()
    if not draft:
        return
    autofixes = list(result.get("autofixes") or [])
    after_period = strip_trailing_period(draft)
    if after_period != draft:
        autofixes.append("stripped trailing period")
        draft = after_period
    truncated, ok = enforce_word_cap(draft, max_words)
    if not ok:
        original_words = len((result.get("draft") or "").split())
        autofixes.append(f"truncated to {max_words}w cap (was {original_words}w)")
        draft = truncated
    result["draft"] = draft
    result["autofixes"] = autofixes
    result["word_count"] = len(draft.split())
