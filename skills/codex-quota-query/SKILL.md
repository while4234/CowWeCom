---
name: codex-quota-query
description: Query the current Codex/GPT/ChatGPT subscription quota, remaining quota, usage, or token usage through the local OpenClaw Codex app-server bridge, with sanitized text/JSON output for manual status checks. Use this instead of local token-usage tracking when the request names Codex, GPT, ChatGPT, OpenAI subscription quota, or current Codex backend usage.
metadata:
  requires:
    bins: ["python", "node"]
---

# Codex Quota Query

Use this skill when the user asks to query Codex, GPT, OpenAI, or ChatGPT subscription quota for the logged-in local Codex/OpenClaw account. It follows the existing qq-openclaw/OpenClaw Discord quota pattern: Python wrapper -> Node script -> OpenClaw Codex app-server -> `account/read` and `account/rateLimits/read`.

## Commands

Run from the CowWechat project root:

```powershell
D:\CowWechat\.venv\Scripts\python.exe D:\CowWechat\skills\codex-quota-query\scripts\check_codex_quota.py --project-dir D:\CowWechat --format text --timeout-ms 120000
D:\CowWechat\.venv\Scripts\python.exe D:\CowWechat\skills\codex-quota-query\scripts\check_codex_quota.py --project-dir D:\CowWechat --format json --timeout-ms 120000
D:\CowWechat\.venv\Scripts\python.exe D:\CowWechat\skills\codex-quota-query\scripts\codex_quota.py snapshot --timeout-seconds 120
D:\CowWechat\.venv\Scripts\python.exe D:\CowWechat\skills\codex-quota-query\scripts\codex_quota.py decision --format json --timeout-seconds 120
```

In the chat/runtime workspace (`C:\Users\RondleLiu\cow`), do not assume relative `.venv\Scripts\python.exe` points at CowWechat. Prefer the absolute CowWechat venv and script paths above. Natural-language chat requests like "查询 codex 使用量" or "查询当前后端 token 使用量" should be handled by the `/backend quota` fast path when available, so they do not need an Agent reasoning loop.

## Safety

- Do not print access tokens, refresh tokens, cookies, auth JSON content, or local identity state.
- The query uses the bundled OpenClaw Codex app-server client and calls `account/read` plus `account/rateLimits/read`.
- Account email is masked by default in text and JSON output.
- The default query does not save snapshots or write runtime state; `codex_quota.py snapshot --save` writes sanitized snapshots under ignored `data/codex-quota-query/`.
- If Node.js, Codex extension dist files, or the app-server bridge are unavailable, report a concise failure instead of opening a browser.
