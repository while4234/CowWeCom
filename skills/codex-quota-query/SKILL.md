---
name: codex-quota-query
description: Query the current Codex/GPT/ChatGPT subscription quota, remaining quota, usage, or token usage through the official local Codex app-server, with sanitized text/JSON output for manual status checks. Use this instead of local token-usage tracking when the request explicitly asks to query Codex, GPT, ChatGPT, OpenAI subscription quota, or current Codex backend usage.
metadata:
  requires:
    bins: ["python", "codex"]
---

# Codex Quota Query

Use this skill when the user asks to query Codex, GPT, OpenAI, or ChatGPT subscription quota for the logged-in local Codex account. It calls the official `codex app-server` over stdio, injects the configured Codex auth JSON with `account/login/start`, then reads `account/read` and `account/rateLimits/read`.

Do not use this fast path for broader analysis requests such as "分析 Codex 平均用量是否超了，后续策略怎么分配". Those should stay in the Agent reasoning path so the assistant can combine quota data, history, and strategy.

## Commands

Run from the CowWechat project root:

```powershell
D:\CowWechat\.venv\Scripts\python.exe D:\CowWechat\skills\codex-quota-query\scripts\check_codex_quota.py --project-dir D:\CowWechat --format text --timeout-ms 120000
D:\CowWechat\.venv\Scripts\python.exe D:\CowWechat\skills\codex-quota-query\scripts\check_codex_quota.py --project-dir D:\CowWechat --format json --timeout-ms 120000
D:\CowWechat\.venv\Scripts\python.exe D:\CowWechat\skills\codex-quota-query\scripts\codex_quota.py snapshot --timeout-seconds 120
D:\CowWechat\.venv\Scripts\python.exe D:\CowWechat\skills\codex-quota-query\scripts\codex_quota.py decision --format json --timeout-seconds 120
```

In the chat/runtime workspace (`C:\Users\RondleLiu\cow`), do not assume relative `.venv\Scripts\python.exe` points at CowWechat. Prefer the absolute CowWechat venv and script paths above. Natural-language chat requests like "查询 codex 使用量" or "查询当前后端 token 使用量" should be handled by the `/backend quota` fast path when available, so they do not need an Agent reasoning loop.

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
