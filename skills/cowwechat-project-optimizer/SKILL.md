---
name: cowwechat-project-optimizer
description: Analyze and optimize this CowWeCom/CowAgent project from local runtime evidence. Use when an admin asks to optimize CowWeCom, optimize this project, analyze model-cache hit rate, analyze real model inputs, improve local reasoning-effort rules, review repeated Agent tool-call chains, preserve or inspect temporary scripts, or turn recurring CowAgent/WeChat/WeCom workflows into reusable skills without exposing user memory or raw chat data.
---

# CowWeCom Project Optimizer

Use this skill for admin-only local optimization of the current CowWeCom deployment. It reads local evidence produced under the ignored Agent workspace, summarizes cache/reasoning/tool/temp-script patterns, and recommends or applies tightly scoped project improvements.

## Hard Rules

- Never upload temporary scripts, optimizer evidence, user memory, conversation stores, logs, raw model inputs, cookies, tokens, config files, or local databases to GitHub.
- Do not print raw user messages, raw memory, raw tool arguments, full model payloads, file contents from private user memory, or secrets in the final answer.
- Treat `memory/users/<user>/` as private to that user. Do not use optimizer reports to expose one user's memory or chat content to another user.
- Only admins should run the raw-evidence optimizer. Normal users can receive a refusal or a high-level explanation.
- Keep `cowagent-workflow-auditor` for reusable-workflow discovery. Use this skill for the project optimization pass that consumes the optimizer evidence store.

## Local Evidence

Runtime code records local-only evidence when the relevant config keys are enabled:

- `project_optimizer_evidence_enabled`
- `project_optimizer_raw_capture_enabled`
- `project_optimizer_preserve_temp_scripts`
- `project_optimizer_delete_raw_after_run`
- `reasoning_effort_policy_runtime_auto_optimize_enabled=false` keeps the old in-Agent reasoning optimizer disabled by default.

Default location:

```text
<agent_workspace>/data/project-optimizer/
```

Important subdirectories:

- `events/`: sanitized JSONL events for task starts/ends, model request shapes, provider payload shapes, tool summaries, and temp-script snapshots.
- `raw_model_inputs/`: local raw user/model input cache with secret and reasoning redaction. This is consumed and deleted by the optimizer run when configured.
- `temp_scripts/`: local snapshots of temporary scripts written under `tmp`, `temp`, `sandbox`, or `workspace` paths.
- `reports/`: sanitized optimizer reports.

## Workflow

1. Confirm the repo root and runtime workspace:

```powershell
$root = git rev-parse --show-toplevel
$workspace = "C:\Users\RondleLiu\cow"
```

2. Run the optimizer report:

```powershell
python <base_dir>\scripts\run_optimizer.py --project-root $root --workspace $workspace --consume-raw --mark-optimized
```

3. Read the generated sanitized report. It should cover:

- analyzed data sources and missing sources
- repeated temporary scripts and whether they should become skills/scripts/docs/skip
- repeated Agent tool-call chains
- prompt-cache hit-rate and real-input shape opportunities
- local reasoning-effort routing opportunities
- privacy/upload guard status

4. If code changes are warranted, make only the smallest scoped changes, validate them, update `README.md`, `GIT_NOTES.md`, and `.codex/HANDOFF.md`, then use the safe GitHub upload flow. Do not stage runtime evidence.

## Command Options

Generate report without deleting raw input cache:

```powershell
python <base_dir>\scripts\run_optimizer.py --project-root $root --workspace $workspace
```

Write report to a specific ignored path:

```powershell
python <base_dir>\scripts\run_optimizer.py --project-root $root --workspace $workspace --output tmp\project-optimizer-report.md
```

Emit machine-readable JSON summary:

```powershell
python <base_dir>\scripts\run_optimizer.py --project-root $root --workspace $workspace --json-out tmp\project-optimizer-report.json
```

Fast-check whether the local machine has accumulated enough model calls for another optimization pass:

```powershell
python <base_dir>\scripts\query_incremental_calls.py --project-root $root --workspace $workspace --threshold 300 --json
```

Mark the current model-call count as optimized after a successful report:

```powershell
python <base_dir>\scripts\query_incremental_calls.py --workspace $workspace --threshold 300 --mark-optimized --report-path "<report-path>"
```

## Codex Daily Automation

On the user's primary machine, prefer a Codex automation over CowAgent's hidden scheduler:

- Schedule: every day at 00:00 local time.
- Working directory: the CowWeCom checkout.
- First run `query_incremental_calls.py --threshold 300 --json`.
- If `due` is false, stop and report only the skip count.
- If `due` is true, run `run_optimizer.py --consume-raw --mark-optimized --call-threshold 300`.
- After the report succeeds, raw optimizer input cache is deleted according to `project_optimizer_delete_raw_after_run`.
- Never commit or upload the generated reports, state JSON, raw cache, temp-script snapshots, or user memory.

## Output Policy

Reports may include hashes, counts, basenames, categories, cache percentages, reasoning rule names, and sanitized recommendations. Reports must not include raw prompts, user memory text, private chat excerpts, full temp-script contents, auth paths, credentials, or per-user identities beyond hashes/labels already safe for local admin diagnostics.

## Error Handling

- If runtime workspace is unavailable, report it as inaccessible and analyze repository evidence only.
- If raw cache is absent, still analyze sanitized events, usage telemetry, reasoning audit logs, and temp-script manifests.
- If raw cache deletion is requested, delete only after the sanitized report is written successfully.
- If a requested optimization would weaken memory isolation or Git upload protection, reject that optimization.
