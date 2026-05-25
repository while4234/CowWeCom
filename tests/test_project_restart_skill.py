import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from agent.skills.manager import SkillManager


def load_restart_module():
    script = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "project-restart"
        / "scripts"
        / "restart_project.py"
    )
    spec = importlib.util.spec_from_file_location("project_restart_script", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_project_restart_skill_prompt_contains_restart_triggers():
    manager = SkillManager()

    prompt = manager.build_skills_prompt(skill_filter=["project-restart"])

    assert "<name>project-restart</name>" in prompt
    assert "重启项目" in prompt
    assert "重启服务" in prompt
    assert "browser page refresh" in prompt


def test_restart_script_resolves_project_root_from_explicit_path(tmp_path):
    module = load_restart_module()
    (tmp_path / "app.py").write_text("", encoding="utf-8")
    (tmp_path / "config-template.json").write_text("{}", encoding="utf-8")
    nested = tmp_path / "skills" / "project-restart"
    nested.mkdir(parents=True)

    assert module.resolve_project_root(str(nested)) == tmp_path.resolve()


def test_schedule_restart_launches_detached_worker(tmp_path, monkeypatch):
    module = load_restart_module()
    (tmp_path / "app.py").write_text("", encoding="utf-8")
    (tmp_path / "config-template.json").write_text("{}", encoding="utf-8")
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    log_path = tmp_path / "project-restart.log"
    calls = []

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(module.sys, "platform", "win32")

    assert module.schedule_restart(tmp_path, 0.5, log_path) == 0

    command, kwargs = calls[0]
    assert command[:2] == [str(venv_python), str(Path(module.__file__).resolve())]
    assert "--worker" in command
    assert str(tmp_path) in command
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["env"]["PYTHONUTF8"] == "1"
    assert kwargs["creationflags"] != 0
    assert log_path.exists()


def test_run_cow_cli_uses_project_python_and_utf8_env(tmp_path, monkeypatch):
    module = load_restart_module()
    python = tmp_path / "python.exe"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_cow_cli(tmp_path, python, ("restart",))

    assert result.returncode == 0
    command, kwargs = calls[0]
    assert command == [str(python), "-m", "cli.cli", "restart"]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
