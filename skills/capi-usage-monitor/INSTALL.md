# Install capi-usage-monitor

## Purpose
Query and locally snapshot CAPI/Codex intermediary quota and usage.

## Requirements
Python 3. For online quota checks, configure the CAPI activation key locally. In this workspace the default key source is `OPENAI_API_KEY`; alternatives are `CAPI_API_KEY`, `CAPI_ACTIVATION_CODE`, or `CAPI_CARD`.

## Install From This Repository
From the repository root on the target machine:

```powershell
New-Item -ItemType Directory -Force $HOME\cow\skills | Out-Null
Copy-Item -Recurse -Force .\skills\capi-usage-monitor $HOME\cow\skills\capi-usage-monitor
```

Then restart CowWechat or reload skills so the runtime scans the copied directory.

## Local Configuration
If this skill is also shipped as a builtin project skill, the copied custom skill under $HOME\cow\skills\capi-usage-monitor takes precedence. Keep any machine-specific config in ignored local files or environment variables.

## Secrets And API Keys
Do not commit API keys or activation codes. Use `env_config` or an ignored local environment file. Snapshots store only hashed/suffixed key identifiers.

