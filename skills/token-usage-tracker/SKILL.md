---
name: token-usage-tracker
description: Local-only per-user token usage tracking and reporting. Use when the user asks to record, query, summarize, audit, export, reset, rebuild, or separate token usage/token量/用量统计 by current user or multiple users, especially phrases like 当前用户token量, 不同用户独立统计, token用量, API usage, quota, local token stats. This skill stores data locally and never calls network APIs.
metadata:
  requires:
    bins: ["python"]
  emoji: "📊"
---

# Token Usage Tracker

Use this skill to track token usage locally per user. Store every usage event as JSONL under the workspace, keyed by a short SHA-256 hash of the stable user id.

## Storage

Default storage path:

`<workspace>/data/token-usage-tracker/`

Files:

- `users/<user_hash>.jsonl` — append-only token events for one user.
- `users/index.json` — local index with `display_name`, counters, first/last seen.

CowAgent also records automatic model usage and prompt-cache telemetry at:

`<workspace>/data/llm_cache_usage.jsonl`

For read commands, the script defaults to `--source auto`: it checks the
token-tracker event store first and automatically falls back to
`llm_cache_usage.jsonl` when no explicit token-tracker events exist. Use
`--source llm-cache` to force the CowAgent runtime log, or `--source both` to
combine both sources.

Privacy rules:

- Do not store raw user ids in usage files.
- Pass a stable `--user-id`; the script stores only `sha256(user_id)[:16]`.
- Do not send token records to any network service.
- Do not store prompts/responses unless explicitly needed elsewhere; this script records counts and metadata only.

## Script

Run the script from this skill's base directory:

```bash
python "<base_dir>/scripts/token_usage.py" <command> [options]
```

If the runtime exposes current user id through environment variables, the script can infer it from one of:

- `COW_CURRENT_USER_ID`
- `COW_USER_ID`
- `WEIXIN_USER_ID`
- `WECHAT_USER_ID`
- `CURRENT_USER_ID`
- `USER_ID`

In multi-user contexts, prefer passing `--user-id` explicitly from the channel/runtime user identifier.

## Commands

### Record exact usage

```bash
python "<base_dir>/scripts/token_usage.py" record \
  --user-id "<stable-user-id>" \
  --input-tokens 123 \
  --output-tokens 45 \
  --model "gpt-5.5" \
  --channel "weixin"
```

If only total usage is known:

```bash
python "<base_dir>/scripts/token_usage.py" record \
  --user-id "<stable-user-id>" \
  --total-tokens 168 \
  --model "gpt-5.5"
```

### Record from OpenAI-compatible usage JSON

Use this for runtime integration when the provider returns `usage`:

```bash
python "<base_dir>/scripts/token_usage.py" record-json \
  --user-id "<stable-user-id>" \
  --json "{\"usage\":{\"prompt_tokens\":123,\"completion_tokens\":45,\"total_tokens\":168}}" \
  --model "gpt-5.5" \
  --channel "weixin"
```

The command accepts these field aliases:

- Input: `input_tokens`, `prompt_tokens`, `prompt`
- Output: `output_tokens`, `completion_tokens`, `completion`
- Total: `total_tokens`, `total`

It can also read JSON from `--file` or stdin.

### Estimate locally from text

```bash
python "<base_dir>/scripts/token_usage.py" record \
  --user-id "<stable-user-id>" \
  --input-text "用户输入" \
  --output-text "助手输出" \
  --model "gpt-5.5" \
  --channel "weixin"
```

The estimator uses `tiktoken` if installed; otherwise it falls back to a local heuristic for CJK/Latin text. Estimated events are marked with `estimated: true`.

Optional metadata:

- `--display-name "<local-display-name>"` for local index display only.
- `--conversation-id ...`
- `--message-id ...`
- `--request-id ...`
- `--meta key=value` repeatable.
- `--data-dir <path>` to override storage location.

### Query usage

Current/specific user:

```bash
python "<base_dir>/scripts/token_usage.py" summary --user-id "<stable-user-id>"
```

Today:

```bash
python "<base_dir>/scripts/token_usage.py" summary --user-id "<stable-user-id>" --period today
```

Current month:

```bash
python "<base_dir>/scripts/token_usage.py" summary --user-id "<stable-user-id>" --period month
```

Date/time range:

```bash
python "<base_dir>/scripts/token_usage.py" summary \
  --user-id "<stable-user-id>" \
  --from-time "2026-05-01" \
  --to-time "2026-06-01"
```

All users:

```bash
python "<base_dir>/scripts/token_usage.py" summary --all
```

Force CowAgent's automatic runtime usage log:

```bash
python "<base_dir>/scripts/token_usage.py" summary --all --source llm-cache
```

If the user asks for current local token usage and no `users/*.jsonl` files
exist, use the default auto source or `--source llm-cache`; do not conclude that
usage is zero until `<workspace>/data/llm_cache_usage.jsonl` has been checked.

List local users:

```bash
python "<base_dir>/scripts/token_usage.py" list-users
```

### Export CSV

```bash
python "<base_dir>/scripts/token_usage.py" export-csv \
  --all \
  --output "tmp/token-usage.csv"
```

For one user:

```bash
python "<base_dir>/scripts/token_usage.py" export-csv \
  --user-id "<stable-user-id>" \
  --period month \
  --output "tmp/token-usage-user.csv"
```

### Rebuild local index

Use this after manual file recovery or if `users/index.json` is missing/stale:

```bash
python "<base_dir>/scripts/token_usage.py" rebuild-index --use-default-user
```

`--use-default-user` only satisfies the shared common argument parser; rebuild scans all local JSONL files and does not merge users.

### Reset usage

Reset one user only after explicit user confirmation:

```bash
python "<base_dir>/scripts/token_usage.py" reset --user-id "<stable-user-id>" --yes
```

Reset all users only after explicit user confirmation:

```bash
python "<base_dir>/scripts/token_usage.py" reset --all --yes
```

## Output interpretation

The script prints JSON. Important fields:

- `summary.events` — number of recorded requests/events.
- `summary.input_tokens` — total prompt/input tokens.
- `summary.output_tokens` — total completion/output tokens.
- `summary.total_tokens` — total tokens.
- `summary.estimated_events` — events based on local estimation.
- `summary.exact_events` — events using provided exact token counts.
- `summary.by_model` — per-model breakdown.
- `summary.by_channel` — per-channel breakdown.

When reading CowAgent runtime telemetry, additional fields include
`cached_tokens`, `uncached_prompt_tokens`, `reasoning_tokens`, and weighted
`cache_hit_rate`.

## Integration guidance

For automatic runtime integration, call `record-json` once after each model request completes, using the usage fields returned by the provider. Use the channel's stable user id as `--user-id`, not display name, so different users remain independent even if names collide.

When answering user queries like “我用了多少 token”, run `summary` for the current user's stable id. When answering admin-style queries like “所有用户 token 用量”, use `summary --all` only if appropriate for that user/session.
