import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.prompt.builder import build_agent_system_prompt, _build_self_evolution_section
from agent.skills.manager import SkillManager
from agent.tools.bash.bash import Bash
from common.self_evolution import (
    ACTIVE_RULES_FILE,
    DATA_DIR_NAME,
    ERRORS_FILE,
    apply_windows_shell_policies,
    classify_windows_shell_failure,
    get_data_dir,
    get_active_prompt_guidance as get_self_evolution_prompt_guidance,
    record_reusable_learning,
    record_windows_shell_policy_application,
    record_windows_shell_failure,
)
from common.tool_attempt_memory import ToolAttemptMemory


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

    def test_detects_specific_windows_command_failures(self):
        env_set = classify_windows_shell_failure(
            'set PYTHONUTF8=1 && python -c "print(1)"',
            "Fatal Python error: preconfig_init_utf8_mode: invalid PYTHONUTF8 environment variable value",
        )
        self.assertEqual(env_set["id"], "windows-cmd-env-set-quoting")
        self.assertIn('set "PYTHONUTF8=1"', env_set["next_action"])

        python_c = classify_windows_shell_failure(
            'python -c "for item in [1]:\n print(item)"',
            'File "<string>", line 1\nSyntaxError: invalid syntax',
        )
        self.assertEqual(python_c["id"], "windows-python-c-quoting")

        npm_shim = classify_windows_shell_failure(
            "python inspect_clawhub_memes.py",
            "[WinError 2] The system cannot find the file specified: 'clawhub'",
        )
        self.assertEqual(npm_shim["id"], "windows-npm-cmd-shim")

    def test_applies_windows_shell_policies_without_history_reads(self):
        decision = apply_windows_shell_policies("set PYTHONUTF8=1 && python -V")

        self.assertEqual(decision.command, 'set "PYTHONUTF8=1" && python -V')
        self.assertEqual(decision.applied_rule_ids, ("windows-cmd-env-set-quoting",))
        self.assertEqual(decision.block_reason, "")

        blocked = apply_windows_shell_policies('python -c "print(1)\nprint(2)"')
        self.assertIn("temporary .py", blocked.block_reason)
        self.assertIn("windows-python-c-quoting", blocked.applied_rule_ids)

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

    def test_records_policy_application_as_compact_active_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            rule = record_windows_shell_policy_application(
                "windows-cmd-env-set-quoting",
                "set PYTHONUTF8=1 && python -V --token=sk-secret123456",
                "rewrote env assignment",
                workspace_root=tmp,
            )

            self.assertEqual(rule["id"], "windows-cmd-env-set-quoting")
            self.assertEqual(rule["count"], 1)
            self.assertNotIn("sk-secret123456", rule["command_preview"])

    def test_records_manual_reusable_learning(self):
        with tempfile.TemporaryDirectory() as tmp:
            rule = record_reusable_learning(
                "clawhub-inspect-file-staging",
                "ClawHub install may not stage files in temp dirs",
                "When localizing ClawHub skills, verify install output and fall back to inspect --file into a staging directory before scanning.",
                details="Do not store sk-secret123456 in memory.",
                workspace_root=tmp,
            )

            self.assertEqual(rule["id"], "clawhub-inspect-file-staging")
            self.assertIn("inspect --file", rule["next_action"])
            self.assertNotIn("sk-secret123456", rule["details_preview"])

            active = json.loads((get_data_dir(tmp) / ACTIVE_RULES_FILE).read_text(encoding="utf-8"))
            self.assertEqual(active["rules"][0]["id"], "clawhub-inspect-file-staging")


class SelfEvolutionPromptCacheTest(unittest.TestCase):
    def test_system_prompt_section_is_static(self):
        with patch(
            "common.self_evolution.get_active_prompt_guidance",
            return_value=["Use Windows-compatible commands."],
        ) as guidance:
            section = "\n".join(_build_self_evolution_section("zh"))

        guidance.assert_not_called()
        self.assertIn("Background execution policy", section)
        self.assertNotIn("Use Windows-compatible commands.", section)
        self.assertIn("system prompt stays cacheable", section)

    def test_full_system_prompt_does_not_read_dynamic_guidance(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
            "common.self_evolution.get_active_prompt_guidance",
            return_value=["Use Windows-compatible commands."],
        ) as guidance:
            prompt = build_agent_system_prompt(workspace_dir=tmp)

        guidance.assert_not_called()
        self.assertIn("Background execution policy", prompt)
        self.assertNotIn("Use Windows-compatible commands.", prompt)

    def test_dynamic_guidance_is_stable_and_metadata_free(self):
        rules = [
            {
                "id": "z-rule",
                "count": 12,
                "last_seen": "2026-05-24T10:00:00Z",
                "next_action": "Use z-safe command shape.",
            },
            {
                "id": "a-rule",
                "count": 1,
                "last_seen": "2026-05-24T11:00:00Z",
                "next_action": "Use a-safe command shape.",
            },
        ]

        with patch("common.self_evolution.list_active_rules", return_value=rules), patch(
            "common.tool_attempt_memory.get_active_prompt_guidance",
            return_value=["Tool policy: use supported scheduler actions."],
        ):
            guidance = get_self_evolution_prompt_guidance(limit=3)

        self.assertEqual(
            guidance,
            [
                "Use a-safe command shape.",
                "Use z-safe command shape.",
                "Tool policy: use supported scheduler actions.",
            ],
        )
        joined = "\n".join(guidance)
        self.assertNotIn("count", joined)
        self.assertNotIn("last_seen", joined)


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

    def test_skill_cli_lists_tool_attempt_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = ToolAttemptMemory(tmp)
            for index in range(3):
                memory.record_attempt(
                    "scheduler",
                    {"action": "teleport", "task_id": f"task-{index}"},
                    "error",
                    "Unknown action: teleport",
                )

            script = Path("skills") / "cowagent-self-evolution" / "scripts" / "self_evolution.py"
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "list",
                    "--source",
                    "tools",
                    "--workspace-root",
                    tmp,
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

            payload = json.loads(result.stdout)
            self.assertTrue(any(rule.get("rule_type") == "policy_shape" for rule in payload))

    def test_skill_cli_logs_manual_learning(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path("skills") / "cowagent-self-evolution" / "scripts" / "self_evolution.py"
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "log-learning",
                    "--workspace-root",
                    tmp,
                    "--id",
                    "clawhub-inspect-file-staging",
                    "--summary",
                    "ClawHub install may not stage files in temp dirs",
                    "--next",
                    "Use inspect --file into a staging directory before scanning.",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

            payload = json.loads(result.stdout[result.stdout.find("{"):])
            self.assertEqual(payload["id"], "clawhub-inspect-file-staging")
            self.assertIn("inspect --file", payload["next_action"])


if __name__ == "__main__":
    unittest.main()
