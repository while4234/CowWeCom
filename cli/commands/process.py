"""cow start/stop/restart/status/logs - Process management commands."""

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import Optional

import click

from cli.utils import get_project_root

_IS_WIN = sys.platform == "win32"


def _get_pid_file():
    return os.path.join(get_project_root(), ".cow.pid")


def _get_log_file():
    return os.path.join(get_project_root(), "nohup.out")


def _get_lock_file():
    return os.path.join(get_project_root(), ".cow.process.lock")


def _get_app_file():
    return os.path.join(get_project_root(), "app.py")


def _normalize_command_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).replace("\\", "/")


def _command_mentions_path(command_line: str, path: str) -> bool:
    normalized_command = os.path.normcase(command_line).replace("\\", "/")
    return _normalize_command_path(path) in normalized_command


def _is_pid_alive(pid: int) -> bool:
    """Check whether a process is still running."""
    if _IS_WIN:
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                stderr=subprocess.DEVNULL,
            )
            return str(pid) in out.decode(errors="ignore")
        except Exception:
            return False

    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _kill_pid(pid: int, force: bool = False):
    """Terminate a process by PID."""
    if _IS_WIN:
        cmd = ["taskkill"]
        if force:
            cmd.append("/F")
        cmd.extend(["/T", "/PID", str(pid)])
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return

    import signal

    sig = signal.SIGKILL if force else signal.SIGTERM
    os.kill(pid, sig)


def _read_pid(validate_service: bool = False) -> Optional[int]:
    pid_file = _get_pid_file()
    if not os.path.exists(pid_file):
        return None
    try:
        with open(pid_file, "r") as f:
            pid = int(f.read().strip())
        if _is_pid_alive(pid) and (
            not validate_service or pid in _find_service_pids()
        ):
            return pid
        os.remove(pid_file)
        return None
    except (ValueError, OSError):
        try:
            os.remove(pid_file)
        except OSError:
            pass
        return None


def _write_pid(pid: int):
    with open(_get_pid_file(), "w") as f:
        f.write(str(pid))


def _remove_pid():
    pid_file = _get_pid_file()
    if os.path.exists(pid_file):
        os.remove(pid_file)


