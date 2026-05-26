#!/usr/bin/env python3
"""Fast local check for CowWeCom optimizer incremental model-call volume."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_FILENAME = "codex_optimizer_state.json"


def _import_project(root: Path) -> None:
    root = root.resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def _expand_workspace(value: str) -> Path:
    if value:
        return Path(os.path.expanduser(value)).resolve()
    return Path(os.path.expanduser(os.getenv("COW_AGENT_WORKSPACE") or "~/cow")).resolve()


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


def _read_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _default_state_path(workspace: Path) -> Path:
    return workspace / "data" / "project-optimizer" / STATE_FILENAME


def build_status(workspace: Path, state_path: Path, threshold: int) -> dict[str, Any]:
    usage_path = workspace / "data" / "llm_cache_usage.jsonl"
    current = _count_lines(usage_path)
    state = _read_state(state_path)
    last = _to_int(state.get("last_optimized_llm_usage_records"))
    incremental = max(0, current - last)
    threshold = max(1, threshold)
    return {
        "due": incremental >= threshold,
        "incremental_calls": incremental,
        "threshold": threshold,
        "current_llm_usage_records": current,
        "last_optimized_llm_usage_records": last,
        "workspace": str(workspace),
        "llm_cache_usage_path": str(usage_path),
        "state_path": str(state_path),
        "last_optimized_at": state.get("updated_at", ""),
        "last_report_path": state.get("last_report_path", ""),
    }


def mark_optimized(state_path: Path, status: dict[str, Any], report_path: str = "") -> dict[str, Any]:
    state = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_optimized_llm_usage_records": status["current_llm_usage_records"],
        "last_report_path": report_path or status.get("last_report_path", ""),
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description="Check whether enough new model calls accumulated for the project optimizer.")
    parser.add_argument("--project-root", default="", help="Optional CowWeCom repo root for config imports.")
    parser.add_argument("--workspace", default="", help="Runtime workspace; default is COW_AGENT_WORKSPACE or ~/cow.")
    parser.add_argument("--threshold", type=int, default=300, help="Model-call threshold since the last optimizer mark.")
    parser.add_argument("--state-file", default="", help="Override optimizer state JSON path.")
    parser.add_argument("--mark-optimized", action="store_true", help="Update the optimized baseline to the current usage count.")
    parser.add_argument("--report-path", default="", help="Report path to store when marking optimized.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()

    if args.project_root:
        _import_project(Path(args.project_root).expanduser())

    workspace = _expand_workspace(args.workspace)
    state_path = Path(args.state_file).expanduser().resolve() if args.state_file else _default_state_path(workspace)
    status = build_status(workspace, state_path, args.threshold)

    if args.mark_optimized:
        status["mark_optimized"] = mark_optimized(state_path, status, args.report_path)
        status = build_status(workspace, state_path, args.threshold) | {"mark_optimized": status["mark_optimized"]}

    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        decision = "due" if status["due"] else "skip"
        print(
            f"{decision}: incremental_calls={status['incremental_calls']} "
            f"threshold={status['threshold']} current={status['current_llm_usage_records']} "
            f"last={status['last_optimized_llm_usage_records']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
