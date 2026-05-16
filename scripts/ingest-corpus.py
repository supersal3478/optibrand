#!/usr/bin/env python3
"""Normalize everything in corpus/ into a single canonical voice corpus.

Reads:
  corpus/linkedin_comments.jsonl    (your replies on others' LinkedIn posts)
  corpus/linkedin_posts.jsonl       (your own LinkedIn posts)
  corpus/x_replies.jsonl            (your replies on X)
  corpus/x_posts.jsonl              (your own X posts)
  corpus/youtube_comments.jsonl     (your YouTube replies)
  corpus/Comments.csv               (LinkedIn data export — Comments.csv)
  corpus/Shares.csv                 (LinkedIn data export — Shares.csv)

Writes:
  corpus/_normalized.jsonl          one record per voice sample, normalized shape:
    {"source": "linkedin_comment"|"linkedin_post"|"x_reply"|"x_post"|"youtube_comment",
     "text": "...",
     "posted_at": "ISO-8601 or null",
     "context": "optional — parent post text / video title / thread context"}

This is the input to voice-train.py. Re-run any time you drop new corpus files.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORPUS = PROJECT_ROOT / "corpus"


def jsonl(path: Path):
    if not path.exists():
        return
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def from_linkedin_comments(path: Path):
    for rec in jsonl(path):
        text = rec.get("comment_text") or rec.get("text")
        if not text:
            continue
        yield {
            "source": "linkedin_comment",
            "text": text,
            "posted_at": rec.get("posted_at"),
            "context": rec.get("parent_url") or rec.get("parent_urn"),
        }


def from_linkedin_posts(path: Path):
    for rec in jsonl(path):
        text = rec.get("text")
        if not text:
            continue
        yield {
            "source": "linkedin_post",
            "text": text,
            "posted_at": rec.get("posted_at"),
            "context": None,
        }


def from_x_replies(path: Path):
    for rec in jsonl(path):
        text = rec.get("text")
        if not text:
            continue
        yield {
            "source": "x_reply",
            "text": text,
            "posted_at": rec.get("posted_at"),
            "context": rec.get("in_reply_to"),
        }


def from_x_posts(path: Path):
    for rec in jsonl(path):
        text = rec.get("text")
        if not text:
            continue
        yield {
            "source": "x_post",
            "text": text,
            "posted_at": rec.get("posted_at"),
            "context": None,
        }


def from_youtube_comments(path: Path):
    for rec in jsonl(path):
        text = rec.get("text")
        if not text:
            continue
        yield {
            "source": "youtube_comment",
            "text": text,
            "posted_at": rec.get("posted_at"),
            "context": rec.get("video_id"),
        }


def from_linkedin_csv_comments(path: Path):
    """LinkedIn data-export Comments.csv has columns:
       Date,Link,Message  (header may vary across export versions)
    """
    if not path.exists():
        return
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            text = row.get("Message") or row.get("Comment") or row.get("CommentText")
            if not text:
                continue
            yield {
                "source": "linkedin_comment",
                "text": text.strip(),
                "posted_at": row.get("Date"),
                "context": row.get("Link"),
            }


def from_linkedin_csv_shares(path: Path):
    """LinkedIn Shares.csv. Columns vary; commonly: Date, ShareLink, ShareCommentary, SharedUrl."""
    if not path.exists():
        return
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            text = row.get("ShareCommentary") or row.get("Commentary")
            if not text:
                continue
            yield {
                "source": "linkedin_post",
                "text": text.strip(),
                "posted_at": row.get("Date"),
                "context": row.get("SharedUrl") or row.get("ShareLink"),
            }


def main() -> int:
    sources = [
        from_linkedin_comments(CORPUS / "linkedin_comments.jsonl"),
        from_linkedin_posts(CORPUS / "linkedin_posts.jsonl"),
        from_x_replies(CORPUS / "x_replies.jsonl"),
        from_x_posts(CORPUS / "x_posts.jsonl"),
        from_youtube_comments(CORPUS / "youtube_comments.jsonl"),
        from_linkedin_csv_comments(CORPUS / "Comments.csv"),
        from_linkedin_csv_shares(CORPUS / "Shares.csv"),
    ]
    out = CORPUS / "_normalized.jsonl"
    counts: dict[str, int] = {}
    with out.open("w") as f:
        for stream in sources:
            for rec in stream:
                counts[rec["source"]] = counts.get(rec["source"], 0) + 1
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    total = sum(counts.values())
    print(f"wrote {out} ({total} records)")
    for src, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {src}: {n}")
    if total == 0:
        print("\nNo records found. Drop your exports into corpus/ — see corpus/README.md.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
