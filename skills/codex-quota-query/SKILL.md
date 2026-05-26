---
name: codex-quota-query
description: Query and analyze the current Codex/GPT/ChatGPT subscription quota, rate-limit windows, remaining quota, usage pace, fair-share overuse, and follow-up usage strategy through the official local Codex app-server. Use when the user asks to 查询/查看 Codex 额度/用量, analyze whether Codex average usage is over budget, compare used progress vs time progress, decide whether to keep using Codex or switch/fallback to CAPI, or plan later Codex usage allocation. For analysis/strategy requests, read this SKILL.md and run the decision or JSON snapshot command before answering; do not browse project files first.
metadata:
  requires:
    bins: ["python", "codex"]
---

# Codex Quota Query

Use this skill when the user asks to query or analyze Codex, GPT, OpenAI, or ChatGPT subscription quota for the logged-in local Codex account. It calls the official `codex app-server` over stdio, injects the configured Codex auth JSON with `account/login/start`, then reads `account/read` and `account/rateLimits/read`.

This skill is not only a quick lookup. Use it for strategic questions such as:

- "分析一下 Codex 当前的平均用量超了多少，后续的使用策略如何分配更合适"
- "Codex 额度是否超预算，还能不能继续用"
- "对比 Codex 已用进度和时间进度，给我一个后续分配策略"
- "当前后端是 Codex 时，先看额度再决定要不要切 CAPI"

For these analysis requests, do not inspect project source code to find quota logic first. Read this SKILL.md, run the command below, then reason from the returned JSON.

## Analysis Workflow

1. Run the decision command first when the user asks whether Codex usage is over budget or asks for a follow-up strategy:

```powershell
py -3 D:\CowAgent\skills\codex-quota-query\scripts\codex_quota.py decision --format json --timeout-seconds 120
```

2. If the user asks for raw quota windows or a current snapshot, run:

```powershell
py -3 D:\CowAgent\skills\codex-quota-query\scripts\check_codex_quota.py --project-dir D:\CowAgent --format json --timeout-ms 120000
```

3. Explain the result in Chinese. Prefer these fields:

- `decision.reason`: `under_fair_share`, `used_above_fair_share`, `remaining_below_minimum`, `rate_limit_reached`, or `quota_window_missing`.
- `decision.allowed_used_percent`: the fair-share usage percent allowed by elapsed days in the window.
- `decision.window.used_percent`: current used percent for the selected Codex quota window.
- `decision.window.remaining_percent`: remaining quota percent.
- `decision.window.resets_at`: reset time for the selected quota window.
- `account.plan_type`: current account plan.

4. If `used_percent > allowed_used_percent`, say how much the account is ahead of the fair-share pace and suggest reducing Codex usage, using CAPI/monthly CAPI for ordinary tasks, and reserving Codex for high-value work until the reset window catches up.

5. If the command fails, report the concrete failure category: missing `codex` app-server, missing/expired auth file, network/backend API failure, or no quota data. Do not fall back to OpenClaw.

## Commands

Run from the CowWechat project root:

```powershell
D:\CowWechat\.venv\Scripts\python.exe D:\CowWechat\skills\codex-quota-query\scripts\check_codex_quota.py --project-dir D:\CowWechat --format text --timeout-ms 120000
D:\CowWechat\.venv\Scripts\python.exe D:\CowWechat\skills\codex-quota-query\scripts\check_codex_quota.py --project-dir D:\CowWechat --format json --timeout-ms 120000
D:\CowWechat\.venv\Scripts\python.exe D:\CowWechat\skills\codex-quota-query\scripts\codex_quota.py snapshot --timeout-seconds 120
D:\CowWechat\.venv\Scripts\python.exe D:\CowWechat\skills\codex-quota-query\scripts\codex_quota.py decision --format json --timeout-seconds 120
```

In the chat/runtime workspace (`C:\Users\RondleLiu\cow`), do not assume relative `.venv\Scripts\python.exe` points at CowWechat. Prefer the absolute CowWechat venv and script paths above. Natural-language chat requests like "查询 codex 使用量" or "查询当前后端 token 使用量" should be handled by the `/backend quota` fast path when available, so they do not need an Agent reasoning loop.

If the Agent is already in a reasoning loop because the user asked for analysis or strategy, still use this skill. The fast path and the skill share the same direct app-server query; the fast path is only for simple chat-visible lookup commands.

## Runtime Requirements

- The machine must have an official Codex executable that supports `codex app-server --listen stdio://`. This may come from the VSCode Codex extension on a desktop machine or a native Windows Codex install on a remote host.
- Put `codex` on `PATH`, set `CODEX_CLI_BINARY`, or pass `--codex-bin`.
- Set `CODEX_AUTH_FILE`, top-level `codex_auth_file`, or `llm_backend.providers.codex.auth_file` to the Codex auth JSON. The script also falls back to `CODEX_HOME/auth.json` or `~/.codex/auth.json`.
- If the remote Codex app reports missing runtime dependencies, install or repair the official Codex app/CLI there; this skill no longer uses bundled OpenClaw extension files.

## Safety

- Do not print access tokens, refresh tokens, cookies, auth JSON content, or local identity state.
- The query uses the official Codex app-server protocol and calls `account/read` plus `account/rateLimits/read`.
- Account email is masked by default in text and JSON output.
- The default query does not save snapshots or write runtime state; `codex_quota.py snapshot --save` writes sanitized snapshots under ignored `data/codex-quota-query/`.
- If the Codex executable, auth file, network, or app-server are unavailable, report a concise failure instead of opening a browser.
