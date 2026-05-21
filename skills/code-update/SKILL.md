---
name: code-update
description: Safely update the running CowWechat project code from the user's GitHub repository. Use when the admin asks in natural language to update, pull, sync, refresh, or deploy newer GitHub code while preserving local config, API keys, tokens, cookies, sessions, credentials, .env files, and runtime state.
metadata:
  cowagent:
    always: true
---

# Code Update

Use this skill when the user asks to update this CowWechat checkout from GitHub, for example:

- "更新一下 GitHub 上的新代码"
- "同步远端代码，不要动本机配置"
- "pull 最新代码"
- "把另一台电脑推上来的代码更新到这台机器"

## Required Tool

Use the `git_code_update` tool. Do not use `bash` for this workflow unless the
tool is unavailable and the user explicitly asks for manual Git commands.

Default parameters:

```json
{
  "remote": "origin",
  "branch": "main"
}
```

## Workflow

1. Confirm the request is from an admin-capable context. The tool also enforces
   admin-only access.
2. Call `git_code_update` with default `origin/main` unless the user names a
   different remote or branch.
3. Explain the result briefly:
   - `updated`: report old/new commit prefixes, changed files, and tell the user
     to restart CowWechat before expecting runtime behavior to change.
   - `up_to_date`: say the code is already current.
   - `dirty`: say local uncommitted code changes blocked the update; do not
     overwrite them.
   - `protected_path`: say the remote attempted to change protected local config
     or secret paths, so the update was refused.
   - `diverged`: say manual merge is required.

## Safety Rules

- Never update `config.json`, `.env*`, credential JSON files, tokens, cookies,
  sessions, private keys, `.codex/`, `.playwright-mcp/`, `.venv/`, logs, or other
  runtime state.
- Only accept fast-forward updates. Do not run `git reset --hard`, force pulls,
  rebases, or destructive clean commands.
- If new code adds a feature that needs API keys or model configuration, ask the
  user to provide those values separately and store them through existing config
  mechanisms such as `env_config` or a local ignored `config.json`, not in Git.
