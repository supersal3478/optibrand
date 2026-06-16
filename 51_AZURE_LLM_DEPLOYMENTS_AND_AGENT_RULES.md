# Azure LLM Deployments — Secret Names, Values, and Agent Rules

> **SECURITY NOTE.** This file contains live API keys. It is committed to this
> PRIVATE repo on purpose so the project is standalone-usable on a fresh laptop.
> If this repo is ever made public or shared outside the owner, ROTATE the Azure
> keys immediately.

Last verified: 2026-06-16 (curl probes against each endpoint; added the two
DeepSeek-V4 deployments). Prior full pass: 2026-05-04.

---

## Purpose

This is the canonical reference for which Azure OpenAI deployments are wired to
this project, the exact secret names that live in `user_api_keys`, what
parameters each deployment accepts, and which model is appropriate for each
agent step. Keep this in sync with reality — re-probe and update when a new
deployment is added or rotated.

---

## Deployments at a glance

| Tag | Deployment name | Resource | Style | Use for |
|---|---|---|---|---|
| (default) | `gpt-5.1-chat` | `072025` | chat | existing agents (do not touch) |
| `GPT_5_2_CHAT` | `gpt-5.2-chat` | `072025` | chat (reasoning-flavored) | reasoning on the existing resource; must keep `temperature=1` |
| `GPT_5_3_CHAT` | `gpt-5.3-chat` | `sjudieh-3891-resource` | chat (reasoning-flavored) | strong reasoning; must keep `temperature=1` |
| `GPT_5_4` | `gpt-5.4` | `sjudieh-3891-resource` | chat | drop-in replacement; supports custom temperature |
| `DeepSeek-V4-Flash` (container env) | `DeepSeek-V4-Flash` | `072025` | chat (DeepSeek) | **cheapest model we have — most-used. Primary LLM for browser-use agents** |
| `DeepSeek-V4-Pro` (container env) | `DeepSeek-V4-Pro` | `072025` | chat (DeepSeek) | stronger/pricier DeepSeek tier; same endpoint + key, only the deployment name differs |

The two `sjudieh` deployments share one resource and one API key — only the
deployment name differs. **All four `072025` deployments — `gpt-5.1-chat`,
`gpt-5.2-chat`, `DeepSeek-V4-Flash`, and `DeepSeek-V4-Pro` — share ONE resource,
ONE endpoint (`https://072025.openai.azure.com`), and ONE API key.** Only the
deployment name (and, for DeepSeek, the api-version) changes between them. The
gpt-5.x ones are stored as separate secret sets so each can be rotated
independently; the DeepSeek ones are not stored in `user_api_keys` at all (see
the DeepSeek section below).

---

## Secrets in `user_api_keys`

These are the exact `service_name` values written to the database via the
dashboard's API-key panel. Code reads them via `secrets.get("<NAME>")`.

### Existing default (gpt-5.1-chat) — DO NOT MODIFY

```
AZURE_API_KEY         = 7Q59T4x2PVt3iToAfN8d67onsv47SORtvvG0uF2SmvvxJJ4LCnHcJQQJ99BFACHYHv6XJ3w3AAAAACOGHQMr
AZURE_API_VERSION     = 2025-01-01-preview
AZURE_DEPLOYMENT_NAME = gpt-5.1-chat
AZURE_ENDPOINT        = https://072025.openai.azure.com/
```

### gpt-5.2-chat (072025 — same resource and key as default)

```
AZURE_API_KEY_GPT_5_2_CHAT         = 7Q59T4x2PVt3iToAfN8d67onsv47SORtvvG0uF2SmvvxJJ4LCnHcJQQJ99BFACHYHv6XJ3w3AAAAACOGHQMr
AZURE_API_VERSION_GPT_5_2_CHAT     = 2025-01-01-preview
AZURE_DEPLOYMENT_NAME_GPT_5_2_CHAT = gpt-5.2-chat
AZURE_ENDPOINT_GPT_5_2_CHAT        = https://072025.openai.azure.com/
```

### gpt-5.3-chat (sjudieh)

```
AZURE_API_KEY_GPT_5_3_CHAT         = Dxl5vI8tpuLPkofNw8WQOWMeOJS66HqVSWja3uZzAPFEbjR7KgMUJQQJ99CDACHYHv6XJ3w3AAAAACOGjehS
AZURE_API_VERSION_GPT_5_3_CHAT     = 2025-01-01-preview
AZURE_DEPLOYMENT_NAME_GPT_5_3_CHAT = gpt-5.3-chat
AZURE_ENDPOINT_GPT_5_3_CHAT        = https://sjudieh-3891-resource.openai.azure.com/
```

### gpt-5.4 (sjudieh)

