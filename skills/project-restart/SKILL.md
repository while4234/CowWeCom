---
name: project-restart
description: Restart the running CowWechat/CowAgent project service. Use by default when the admin says 重启, 重启项目, 重启服务, 重启 CowWechat, 重启 CowAgent, restart project, restart service, reload service, or asks to apply code/config/skill changes by restarting the current project. Do not use for browser page refresh, full computer reboot, Docker restart, Git pull/update, or unrelated app restarts unless the user explicitly names CowWechat/CowAgent.
metadata:
  cowagent:
    default_enabled: true
    requires:
      anyBins: ["python", "python3", "py"]
---

# Project Restart

## Overview

Use this skill to restart only the current CowWechat/CowAgent project service. It stops all Python `app.py` processes whose command line points at the current project root, then starts one fresh background service through the existing `cli.cli restart` process manager.

## Workflow

1. Resolve the current CowWechat project root. Prefer the active project checkout, or pass it explicitly with `--root`.
2. Run the bundled restart helper:

```powershell
$env:PYTHONUTF8='1'
.\.venv\Scripts\python.exe .\skills\project-restart\scripts\restart_project.py --root .
```

3. Tell the user the restart has been scheduled. The helper intentionally returns before the service is killed so an in-process Agent can reply before restarting itself.
4. For external maintenance shells only, use `--run-now --delay 0` to restart synchronously and print validation output through the worker log.

## Safety Rules

- Use this skill for the current CowWechat/CowAgent project only.
- Do not use this skill for `git pull`, code update, Docker restart, browser refresh, OS reboot, or unrelated services.
- Do not use legacy `scripts/start.sh` or `scripts/shutdown.sh`; those can block on log tailing or miss Windows/venv Python launches.
- Keep the default detached-worker mode when the request came through the running Agent. Direct `cow restart` from inside the service can kill the tool subprocess before it starts the replacement service.
- Check `<project-root>/project-restart.log` only if the user asks for restart diagnostics.
