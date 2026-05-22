# Install code-update

## Purpose
Safely fast-forward the running CowWechat checkout from GitHub while protecting local config and secrets.

## Requirements
CowWechat runtime with the `git_code_update` tool and a configured Git remote.

## Install From This Repository
From the repository root on the target machine:

```powershell
New-Item -ItemType Directory -Force $HOME\cow\skills | Out-Null
Copy-Item -Recurse -Force .\skills\code-update $HOME\cow\skills\code-update
```

Then restart CowWechat or reload skills so the runtime scans the copied directory.

## Local Configuration
If this skill is also shipped as a builtin project skill, the copied custom skill under $HOME\cow\skills\code-update takes precedence. Keep any machine-specific config in ignored local files or environment variables.

## Secrets And API Keys
No API key is stored in this skill. Git credentials must be configured locally through the host Git credential manager or environment.

