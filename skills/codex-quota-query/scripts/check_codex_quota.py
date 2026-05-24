#!/usr/bin/env python3
"""Python wrapper for the OpenClaw Codex quota app-server query."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


DEFAULT_TIMEOUT_MS = 45000


def default_skill_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_script_path(project_dir: Path, override: str) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    candidates = [
        project_dir / "scripts" / "query_codex_openai_quota.mjs",
        project_dir / "skills" / "codex-quota-query" / "scripts" / "query_codex_openai_quota.mjs",
        default_skill_dir() / "scripts" / "query_codex_openai_quota.mjs",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve()


def home_path(*parts: str) -> str:
    return str(Path.home().joinpath(*parts))


def first_existing_path(paths: List[str]) -> str:
    for candidate in paths:
        if candidate and Path(candidate).exists():
            return candidate
    return next((candidate for candidate in paths if candidate), "")


def prepare_env() -> Dict[str, str]:
    env = dict(os.environ)
    openclaw_root = env.get("OPENCLAW_QQ_ROOT") or first_existing_path(
        [
            home_path(".openclaw-qq"),
            env.get("OPENCLAW_ROOT", ""),
            home_path(".openclaw"),
        ]
    )
    env.setdefault("OPENCLAW_QQ_ROOT", openclaw_root)
    env.setdefault(
        "OPENCLAW_QQ_CONFIG",
        first_existing_path(
            [
                str(Path(openclaw_root) / "openclaw.json") if openclaw_root else "",
                home_path(".openclaw-qq", "openclaw.json"),
                home_path(".openclaw", "openclaw.json"),
            ]
        ),
    )
    env.setdefault(
        "OPENCLAW_CODEX_AGENT_DIR",
        first_existing_path(
            [
                str(Path(openclaw_root) / "agents" / "qq_openclaw" / "agent") if openclaw_root else "",
                home_path(".openclaw-qq", "agents", "qq_openclaw", "agent"),
                home_path(".openclaw", "agents", "qq_openclaw", "agent"),
                home_path(".openclaw", "agents", "discord_openclaw", "agent"),
                home_path(".openclaw", "agents", "main", "agent"),
            ]
        ),
    )
    env.setdefault(
        "OPENCLAW_CODEX_DIST",
        first_existing_path(
            [
                str(Path(openclaw_root) / "extensions" / "codex" / "dist") if openclaw_root else "",
                home_path(".openclaw-qq", "extensions", "codex", "dist"),
                home_path(".openclaw", "extensions", "codex", "dist"),
            ]
        ),
    )
    env.setdefault("PYTHONUTF8", "1")
    return env


def emit_error(message: str, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False, indent=2))
    else:
        print(message, file=sys.stderr)


def timeout_seconds(timeout_ms: int) -> float:
    return max(60.0, timeout_ms / 1000.0 + 45.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query GPT/Codex quota without opening a browser.")
    parser.add_argument("--project-dir", default=str(default_skill_dir()), help="Skill directory or CowWechat project root")
    parser.add_argument("--script", default="", help="Override the Node query script path")
    parser.add_argument("--node", default="", help="Override the Node executable")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--timeout-ms", type=int, default=None)
    parser.add_argument("--show-account", action="store_true", help="Show the full account label instead of masking it")
    parser.add_argument("--mock-account-file", default="", help=argparse.SUPPRESS)
    parser.add_argument("--mock-rate-limits-file", default="", help=argparse.SUPPRESS)
    parser.add_argument("--now-ms", default="", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    project_dir = Path(args.project_dir).expanduser().resolve()
    script_path = resolve_script_path(project_dir, args.script)
    skill_dir = script_path.parent.parent
    if not script_path.exists():
        emit_error("GPT/Codex quota query script was not found.", args.format)
        return 2

    node = args.node or shutil.which("node")
    if not node:
        emit_error("Node.js was not found; cannot query GPT/Codex quota through OpenClaw.", args.format)
        return 2

    timeout_ms = args.timeout_ms
    if timeout_ms is None:
        raw_timeout = os.environ.get("QQ_OPENCLAW_CODEX_QUOTA_TIMEOUT_MS") or os.environ.get("OPENCLAW_CODEX_QUOTA_TIMEOUT_MS")
        try:
            timeout_ms = int(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_MS
        except ValueError:
            timeout_ms = DEFAULT_TIMEOUT_MS
    timeout_ms = max(1, timeout_ms)

    command = [node, str(script_path), "--format", args.format, "--timeout-ms", str(timeout_ms)]
    if args.show_account:
        command.append("--show-account")
    if args.mock_account_file:
        command.extend(["--mock-account-file", args.mock_account_file])
    if args.mock_rate_limits_file:
        command.extend(["--mock-rate-limits-file", args.mock_rate_limits_file])
    if args.now_ms:
        command.extend(["--now-ms", args.now_ms])

    try:
        completed = subprocess.run(
            command,
            cwd=str(skill_dir),
            env=prepare_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_seconds(timeout_ms),
            check=False,
        )
    except subprocess.TimeoutExpired:
        emit_error(
            f"GPT/Codex quota query timed out after {int(timeout_seconds(timeout_ms))} seconds.",
            args.format,
        )
        return 124

    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0:
        if args.format == "json" and output.startswith("{"):
            print(output)
        else:
            emit_error(output or f"GPT/Codex quota query failed with exit code {completed.returncode}.", args.format)
        return completed.returncode or 1

    print(output or "Query completed, but GPT/Codex quota script returned no content.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
