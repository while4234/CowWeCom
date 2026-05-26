#!/usr/bin/env python3
"""Generate a sanitized CowWeCom project-optimization report."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEDULER_STATE_FILENAME = "codex_optimizer_state.json"


def _import_project(root: Path) -> None:
    root = root.resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def _read_jsonl(path: Path, limit: int = 5000) -> list[dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except OSError:
        return []
    return rows[-max(1, limit):]


def _read_all_jsonl(paths: Iterable[Path], limit_per_file: int = 5000) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        rows.extend(_read_jsonl(path, limit_per_file))
    rows.sort(key=lambda item: str(item.get("timestamp") or ""))
    return rows


def _usage_records(workspace: Path) -> list[dict[str, Any]]:
    return _read_jsonl(_llm_usage_path(workspace), limit=10000)


def _reasoning_records(workspace: Path) -> list[dict[str, Any]]:
    data = workspace / "data"
    names = (
        "reasoning_effort_policy_decisions_private.jsonl",
        "reasoning_effort_policy_decisions_group.jsonl",
        "reasoning_effort_policy_decisions.jsonl",
    )
    return _read_all_jsonl((data / name for name in names), limit_per_file=5000)


def _optimizer_event_records(data_dir: Path) -> list[dict[str, Any]]:
    events_dir = data_dir / "events"
    paths = sorted(events_dir.glob("*.jsonl")) if events_dir.exists() else []
    return _read_all_jsonl(paths, limit_per_file=5000)


def _raw_record_count(data_dir: Path) -> tuple[int, int, list[str]]:
    raw_dir = data_dir / "raw_model_inputs"
    files = sorted(raw_dir.glob("*.jsonl")) if raw_dir.exists() else []
    count = 0
    bytes_total = 0
    for path in files:
        rows = _read_jsonl(path, limit=1_000_000)
        count += len(rows)
        try:
            bytes_total += path.stat().st_size
        except OSError:
            pass
    return count, bytes_total, [str(path) for path in files]


def _temp_script_records(data_dir: Path) -> list[dict[str, Any]]:
    return _read_jsonl(data_dir / "temp_scripts" / "manifest.jsonl", limit=10000)


def _llm_usage_path(workspace: Path) -> Path:
    return workspace / "data" / "llm_cache_usage.jsonl"


def _scheduler_state_path(data_dir: Path) -> Path:
    return data_dir / SCHEDULER_STATE_FILENAME


def _count_lines(path: Path) -> int:
    if not path.exists() or not path.is_file():
        return 0
    total = 0
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                total += chunk.count(b"\n")
    except OSError:
        return 0
    return total


def _read_scheduler_state(data_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads(_scheduler_state_path(data_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _incremental_call_status(workspace: Path, data_dir: Path, threshold: int) -> dict[str, Any]:
    current = _count_lines(_llm_usage_path(workspace))
    state = _read_scheduler_state(data_dir)
    last = _to_int(state.get("last_optimized_llm_usage_records"))
    incremental = max(0, current - last)
    threshold = max(1, threshold)
    return {
        "current_llm_usage_records": current,
        "last_optimized_llm_usage_records": last,
        "incremental_calls": incremental,
        "threshold": threshold,
        "due": incremental >= threshold,
        "state_path": str(_scheduler_state_path(data_dir)),
        "llm_cache_usage_path": str(_llm_usage_path(workspace)),
    }


def _mark_optimized(workspace: Path, data_dir: Path, report_path: Path) -> dict[str, Any]:
    state = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_optimized_llm_usage_records": _count_lines(_llm_usage_path(workspace)),
        "last_report_path": str(report_path),
    }
    path = _scheduler_state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state["state_path"] = str(path)
    return state


def _cache_summary(usage: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    prompt_tokens = sum(_to_int(item.get("prompt_tokens")) for item in usage)
    cached_tokens = sum(_to_int(item.get("cached_tokens")) for item in usage)
    long_zero = [
        item for item in usage
        if _to_int(item.get("prompt_tokens")) >= 50000 and _to_int(item.get("cached_tokens")) == 0
    ]
    by_kind: dict[str, dict[str, Any]] = {}
    for item in usage:
        kind = str(item.get("request_kind") or "unknown")
        bucket = by_kind.setdefault(kind, {"requests": 0, "prompt_tokens": 0, "cached_tokens": 0})
        bucket["requests"] += 1
        bucket["prompt_tokens"] += _to_int(item.get("prompt_tokens"))
        bucket["cached_tokens"] += _to_int(item.get("cached_tokens"))
    for bucket in by_kind.values():
        bucket["cache_hit_rate"] = cached_tokens_rate(bucket["cached_tokens"], bucket["prompt_tokens"])

    provider_payloads = [item for item in events if item.get("event_type") == "provider_payload"]
    request_shapes = Counter(str(item.get("request_kind") or "unknown") for item in provider_payloads)
    unstable_context = [
        item for item in provider_payloads
        if _to_int(item.get("self_evolution_context_chars")) > 0
        or _to_int(item.get("retrieved_knowledge_chars")) > 0
        or _to_int(item.get("tool_result_chars")) > 20000
    ]
    return {
        "usage_records": len(usage),
        "optimizer_provider_payloads": len(provider_payloads),
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached_tokens,
        "cache_hit_rate": cached_tokens_rate(cached_tokens, prompt_tokens),
        "long_input_zero_cache_requests": len(long_zero),
        "by_request_kind": sorted(by_kind.items(), key=lambda item: item[1]["prompt_tokens"], reverse=True)[:10],
        "provider_request_kinds": request_shapes.most_common(10),
        "unstable_context_payloads": len(unstable_context),
    }


def _reasoning_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = [item for item in records if item.get("event_type") in ("decision", None, "")]
    outcomes = [item for item in records if item.get("event_type") == "task_outcome"]
    effort_counts = Counter(str(item.get("selected_effort") or "unknown") for item in decisions)
    rules = Counter(str(item.get("local_rule") or item.get("reason") or "unknown") for item in decisions)
    failures = [
        item for item in outcomes
        if str(item.get("task_status") or "").lower() not in {"", "success"}
        or bool(item.get("max_turns_exhausted"))
        or _to_int(item.get("tool_attempt_error_count")) > 0
    ]
    return {
        "records": len(records),
        "decisions": len(decisions),
        "outcomes": len(outcomes),
        "effort_counts": effort_counts.most_common(),
        "top_rules": rules.most_common(12),
        "failure_or_retry_outcomes": len(failures),
    }


def _tool_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    tools = [item for item in events if item.get("event_type") == "tool_event"]
    by_name = Counter(str(item.get("tool_name") or "unknown") for item in tools)
    errors = Counter(
        str(item.get("tool_name") or "unknown")
        for item in tools
        if str(item.get("status") or "").lower() not in {"success", "skipped"}
    )
    repeated_shapes = Counter(
        (str(item.get("tool_name") or ""), str(item.get("argument_shape_hash") or ""))
        for item in tools
        if item.get("argument_shape_hash")
    )
    repeated = [
        {"tool": name, "argument_shape_hash": shape, "count": count}
        for (name, shape), count in repeated_shapes.most_common(20)
        if count >= 2
    ]
    return {
        "tool_events": len(tools),
        "top_tools": by_name.most_common(12),
        "tool_errors": errors.most_common(12),
        "repeated_argument_shapes": repeated[:12],
    }


def _temp_script_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    basenames = Counter(str(item.get("basename") or "unknown") for item in records)
    content_hashes = Counter(str(item.get("content_hash") or "") for item in records if item.get("content_hash"))
    repeated_names = [(name, count) for name, count in basenames.most_common(20) if count >= 2]
    repeated_content = [(digest, count) for digest, count in content_hashes.most_common(20) if count >= 2]
    return {
        "snapshots": len(records),
        "repeated_basenames": repeated_names[:12],
        "repeated_content_hashes": repeated_content[:12],
        "recent_basenames": [str(item.get("basename") or "") for item in records[-12:]],
    }


def _candidate_table(cache: dict[str, Any], reasoning: dict[str, Any], tools: dict[str, Any], scripts: dict[str, Any]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    if scripts["repeated_basenames"] or scripts["repeated_content_hashes"]:
        candidates.append({
            "workflow": "Repeated temporary script creation",
            "evidence": f"{scripts['snapshots']} snapshots; repeated names/content present",
            "frequency": "2+",
            "current": "Ad hoc tmp/workspace scripts archived locally",
            "recommendation": "\u65b0\u5efa skill",
            "reason": "Stable scripts are being recreated; convert high-repeat flows into reusable skill scripts.",
        })
    else:
        candidates.append({
            "workflow": "Temporary script preservation",
            "evidence": f"{scripts['snapshots']} snapshots",
            "frequency": "ongoing",
            "current": "Local archive",
            "recommendation": "\u4fdd\u7559\u4e3a\u811a\u672c",
            "reason": "Evidence is useful, but no repeated script name/content has crossed the skill threshold yet.",
        })

    if cache["long_input_zero_cache_requests"] or cache["unstable_context_payloads"]:
        candidates.append({
            "workflow": "Prompt-cache input-shape optimization",
            "evidence": f"{cache['long_input_zero_cache_requests']} long zero-cache requests; {cache['unstable_context_payloads']} unstable context payloads",
            "frequency": "ongoing",
            "current": "Telemetry plus raw local input cache",
            "recommendation": "\u6269\u5c55\u5df2\u6709 skill",
            "reason": "Extend token/project optimizer reporting before changing prompt layout.",
        })

    if reasoning["failure_or_retry_outcomes"] or reasoning["decisions"]:
        candidates.append({
            "workflow": "Local reasoning-depth rule tuning",
            "evidence": f"{reasoning['decisions']} decisions; {reasoning['failure_or_retry_outcomes']} risky outcomes",
            "frequency": "ongoing",
            "current": "Local reasoning_effort_policy audit/optimizer",
            "recommendation": "\u6269\u5c55\u5df2\u6709 skill",
            "reason": "Existing reasoning policy owns rule learning; this project skill guides safe local-rule edits.",
        })

    if tools["repeated_argument_shapes"]:
        candidates.append({
            "workflow": "Repeated Agent tool-call chains",
            "evidence": f"{len(tools['repeated_argument_shapes'])} repeated tool argument shapes",
            "frequency": "2+",
            "current": "Tool loop plus safe attempt memory",
            "recommendation": "\u65b0\u5efa skill",
            "reason": "Repeated tool shapes may indicate a reusable deterministic helper.",
        })

    candidates.append({
        "workflow": "User memory export or cross-user memory analysis",
        "evidence": "Protected by access policy and Git preflight",
        "frequency": "n/a",
        "current": "Private memory/users isolation",
        "recommendation": "skip",
        "reason": "Violates privacy boundary; optimizer must use hashes/counts only.",
    })
    return candidates


def _write_markdown(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# CowWeCom Project Optimizer Report",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Project root: `{report['project_root']}`",
        f"- Workspace: `{report['workspace']}`",
        f"- Optimizer data dir: `{report['optimizer_data_dir']}`",
        "",
        "## Data Sources",
    ]
    for source in report["data_sources"]:
        lines.append(f"- {source}")
    lines.extend([
        "",
        "## Cache",
        f"- Usage records: {report['cache']['usage_records']}",
        f"- Provider payload evidence: {report['cache']['optimizer_provider_payloads']}",
        f"- Prompt tokens: {report['cache']['prompt_tokens']}",
        f"- Cached tokens: {report['cache']['cached_tokens']}",
        f"- Cache hit rate: {report['cache']['cache_hit_rate']:.2%}",
        f"- Long zero-cache requests: {report['cache']['long_input_zero_cache_requests']}",
        f"- Unstable context payloads: {report['cache']['unstable_context_payloads']}",
        "",
        "## Reasoning Effort",
        f"- Decisions: {report['reasoning']['decisions']}",
        f"- Outcomes: {report['reasoning']['outcomes']}",
        f"- Failure/retry outcomes: {report['reasoning']['failure_or_retry_outcomes']}",
        f"- Efforts: {report['reasoning']['effort_counts']}",
        f"- Top rules: {report['reasoning']['top_rules']}",
        "",
        "## Tools And Temporary Scripts",
        f"- Tool events: {report['tools']['tool_events']}",
        f"- Top tools: {report['tools']['top_tools']}",
        f"- Repeated tool argument shapes: {report['tools']['repeated_argument_shapes']}",
        f"- Temp script snapshots: {report['temp_scripts']['snapshots']}",
        f"- Repeated temp script basenames: {report['temp_scripts']['repeated_basenames']}",
        "",
        "## Candidate Table",
        "| \u5019\u9009\u5de5\u4f5c\u6d41 | \u8bc1\u636e\u6765\u6e90 | \u91cd\u590d\u9891\u7387 | \u5f53\u524d\u5b9e\u73b0\u65b9\u5f0f | \u63a8\u8350\u5904\u7406 | \u7406\u7531 |",
        "|---|---|---:|---|---|---|",
    ])
    for item in report["candidates"]:
        lines.append(
            "| {workflow} | {evidence} | {frequency} | {current} | {recommendation} | {reason} |".format(**{
                key: _escape_table(str(value)) for key, value in item.items()
            })
        )
    incremental = report.get("incremental_calls", {})
    lines.extend([
        "",
        "## Privacy Guard",
        "- Raw user/model input cache is local-only and ignored by Git.",
        "- This report does not include raw user messages, memory text, tool arguments, full payloads, or secrets.",
        f"- Raw cache records before consume: {report['raw_cache']['records']}",
        f"- Raw cache bytes before consume: {report['raw_cache']['bytes']}",
        f"- Raw cache consume result: {report['raw_cache'].get('consume_result', {})}",
        f"- Incremental LLM calls since last Codex optimizer run: {incremental.get('incremental_calls', 0)} / {incremental.get('threshold', 0)}",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def _escape_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def cached_tokens_rate(cached: int, prompt: int) -> float:
    return (cached / prompt) if prompt else 0.0


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def build_report(project_root: Path, workspace: Path, data_dir: Path, threshold: int = 300) -> dict[str, Any]:
    events = _optimizer_event_records(data_dir)
    usage = _usage_records(workspace)
    reasoning = _reasoning_records(workspace)
    scripts = _temp_script_records(data_dir)
    raw_count, raw_bytes, raw_files = _raw_record_count(data_dir)
    cache = _cache_summary(usage, events)
    reasoning_summary = _reasoning_summary(reasoning)
    tool_summary = _tool_summary(events)
    script_summary = _temp_script_summary(scripts)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(project_root),
        "workspace": str(workspace),
        "optimizer_data_dir": str(data_dir),
        "data_sources": [
            f"optimizer sanitized events: {len(events)} records",
            f"optimizer raw input cache: {raw_count} records in {len(raw_files)} files",
            f"llm cache usage telemetry: {len(usage)} records",
            f"reasoning effort audit logs: {len(reasoning)} records",
            f"temporary script manifest: {len(scripts)} records",
        ],
        "cache": cache,
        "reasoning": reasoning_summary,
        "tools": tool_summary,
        "temp_scripts": script_summary,
        "raw_cache": {"records": raw_count, "bytes": raw_bytes, "files": len(raw_files)},
        "incremental_calls": _incremental_call_status(workspace, data_dir, threshold),
        "candidates": _candidate_table(cache, reasoning_summary, tool_summary, script_summary),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a local CowWeCom optimization report.")
    parser.add_argument("--project-root", default=".", help="CowWeCom repository root.")
    parser.add_argument("--workspace", default="", help="Runtime workspace; default comes from config.")
    parser.add_argument("--output", default="", help="Markdown report path.")
    parser.add_argument("--json-out", default="", help="Optional JSON report path.")
    parser.add_argument("--consume-raw", action="store_true", help="Delete raw input cache after report is written.")
    parser.add_argument("--call-threshold", type=int, default=300, help="Incremental model-call threshold for scheduled optimization.")
    parser.add_argument("--mark-optimized", action="store_true", help="Record the current LLM usage count as optimized after a successful report.")
    args = parser.parse_args()

    project_root = Path(args.project_root).expanduser().resolve()
    _import_project(project_root)

    from common.project_optimizer_evidence import consume_raw_input_cache, data_dir
    from common.utils import expand_path
    from config import conf

    workspace = Path(expand_path(args.workspace or conf().get("agent_workspace", "~/cow"))).resolve()
    optimizer_dir = data_dir()
    report = build_report(project_root, workspace, optimizer_dir, threshold=max(1, args.call_threshold))

    output = Path(args.output).expanduser().resolve() if args.output else (
        optimizer_dir / "reports" / f"project-optimizer-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.md"
    )
    _write_markdown(output, report)
    report["report_path"] = str(output)

    if args.json_out:
        json_out = Path(args.json_out).expanduser().resolve()
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.consume_raw and bool(conf().get("project_optimizer_delete_raw_after_run", True)):
        report["raw_cache"]["consume_result"] = consume_raw_input_cache(reason="cowwechat_project_optimizer")
        _write_report_outputs(output, report, args.json_out)

    if args.mark_optimized:
        report["incremental_calls"]["mark_optimized"] = _mark_optimized(workspace, optimizer_dir, output)
        _write_report_outputs(output, report, args.json_out)

    print(str(output))
    return 0


def _write_report_outputs(output: Path, report: Mapping[str, Any], json_out: str) -> None:
    _write_markdown(output, report)
    if json_out:
        Path(json_out).expanduser().resolve().write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    raise SystemExit(main())