```
AZURE_API_KEY_GPT_5_4         = Dxl5vI8tpuLPkofNw8WQOWMeOJS66HqVSWja3uZzAPFEbjR7KgMUJQQJ99CDACHYHv6XJ3w3AAAAACOGjehS
AZURE_API_VERSION_GPT_5_4     = 2025-01-01-preview
AZURE_DEPLOYMENT_NAME_GPT_5_4 = gpt-5.4
AZURE_ENDPOINT_GPT_5_4        = https://sjudieh-3891-resource.openai.azure.com/
```

> Endpoint values intentionally end with a trailing slash and no `/openai/...`
> path. The `AzureOpenAI` Python client appends `/openai/deployments/<deployment>/...`
> on its own when given `azure_endpoint=...` + `api_version=...` + `model=<deployment>`.

### DeepSeek-V4 (Flash + Pro) — same 072025 endpoint & key, container-env only

There are **two DeepSeek deployments on the `072025` resource**, and they use the
**exact same endpoint and API key as `gpt-5.1-chat`** (the default `7Q59…` key).
The ONLY differences from gpt-5.1 are the deployment name and the api-version:

```
# Shared by BOTH DeepSeek deployments (identical to gpt-5.1-chat's resource/key):
AZURE_ENDPOINT    = https://072025.openai.azure.com      # same 072025 resource as gpt-5.1
AZURE_API_KEY     = 7Q59T4x2PVt3iToAfN8d67onsv47SORtvvG0uF2SmvvxJJ4LCnHcJQQJ99BFACHYHv6XJ3w3AAAAACOGHQMr
AZURE_API_VERSION = 2025-03-01-preview                   # DeepSeek's version — NOT gpt-5.1's 2025-01-01-preview

# The two deployments (pick via AZURE_MODEL / the deployment-name path segment):
DeepSeek-V4-Flash    # cheapest — most-used; the primary browser-use model
DeepSeek-V4-Pro      # stronger, pricier; same key, only the name differs
```

**Why Flash is everywhere:** `DeepSeek-V4-Flash` is the **cheapest model wired to
this project**, so it's the default workhorse — anywhere cost matters and the task
isn't reasoning-heavy, we run Flash. `DeepSeek-V4-Pro` is the higher-quality (and
more expensive) DeepSeek tier on the same key; reach for it only when Flash's
quality isn't enough and you still want to stay on DeepSeek pricing rather than
jumping to a gpt-5.x deployment.

**Not in `user_api_keys`.** Unlike the gpt-5.x sets, DeepSeek has **no stored
secret**. It's configured purely through **container env vars in
`docker-compose.yml`** (the `browser-use-service` block): `AZURE_MODEL=DeepSeek-V4-Flash`,
plus the shared `AZURE_ENDPOINT` / `AZURE_API_KEY` / `AZURE_API_VERSION` above.

**For this project (brand-growth-engine):** Hermes reaches DeepSeek through the
OpenAI-compatible `/openai/v1` shim — `AZURE_FOUNDRY_BASE_URL=https://072025.openai.azure.com/openai/v1`
with `model=DeepSeek-V4-Flash` (default) or `DeepSeek-V4-Pro` (escalation). The
shim handles the api-version itself, so you do NOT set `2025-03-01-preview`
manually here. Both were curl-verified HTTP 200 through that shim on 2026-06-16.

**Browser-use agents: Flash-first, gpt-5.1 fallback.** Some browser-use agents run
**`DeepSeek-V4-Flash` as the primary model (attempt 1, cheap)** and then **fall back
to `gpt-5.1-chat` on retries (attempts 2–3)**, because gpt-5.1 is a bit more reliable
at certain tasks (it returns well-formed output and doesn't intermittently blow the
90s per-LLM-call timeout the way Flash sometimes does). ~84% of runs finish on the
cheap Flash attempt; only the failing minority pay for gpt-5.1. The per-attempt
model-switch logic and rationale are documented in
[file 76](76_BROWSER_USE_DEEPSEEK_GPT51_MODEL_FALLBACK_2026-06-09.md).

> **Note (observed 2026-06-16):** `DeepSeek-V4-Pro` occasionally returns a transient
> HTTP 500 (`internal_server_error`) on the first call, then succeeds on retry. Treat
> 500s from Pro as retryable, not fatal. `DeepSeek-V4-Flash` was clean across probes.

---

## Capability matrix (verified by curl)

