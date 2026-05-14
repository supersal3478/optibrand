---
name: youtube-engage
description: "YouTube Data API v3 via curl: list comments on the user's channel, reply to good ones, delete spam, set moderation status. OAuth-based, fully ToS-compliant for the user's own channel."
version: 0.1.0
author: brand-growth-engine
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [youtube, social-media, moderation, comment-management]
prerequisites:
  env_vars:
    - YT_CHANNEL_ID
    - YT_OAUTH_CLIENT_ID
    - YT_OAUTH_CLIENT_SECRET
  files:
    - ~/.hermes/state/youtube_token.json   # populated by the one-time OAuth flow below
  skills:
    - brand-guard
    - reply-drafter
---

# youtube-engage

Manage comments on the user's own YouTube channel via the official Data API v3. This skill is **fully API-legal** — Google explicitly supports this for channel owners. Phase 1 of our rollout starts here because the legal/ToS risk is essentially zero.

The skill exposes operations to:
- List recent comments on the user's videos
- Reply to comments (after `reply-drafter` + `brand-guard`)
- Delete comments (spam/toxic)
- Hold comments for review (`setModerationStatus: heldForReview`)

Comments on the user's videos are addressable by the channel owner without needing the original commenter's auth.

---

## Secret Safety (MANDATORY)

- The OAuth refresh token in `~/.hermes/state/youtube_token.json` is highly sensitive. **Never** read its contents into agent context. **Never** include `Authorization` headers in any output the agent will see.
- The user must complete the OAuth flow once, manually, in a browser — outside the agent session.
- To verify auth is in place, only check `[ -f ~/.hermes/state/youtube_token.json ]`.

---

## One-Time User Setup (do this manually)

This must be done by the user outside the agent session. The agent should NOT execute these steps for them.

