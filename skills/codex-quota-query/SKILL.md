---
name: codex-quota-query
description: Query the current Codex/GPT subscription quota through the local OpenClaw Codex app-server bridge, with sanitized text/JSON output for manual status checks.
metadata:
  requires:
    bins: ["python", "node"]
---

# Codex Quota Query

Use this skill when the user asks to query Codex, GPT, OpenAI, or ChatGPT subscription quota for the logged-in local Codex/OpenClaw account. It follows the existing qq-openclaw/OpenClaw Discord quota pattern: Python wrapper -> Node script -> OpenClaw Codex app-server -> `account/read` and `account/rateLimits/read`.

## Commands

Run from the CowWechat project root:

```powershell
.venv\Scripts\python.exe skills\codex-quota-query\scripts\codex_quota.py snapshot
.venv\Scripts\python.exe skills\codex-quota-query\scripts\codex_quota.py decision --format json
.venv\Scripts\python.exe skills\codex-quota-query\scripts\check_codex_quota.py --project-dir D:\CowWechat --format text
.venv\Scripts\python.exe skills\codex-quota-query\scripts\check_codex_quota.py --project-dir D:\CowWechat --format json
```

## Safety

- Do not print access tokens, refresh tokens, cookies, auth JSON content, or local identity state.
- The query uses the bundled OpenClaw Codex app-server client and calls `account/read` plus `account/rateLimits/read`.
- Account email is masked by default in text and JSON output.
- The default query does not save snapshots or write runtime state; `codex_quota.py snapshot --save` writes sanitized snapshots under ignored `data/codex-quota-query/`.
- If Node.js, Codex extension dist files, or the app-server bridge are unavailable, report a concise failure instead of opening a browser.
