---
name: capi-usage-monitor
description: Query and locally monitor CAPI/Codex intermediary quota and usage from https://omilg.com/dhh8888/login. Use the capi provider/key for quota-card checks and capi_monthly provider/key for monthly-card checks. Use when the user asks to check capi/codex中转站剩余额度、总额度、用量统计、usage、余额、套餐、到期时间, diagnose configuration, export history, or create daily midnight snapshots for quick later review. Supports local snapshot history and CSV export; API keys are read from env/argument and never persisted.
metadata:
  requires:
    bins: ["python"]
  emoji: "💳"
---

# CAPI Usage Monitor

Use this skill to query quota/usage for the CAPI/Codex usage dashboard at:

`https://omilg.com/dhh8888/login`

In this workspace, the quota-card CAPI key normally comes from the `capi`
provider, whose default environment variable is `CAPI_API_KEY`. Monthly-card
queries must use the `capi_monthly` provider/key, normally
`CAPI_MONTHLY_API_KEY`, not the quota-card key.

The frontend currently uses these backend APIs:

- `POST https://deepl.micosoft.icu/api/users/card-login`
- `GET https://deepl.micosoft.icu/api/users/whoami`
- `POST https://deepl.micosoft.icu/api/chatgpt/usages`
- `POST https://deepl.micosoft.icu/api/chatgpt/chatlog`

Prefer API mode over browser automation. Browser automation is only a fallback for diagnosing page changes.

## Security Rules

- Do not store the raw API key / activation code in files.
- Default quota-card key source in this workspace: the `capi` provider, usually `CAPI_API_KEY`.
- Default monthly-card key source in this workspace: the `capi_monthly` provider, usually `CAPI_MONTHLY_API_KEY`.
- Optional script-level override key sources: `CAPI_ACTIVATION_CODE`, `CAPI_CARD`, `--api-key`, or `--api-key-env`.
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

Online diagnostic using the default `CAPI_API_KEY`:

```bash
python "<base_dir>/scripts/capi_usage.py" doctor --online
```

If no usable key is configured, `doctor` shows `api_key_configured: false`. In this workspace, `CAPI_API_KEY` should normally make it true.

### Query Current Quota and Usage

Use the default configured quota-card key (`CAPI_API_KEY` in this workspace):

```bash
python "<base_dir>/scripts/capi_usage.py" snapshot --period today
```

For user-facing output that adapts to card type, prefer text format:

```bash
python "<base_dir>/scripts/capi_usage.py" snapshot --period today --format text
```

Text output labels total quota cards as `quota card` and day/month cards as
`daily/monthly card`. For day/month cards it emphasizes today's used,
remaining, 00:00 reset, and expiry; for quota cards it emphasizes total used
and total remaining.

For `today`, `yesterday`, `month`, or explicit `--start/--end` ranges, the script
uses `/api/chatgpt/chatlog` as the authoritative source for `usage_summary`.
The backend `/api/chatgpt/usages` endpoint may return historical aggregate
buckets even when date filters are supplied, so do not use it for daily totals
unless debugging the backend itself.

Save a local snapshot:

```bash
python "<base_dir>/scripts/capi_usage.py" snapshot --period today --save
```

Use a specific env variable only when overriding the default. For monthly-card
queries, route through the `capi_monthly` provider/key and pass that key via a
temporary environment variable:

```bash
python "<base_dir>/scripts/capi_usage.py" snapshot --api-key-env CAPI_API_KEY --period today
```

```bash
python "<base_dir>/scripts/capi_usage.py" snapshot --api-key-env CAPI_MONTHLY_API_KEY --period today --format text
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

Inspect backend aggregate buckets only when debugging a dashboard mismatch:

```bash
python "<base_dir>/scripts/capi_usage.py" snapshot --period today --usage-source usages
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
Use capi-usage-monitor to execute `python <base_dir>/scripts/capi_usage.py snapshot --period yesterday --save`, then report a short summary with remaining quota, used quota, total quota, usage percent, and usage cost. The script uses CAPI_API_KEY as the CAPI key in this workspace. Do not print any secret.
```

Use `--period yesterday` at midnight to summarize the just-finished day. Use `--period today` if the user wants a snapshot immediately after reset.

## Output Interpretation

Important fields:

- `quota.total` — total quota for total-mode cards, otherwise daily quota.
- `quota.used` — used quota for current mode.
- `quota.remaining` — current remaining quota.
- `quota.progress` — percent used.
- `quota.mode` — `daily` for day/month cards that reset by day; `total` for quota cards that spend from a total pool.
- `quota.daily` — daily quota. If the backend returns `0` for both `vip.day_score` and `day_score`, the script follows the frontend fallback and uses `90` by default. Override with `--default-daily-quota` or `CAPI_USAGE_DEFAULT_DAILY_QUOTA` if the dashboard changes.
- `quota.total_mode` — legacy boolean form of `quota.mode == "total"`.
- `quota.expire_at` — expiration timestamp when provided.
- `usage_summary.source` — `chatlog` for filtered periods by default; `usages` only when explicitly requested or no date filter is present.
- `usage_summary.total_cost` — usage cost in selected period from `usage_summary.source`.
- `usage_summary.by_model` — cost by model.

Quota modes:

- Day/month cards: backend `vip.score` is `0`; use daily quota and `user.day_score_used` for used/remaining. If the backend daily quota is `0`, use the frontend's current fallback of `90`.
- Total quota cards: backend `vip.score` is positive; use `vip.score` as total and `user.score_used` as used. Do not use `vip.score_used` for the current user total, because it can lag or represent the VIP package row rather than the live account field.

## Known Dependencies and Limits

- Backend base is currently `https://deepl.micosoft.icu`; override with `CAPI_USAGE_API_BASE` or `--api-base` if the site changes.
- Configure the dashboard activation/API key as `CAPI_API_KEY` or pass it explicitly with `--api-key` / `--api-key-env`.
- If the backend response schema changes, inspect the frontend bundle again and update parser functions.
- Keep the project copy under `skills/capi-usage-monitor/` and the deployed custom copy under `~/cow/skills/capi-usage-monitor/` in sync after fixes; the custom copy overrides the builtin skill at runtime.
