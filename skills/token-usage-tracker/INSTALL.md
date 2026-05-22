# Install token-usage-tracker

## Purpose
Local-only per-user token usage tracking and reporting.

## Requirements
Python 3. Optional `tiktoken` improves local text estimation but is not required.

## Install From This Repository
From the repository root on the target machine:

```powershell
New-Item -ItemType Directory -Force $HOME\cow\skills | Out-Null
Copy-Item -Recurse -Force .\skills\token-usage-tracker $HOME\cow\skills\token-usage-tracker
```

Then restart CowWechat or reload skills so the runtime scans the copied directory.

## Local Configuration
If this skill is also shipped as a builtin project skill, the copied custom skill under $HOME\cow\skills\token-usage-tracker takes precedence. Keep any machine-specific config in ignored local files or environment variables.

## Secrets And API Keys
No API key required. Usage records stay local under `data/token-usage-tracker/`; do not commit private user identifiers or local usage databases.

