# Install safe-github-upload

## Purpose
Safely commit and push CowWechat code to the user's GitHub repository while respecting `.gitignore` and protecting secrets, credentials, logs, runtime state, and generated local data.

## Requirements
CowWechat runtime with Git available and GitHub credentials configured locally through the host Git credential manager or `GITHUB_TOKEN`.

## Install From This Repository
From the repository root on the target machine:

```powershell
New-Item -ItemType Directory -Force $HOME\cow\skills | Out-Null
Copy-Item -Recurse -Force .\skills\safe-github-upload $HOME\cow\skills\safe-github-upload
```

Then restart CowWechat or reload skills so the runtime scans the copied directory.

## Local Configuration
If this skill is also shipped as a builtin project skill, the copied custom skill under `$HOME\cow\skills\safe-github-upload` takes precedence. Keep machine-specific config in ignored local files, Git Credential Manager, or environment variables.

## Secrets And API Keys
No API key is stored in this skill. Do not commit `GITHUB_TOKEN`, credentials, cookies, sessions, QR login files, private keys, `.env`, or `config.json`.
