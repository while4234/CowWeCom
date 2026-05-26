---
name: cowagent-workflow-auditor
description: Audit CowAgent/CowWechat runtime logs, conversation stores, tool-call traces, workspace/tmp artifacts, and installed skills/plugins to find repeated agent workflows worth turning into reusable skills. Use when asked to analyze historical user usage, repeated temporary scripts, repeated tool-call chains, skill/plugin gaps, or to produce a privacy-safe reusable-workflow candidate report.
---

# CowAgent Workflow Auditor

## Overview

Use this skill to inspect a CowAgent/CowWechat checkout plus its local runtime workspace and summarize repeatable workflows without exposing private chat text or secrets. Prefer evidence from file paths, counts, tool names, timestamps, and redacted titles over raw conversation content.

## Privacy Rules

- Do not quote private user messages, credentials, cookies, tokens, or full config values.
- Report conversation evidence as counts, short redacted labels, and workflow categories.
- Treat `config.json`, `.env`, auth files, memory files, and SQLite conversation rows as sensitive. Read only the minimum needed to classify workflow patterns.
- If a source is absent or blocked, say `无法访问` for that source.

## Workflow

1. Confirm repository structure first:
   - `skills/`, `plugins/`, `agent/`, `channel/`, `bridge/`, `common/`
   - runtime workspace from `config.py` or `config.json` `agent_workspace`, usually `~/cow`
   - logs such as `run.log`, `nohup.out`, workspace `run.log`, and project handoff files
2. Run the bundled scanner to collect non-sensitive counts:

```powershell
python <base_dir>\scripts\audit_workflows.py --project-root D:\CowWechat --workspace C:\Users\RondleLiu\cow --json-out tmp\workflow-audit.json
```

3. Inspect the JSON summary and only open raw files when a specific count needs confirmation.
4. Cross-check existing skills before recommending a new one:

```powershell
Get-ChildItem skills -Directory | Select-Object -ExpandProperty Name | Sort-Object
```

5. Classify candidates using this decision rule:
   - `新建 skill`: repeated at least twice, stable inputs, standard steps, clear output, not already covered.
   - `扩展已有 skill`: repeated wrapper scripts or tool chains around an existing skill.
   - `保留为脚本`: deterministic maintenance or validation flow that is too implementation-specific.
   - `写入文档`: one-time policy or operator guidance with no reusable execution steps.
   - `skip`: sensitive, too rare, unstable, or already solved well.

## Evidence To Prioritize

- Repeated `write(path=tmp\*.py)` followed by `bash(command=python tmp\*.py)`.
- Repeated numbered workspace directories such as `workspace\foo-test`, `workspace\foo-test2`, `workspace\foo-test3`.
- Repeated skill read plus script execution chains, especially if a wrapper script is rewritten each time.
- Repeated browser/search/fetch chains that follow the same source policy.
- Repeated file-ingress or channel-specific attachment handling across Weixin, WeCom, and web.

## Output Format

Return a concise report:

```markdown
## Data Sources
- ...

## Repeated Patterns
- ...

| 候选工作流 | 证据来源 | 重复频率 | 当前实现方式 | 推荐处理 | 理由 |
|---|---|---:|---|---|---|
| ... |

## Landed Changes
- ...
```

## Error Handling

- If logs are huge, scan only matched lines and summarize counts.
- If SQLite cannot be opened, fall back to log/tool traces and mark conversation rows as inaccessible.
- If the runtime workspace is unavailable, audit only the repository and say the runtime workspace is inaccessible.
- If scanner output conflicts with direct code inspection, trust code inspection and mention the discrepancy.
