import tempfile
import unittest
from pathlib import Path

from agent.skills.manager import SkillManager


class A2EDailyCheckinSkillTest(unittest.TestCase):
    def test_builtin_skill_is_discovered_and_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SkillManager(custom_dir=str(Path(tmp) / "skills"))

            entry = manager.get_skill("a2e-daily-checkin")

        self.assertIsNotNone(entry)
        self.assertEqual(entry.skill.source, "builtin")
        self.assertTrue(manager.is_skill_enabled("a2e-daily-checkin"))

    def test_builtin_skill_prompt_points_to_helper_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SkillManager(custom_dir=str(Path(tmp) / "skills"))
            prompt = manager.build_skills_prompt(skill_filter=["a2e-daily-checkin"])
            entry = manager.get_skill("a2e-daily-checkin")
            content = Path(entry.skill.file_path).read_text(encoding="utf-8")

        self.assertIn("<name>a2e-daily-checkin</name>", prompt)
        self.assertIn("skills\\a2e-daily-checkin\\SKILL.md", prompt)
        self.assertIn("a2e_checkin.ps1", content)
        self.assertIn("video.a2e.ai", prompt)


if __name__ == "__main__":
    unittest.main()
