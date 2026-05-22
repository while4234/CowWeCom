---
name: capi-usage-monitor
description: Query and locally monitor CAPI/Codex intermediary quota and usage from https://omilg.com/dhh8888/login by logging in with the configured OPENAI_API_KEY, which is also the CAPI activation/API key in this workspace. Use when the user asks to check capi/codex中转站剩余额度、总额度、用量统计、usage、余额、套餐、到期时间, diagnose configuration, export history, or create daily midnight snapshots for quick later review. Supports local snapshot history and CSV export; API keys are read from env/argument and never persisted.
metadata:
  requires:
    bins: ["python"]
  emoji: "💳"
---

# CAPI Usage Monitor

Use this skill to query quota/usage for the CAPI/Codex usage dashboard at:

`https://omilg.com/dhh8888/login`

In this workspace, **the CAPI key and `OPENAI_API_KEY` are the same key**. Treat the configured `OPENAI_API_KEY` as the default CAPI activation/API key unless the user explicitly overrides it.

The frontend currently uses these backend APIs:

- `POST https://deepl.micosoft.icu/api/users/card-login`
- `GET https://deepl.micosoft.icu/api/users/whoami`
- `POST https://deepl.micosoft.icu/api/chatgpt/usages`
- `POST https://deepl.micosoft.icu/api/chatgpt/chatlog`

Prefer API mode over browser automation. Browser automation is only a fallback for diagnosing page changes.

## Security Rules

- Do not store the raw API key / activation code in files.
- Default key source in this workspace: `OPENAI_API_KEY`.
- Optional override key sources: `CAPI_API_KEY`, `CAPI_ACTIVATION_CODE`, `CAPI_CARD`, `--api-key`, or `--api-key-env`.
- Snapshot files store only `key_hash = sha256(key)[:16]` and `key_suffix` for identification.
- Do not print the full key in replies or logs.
- If the user asks to configure the key, use `env_config` and keep values masked.

## Script

Run:

```bash
python "<base_dir>/scripts/capi_usage.py" <command> [options]
```

Default local storage:

`<workspace>/data/capi-usage-monitor/`

Files:

- `snapshots/<key_hash>-YYYY-MM-DD.jsonl` — append-only daily snapshots.
- `latest-<key_hash>.json` — latest snapshot for quick lookup.

## Commands

### Diagnose configuration

Local-only diagnostic:

```bash
python "<base_dir>/scripts/capi_usage.py" doctor
```

Online diagnostic using the default `OPENAI_API_KEY`:

```bash
python "<base_dir>/scripts/capi_usage.py" doctor --online
```

If no usable key is configured, `doctor` shows `api_key_configured: false`. In this workspace, `OPENAI_API_KEY` should normally make it true.

### Query Current Quota and Usage

Use the default configured key (`OPENAI_API_KEY` in this workspace):

```bash
python "<base_dir>/scripts/capi_usage.py" snapshot --period today
```

Save a local snapshot:

```bash
python "<base_dir>/scripts/capi_usage.py" snapshot --period today --save
```

Use a specific env variable only when overriding the default:

```bash
python "<base_dir>/scripts/capi_usage.py" snapshot --api-key-env CAPI_API_KEY --period today
```

Or pass key directly only if necessary:

```bash
python "<base_dir>/scripts/capi_usage.py" snapshot --api-key "<activation-code>" --period today
```

Useful period options:

- `--period today`
- `--period yesterday`
- `--period month`
- `--period all`
- `--start 2026-05-01 --end 2026-05-22`

Include one page of chatlog detail if needed:

```bash
python "<base_dir>/scripts/capi_usage.py" snapshot --period today --include-chatlog --page 1 --page-size 10
```

Do not use `--raw` in routine reports unless debugging; it may include backend fields that are not needed for normal replies.

### Read Latest Snapshot

```bash
python "<base_dir>/scripts/capi_usage.py" latest
```

If a key is configured, latest filters to that key hash. Without a key, it returns the newest local latest file.

### Read Local History

```bash
python "<base_dir>/scripts/capi_usage.py" history --limit 10
```

If a key is configured, history filters to that key hash. Without a key, it returns all local snapshots.

### Export CSV

```bash
python "<base_dir>/scripts/capi_usage.py" export-csv --output "tmp/capi-usage-history.csv"
```

CSV includes snapshot time, account, total quota, used quota, remaining quota, progress, period, and usage total cost.

## Daily Midnight Snapshot

A scheduler task should run every day at 00:00.

Recommended scheduled AI task:

```text
Use capi-usage-monitor to execute `python <base_dir>/scripts/capi_usage.py snapshot --period yesterday --save`, then report a short summary with remaining quota, used quota, total quota, usage percent, and usage cost. The script uses OPENAI_API_KEY as the CAPI key in this workspace. Do not print any secret.
```

Use `--period yesterday` at midnight to summarize the just-finished day. Use `--period today` if the user wants a snapshot immediately after reset.

## Output Interpretation

Important fields:

- `quota.total` — total quota for total-mode cards, otherwise daily quota.
- `quota.used` — used quota for current mode.
- `quota.remaining` — current remaining quota.
- `quota.progress` — percent used.
- `quota.daily` — daily quota.
- `quota.total_mode` — whether this is a total-quota card.
- `quota.expire_at` — expiration timestamp when provided.
- `usage_summary.total_cost` — usage cost in selected period.
- `usage_summary.by_model` — cost by model.

## Known Dependencies and Limits

- Backend base is currently `https://deepl.micosoft.icu`; override with `CAPI_USAGE_API_BASE` or `--api-base` if the site changes.
- `OPENAI_API_KEY` is currently known to work as this dashboard's activation/API key.
- If the backend response schema changes, inspect the frontend bundle again and update parser functions.
