---
name: safe-github-upload
description: Safely commit and push CowWechat code to the user's GitHub repository from WeChat or chat commands. Use when an admin asks to 提交代码, 上传代码, 推送到 GitHub, 发布修复, sync/push code, or create/update a skill and publish it. Enforces .gitignore-aware staging, secret/runtime-state protection, GIT_NOTES maintenance, validation before commit, origin-only push, and syncing skill changes to both the deployed workspace skills directory and the repository skills directory.
metadata:
  cowagent:
    always: true
---

# Safe GitHub Upload

## Overview

Use this skill whenever an admin asks the agent to commit, upload, push, publish,
or sync CowWechat code to GitHub. The goal is to publish only intentional source
code and documentation while leaving secrets, runtime state, databases, logs,
credentials, and ignored local files on the machine.

Default repository:

- Project root: `D:\CowWechat`
- Push remote: `origin`
- Push branch: `main`
- Never push to `upstream`; it is the original CowAgent project.

## Required Preflight

Before staging and again after staging, run the bundled guard script:

```powershell
$env:PYTHONUTF8='1'
.venv\Scripts\python.exe skills\safe-github-upload\scripts\preflight.py --root D:\CowWechat
```

If the script reports `BLOCKED`, do not commit or push until the blocked staged
files or detected secrets are removed from the index. Unstaged runtime-state
warnings may be left alone and reported to the user.

## Upload Workflow

1. Confirm the request is from an admin-capable context.
2. Find the Git root with `git rev-parse --show-toplevel`.
3. Run the preflight script before staging.
4. Review `git status --short --branch`. Treat pre-existing unrelated changes as
   user work unless the user explicitly says to commit all project code.
5. Stage only intentional source, tests, docs, and safe skill files:
   - Prefer path-specific `git add -- <paths>`.
   - For "commit all code", still exclude protected/runtime files flagged by the
     preflight script.
   - Never use `git add -f` for ignored or protected files.
6. Run the preflight script again after staging.
7. Inspect staged content:
   - `git diff --cached --name-status`
   - `git diff --cached --check`
   - Use `git diff --cached -- <path>` for risky files.
8. Run focused validation for the changed area. For skill-only changes, run:
   - `.venv\Scripts\python.exe skills\skill-creator\scripts\quick_validate.py skills\<skill>`
   - Python syntax checks for any bundled scripts.
9. Update `GIT_NOTES.md` with the change, validation, rollback notes, and final
   GitHub target.
10. Commit with a concise message such as `feat: add safe github upload skill`.
11. If `GIT_NOTES.md` needs the final hash, amend the commit when low-risk.
12. Push with `git push origin main`.
13. Report commit hash, push result, validation, and any uncommitted local files.

## Protected Files

Use `.gitignore` as the first source of truth. In this project, never commit:

- `config.json`, `config.json.backup.*`
- `.env`, `.env.*` except safe templates like `.env.example`
- `*.key`, `*.pem`, private keys, `id_rsa`, `id_ed25519`
- `credentials*.json`, `token*.json`, `cookies*.json`,
  `session*.json`, `service-account*.json`
- `.weixin_cow_credentials.json`, `QR.png`
- `.venv/`, `venv*/`, `node_modules/`, build output
- `logs/`, `*.log`, `tmp/`, `workspace/`, `secrets/`, `local/`
- `.codex/`, `.playwright-mcp/`
- runtime databases, generated indexes, chat logs, local memory dumps, and other
  machine state unless the user explicitly requests a safe export file

Project exception: protocol knowledge backend artifacts under
`knowledge_backend/` are intended to be portable when they come from uploaded
protocol/specification ingestion. Commit `knowledge_backend/indexes/kb.sqlite`,
`knowledge_backend/originals/`, `knowledge_backend/derived/`,
`knowledge_backend/reports/`, and `knowledge_backend/manifest.json` after
validation so another machine can reuse the parsed protocol library and
model-generated study documents without reprocessing the source document.

Safe examples and placeholders are allowed only when they contain no real keys:

- `.env.example`
- documented fake tokens in tests
- templates that say `<redacted>` or placeholder values

## Skill Development Sync Rule

When creating or updating any CowWechat skill, maintain both copies:

1. Repository builtin copy: `D:\CowWechat\skills\<skill-name>\`
2. Deployed workspace copy: `$HOME\cow\skills\<skill-name>\`

Edit and validate the repository copy first. Then copy the entire skill directory
to the deployed workspace copy so the running Agent sees the same behavior:

```powershell
New-Item -ItemType Directory -Force $HOME\cow\skills | Out-Null
Copy-Item -Recurse -Force .\skills\<skill-name> $HOME\cow\skills\<skill-name>
```

After syncing, commit and push the repository copy. Do not copy secrets, pycache,
logs, generated snapshots, or local runtime files into either skill directory.

## GitHub Token Rule

Use the already configured Git credential manager or `GITHUB_TOKEN` from the
environment. Never write tokens into files, commit messages, remotes, logs, or
chat output. If a command prints a token-bearing URL, stop and redact it before
reporting.

## Failure Handling

- If protected files are staged, unstage only those paths with
  `git restore --staged -- <path>` and rerun preflight.
- If the push is rejected because remote is ahead, fetch and inspect. Use normal
  non-destructive merge or rebase only when appropriate; never force push without
  explicit user approval.
- If validation fails, fix the issue before committing. If the user insists on a
  commit with known failures, document the failure in `GIT_NOTES.md` and the
  final response.
- If a runtime database outside the protocol knowledge backend is modified,
  leave it unstaged unless the user explicitly asked to publish a safe export.
