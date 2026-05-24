import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.tools.bash.bash import Bash


class TestBashTool(unittest.TestCase):
    def test_normalize_subprocess_env_skips_none_and_stringifies_values(self):
        env = Bash._normalize_subprocess_env({
            "TEXT": "value",
            "NUMBER": 3,
            "NONE": None,
            None: "ignored",
        })

        self.assertEqual(env["TEXT"], "value")
        self.assertEqual(env["NUMBER"], "3")
        self.assertNotIn("NONE", env)
        self.assertNotIn(None, env)

    def test_prepend_current_python_dir_adds_interpreter_directory(self):
        env = {"PATH": r"C:\Windows\System32"}

        with patch("agent.tools.bash.bash.sys.executable", r"D:\CowAgent\.venv312\Scripts\python.exe"):
            Bash._prepend_current_python_dir(env)

        self.assertTrue(env["PATH"].startswith(r"D:\CowAgent\.venv312\Scripts"))

    def test_windows_guard_rewrites_unquoted_cmd_set_before_execution(self):
        fake_run = SimpleNamespace(returncode=0, stdout="ok", stderr="")
        commands = []

        def capture_run(command, **kwargs):
            commands.append(command)
            return fake_run

        with patch.object(Bash, "_IS_WIN", True), \
                patch("agent.tools.bash.bash.expand_path", return_value=str(Path("missing.env"))), \
                patch("agent.tools.bash.bash.subprocess.run", side_effect=capture_run), \
                patch.object(Bash, "_record_policy_application") as record_policy:
            result = Bash({"cwd": "."}).execute({"command": "set PYTHONUTF8=1 && python -V"})

        self.assertEqual(result.status, "success")
        self.assertIn('set "PYTHONUTF8=1" && python -V', commands[0])
        record_policy.assert_called_once()
        self.assertEqual(record_policy.call_args.args[0], "windows-cmd-env-set-quoting")

    def test_windows_guard_blocks_multiline_python_c_before_execution(self):
        with patch.object(Bash, "_IS_WIN", True), \
                patch("agent.tools.bash.bash.subprocess.run") as run, \
                patch.object(Bash, "_record_reusable_failure") as record_failure:
            result = Bash({"cwd": "."}).execute({"command": 'python -c "print(1)\nprint(2)"'})

        self.assertEqual(result.status, "error")
        self.assertTrue(result.result["details"]["self_evolution_guard"])
        self.assertIn("temporary .py", result.result["output"])
        run.assert_not_called()
        record_failure.assert_called_once()


if __name__ == "__main__":
    unittest.main()
