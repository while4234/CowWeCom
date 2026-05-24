import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def load_find_skills_module():
    script = Path(__file__).resolve().parents[1] / "skills" / "find-skills-skill" / "scripts" / "find_skills.py"
    spec = importlib.util.spec_from_file_location("find_skills_skill_script", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_find_executable_checks_winget_node_install_when_path_is_missing(tmp_path, monkeypatch):
    module = load_find_skills_module()
    node_dir = (
        tmp_path
        / "Microsoft"
        / "WinGet"
        / "Packages"
        / "OpenJS.NodeJS.LTS_Test_8wekyb3d8bbwe"
        / "node-v24.15.0-win-x64"
    )
    node_dir.mkdir(parents=True)
    clawhub = node_dir / "clawhub.cmd"
    clawhub.write_text("@echo off\n", encoding="utf-8")

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)

    assert module.find_executable(("clawhub.cmd", "clawhub")) == str(clawhub)


def test_run_clawhub_falls_back_from_direct_cli_to_npx(monkeypatch):
    module = load_find_skills_module()
    commands = [
        module.CliCommand(label="clawhub", argv=["clawhub", "search", "weather"]),
        module.CliCommand(label="npx clawhub", argv=["npx", "clawhub", "search", "weather"]),
    ]
    monkeypatch.setattr(module, "resolve_clawhub_commands", lambda _query, _sort: commands)

    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if argv[0] == "clawhub":
            return SimpleNamespace(returncode=1, stderr="direct failed", stdout="")
        return SimpleNamespace(returncode=0, stderr="", stdout="quick-weather  Quick Weather  (3.2)")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.run_clawhub("weather", "installs") == "quick-weather  Quick Weather  (3.2)"
    assert calls == [commands[0].argv, commands[1].argv]


def test_run_clawhub_returns_none_when_no_cli_is_available(monkeypatch):
    module = load_find_skills_module()
    monkeypatch.setattr(module, "resolve_clawhub_commands", lambda _query, _sort: [])

    assert module.run_clawhub("weather", "installs") is None
