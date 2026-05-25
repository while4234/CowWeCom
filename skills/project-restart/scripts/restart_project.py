#!/usr/bin/env python3
"""Restart the current CowWechat/CowAgent project service."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


PROJECT_MARKERS = ("app.py", "config-template.json")
DEFAULT_DELAY_SECONDS = 2.0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stop all current-project CowAgent processes and start one fresh service."
    )
    parser.add_argument(
        "--root",
        help="CowWechat project root. Defaults to COW_PROJECT_ROOT, cwd, or an ancestor with app.py.",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Run the restart in this process instead of scheduling a detached worker.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help="Seconds for the detached worker to wait before restarting.",
    )
    parser.add_argument(
        "--log",
        help="Worker log path. Defaults to <project-root>/project-restart.log.",
    )
    args = parser.parse_args()

    root = resolve_project_root(args.root)
    log_path = Path(args.log).expanduser().resolve() if args.log else root / "project-restart.log"

    if args.worker or args.run_now:
        return restart_project(root, max(args.delay, 0.0), log_path)

    return schedule_restart(root, max(args.delay, 0.0), log_path)


def resolve_project_root(explicit_root: Optional[str]) -> Path:
    candidates = []
    if explicit_root:
        candidates.append(Path(explicit_root).expanduser())

    env_root = os.environ.get("COW_PROJECT_ROOT")
    if env_root:
        candidates.append(Path(env_root).expanduser())

    candidates.extend([Path.cwd(), Path(__file__).resolve()])

    for candidate in candidates:
        root = find_project_root(candidate)
        if root:
            return root

    raise SystemExit(
        "Could not find a CowWechat project root. Pass --root <path> from the project checkout."
    )


def find_project_root(start: Path) -> Optional[Path]:
    current = start if start.is_dir() else start.parent
    for path in (current, *current.parents):
        if all((path / marker).exists() for marker in PROJECT_MARKERS):
            return path.resolve()
    return None


def choose_project_python(root: Path) -> Path:
    if sys.platform == "win32":
        venv_python = root / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = root / ".venv" / "bin" / "python"
    return venv_python if venv_python.exists() else Path(sys.executable)


def schedule_restart(root: Path, delay: float, log_path: Path) -> int:
    python = choose_project_python(root)
    command = [
        str(python),
        str(Path(__file__).resolve()),
        "--worker",
        "--root",
        str(root),
        "--delay",
        str(delay),
        "--log",
        str(log_path),
    ]

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"\n[{timestamp()}] scheduling restart worker\n")
        popen_kwargs = {
            "cwd": str(root),
            "stdin": subprocess.DEVNULL,
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
            "env": command_environment(),
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
        else:
            popen_kwargs["start_new_session"] = True
        subprocess.Popen(command, **popen_kwargs)

    print(f"Restart scheduled for {root}. Worker log: {log_path}")
    return 0


def restart_project(root: Path, delay: float, log_path: Path) -> int:
    if delay > 0:
        time.sleep(delay)

    python = choose_project_python(root)
    write_log(log_path, f"restart begin: root={root}, python={python}")

    restart = run_cow_cli(root, python, ("restart",))
    write_log(log_path, restart.stdout)
    if restart.stderr:
        write_log(log_path, restart.stderr)

    status = run_cow_cli(root, python, ("status",))
    write_log(log_path, status.stdout)
    if status.stderr:
        write_log(log_path, status.stderr)

    if restart.returncode != 0:
        write_log(log_path, f"restart failed with exit code {restart.returncode}")
        return restart.returncode
    if status.returncode != 0:
        write_log(log_path, f"status failed with exit code {status.returncode}")
        return status.returncode

    write_log(log_path, "restart complete")
    return 0


def run_cow_cli(root: Path, python: Path, args: Iterable[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(python), "-m", "cli.cli", *args],
        cwd=str(root),
        env=command_environment(),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )


def command_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def write_log(log_path: Path, message: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        for line in (message or "").splitlines() or [""]:
            log_file.write(f"[{timestamp()}] {line}\n")


def timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


if __name__ == "__main__":
    raise SystemExit(main())
