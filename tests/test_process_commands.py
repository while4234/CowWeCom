from contextlib import contextmanager

import click
from click.testing import CliRunner

from cli.commands import process


@contextmanager
def _no_lock():
    yield


def test_restart_defaults_to_no_log_tail(monkeypatch):
    tail_calls = []
    monkeypatch.setattr(process, "_process_lock", lambda: _no_lock())
    monkeypatch.setattr(
        process,
        "_restart_service",
        lambda: {"pid": 123, "log_file": "nohup.out", "started": True},
    )
    monkeypatch.setattr(process, "_tail_log", lambda log_file: tail_calls.append(log_file))

    result = CliRunner().invoke(process.restart)

    assert result.exit_code == 0
    assert tail_calls == []


def test_restart_logs_option_tails(monkeypatch):
    tail_calls = []
    monkeypatch.setattr(process, "_process_lock", lambda: _no_lock())
    monkeypatch.setattr(
        process,
        "_restart_service",
        lambda: {"pid": 123, "log_file": "nohup.out", "started": True},
    )
    monkeypatch.setattr(process, "_tail_log", lambda log_file: tail_calls.append(log_file))

    result = CliRunner().invoke(process.restart, ["--logs"])

    assert result.exit_code == 0
    assert tail_calls == ["nohup.out"]


def test_windows_kill_pid_uses_taskkill_tree(monkeypatch):
    calls = []
    monkeypatch.setattr(process, "_IS_WIN", True)
    monkeypatch.setattr(
        process.subprocess,
        "run",
        lambda cmd, **kwargs: calls.append(cmd),
    )

    process._kill_pid(123)
    process._kill_pid(456, force=True)

    assert calls[0] == ["taskkill", "/T", "/PID", "123"]
    assert calls[1] == ["taskkill", "/F", "/T", "/PID", "456"]


def test_stop_keeps_pid_file_when_force_kill_fails(tmp_path, monkeypatch):
    pid_file = tmp_path / ".cow.pid"
    pid_file.write_text("123")
    monkeypatch.setattr(process, "get_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(process, "_find_service_pids", lambda: [123])
    monkeypatch.setattr(process, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(process, "_kill_pid", lambda pid, force=False: None)
    monkeypatch.setattr(process, "_wait_for_pids_exit", lambda pids, timeout: sorted(pids))

    try:
        process._stop_service()
    except click.ClickException as exc:
        assert "did not stop cleanly" in str(exc)
    else:
        raise AssertionError("expected ClickException")

    assert pid_file.read_text() == "123"


def test_stop_removes_pid_file_after_tree_exit(tmp_path, monkeypatch):
    pid_file = tmp_path / ".cow.pid"
    pid_file.write_text("123")
    monkeypatch.setattr(process, "get_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(process, "_find_service_pids", lambda: [123])
    monkeypatch.setattr(process, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(process, "_kill_pid", lambda pid, force=False: None)
    monkeypatch.setattr(process, "_wait_for_pids_exit", lambda pids, timeout: [])

    assert process._stop_service() is True
    assert not pid_file.exists()
