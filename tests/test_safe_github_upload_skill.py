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
                "/memory/",
                "data/project-optimizer/",
                "data/grok-real-mode-assets/",
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

    def test_preflight_allows_source_directory_named_memory(self):
        source_dir = self.root / "agent" / "tools" / "memory"
        source_dir.mkdir(parents=True)
        (source_dir / "memory_get.py").write_text("print('source')\n", encoding="utf-8")
        run_git(self.root, "add", "agent/tools/memory/memory_get.py")

        result = self.run_preflight()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("agent/tools/memory/memory_get.py", payload["staged_files"])

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

    def test_preflight_allows_protocol_knowledge_artifacts(self):
        index_dir = self.root / "public_protocol_knowledge" / "indexes"
        originals_dir = self.root / "public_protocol_knowledge" / "originals"
        derived_dir = self.root / "public_protocol_knowledge" / "derived" / "axi"
        reports_dir = self.root / "public_protocol_knowledge" / "reports"
        for path in (index_dir, originals_dir, derived_dir, reports_dir):
            path.mkdir(parents=True, exist_ok=True)
        (index_dir / "kb.sqlite").write_bytes(b"portable protocol index")
        (originals_dir / "axi.pdf").write_bytes(b"%PDF protocol spec")
        (derived_dir / "study.md").write_text("# Study\n", encoding="utf-8")
        (reports_dir / "study-report.json").write_text("{}\n", encoding="utf-8")
        run_git(self.root, "add", "public_protocol_knowledge")

        result = self.run_preflight()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertNotIn("public_protocol_knowledge/indexes/kb.sqlite", payload["protected_staged"])
        self.assertNotIn("public_protocol_knowledge/originals/axi.pdf", payload["protected_staged"])
        self.assertNotIn("public_protocol_knowledge/derived/axi/study.md", payload["protected_staged"])
        self.assertNotIn("public_protocol_knowledge/reports/study-report.json", payload["protected_staged"])

    def test_preflight_blocks_personal_knowledge_backend_artifacts(self):
        personal_dir = self.root / "knowledge_backend" / "derived"
        personal_dir.mkdir(parents=True, exist_ok=True)
        (personal_dir / "chat-summary.md").write_text("# Personal\n", encoding="utf-8")
        run_git(self.root, "add", "-f", "knowledge_backend/derived/chat-summary.md")

        result = self.run_preflight()

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("knowledge_backend/derived/chat-summary.md", payload["protected_staged"])

    def test_preflight_blocks_user_memory_and_optimizer_raw_evidence(self):
        memory_dir = self.root / "memory" / "users" / "u1"
        optimizer_dir = self.root / "data" / "project-optimizer" / "raw_model_inputs"
        memory_dir.mkdir(parents=True, exist_ok=True)
        optimizer_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "MEMORY.md").write_text("private memory\n", encoding="utf-8")
        (optimizer_dir / "2026-05-26.jsonl").write_text('{"user_message":"private"}\n', encoding="utf-8")
        run_git(self.root, "add", "-f", "memory/users/u1/MEMORY.md")
        run_git(self.root, "add", "-f", "data/project-optimizer/raw_model_inputs/2026-05-26.jsonl")

        result = self.run_preflight()

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertIn("memory/users/u1/MEMORY.md", payload["protected_staged"])
        self.assertIn("data/project-optimizer/raw_model_inputs/2026-05-26.jsonl", payload["protected_staged"])

    def test_preflight_blocks_grok_real_mode_runtime_assets(self):
        assets_dir = self.root / "data" / "grok-real-mode-assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        (assets_dir / "grok_real_mode_assets.xlsx").write_bytes(b"local workbook")
        (assets_dir / "grok_real_mode_assets.cache.json").write_text("{}\n", encoding="utf-8")
        run_git(self.root, "add", "-f", "data/grok-real-mode-assets/grok_real_mode_assets.xlsx")
        run_git(self.root, "add", "-f", "data/grok-real-mode-assets/grok_real_mode_assets.cache.json")

        result = self.run_preflight()

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertIn("data/grok-real-mode-assets/grok_real_mode_assets.xlsx", payload["protected_staged"])
        self.assertIn("data/grok-real-mode-assets/grok_real_mode_assets.cache.json", payload["protected_staged"])


if __name__ == "__main__":
    unittest.main()
