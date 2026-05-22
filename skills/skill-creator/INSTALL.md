# Install skill-creator

## Purpose
Create, validate, and package CowAgent skills.

## Requirements
Python 3. Optional validation dependencies can be installed in the local project environment.

## Install From This Repository
From the repository root on the target machine:

```powershell
New-Item -ItemType Directory -Force $HOME\cow\skills | Out-Null
Copy-Item -Recurse -Force .\skills\skill-creator $HOME\cow\skills\skill-creator
```

Then restart CowWechat or reload skills so the runtime scans the copied directory.

## Local Configuration
If this skill is also shipped as a builtin project skill, the copied custom skill under $HOME\cow\skills\skill-creator takes precedence. Keep any machine-specific config in ignored local files or environment variables.

## Secrets And API Keys
No API key required unless the new skill integrates an external API; then declare the required env var and document that users must provide it locally.

