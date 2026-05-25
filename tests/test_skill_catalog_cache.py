import json
import tempfile
import unittest
from pathlib import Path

from agent.skills.cache import SkillCatalogCache


def write_skill(path: Path, name: str, description: str, body: str = "## Usage\nUse it.\n") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"display-name: {name} Display\n"
        f"description: {description}\n"
        "---\n"
        f"# {name}\n\n"
        f"{body}",
        encoding="utf-8",
    )


class SkillCatalogCacheTest(unittest.TestCase):
    def test_formats_local_list_from_cached_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtin = root / "builtin"
            custom = root / "custom"
            write_skill(builtin / "alpha", "alpha", "Alpha description")
            write_skill(custom / "beta", "beta", "Beta description")
            (custom / "skills_config.json").write_text(
                json.dumps(
                    {
                        "beta": {
                            "name": "beta",
                            "display_name": "Beta Manual",
                            "description": "old",
                            "source": "custom",
                            "enabled": False,
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            catalog = SkillCatalogCache(str(builtin), str(custom))
            text = catalog.format_local_list()

            self.assertIn("本地技能/功能", text)
            self.assertIn("alpha Display (alpha)", text)
            self.assertIn("Beta Manual (beta)", text)
            self.assertIn("[off]", text)

    def test_refreshes_when_skill_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtin = root / "builtin"
            custom = root / "custom"
            skill_dir = builtin / "alpha"
            write_skill(skill_dir, "alpha", "First description")

            catalog = SkillCatalogCache(str(builtin), str(custom))
            self.assertIn("First description", catalog.format_skill_usage("alpha"))

            write_skill(skill_dir, "alpha", "Second description")

            self.assertIn("Second description", catalog.format_skill_usage("alpha"))

    def test_resolves_single_skill_usage_from_display_name_in_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtin = root / "builtin"
            custom = root / "custom"
            write_skill(
                builtin / "capi-usage-monitor",
                "capi-usage-monitor",
                "Query CAPI usage.",
                body="## Commands\nRun snapshot.\n",
            )

            catalog = SkillCatalogCache(str(builtin), str(custom))
            entry = catalog.find_entry_in_text("capi usage monitor 怎么用")
            usage = catalog.format_skill_usage("capi-usage-monitor")

            self.assertIsNotNone(entry)
            self.assertEqual(entry.name, "capi-usage-monitor")
            self.assertIn("Run snapshot.", usage)


if __name__ == "__main__":
    unittest.main()