def _iter_python_processes():
    if _IS_WIN:
        script = (
            "$items = Get-CimInstance Win32_Process "
            "-Filter \"Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'py.exe'\" "
            "| Select-Object ProcessId,CommandLine; "
            "$items | ConvertTo-Json -Compress"
        )
        try:
            output = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", script],
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception:
            return []

        output = output.strip()
        if not output:
            return []
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return []
        if isinstance(data, dict):
            data = [data]
        return [
            (int(item.get("ProcessId")), item.get("CommandLine") or "")
            for item in data
            if item.get("ProcessId")
        ]

    try:
        output = subprocess.check_output(
            ["ps", "-eo", "pid=,args="],
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        return []

    processes = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command_line = stripped.partition(" ")
        try:
            processes.append((int(pid_text), command_line))
        except ValueError:
            continue
    return processes


def _find_service_pids():
    app_py = _get_app_file()
    current_pid = os.getpid()
    pids = []
    for pid, command_line in _iter_python_processes():
        if pid == current_pid:
            continue
        if command_line and _command_mentions_path(command_line, app_py):
            pids.append(pid)
    return sorted(set(pids))


def _recover_running_service_pid() -> Optional[int]:
    pids = _find_service_pids()
    if not pids:
        return None
    _write_pid(pids[0])
    return pids[0]


def _wait_for_pids_exit(pids, timeout: float):
    deadline = time.time() + timeout
    remaining = set(pids)
    while remaining and time.time() < deadline:
        remaining = {pid for pid in remaining if _is_pid_alive(pid)}
        if remaining:
            time.sleep(0.1)
    return sorted(remaining)


def _wait_for_pid_alive(pid: int, timeout: float):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def _lock_is_stale(lock_file: str, stale_after_seconds: float = 120.0) -> bool:
    try:
        return time.time() - os.path.getmtime(lock_file) > stale_after_seconds
    except OSError:
        return True


@contextmanager
def _process_lock(timeout: float = 30.0):
    lock_file = _get_lock_file()
    deadline = time.time() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
        except FileExistsError:
            if _lock_is_stale(lock_file):
                try:
                    os.remove(lock_file)
                except OSError:
                    pass
                continue
            if time.time() >= deadline:
                raise click.ClickException(
                    "Another cow process command is still running. "
                    "Wait a moment and try again."
                )
            time.sleep(0.2)

    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.remove(lock_file)
        except OSError:
            pass


def _start_service(foreground: bool = False):
    pid = _read_pid(validate_service=True) or _recover_running_service_pid()
    if pid:
        click.echo(f"CowAgent is already running (PID: {pid}).")
        return {"pid": pid, "log_file": _get_log_file(), "started": False}

    root = get_project_root()
    app_py = _get_app_file()
    if not os.path.exists(app_py):
        raise click.ClickException("app.py not found in project root.")

    python = sys.executable

    if foreground:
        click.echo("Starting CowAgent in foreground...")
        if _IS_WIN:
            sys.exit(subprocess.call([python, app_py], cwd=root))
        os.execv(python, [python, app_py])

    log_file = _get_log_file()
    click.echo("Starting CowAgent...")

    popen_kwargs = dict(cwd=root)
    if _IS_WIN:
        CREATE_NO_WINDOW = 0x08000000
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        )
    else:
        popen_kwargs["start_new_session"] = True

    with open(log_file, "a") as log:
        proc = subprocess.Popen(
            [python, app_py],
            stdout=log,
            stderr=log,
            **popen_kwargs,
        )

    if not _wait_for_pid_alive(proc.pid, timeout=5.0):
        raise click.ClickException("CowAgent did not stay running after start.")

    _write_pid(proc.pid)
    click.echo(click.style(f"OK CowAgent started (PID: {proc.pid})", fg="green"))
    click.echo(f"  Logs: {log_file}")
    return {"pid": proc.pid, "log_file": log_file, "started": True}


def _stop_service():
    pid = _read_pid(validate_service=True)
    pids = []
    if pid:
        pids.append(pid)
    pids.extend(_find_service_pids())
    pids = sorted({candidate for candidate in pids if candidate != os.getpid()})

    if not pids:
        _remove_pid()
        click.echo("CowAgent is not running.")
        return False

    click.echo(f"Stopping CowAgent (PID: {', '.join(str(item) for item in pids)})...")
    for candidate in pids:
        try:
            _kill_pid(candidate)
        except (ProcessLookupError, OSError):
            pass

    remaining = _wait_for_pids_exit(pids, timeout=5.0)
    for candidate in remaining:
        try:
            _kill_pid(candidate, force=True)
        except (ProcessLookupError, OSError):
            pass

    remaining = _wait_for_pids_exit(remaining, timeout=3.0)
    if remaining:
        raise click.ClickException(
            "CowAgent did not stop cleanly: "
            + ", ".join(str(item) for item in remaining)
        )

    _remove_pid()
    click.echo(click.style("OK CowAgent stopped.", fg="green"))
    return True


def _restart_service():
    _stop_service()
    time.sleep(1)
    return _start_service(foreground=False)


