import subprocess
import tempfile
import unittest
from pathlib import Path

from agent.skills.manager import SkillManager
from agent.tools.tool_manager import ToolManager
from common.git_code_updater import GitCodeUpdater


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


class GitCodeUpdaterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.checkout = self.root / "checkout"
        self.source.mkdir()
        run_git(self.source, "init", "-b", "main")
        run_git(self.source, "config", "user.name", "Test User")
        run_git(self.source, "config", "user.email", "test@example.com")
        (self.source / ".gitignore").write_text("config.json\n.env\n", encoding="utf-8")
        (self.source / "app.py").write_text("print('v1')\n", encoding="utf-8")
        run_git(self.source, "add", ".")
        run_git(self.source, "commit", "-m", "initial")
        run_git(self.root, "clone", str(self.source), str(self.checkout))

    def tearDown(self):
        self.tmp.cleanup()

    def commit_source_change(self, path, content, message="change"):
        target = self.source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        run_git(self.source, "add", path)
        run_git(self.source, "commit", "-m", message)

    def test_fast_forward_update_preserves_ignored_local_config(self):
        (self.checkout / "config.json").write_text('{"secret":"local"}\n', encoding="utf-8")
        self.commit_source_change("feature.py", "print('feature')\n")

        result = GitCodeUpdater(self.checkout).update("origin", "main")

        self.assertEqual(result.status, "updated")
        self.assertTrue((self.checkout / "feature.py").exists())
        self.assertEqual((self.checkout / "config.json").read_text(encoding="utf-8"), '{"secret":"local"}\n')

    def test_dirty_worktree_is_refused(self):
        (self.checkout / "app.py").write_text("print('local edit')\n", encoding="utf-8")
        self.commit_source_change("feature.py", "print('feature')\n")

        result = GitCodeUpdater(self.checkout).update("origin", "main")

        self.assertEqual(result.status, "dirty")
        self.assertFalse((self.checkout / "feature.py").exists())

    def test_remote_protected_path_is_refused_before_merge(self):
        (self.source / "config.json").write_text('{"secret":"remote"}\n', encoding="utf-8")
        run_git(self.source, "add", "-f", "config.json")
        run_git(self.source, "commit", "-m", "add config")
        (self.checkout / "config.json").write_text('{"secret":"local"}\n', encoding="utf-8")

        result = GitCodeUpdater(self.checkout).update("origin", "main")

        self.assertEqual(result.status, "protected_path")
        self.assertIn("config.json", result.protected_files)
        self.assertEqual((self.checkout / "config.json").read_text(encoding="utf-8"), '{"secret":"local"}\n')


class CodeUpdateRegistrationTest(unittest.TestCase):
    def test_builtin_skill_is_discovered(self):
        manager = SkillManager()

        self.assertIn("code-update", manager.skills)
        self.assertTrue(manager.is_skill_enabled("code-update"))
        self.assertIn("GitHub", manager.skills["code-update"].skill.description)

    def test_git_code_update_tool_is_loaded(self):
        manager = ToolManager()
        manager.tool_classes = {}
        manager._load_tools_from_init()

        self.assertIn("git_code_update", manager.tool_classes)


if __name__ == "__main__":
    unittest.main()