1. **Create a Google Cloud project** at https://console.cloud.google.com.
2. **Enable the YouTube Data API v3** for the project.
3. **Create OAuth 2.0 credentials** of type "Desktop app". Download the JSON.
4. Set `YT_OAUTH_CLIENT_ID` and `YT_OAUTH_CLIENT_SECRET` in `~/.hermes/.env` from that JSON.
5. **Set `YT_CHANNEL_ID`** in the same `.env` (find it at https://www.youtube.com/account_advanced).
6. Run the auth bootstrap:
   ```bash
   python -m brand_growth_engine.youtube_auth
   ```
   This opens a browser, completes the OAuth flow, and writes `~/.hermes/state/youtube_token.json` with the refresh token. (Implementation: a small helper script in the project that uses `google-auth-oauthlib`. Skill agents don't need to know its internals — just verify the token file exists.)
7. Verify with: `youtube-engage status` (described below).

---

## Quota awareness

The Data API v3 default quota is **10,000 units/day**. Operation costs:

| Operation | Quota cost |
|---|---|
| `commentThreads.list` | 1 |
| `comments.list` | 1 |
| `comments.insert` (reply) | 50 |
| `comments.update` | 50 |
| `comments.setModerationStatus` | 50 |
| `comments.delete` | 50 |

At our caps (40 replies/day + uncapped deletes), worst case ~5000 units/day on a normal day. A viral video can push us over. **The skill must check remaining quota before write operations and queue (don't crash) when low.**

Track usage in `~/.hermes/logs/youtube_quota.jsonl`:
```json
{"ts": "...", "op": "comments.insert", "units": 50, "cumulative_today": 1230}
```

When `cumulative_today > 9000`, skip writes for the rest of the day; reads still allowed.

---

## API access pattern

Use `curl` with a fresh access token derived from the refresh token. The skill's helper script (or the agent inline if needed) handles refresh:

```bash
# Get fresh access token (the helper handles caching + refresh)
ACCESS_TOKEN=$(python -m brand_growth_engine.youtube_token)

# List recent comments on your channel
curl -s -H "Authorization: Bearer $ACCESS_TOKEN" \
  "https://www.googleapis.com/youtube/v3/commentThreads?part=snippet,replies&allThreadsRelatedToChannelId=$YT_CHANNEL_ID&maxResults=50&order=time"

# Reply to a comment (parent ID is the top-level comment's id)
curl -s -X POST -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d "{\"snippet\":{\"parentId\":\"$PARENT_ID\",\"textOriginal\":\"$REPLY_TEXT\"}}" \
  "https://www.googleapis.com/youtube/v3/comments?part=snippet"

# Set moderation status (rejected = soft-delete from public view)
curl -s -X POST -H "Authorization: Bearer $ACCESS_TOKEN" \
  "https://www.googleapis.com/youtube/v3/comments/setModerationStatus?id=$COMMENT_ID&moderationStatus=rejected"

# Hard delete (only the channel owner can delete others' comments on their videos)
curl -s -X DELETE -H "Authorization: Bearer $ACCESS_TOKEN" \
  "https://www.googleapis.com/youtube/v3/comments?id=$COMMENT_ID"
```

The token helper script is responsible for never echoing the bearer to stdout in a form the agent will read; redirect to `/dev/stderr` on errors.

---

## Workflow: scheduled moderation pass (every 30 min)

This is the canonical inbound flow.

1. **Check quota**: read `~/.hermes/logs/youtube_quota.jsonl`, abort if today's cumulative ≥ 9000.
2. **Fetch new comments**: `commentThreads.list` with `order=time`, filter for comments newer than the last seen timestamp (stored in `~/.hermes/state/youtube_last_seen.txt`).
3. **For each comment**:
   a. Skip if already in `~/.hermes/state/youtube_processed.jsonl`.
   b. Run `spam-classifier` skill on the comment text.
   c. **If spam_score ≥ 0.85**: call `comments.setModerationStatus?moderationStatus=rejected`. Log to `audit.jsonl` and `youtube_processed.jsonl`.
   d. **If toxic_score ≥ 0.80**: same — reject.
   e. **If neutral with no engagement value**: skip (don't reply, don't delete).
   f. **If valuable**: invoke `reply-drafter` with `target_kind=yt_mod_reply, platform=youtube, parent_text=<comment>`. If drafter returns a draft, post it via `comments.insert`.
   g. Log every action to `~/.hermes/logs/audit.jsonl`.
4. **Update last_seen** to the most recent processed comment's timestamp.
5. **Update quota log**.

Per-day caps from `config/caps.yaml`:
- `replies_per_day: 40` — track in `~/.hermes/state/youtube_replies_today.txt`. Reset at midnight local.
- Deletes are uncapped (the API rate is the only ceiling).

---

## Workflow: status check

`youtube-engage status` should report:
- Token file present? (existence check only — never read contents)
- Last successful API call timestamp
- Today's quota usage / 10000
- Today's replies posted / 40
- Comments queued for review (if any held)

---

## Pre-flight checks (every job start)

1. `~/.hermes/state/youtube_token.json` exists?
2. `YT_CHANNEL_ID` set?
3. Quota under cap?
4. `caps.yaml: youtube.live` is `true`?

Missing any → log and skip the job.

---

## Failure modes

- **403 Forbidden**: usually means the OAuth scopes are insufficient. The user needs to re-run the OAuth flow with the `youtube.force-ssl` scope. Surface a clear message; do not retry.
- **401 Unauthorized**: refresh token may be revoked. Surface to user; do not loop.
- **quotaExceeded**: log and back off until midnight UTC (Google's quota window). Don't retry on the same UTC day.
- **commentsDisabled**: video has comments disabled. Skip silently.
- **rateLimitExceeded**: respect the Retry-After header.

---

## Notes

- **Channel owner privilege**: the user can delete and moderate comments on their own videos without the original commenter's permission. This is the entire premise of the skill.
- **Comment threads vs comments**: top-level comments come from `commentThreads`, and replies under them come from `comments` keyed by `parentId`. When replying, set `parentId` to the top-level comment's id (not the thread id).
- **Soft delete vs hard delete**: `setModerationStatus: rejected` removes from public view but keeps the record. `comments.delete` is permanent. For spam, prefer `rejected` — it's reversible and creates a moderation history.
- **No DM/private message support**: the API is comments-only.
