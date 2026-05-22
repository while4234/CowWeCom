# Install knowledge-wiki

## Purpose
Maintain a local structured knowledge wiki.

## Requirements
No API key required for local wiki operations.

## Install From This Repository
From the repository root on the target machine:

```powershell
New-Item -ItemType Directory -Force $HOME\cow\skills | Out-Null
Copy-Item -Recurse -Force .\skills\knowledge-wiki $HOME\cow\skills\knowledge-wiki
```

Then restart CowWechat or reload skills so the runtime scans the copied directory.

## Local Configuration
If this skill is also shipped as a builtin project skill, the copied custom skill under $HOME\cow\skills\knowledge-wiki takes precedence. Keep any machine-specific config in ignored local files or environment variables.

## Secrets And API Keys
Do not commit private source documents or personal notes unless intentionally publishing them.