@click.command()
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground (don't daemonize)")
@click.option("--no-logs", is_flag=True, help="Don't tail logs after starting")
def start(foreground, no_logs):
    """Start CowAgent."""
    with _process_lock():
        result = _start_service(foreground=foreground)

    if not no_logs:
        click.echo("  Press Ctrl+C to stop tailing logs.\n")
        _tail_log(result["log_file"])


@click.command()
def stop():
    """Stop CowAgent."""
    with _process_lock():
        _stop_service()


@click.command()
@click.option("--logs", "tail_logs", is_flag=True, help="Tail logs after restarting")
@click.option(
    "--no-logs",
    is_flag=True,
    help="Deprecated compatibility flag; restart exits without logs by default",
)
def restart(tail_logs, no_logs):
    """Restart CowAgent."""
    with _process_lock():
        result = _restart_service()

    if tail_logs and not no_logs:
        click.echo("  Press Ctrl+C to stop tailing logs.\n")
        _tail_log(result["log_file"])


@click.command()
@click.pass_context
def update(ctx):
    """Update CowAgent and restart."""
    root = get_project_root()

    ctx.invoke(stop)

    if os.path.isdir(os.path.join(root, ".git")):
        click.echo("Pulling latest code...")
        ret = subprocess.call(["git", "pull"], cwd=root)
        if ret != 0:
            click.echo("Error: git pull failed.", err=True)
            sys.exit(1)
    else:
        click.echo("Not a git repository, skipping code update.")

    python = sys.executable
    req_file = os.path.join(root, "requirements.txt")

    if _IS_WIN:
        bat = os.path.join(root, "_cow_update.bat")
        lines = [
            "@echo off",
            "chcp 65001 >nul",
            "echo Waiting for cow.exe to exit...",
            "timeout /t 3 /nobreak >nul",
        ]
        if os.path.exists(req_file):
            lines.append("echo Installing dependencies...")
            lines.append(f'"{python}" -m pip install -r requirements.txt -q')
        lines += [
            "echo Reinstalling cow CLI...",
            f'"{python}" -m pip install -e . -q',
            "echo Starting CowAgent...",
            f'"{python}" -m cli.cli start --no-logs',
            "echo.",
            "echo Update complete. You can close this window.",
            "pause >nul",
            'del "%~f0"',
        ]
        with open(bat, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        subprocess.Popen(
            ["cmd.exe", "/c", "start", "CowAgent Update", "/wait", bat],
            cwd=root,
        )
        click.echo(
            click.style(
                "OK Update script launched. Please follow the new window for progress.",
                fg="green",
            )
        )
        return

    if os.path.exists(req_file):
        click.echo("Installing dependencies...")
        subprocess.call(
            [python, "-m", "pip", "install", "-r", "requirements.txt", "-q"],
            cwd=root,
        )
    click.echo("Reinstalling cow CLI...")
    subprocess.call(
        [python, "-m", "pip", "install", "-e", ".", "-q"],
        cwd=root,
    )

    click.echo("")
    time.sleep(1)
    ctx.invoke(start, no_logs=True)


@click.command()
def status():
    """Show CowAgent running status."""
    from cli import __version__
    from cli.utils import load_config_json

    pid = _read_pid(validate_service=True) or _recover_running_service_pid()
    if pid:
        click.echo(click.style(f"CowAgent is running (PID: {pid})", fg="green"))
    else:
        click.echo(click.style("CowAgent is not running", fg="red"))

    click.echo(f"  Version: v{__version__}")

    cfg = load_config_json()
    if cfg:
        channel = cfg.get("channel_type", "unknown")
        if isinstance(channel, list):
            channel = ", ".join(channel)
        click.echo(f"  Channel: {channel}")
        click.echo(f"  Model: {cfg.get('model', 'unknown')}")
        mode = "Chat" if cfg.get("agent") is False else "Agent"
        click.echo(f"  Mode: {mode}")


@click.command()
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
@click.option("--lines", "-n", default=50, help="Number of lines to show")
def logs(follow, lines):
    """View CowAgent logs."""
    log_file = _get_log_file()
    if not os.path.exists(log_file):
        click.echo("No log file found.")
        return

    if follow:
        _tail_log(log_file, lines)
    else:
        _print_last_lines(log_file, lines)


def _print_last_lines(file_path: str, n: int = 50):
    """Print the last N lines of a file."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        for line in all_lines[-n:]:
            click.echo(line, nl=False)
    except Exception as e:
        click.echo(f"Error reading log file: {e}", err=True)


def _tail_log(log_file: str, lines: int = 50):
    """Follow log file output. Blocks until Ctrl+C."""
    _print_last_lines(log_file, lines)

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    click.echo(line, nl=False)
                else:
                    time.sleep(0.3)
    except KeyboardInterrupt:
        pass
