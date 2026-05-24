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

Use this skill to inspect or manually maintain CowAgent's local self-evolution records. Normal automatic recording and deterministic policy application are handled by the runtime and do not require the agent to call this script.

## Storage

Runtime records are stored under:

`<agent_workspace>/data/cowagent-self-evolution/`

Files:

- `reusable_errors.jsonl` - append-only event log with redacted command/output previews and deterministic policy applications.
- `active_rules.json` - compact rules used for bounded prompt guidance and local execution policies.
- `post_task_reflections.jsonl` - append-only background reflection reports for assistant process text mined after completed tasks.

Tool-attempt policy records are stored separately under:

`<agent_workspace>/data/tool-attempt-memory/`

That store keeps only hashes, counts, failure classes, and safe selector-shaped argument metadata. It does not store raw tool arguments, raw outputs, prompts, private paths, tokens, or API keys.

Do not store secrets, raw `.env` values, cookies, tokens, API keys, or private auth paths in these records.

## Script

Run from the project root when possible:

```bash
python "<base_dir>/scripts/self_evolution.py" doctor
python "<base_dir>/scripts/self_evolution.py" list
python "<base_dir>/scripts/self_evolution.py" list --source tools
python "<base_dir>/scripts/self_evolution.py" seed
python "<base_dir>/scripts/self_evolution.py" log-shell --command "<failed command>" --output "<stderr/stdout>" --exit-code 1
python "<base_dir>/scripts/self_evolution.py" log-learning --id "<stable-rule-id>" --summary "<short lesson>" --next "<what to do next time>" --details "<optional context>"
python "<base_dir>/scripts/self_evolution.py" reflect-task --process-text "<assistant process text before tool calls>"
```

Commands:

- `doctor` prints the self-evolution storage path, tool-attempt storage path, and compact rule counts.
- `list` prints both self-evolution and tool-attempt active compact rules as JSON.
- `list --source self` prints only shell/self-evolution rules.
- `list --source tools` prints all generic tool-attempt policy rules.
- `seed` writes the built-in Windows shell dialect rule if it is not already present.
- `log-shell` manually records a failed shell command when it matches a reusable Windows dialect mistake.
- `log-learning` manually records a reusable workflow or tool-behavior lesson when the issue is not a failed shell command.
- `reflect-task` runs the post-task reflection path for supplied assistant process text. The runtime normally schedules this automatically after a completed Agent task; the CLI command is for diagnostics/backfill and does not call a model.

All commands accept `--workspace-root <path>` after the command name for diagnostics against a specific agent workspace.

## Operating Rules

- Prefer the runtime's automatic side-channel recorder. It does not consume `agent_max_steps`, does not add messages to conversation history, and does not send WeChat-visible notices.
- After a completed Agent task, CowAgent queues a background self-evolution reflection. It first refreshes existing tool-attempt/self-evolution rules, then analyzes only assistant process text that appeared before tool calls; final answers, user prompts, raw tool arguments, and raw tool outputs are excluded.
- The post-task reflection always keeps the tool-error refresh step. Model-based missed-lesson analysis is skipped only for short non-development tasks (fewer than 10 process turns) when tool-error learning did not create or update any reusable lesson; development/code tasks always keep model-based missed-lesson analysis enabled.
- The background reflection is bounded, redacted, failure-isolated, and writes only compact reusable lessons through `active_rules.json`. New guidance is picked up on later requests through the request-scoped self-evolution context.
- CowAgent loads compact tool-attempt rules once per user request and uses an in-process mtime cache; individual tool calls use in-memory lookups rather than scanning historical logs.
- High-confidence compact rules are injected into request-scoped runtime context in a stable order before volatile time or retrieved-knowledge context. Keep the system prompt section static so learned-rule churn does not reduce system-prompt cache hits; the execution-layer guard remains the fallback.
- High-confidence local policies can be applied before execution, such as rewriting unsafe Windows cmd environment assignment syntax or blocking fragile multi-line `python -c` snippets before launching `cmd.exe`.
- Use this script only for diagnostics, manual backfill, or confirming that active rules exist.
- For Windows shell work, remember that CowAgent's `bash` tool runs through `cmd.exe`. Avoid Bash heredocs such as `python - <<EOF`, and avoid Unix-only commands like `grep`, `sed`, `awk`, `head`, and `tail`.
- For QR/image parsing command workflows on Windows, prefer `python -c`, a temporary `.py` file, cmd-compatible commands, or explicit `powershell -NoProfile -Command` with Windows-compatible quoting.
