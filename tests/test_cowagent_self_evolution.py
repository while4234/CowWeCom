import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.prompt.builder import _build_self_evolution_section
from agent.skills.manager import SkillManager
from agent.tools.bash.bash import Bash
from common.self_evolution import (
    ACTIVE_RULES_FILE,
    DATA_DIR_NAME,
    ERRORS_FILE,
    classify_windows_shell_failure,
    get_data_dir,
    record_windows_shell_failure,
)


class SelfEvolutionDetectionTest(unittest.TestCase):
    def test_detects_bash_heredoc_and_unix_commands(self):
        heredoc = classify_windows_shell_failure(
            "python - <<'PY'\nprint('hi')\nPY",
            "<< was unexpected at this time.",
        )
        self.assertIsNotNone(heredoc)
        self.assertEqual(heredoc["id"], "windows-shell-dialect")

        grep = classify_windows_shell_failure(
            "grep -R token .",
            "'grep' is not recognized as an internal or external command",
        )
        self.assertIsNotNone(grep)
        self.assertIn("cmd.exe", grep["next_action"])

    def test_record_deduplicates_counts_and_redacts_previews(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = "grep token . --api-key=sk-secret123456"
            output = "Bearer abcdefghijklmnopqrstuvwxyz failed"

            first = record_windows_shell_failure(command, output, exit_code=1, workspace_root=tmp)
            second = record_windows_shell_failure(command, output, exit_code=1, workspace_root=tmp)

            self.assertEqual(second["count"], 2)
            self.assertNotIn("sk-secret123456", second["command_preview"])
            self.assertNotIn("abcdefghijklmnopqrstuvwxyz", second["output_preview"])

            data_dir = get_data_dir(tmp)
            self.assertEqual(data_dir, Path(tmp) / "data" / DATA_DIR_NAME)
            events = (data_dir / ERRORS_FILE).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(events), 2)

            active = json.loads((data_dir / ACTIVE_RULES_FILE).read_text(encoding="utf-8"))
            self.assertEqual(active["rules"][0]["id"], "windows-shell-dialect")
            self.assertEqual(first["id"], second["id"])


class SelfEvolutionPromptTest(unittest.TestCase):
    def test_prompt_guidance_is_hidden_and_compact(self):
        with patch(
            "common.self_evolution.get_active_prompt_guidance",
            return_value=["Use Windows-compatible commands."],
        ):
            section = "\n".join(_build_self_evolution_section("zh"))

        self.assertIn("后台经验规则", section)
        self.assertIn("Use Windows-compatible commands.", section)
        self.assertIn("不要主动告诉用户", section)


class BashSelfEvolutionHookTest(unittest.TestCase):
    def test_failed_windows_bash_records_side_channel_without_changing_result(self):
        fake_run = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="'grep' is not recognized as an internal or external command",
        )

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(Bash, "_IS_WIN", True), \
                patch("agent.tools.bash.bash.expand_path", return_value=str(Path(tmp) / ".env")), \
                patch("agent.tools.bash.bash.subprocess.run", return_value=fake_run), \
                patch("common.self_evolution.record_windows_shell_failure") as record:
            result = Bash({"cwd": tmp}).execute({"command": "grep token ."})

        self.assertEqual(result.status, "error")
        self.assertEqual(result.result["exit_code"], 1)
        record.assert_called_once()
        self.assertEqual(record.call_args.args[0], "grep token .")


class CowAgentSelfEvolutionSkillTest(unittest.TestCase):
    def test_builtin_skill_is_discovered_and_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SkillManager(custom_dir=str(Path(tmp) / "skills"))

            entry = manager.get_skill("cowagent-self-evolution")
            self.assertIsNotNone(entry)
            self.assertEqual(entry.skill.source, "builtin")
            self.assertTrue(manager.is_skill_enabled("cowagent-self-evolution"))

    def test_builtin_skill_appears_in_filtered_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SkillManager(custom_dir=str(Path(tmp) / "skills"))
            prompt = manager.build_skills_prompt(skill_filter=["cowagent-self-evolution"])

        self.assertIn("<name>cowagent-self-evolution</name>", prompt)
        self.assertIn("SKILL.md", prompt)


if __name__ == "__main__":
    unittest.main()
