import json
import tempfile
import unittest
from pathlib import Path

from agent.skills.manager import SkillManager


class SkillDisplayNameSyncTest(unittest.TestCase):
    def test_repairs_placeholder_display_name_from_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            custom_dir = Path(tmp) / "skills"
            skill_dir = custom_dir / "wecom-cli"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: wecom-cli\n"
                "display-name: 企业微信 CLI\n"
                "description: Test skill.\n"
                "---\n"
                "# Test\n",
                encoding="utf-8",
            )
            config_path = custom_dir / "skills_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "wecom-cli": {
                            "name": "wecom-cli",
                            "description": "old",
                            "source": "github",
                            "enabled": False,
                            "category": "skill",
                            "display_name": "????CLI",
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            manager = SkillManager(
                builtin_dir=str(Path(tmp) / "empty"),
                custom_dir=str(custom_dir),
            )

            entry = manager.get_skills_config()["wecom-cli"]
            self.assertEqual(entry["display_name"], "企业微信 CLI")
            self.assertFalse(entry["enabled"])

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["wecom-cli"]["display_name"], "企业微信 CLI")

    def test_preserves_valid_existing_display_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            custom_dir = Path(tmp) / "skills"
            skill_dir = custom_dir / "sample"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: sample\n"
                "display-name: Frontmatter Label\n"
                "description: Test skill.\n"
                "---\n"
                "# Test\n",
                encoding="utf-8",
            )
            config_path = custom_dir / "skills_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sample": {
                            "name": "sample",
                            "description": "old",
                            "source": "custom",
                            "enabled": True,
                            "category": "skill",
                            "display_name": "Manual Label",
                        }
                    }
                ),
                encoding="utf-8",
            )

            manager = SkillManager(
                builtin_dir=str(Path(tmp) / "empty"),
                custom_dir=str(custom_dir),
            )

            self.assertEqual(
                manager.get_skills_config()["sample"]["display_name"],
                "Manual Label",
            )

    def test_loads_skills_config_with_utf8_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            custom_dir = Path(tmp) / "skills"
            skill_dir = custom_dir / "sample"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: sample\n"
                "description: Fresh description.\n"
                "---\n"
                "# Test\n",
                encoding="utf-8",
            )
            config_path = custom_dir / "skills_config.json"
            config_path.write_text(
                "\ufeff" + json.dumps(
                    {
                        "sample": {
                            "name": "sample",
                            "description": "old",
                            "source": "custom",
                            "enabled": False,
                            "category": "system_dev",
                        }
                    }
                ),
                encoding="utf-8",
            )

            manager = SkillManager(
                builtin_dir=str(Path(tmp) / "empty"),
                custom_dir=str(custom_dir),
            )

            entry = manager.get_skills_config()["sample"]
            self.assertFalse(entry["enabled"])
            self.assertEqual(entry["category"], "system_dev")
            self.assertEqual(entry["description"], "Fresh description.")


if __name__ == "__main__":
    unittest.main()
