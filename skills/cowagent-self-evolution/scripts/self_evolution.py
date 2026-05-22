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
            record_windows_shell_failure,
        )
    except Exception as e:
        raise SystemExit(
            "Unable to import CowAgent runtime helpers. Run this script from the "
            f"CowWechat project root, or ensure the project root is on PYTHONPATH. Error: {e}"
        )
    return ensure_seed_rules, get_data_dir, list_active_rules, record_windows_shell_failure


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or update CowAgent self-evolution records.")
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("doctor", help="Show storage path and active rule count.")
    sub.add_parser("list", help="Print active rules as JSON.")
    sub.add_parser("seed", help="Persist built-in seed rules.")

    log_shell = sub.add_parser("log-shell", help="Record a reusable Windows shell failure.")
    log_shell.add_argument("--command", dest="shell_command", required=True)
    log_shell.add_argument("--output", default="")
    log_shell.add_argument("--exit-code", type=int, default=None)

    args = parser.parse_args()
    ensure_seed_rules, get_data_dir, list_active_rules, record_windows_shell_failure = _load_api()

    if args.action == "doctor":
        rules = list_active_rules()
        print(json.dumps({
            "data_dir": str(get_data_dir()),
            "active_rules": len(rules),
        }, ensure_ascii=False, indent=2))
        return 0

    if args.action == "list":
        print(json.dumps(list_active_rules(), ensure_ascii=False, indent=2))
        return 0

    if args.action == "seed":
        print(json.dumps(ensure_seed_rules(), ensure_ascii=False, indent=2))
        return 0

    if args.action == "log-shell":
        result = record_windows_shell_failure(
            args.shell_command,
            args.output,
            exit_code=args.exit_code,
        )
        print(json.dumps(result or {"recorded": False}, ensure_ascii=False, indent=2))
        return 0

    parser.error(f"unknown command: {args.action}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
