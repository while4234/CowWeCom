import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NODE_SCRIPT = PROJECT_ROOT / "skills" / "codex-quota-query" / "scripts" / "query_codex_openai_quota.mjs"
PY_WRAPPER = PROJECT_ROOT / "skills" / "codex-quota-query" / "scripts" / "check_codex_quota.py"
NODE = shutil.which("node")


class CodexQuotaQuerySkillTest(unittest.TestCase):
    def write_fixture_files(self, tmp: Path):
        account = {
            "account": {
                "email": "person@example.com",
                "planType": "plus",
                "type": "chatgpt",
            }
        }
        limits = {
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "limitName": "Codex",
                    "primary": {
                        "usedPercent": 40.2,
                        "windowDurationMins": 1440,
                        "resetsAt": 1800000000,
                    },
                    "secondary": {
                        "used_percent": 100,
                        "window_duration_mins": 10080,
                        "resets_at": 1800003600,
                    },
                    "rateLimitReachedType": "secondary",
                    "credits": {
                        "balance": 0,
                        "hasCredits": False,
                        "unlimited": False,
                    },
                }
            }
        }
        account_path = tmp / "account.json"
        limits_path = tmp / "limits.json"
        account_path.write_text(json.dumps(account), encoding="utf-8")
        limits_path.write_text(json.dumps(limits), encoding="utf-8")
        return account_path, limits_path

    @unittest.skipUnless(NODE, "node is required")
    def test_node_json_normalizes_rate_limits_and_masks_email(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            account_path, limits_path = self.write_fixture_files(Path(raw_tmp))

            completed = subprocess.run(
                [
                    NODE,
                    str(NODE_SCRIPT),
                    "--format",
                    "json",
                    "--mock-account-file",
                    str(account_path),
                    "--mock-rate-limits-file",
                    str(limits_path),
                ],
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("person@example.com", completed.stdout)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["account"]["email_masked"], "p***@example.com")
        self.assertTrue(payload["summary"]["blocked"])
        self.assertEqual(payload["rate_limits"][0]["limit_name"], "Codex")
        self.assertEqual(payload["rate_limits"][0]["windows"][0]["remaining_percent"], 59.8)
        self.assertEqual(payload["rate_limits"][0]["windows"][1]["remaining_percent"], 0)

    @unittest.skipUnless(NODE, "node is required")
    def test_node_text_output_is_chat_friendly(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            account_path, limits_path = self.write_fixture_files(Path(raw_tmp))

            completed = subprocess.run(
                [
                    NODE,
                    str(NODE_SCRIPT),
                    "--format",
                    "text",
                    "--mock-account-file",
                    str(account_path),
                    "--mock-rate-limits-file",
                    str(limits_path),
                ],
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("GPT/Codex quota", completed.stdout)
        self.assertIn("p***@example.com", completed.stdout)
        self.assertNotIn("person@example.com", completed.stdout)
        self.assertIn("remaining about 59.8%", completed.stdout)
        self.assertIn("blocked", completed.stdout)

    @unittest.skipUnless(NODE, "node is required")
    def test_python_wrapper_delegates_to_node_with_json_output(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            account_path, limits_path = self.write_fixture_files(Path(raw_tmp))

            completed = subprocess.run(
                [
                    sys.executable,
                    str(PY_WRAPPER),
                    "--format",
                    "json",
                    "--project-dir",
                    str(PROJECT_ROOT),
                    "--mock-account-file",
                    str(account_path),
                    "--mock-rate-limits-file",
                    str(limits_path),
                ],
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source"], "openclaw-codex-app-server")


if __name__ == "__main__":
    unittest.main()
