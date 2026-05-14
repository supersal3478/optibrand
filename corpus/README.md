# corpus/

Voice training data. Drop your platform exports here as JSON Lines. The `corpus-ingest` skill reads everything in this folder once at Phase 0 and seeds `~/.hermes/memory/voice_profile.json`. The `voice-profile` skill re-trains weekly using this folder + sent replies.

## Expected files

Each file is JSONL — one record per line.

### `linkedin_posts.jsonl`
```json
{"id": "urn:li:share:123", "text": "...", "posted_at": "2026-04-15T12:00:00Z", "likes": 42, "comments": 7}
```

### `linkedin_comments.jsonl`
Comments **you** wrote on others' posts (best voice signal):
```json
{"id": "...", "parent_post_url": "https://...", "text": "...", "posted_at": "..."}
```

### `x_replies.jsonl`
Your replies on X:
```json
{"id": "1789...", "in_reply_to": "1788...", "text": "...", "posted_at": "..."}
```

### `youtube_comments.jsonl`
Your replies in YouTube comment threads:
```json
{"id": "Ugw...", "video_id": "abc123", "text": "...", "posted_at": "..."}
```

## How to export

- **LinkedIn**: Settings → Data Privacy → Get a copy of your data → "The works" (takes 24h). Use `Comments.csv` and `Shares.csv`. There is no first-party comments-as-jsonl export — `scripts/ingest-corpus.py` will convert the CSVs.
- **X**: Settings → Your account → Download an archive of your data. Use `tweets.js`; the script will parse out replies.
- **YouTube**: Google Takeout → My Activity → YouTube. Comments are in `My Activity.html`. Script parses the HTML.

If you have fewer than 50 of any kind, that's fine — the agent will lean more heavily on `BRAND.md` until it has more signal.

## Privacy

Files in this folder may contain private message context (handles you've engaged, threads you've replied to). The default `.gitignore` does **not** exclude this folder — uncomment the relevant line in `.gitignore` if you don't want it committed.

`x_approval.pdf` (proof of X auto-reply approval) is git-ignored by default.