| Param / feature | `gpt-5.1-chat` (default) | `gpt-5.2-chat` | `gpt-5.3-chat` | `gpt-5.4` |
|---|---|---|---|---|
| `max_completion_tokens` | yes | yes | yes | yes |
| `max_tokens` (legacy) | n/a (don't use) | **rejected (HTTP 400)** | **rejected (HTTP 400)** | **rejected (HTTP 400)** |
| `temperature` non-default (e.g. 0.7) | yes | **rejected (HTTP 400)** — must equal 1 or be omitted | **rejected (HTTP 400)** — must equal 1 or be omitted | yes |
| `response_format = {"type": "json_object"}` | yes | yes | yes | yes |
| Streaming | yes | yes | yes | yes |
| Auth header | `api-key` | `api-key` | `api-key` (or `Authorization: Bearer`) | `api-key` |
| Minimum sane `max_completion_tokens` for visible output | small caps OK | **≥256** — reasoning-style; small caps may return empty `content` with `finish_reason="length"` | **≥256** — same reason | small caps OK |

---

## Hard rules for agents

1. **Always use `max_completion_tokens`. Never `max_tokens`.** All three new
   deployments (5.2, 5.3, 5.4) 400 on the legacy name. Standardize on the new
   name across every agent so the same call signature works everywhere.
2. **Do not pass `temperature=<non-1>` if a call may route to gpt-5.2-chat or
   gpt-5.3-chat.** Either omit `temperature` entirely (gets default 1, accepted
   everywhere) or gate it behind a capability check.
3. **Set `max_completion_tokens` ≥ 256 when calling gpt-5.2-chat or
   gpt-5.3-chat.** For batched JSON outputs (analyzer, synthesizer) the
   existing 8192 is fine. The risk is short utility calls — raise their cap.
4. **`response_format={"type":"json_object"}` is universal** — keep it on
   everywhere a JSON response is needed, regardless of deployment.
5. **Endpoint values must be the bare resource URL** (e.g.
   `https://sjudieh-3891-resource.openai.azure.com/`), not the full
   `/openai/deployments/...` URL. The SDK builds the path.

---

## Agent-by-agent recommendation

| Agent | Step type | Recommended tag | Why |
|---|---|---|---|
| `mvg_fetch_conversations` | no LLM | — | Airtable only |
| `mvg_clean_convos` | no LLM | — | regex filter |
| `mvg_analyzer` | high-volume batched JSON (10–14 batches × 20 records) | `GPT_5_2_CHAT` (verified fast in practice on 2026-05-04 — see note below), `gpt-5.1-chat`, or `GPT_5_4` | original assumption was that reasoning-style models would be too slow here, but `gpt-5.2-chat` ran a 9-batch / 173-record analyzer pass MEANINGFULLY FASTER than `gpt-5.1-chat` did on the same input. Theory: 5.2's reasoning lets it return well-formed JSON on the first try (no batch-split fallback fires), whereas 5.1 hit the recursive split + stub fallback on at least one batch and added ~3 min of recovery overhead. **For high-volume JSON-mode tasks, 5.2 may actually be the throughput winner because it avoids retry overhead.** |
| `mvg_synthesizer` | one-shot narrative + blog ideation | `GPT_5_3_CHAT` or `GPT_5_2_CHAT` (reasoning), or `GPT_5_4` (faster, supports temperature) | small number of calls, output quality matters more than throughput. Already passes `temperature=1`, so the reasoning-style deployments are compatible without code changes |
| `mvg_pdf_report_gen` | no LLM | — | weasyprint render |

The synthesizer's `temperature=1` is intentional — it is the only value
gpt-5.3-chat accepts and the default everywhere else. Do not change it.

---

## Resolution rules in code

The intended resolver pattern (to be added in a follow-up):

```python
def _resolve_azure_client(secrets, tag=None):
    """
    tag: None => default (AZURE_*); else "GPT_5_4" or "GPT_5_3_CHAT".
    Returns (AzureOpenAI client, deployment_name, capabilities_dict).
    """
    suffix = f"_{tag}" if tag else ""
    endpoint        = secrets.get(f"AZURE_ENDPOINT{suffix}")        or secrets["AZURE_ENDPOINT"]
    api_key         = secrets.get(f"AZURE_API_KEY{suffix}")         or secrets["AZURE_API_KEY"]
    api_version     = secrets.get(f"AZURE_API_VERSION{suffix}")     or secrets["AZURE_API_VERSION"]
    deployment_name = secrets.get(f"AZURE_DEPLOYMENT_NAME{suffix}") or secrets["AZURE_DEPLOYMENT_NAME"]

    capabilities = {
        "GPT_5_4":      {"temperature_override_ok": True,  "min_max_completion_tokens": 16},
        "GPT_5_3_CHAT": {"temperature_override_ok": False, "min_max_completion_tokens": 256},
        "GPT_5_2_CHAT": {"temperature_override_ok": False, "min_max_completion_tokens": 256},
    }.get(tag, {"temperature_override_ok": True, "min_max_completion_tokens": 16})

    return AzureOpenAI(
        azure_endpoint=endpoint, api_key=api_key, api_version=api_version
    ), deployment_name, capabilities
```

Resolution priority for the tag itself:
1. `data.get("model_tag")` — per-run override sent in the request body.
2. `secrets.get(f"<AGENT_NAME>_MODEL_TAG")` — per-agent default in secrets.
3. `None` (use the global `AZURE_*` default).

---

## Curl probes (for re-verification)

Re-run these whenever a key is rotated, an endpoint moves, or a new model is
introduced. They are the source of truth for the matrix above.

### gpt-5.2-chat — basic ping (≥256 cap)

```bash
curl -sS -X POST "https://072025.openai.azure.com/openai/deployments/gpt-5.2-chat/chat/completions?api-version=2025-01-01-preview" \
  -H "Content-Type: application/json" \
  -H "api-key: $AZURE_API_KEY_GPT_5_2_CHAT" \
  -d '{"messages":[{"role":"user","content":"Reply with: ping"}],"max_completion_tokens":256}'
```
Expected: HTTP 200, `content: "ping"`, `finish_reason: "stop"`.

### gpt-5.3-chat — basic ping (≥256 cap)

```bash
curl -sS -X POST "https://sjudieh-3891-resource.openai.azure.com/openai/deployments/gpt-5.3-chat/chat/completions?api-version=2025-01-01-preview" \
  -H "Content-Type: application/json" \
  -H "api-key: $AZURE_API_KEY_GPT_5_3_CHAT" \
  -d '{"messages":[{"role":"user","content":"Reply with: ping"}],"max_completion_tokens":256}'
```
Expected: HTTP 200, `content: "ping"`, `finish_reason: "stop"`.
If you see `finish_reason: "length"` and empty content, raise the cap.

### gpt-5.4 — basic ping

```bash
curl -sS -X POST "https://sjudieh-3891-resource.openai.azure.com/openai/deployments/gpt-5.4/chat/completions?api-version=2025-01-01-preview" \
  -H "Content-Type: application/json" \
  -H "api-key: $AZURE_API_KEY_GPT_5_4" \
  -d '{"messages":[{"role":"user","content":"Reply with: ping"}],"max_completion_tokens":16}'
```
Expected: HTTP 200, `content: "ping"`.

### DeepSeek-V4-Flash / -Pro — basic ping (shared 072025 key, DeepSeek api-version)

```bash
# Flash (cheapest, primary browser-use model)
curl -sS -X POST "https://072025.openai.azure.com/openai/deployments/DeepSeek-V4-Flash/chat/completions?api-version=2025-03-01-preview" \
  -H "Content-Type: application/json" \
  -H "api-key: $AZURE_API_KEY" \
  -d '{"messages":[{"role":"user","content":"Reply with exactly: ping"}],"max_completion_tokens":64}'

# Pro (stronger, pricier; same key) — retry on transient 500
curl -sS -X POST "https://072025.openai.azure.com/openai/deployments/DeepSeek-V4-Pro/chat/completions?api-version=2025-03-01-preview" \
  -H "Content-Type: application/json" \
  -H "api-key: $AZURE_API_KEY" \
  -d '{"messages":[{"role":"user","content":"Reply with exactly: ping"}],"max_completion_tokens":64}'
```
Expected: HTTP 200, `content: "ping"`. Both use the default `AZURE_API_KEY`
(`7Q59…`). To list every deployment on the resource:
`curl -s "https://072025.openai.azure.com/openai/deployments?api-version=2023-03-15-preview" -H "api-key: $AZURE_API_KEY"`.

### Negative checks (these MUST fail to confirm the rules)

```bash
# gpt-5.4 with max_tokens (legacy) — must 400
curl -sS -X POST "https://sjudieh-3891-resource.openai.azure.com/openai/deployments/gpt-5.4/chat/completions?api-version=2025-01-01-preview" \
  -H "Content-Type: application/json" -H "api-key: $AZURE_API_KEY_GPT_5_4" \
  -d '{"messages":[{"role":"user","content":"ping"}],"max_tokens":16}'

# gpt-5.3-chat with temperature=0.7 — must 400
curl -sS -X POST "https://sjudieh-3891-resource.openai.azure.com/openai/deployments/gpt-5.3-chat/chat/completions?api-version=2025-01-01-preview" \
  -H "Content-Type: application/json" -H "api-key: $AZURE_API_KEY_GPT_5_3_CHAT" \
  -d '{"messages":[{"role":"user","content":"ping"}],"max_completion_tokens":256,"temperature":0.7}'

# gpt-5.2-chat with temperature=0.7 — must 400
curl -sS -X POST "https://072025.openai.azure.com/openai/deployments/gpt-5.2-chat/chat/completions?api-version=2025-01-01-preview" \
  -H "Content-Type: application/json" -H "api-key: $AZURE_API_KEY_GPT_5_2_CHAT" \
  -d '{"messages":[{"role":"user","content":"ping"}],"max_completion_tokens":256,"temperature":0.7}'
```

If either of these starts returning 200, the deployment behavior changed
upstream — re-do the full probe and update this document.
