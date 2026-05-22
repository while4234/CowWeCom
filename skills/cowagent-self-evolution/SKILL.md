---
name: cowagent-self-evolution
description: CowAgent self-evolution memory for local execution lessons. Use when the agent needs to diagnose, list, seed, or manually record reusable runtime mistakes, especially Windows cmd/PowerShell versus Unix shell dialect failures, Bash heredoc errors, QR/image parsing command failures, and user corrections that should be remembered in the background without being shown in WeChat.
metadata:
  cowagent:
    default_enabled: true
    requires:
      bins: ["python"]
    emoji: "brain"
---

# CowAgent Self Evolution

Use this skill to inspect or manually maintain CowAgent's local self-evolution records. Normal automatic recording is handled by the runtime and does not require the agent to call this script.

## Storage

Runtime records are stored under:

`<agent_workspace>/data/cowagent-self-evolution/`

Files:

- `reusable_errors.jsonl` - append-only event log with redacted command/output previews.
- `active_rules.json` - compact rules injected into future system prompts.

Do not store secrets, raw `.env` values, cookies, tokens, API keys, or private auth paths in these records.

## Script

Run from the project root when possible:

```bash
python "<base_dir>/scripts/self_evolution.py" doctor
python "<base_dir>/scripts/self_evolution.py" list
python "<base_dir>/scripts/self_evolution.py" seed
python "<base_dir>/scripts/self_evolution.py" log-shell --command "<failed command>" --output "<stderr/stdout>" --exit-code 1
```

Commands:

- `doctor` prints the active storage path and current compact rule count.
- `list` prints active compact rules as JSON.
- `seed` writes the built-in Windows shell dialect rule if it is not already present.
- `log-shell` manually records a failed shell command when it matches a reusable Windows dialect mistake.

## Operating Rules

- Prefer the runtime's automatic side-channel recorder. It does not consume `agent_max_steps`, does not add messages to conversation history, and does not send WeChat-visible notices.
- Use this script only for diagnostics, manual backfill, or confirming that active rules exist.
- For Windows shell work, remember that CowAgent's `bash` tool runs through `cmd.exe`. Avoid Bash heredocs such as `python - <<EOF`, and avoid Unix-only commands like `grep`, `sed`, `awk`, `head`, and `tail`.
- For QR/image parsing command workflows on Windows, prefer `python -c`, a temporary `.py` file, cmd-compatible commands, or explicit `powershell -NoProfile -Command` with Windows-compatible quoting.
