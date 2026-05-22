# Install image-generation

## Purpose
Generate or edit images through CowAgent background jobs.

## Requirements
Python 3. Preferred `codex_auth` runtime requires the user to be logged in locally to Codex (`CODEX_HOME/auth.json` or `CODEX_AUTH_FILE`). API fallback requires at least one user-provided key: `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ARK_API_KEY`, `DASHSCOPE_API_KEY`, `MINIMAX_API_KEY`, or `LINKAI_API_KEY`.

## Install From This Repository
From the repository root on the target machine:

```powershell
New-Item -ItemType Directory -Force $HOME\cow\skills | Out-Null
Copy-Item -Recurse -Force .\skills\image-generation $HOME\cow\skills\image-generation
```

Then restart CowWechat or reload skills so the runtime scans the copied directory.

## Local Configuration
If this skill is also shipped as a builtin project skill, the copied custom skill under $HOME\cow\skills\image-generation takes precedence. Keep any machine-specific config in ignored local files or environment variables.

## Secrets And API Keys
Never commit Codex `auth.json`, API keys, access tokens, user image inputs, or private generated outputs. Configure credentials only in local ignored config/environment.

