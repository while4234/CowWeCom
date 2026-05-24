import json
import tempfile
import unittest
from pathlib import Path

from common.tool_attempt_memory import (
    FAILURE_NON_RETRYABLE_ARGS,
    FAILURE_SHELL_DIALECT,
    FAILURE_TRANSIENT,
    ToolAttemptMemory,
    classify_tool_failure,
    get_active_prompt_guidance,
    list_active_rules,
)


class TestToolAttemptMemory(unittest.TestCase):
    def test_records_non_retryable_without_raw_args_or_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = ToolAttemptMemory(tmpdir)
            secret_path = r"C:\Users\RondleLiu\secret-token.txt"

            record = memory.record_attempt(
                "read",
                {"path": secret_path, "limit": 10},
                "error",
                f"File not found: {secret_path}",
            )

            self.assertEqual(record["failure_class"], FAILURE_NON_RETRYABLE_ARGS)
            data_text = (Path(tmpdir) / "data" / "tool-attempt-memory" / "attempts.jsonl").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(secret_path, data_text)
            self.assertNotIn("secret-token", data_text)
            self.assertIn('"args_hash"', data_text)
            self.assertIn('"result_hash"', data_text)

    def test_persistent_skip_after_three_non_retryable_failures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = ToolAttemptMemory(tmpdir)
            args = {"path": r"D:\missing.md", "offset": 999999}
            for _ in range(3):
                memory.record_attempt("read", args, "error", "Offset beyond end of file")

            decision = memory.should_skip("read", {"offset": 999999, "path": r"D:\missing.md"})

            self.assertTrue(decision.should_skip)
            self.assertEqual(decision.failure_class, FAILURE_NON_RETRYABLE_ARGS)
            self.assertEqual(decision.count, 3)

    def test_transient_failure_never_persistent_skip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = ToolAttemptMemory(tmpdir)
            args = {"url": "https://example.invalid"}
            for _ in range(3):
                memory.record_attempt("browser", args, "error", "SSL connection error: timed out")

            decision = memory.should_skip("browser", args)

            self.assertFalse(decision.should_skip)
            self.assertEqual(
                classify_tool_failure("browser", args, "error", "SSL connection error"),
                FAILURE_TRANSIENT,
            )

    def test_existing_read_path_is_not_short_circuited(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = ToolAttemptMemory(tmpdir)
            path = Path(tmpdir) / "later-created.md"
            args = {"path": str(path)}
            for _ in range(3):
                memory.record_attempt("read", args, "error", "File not found")

            path.write_text("now available", encoding="utf-8")
            decision = memory.should_skip("read", args)

            self.assertFalse(decision.should_skip)

    def test_bash_shell_dialect_classified(self):
        failure = classify_tool_failure(
            "bash",
            {"command": "grep -R token ."},
            "error",
            "'grep' is not recognized as an internal or external command",
        )

        self.assertEqual(failure, FAILURE_SHELL_DIALECT)

    def test_active_rules_do_not_store_raw_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = ToolAttemptMemory(tmpdir)
            args = {"path": r"D:\private\api-key.txt"}
            for _ in range(3):
                memory.record_attempt("read", args, "error", "File not found")

            rules_path = Path(tmpdir) / "data" / "tool-attempt-memory" / "active_rules.json"
            payload = json.loads(rules_path.read_text(encoding="utf-8"))
            serialized = json.dumps(payload, ensure_ascii=False)

            self.assertNotIn("api-key", serialized)
            self.assertNotIn(r"D:\private", serialized)

    def test_policy_shape_skip_for_repeated_unsupported_action_across_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = ToolAttemptMemory(tmpdir)
            for index in range(3):
                memory.record_attempt(
                    "scheduler",
                    {"action": "teleport", "task_id": f"task-{index}"},
                    "error",
                    "Unknown action: teleport",
                )

            decision = memory.should_skip(
                "scheduler",
                {"action": "teleport", "task_id": "task-new"},
            )

            self.assertTrue(decision.should_skip)
            self.assertEqual(decision.failure_class, FAILURE_NON_RETRYABLE_ARGS)
            self.assertIn("rule=policy_shape", decision.reason)

    def test_policy_shape_does_not_skip_value_specific_missing_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = ToolAttemptMemory(tmpdir)
            for index in range(3):
                memory.record_attempt(
                    "read",
                    {"path": fr"D:\missing-{index}.md"},
                    "error",
                    "File not found",
                )

            decision = memory.should_skip("read", {"path": r"D:\different.md"})

            self.assertFalse(decision.should_skip)

    def test_lists_compact_active_rules_for_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = ToolAttemptMemory(tmpdir)
            for index in range(3):
                memory.record_attempt(
                    "scheduler",
                    {"action": "teleport", "task_id": f"task-{index}"},
                    "error",
                    "Unknown action: teleport",
                )

            rules = list_active_rules(tmpdir)

            self.assertGreaterEqual(len(rules), 2)
            self.assertTrue(any(rule.get("rule_type") == "policy_shape" for rule in rules))

    def test_tool_prompt_guidance_is_stable_and_actionable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = ToolAttemptMemory(tmpdir)
            for index in range(3):
                memory.record_attempt(
                    "scheduler",
                    {"action": "teleport", "task_id": f"task-{index}"},
                    "error",
                    "Unknown action: teleport",
                )

            guidance = get_active_prompt_guidance(workspace_root=tmpdir)

            self.assertEqual(len(guidance), 1)
            self.assertIn("scheduler", guidance[0])
            self.assertIn("action=teleport", guidance[0])
            self.assertIn("unknown_action", guidance[0])


if __name__ == "__main__":
    unittest.main()
