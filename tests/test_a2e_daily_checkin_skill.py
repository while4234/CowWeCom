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

    def test_helper_auto_closes_after_verified_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SkillManager(custom_dir=str(Path(tmp) / "skills"))
            entry = manager.get_skill("a2e-daily-checkin")
            skill_content = Path(entry.skill.file_path).read_text(encoding="utf-8")
            script_content = (
                Path(entry.skill.file_path).parent / "scripts" / "a2e_checkin.ps1"
            ).read_text(encoding="utf-8")

        self.assertIn("-KeepOpen", skill_content)
        self.assertIn("[switch]$KeepOpen", script_content)
        self.assertIn("$autoCloseAfterVerifiedClaim", script_content)
        self.assertIn("Close-ChromeWindow $openedWindow", script_content)
        self.assertIn("Close-AllA2EChromeWindows", script_content)
        self.assertIn("FinalCloseSweep", script_content)

    def test_helper_surfaces_manual_verification_for_notification(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SkillManager(custom_dir=str(Path(tmp) / "skills"))
            entry = manager.get_skill("a2e-daily-checkin")
            skill_content = Path(entry.skill.file_path).read_text(encoding="utf-8")
            script_content = (
                Path(entry.skill.file_path).parent / "scripts" / "a2e_checkin.ps1"
            ).read_text(encoding="utf-8")

        self.assertIn("ManualActionRequired", skill_content)
        self.assertIn("NeedsNotification", skill_content)
        self.assertIn("New-ManualActionRequired", script_content)
        self.assertIn("NeedsNotification = $true", script_content)
        self.assertIn("human verification", script_content)

    def test_helper_can_fall_back_to_a2e_profile_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SkillManager(custom_dir=str(Path(tmp) / "skills"))
            entry = manager.get_skill("a2e-daily-checkin")
            script_content = (
                Path(entry.skill.file_path).parent / "scripts" / "a2e_checkin.ps1"
            ).read_text(encoding="utf-8")

        self.assertIn("$fallbackTokens = @()", script_content)
        self.assertIn("$fallbackTokens += $match.Groups[1].Value", script_content)
        self.assertIn("return $fallbackTokens[0]", script_content)

    def test_helper_sanitizes_token_before_api_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SkillManager(custom_dir=str(Path(tmp) / "skills"))
            entry = manager.get_skill("a2e-daily-checkin")
            script_content = (
                Path(entry.skill.file_path).parent / "scripts" / "a2e_checkin.ps1"
            ).read_text(encoding="utf-8")

        self.assertIn("function Normalize-A2EAccessToken", script_content)
        self.assertIn("[\\x00-\\x1F\\x7F]", script_content)
        self.assertIn("Normalize-A2EAccessToken (Get-A2EAccessToken $Config)", script_content)


if __name__ == "__main__":
    unittest.main()
