import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent.skills.manager import SkillManager


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "skills" / "safe-github-upload" / "scripts" / "preflight.py"


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


class SafeGithubUploadSkillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run_git(self.root, "init", "-b", "main")
        run_git(self.root, "config", "user.name", "Test User")
        run_git(self.root, "config", "user.email", "test@example.com")
        ignore = "\n".join(
            [
                "config.json",
                ".env",
                ".env.*",
                "*.key",
                "*.pem",
                "credentials*.json",
                "token*.json",
                "cookies*.json",
                "session*.json",
                ".weixin_cow_credentials.json",
                ".codex/",
                ".playwright-mcp/",
                "",
            ]
        )
        (self.root / ".gitignore").write_text(ignore, encoding="utf-8")
        (self.root / "app.py").write_text("print('ok')\n", encoding="utf-8")
        run_git(self.root, "add", ".")
        run_git(self.root, "commit", "-m", "initial")

    def tearDown(self):
        self.tmp.cleanup()

    def run_preflight(self):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root), "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def test_skill_is_discovered(self):
        manager = SkillManager()

        self.assertIn("safe-github-upload", manager.skills)
        self.assertTrue(manager.is_skill_enabled("safe-github-upload"))

    def test_preflight_blocks_staged_protected_file(self):
        (self.root / "config.json").write_text('{"secret":"local"}\n', encoding="utf-8")
        run_git(self.root, "add", "-f", "config.json")

        result = self.run_preflight()

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("config.json", payload["protected_staged"])

    def test_preflight_allows_safe_staged_code(self):
        (self.root / "feature.py").write_text("print('feature')\n", encoding="utf-8")
        run_git(self.root, "add", "feature.py")

        result = self.run_preflight()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("feature.py", payload["staged_files"])

    def test_preflight_allows_staged_deletion_of_protected_runtime_file(self):
        index_dir = self.root / "knowledge_backend" / "indexes"
        index_dir.mkdir(parents=True)
        db_path = index_dir / "kb.sqlite"
        db_path.write_bytes(b"local runtime db")
        run_git(self.root, "add", "knowledge_backend/indexes/kb.sqlite")
        run_git(self.root, "commit", "-m", "track runtime db")
        run_git(self.root, "rm", "knowledge_backend/indexes/kb.sqlite")

        result = self.run_preflight()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("knowledge_backend/indexes/kb.sqlite", payload["staged_files"])
        self.assertNotIn("knowledge_backend/indexes/kb.sqlite", payload["protected_staged"])


if __name__ == "__main__":
    unittest.main()
