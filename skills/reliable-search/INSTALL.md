# Install reliable-search

## Purpose
Reliable web search through Serper Google Search and Brave Search provider fallback.

## Requirements
Python 3 and at least one user-provided search API key: `SERPER_API_KEY` or `BRAVE_API_KEY`.

## Install From This Repository
From the repository root on the target machine:

```powershell
New-Item -ItemType Directory -Force $HOME\cow\skills | Out-Null
Copy-Item -Recurse -Force .\skills\reliable-search $HOME\cow\skills\reliable-search
```

Then restart CowWechat or reload skills so the runtime scans the copied directory.

## Local Configuration
If this skill is also shipped as a builtin project skill, the copied custom skill under $HOME\cow\skills\reliable-search takes precedence. Keep any machine-specific config in ignored local files or environment variables.

## Secrets And API Keys
The user must provide search API keys locally, for example with `env_config` or an ignored `.env`. Do not commit keys, raw provider responses containing private queries, or local debug dumps.

