import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PY_WRAPPER = PROJECT_ROOT / "skills" / "codex-quota-query" / "scripts" / "check_codex_quota.py"


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

    def test_mock_json_normalizes_rate_limits_and_masks_email(self):
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
        self.assertNotIn("person@example.com", completed.stdout)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source"], "codex-app-server")
        self.assertEqual(payload["account"]["email_masked"], "p***@example.com")
        self.assertTrue(payload["summary"]["blocked"])
        self.assertEqual(payload["rate_limits"][0]["limit_name"], "Codex")
        self.assertEqual(payload["rate_limits"][0]["windows"][0]["remaining_percent"], 59.8)
        self.assertEqual(payload["rate_limits"][0]["windows"][1]["remaining_percent"], 0)

    def test_mock_text_output_is_chat_friendly(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            account_path, limits_path = self.write_fixture_files(Path(raw_tmp))

            completed = subprocess.run(
                [
                    sys.executable,
                    str(PY_WRAPPER),
                    "--format",
                    "text",
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
        self.assertIn("GPT/Codex quota", completed.stdout)
        self.assertIn("p***@example.com", completed.stdout)
        self.assertNotIn("person@example.com", completed.stdout)
        self.assertIn("remaining about 59.8%", completed.stdout)
        self.assertIn("blocked", completed.stdout)

    def test_direct_app_server_uses_configured_auth_file(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            account_path = tmp / "auth.json"
            account_path.write_text(
                json.dumps({
                    "tokens": {
                        "access_token": _jwt({"exp": 4102444800}),
                        "account_id": "acct-test",
                    }
                }),
                encoding="utf-8",
            )
            config_path = tmp / "config.json"
            config_path.write_text(
                json.dumps({"llm_backend": {"providers": {"codex": {"auth_file": str(account_path)}}}}),
                encoding="utf-8",
            )
            fake_codex = tmp / "fake_codex.py"
            transcript = tmp / "transcript.jsonl"
            fake_codex.write_text(_fake_codex_app_server_source(transcript), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(PY_WRAPPER),
                    "--project-dir",
                    str(tmp),
                    "--codex-command-json",
                    json.dumps([sys.executable, str(fake_codex)]),
                    "--format",
                    "json",
                    "--timeout-ms",
                    "5000",
                ],
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["source"], "codex-app-server")
            self.assertEqual(payload["account"]["email_masked"], "p***@example.com")
            sent = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines() if line.strip()]
            login = next(item for item in sent if item.get("method") == "account/login/start")
            self.assertEqual(login["params"]["type"], "chatgptAuthTokens")
            self.assertEqual(login["params"]["chatgptAccountId"], "acct-test")
            self.assertTrue(login["params"]["accessToken"])


def _fake_codex_app_server_source(transcript: Path) -> str:
    escaped_transcript = str(transcript).replace("\\", "\\\\")
    return textwrap.dedent(
        f"""
        import json
        import sys
        from pathlib import Path

        if sys.argv[1:3] != ["app-server", "--listen"]:
            sys.exit(2)

        transcript = Path(r"{escaped_transcript}")
        with transcript.open("a", encoding="utf-8") as log:
            for line in sys.stdin:
                message = json.loads(line)
                log.write(json.dumps(message) + "\\n")
                log.flush()
                method = message.get("method")
                request_id = message.get("id")
                if method == "initialize":
                    result = {{"codexHome": "fake", "userAgent": "fake"}}
                elif method == "account/login/start":
                    result = {{"type": "chatgptAuthTokens"}}
                elif method == "account/read":
                    result = {{"requiresOpenaiAuth": True, "account": {{"type": "chatgpt", "email": "person@example.com", "planType": "plus"}}}}
                elif method == "account/rateLimits/read":
                    result = {{"rateLimits": {{"limitId": "codex", "limitName": "Codex", "primary": {{"usedPercent": 12, "windowDurationMins": 1440}}}}}}
                else:
                    result = {{}}
                print(json.dumps({{"id": request_id, "result": result}}), flush=True)
        """
    ).strip()


def _jwt(claims: dict) -> str:
    header = _b64({"alg": "none"})
    payload = _b64(claims)
    return f"{header}.{payload}."


def _b64(value: dict) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


if __name__ == "__main__":
    unittest.main()
