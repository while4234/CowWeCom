#!/usr/bin/env python3
"""CLI helper for CowAgent self-evolution records."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _add_project_root_to_path() -> None:
    candidates = [Path.cwd(), Path(__file__).resolve()]
    for start in candidates:
        for path in [start, *start.parents]:
            if (path / "common" / "self_evolution.py").is_file():
                sys.path.insert(0, str(path))
                return


def _load_api():
    _add_project_root_to_path()
    try:
        from common.self_evolution import (
            ensure_seed_rules,
            get_data_dir,
            list_active_rules,
            record_reusable_learning,
            record_windows_shell_failure,
            run_post_task_reflection_once,
        )
        from common.tool_attempt_memory import (
            get_data_dir as get_tool_attempt_data_dir,
            list_active_rules as list_tool_attempt_rules,
        )
    except Exception as e:
        raise SystemExit(
            "Unable to import CowAgent runtime helpers. Run this script from the "
            f"CowWechat project root, or ensure the project root is on PYTHONPATH. Error: {e}"
        )
    return {
        "ensure_seed_rules": ensure_seed_rules,
        "get_data_dir": get_data_dir,
        "list_active_rules": list_active_rules,
        "record_reusable_learning": record_reusable_learning,
        "record_windows_shell_failure": record_windows_shell_failure,
        "run_post_task_reflection_once": run_post_task_reflection_once,
        "get_tool_attempt_data_dir": get_tool_attempt_data_dir,
        "list_tool_attempt_rules": list_tool_attempt_rules,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or update CowAgent self-evolution records.")
    sub = parser.add_subparsers(dest="action", required=True)
    workspace_help = "Optional agent workspace root. Defaults to CowAgent's configured workspace."

    doctor_parser = sub.add_parser("doctor", help="Show storage path and active rule count.")
    doctor_parser.add_argument("--workspace-root", default=None, help=workspace_help)
    list_parser = sub.add_parser("list", help="Print active rules as JSON.")
    list_parser.add_argument("--workspace-root", default=None, help=workspace_help)
    list_parser.add_argument(
        "--source",
        choices=("all", "self", "tools"),
        default="all",
        help="Which rule store to list. Defaults to all.",
    )
    seed_parser = sub.add_parser("seed", help="Persist built-in seed rules.")
    seed_parser.add_argument("--workspace-root", default=None, help=workspace_help)

    log_shell = sub.add_parser("log-shell", help="Record a reusable Windows shell failure.")
    log_shell.add_argument("--workspace-root", default=None, help=workspace_help)
    log_shell.add_argument("--command", dest="shell_command", required=True)
    log_shell.add_argument("--output", default="")
    log_shell.add_argument("--exit-code", type=int, default=None)

    log_learning = sub.add_parser("log-learning", help="Record a reusable manual workflow lesson.")
    log_learning.add_argument("--workspace-root", default=None, help=workspace_help)
    log_learning.add_argument("--id", dest="rule_id", required=True)
    log_learning.add_argument("--summary", required=True)
    log_learning.add_argument("--next", dest="next_action", required=True)
    log_learning.add_argument("--details", default="")

    reflect_task = sub.add_parser(
        "reflect-task",
        help="Run one post-task reflection from assistant process text without a model call.",
    )
    reflect_task.add_argument("--workspace-root", default=None, help=workspace_help)
    reflect_task.add_argument(
        "--process-text",
        action="append",
        default=[],
        help="Assistant process/progress text that preceded tool calls. May be repeated.",
    )
    reflect_task.add_argument(
        "--process-text-file",
        default="",
        help="Optional UTF-8 file containing one or more assistant process statements.",
    )

    args = parser.parse_args()
    api = _load_api()
    workspace_root = args.workspace_root

    if args.action == "doctor":
        self_rules = api["list_active_rules"](workspace_root)
        tool_rules = api["list_tool_attempt_rules"](workspace_root)
        print(json.dumps({
            "data_dir": str(api["get_data_dir"](workspace_root)),
            "active_rules": len(self_rules),
            "tool_attempt_memory": {
                "data_dir": str(api["get_tool_attempt_data_dir"](workspace_root)),
                "active_rules": len(tool_rules),
            },
        }, ensure_ascii=False, indent=2))
        return 0

    if args.action == "list":
        if args.source == "self":
            payload = api["list_active_rules"](workspace_root)
        elif args.source == "tools":
            payload = api["list_tool_attempt_rules"](workspace_root)
        else:
            payload = {
                "cowagent_self_evolution": api["list_active_rules"](workspace_root),
                "tool_attempt_memory": api["list_tool_attempt_rules"](workspace_root),
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.action == "seed":
        print(json.dumps(api["ensure_seed_rules"](workspace_root), ensure_ascii=False, indent=2))
        return 0

    if args.action == "log-shell":
        result = api["record_windows_shell_failure"](
            args.shell_command,
            args.output,
            exit_code=args.exit_code,
            workspace_root=workspace_root,
        )
        print(json.dumps(result or {"recorded": False}, ensure_ascii=False, indent=2))
        return 0

    if args.action == "log-learning":
        result = api["record_reusable_learning"](
            args.rule_id,
            args.summary,
            args.next_action,
            details=args.details,
            workspace_root=workspace_root,
        )
        print(json.dumps(result or {"recorded": False}, ensure_ascii=False, indent=2))
        return 0

    if args.action == "reflect-task":
        texts = list(args.process_text or [])
        if args.process_text_file:
            path = Path(args.process_text_file)
            texts.append(path.read_text(encoding="utf-8"))
        result = api["run_post_task_reflection_once"](
            model_adapter=None,
            intermediate_texts=texts,
            workspace_root=workspace_root,
            reason="cli",
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    parser.error(f"unknown command: {args.action}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
