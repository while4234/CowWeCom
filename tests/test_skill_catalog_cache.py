import json
import tempfile
import unittest
from pathlib import Path

from agent.skills.cache import SkillCatalogCache


def write_skill(
    path: Path,
    name: str,
    description: str,
    body: str = "## Usage\nUse it.\n",
    frontmatter: dict | None = None,
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    extra = ""
    for key, value in (frontmatter or {}).items():
        extra += f"{key}: {value}\n"
    (path / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"display-name: {name} Display\n"
        f"description: {description}\n"
        f"{extra}"
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

    def test_builds_layered_summaries_and_category_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtin = root / "builtin"
            custom = root / "custom"
            write_skill(
                builtin / "amap-cowwechat",
                "amap-cowwechat",
                "使用高德地图提供通勤、路线、路况和旅游路线分析。",
                body=(
                    "## 支持命令\n"
                    "高德 上班\n"
                    "高德 路况 北京南站 到 故宫\n"
                ),
            )
            write_skill(
                builtin / "stock-analysis",
                "stock-analysis",
                "Analyze stocks and cryptocurrencies using Yahoo Finance data.",
                body="## Usage\npython stock.py quote AAPL\n",
            )

            catalog = SkillCatalogCache(str(builtin), str(custom))
            overview = catalog.overview_summary()
            category = catalog.category_summary("travel_location")
            detail = catalog.format_skill_detail_summary("amap-cowwechat")

            self.assertIn("出行地图", overview)
            self.assertIn("金融行情", overview)
            self.assertIn("amap-cowwechat", category)
            self.assertNotIn("stock-analysis", category)
            self.assertIn("高德 上班", detail)
            self.assertIn("READ_FULL_SKILL", detail)

    def test_full_skill_context_strips_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtin = root / "builtin"
            custom = root / "custom"
            body = "## Internal Details\nFull parameter table lives here.\n"
            write_skill(builtin / "alpha", "alpha", "Alpha description", body=body)

            catalog = SkillCatalogCache(str(builtin), str(custom))
            full = catalog.full_skill_context("alpha")

            self.assertIn("完整 SKILL.md", full)
            self.assertIn("Full parameter table lives here.", full)
            self.assertNotIn("description: Alpha description", full)

    def test_builds_layered_catalog_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtin = root / "builtin"
            custom = root / "custom"
            write_skill(
                builtin / "doc-helper",
                "doc-helper",
                "Summarize local docs. Use when users ask about pdf, docx, or markdown files.",
                body=(
                    "## Usage\n"
                    "- /doc-helper summarize --input report.pdf --format markdown\n\n"
                    "## Workflow\n"
                    "Extract tables before summarizing.\n"
                    "Keep citations next to every claim.\n\n"
                    "## Internal Notes\n"
                    "Implementation Secret: full context only.\n"
                ),
                frontmatter={"category": "documents"},
            )

            catalog = SkillCatalogCache(str(builtin), str(custom))
            entry = catalog.find_entry("doc-helper")
            self.assertIsNotNone(entry)
            self.assertEqual(entry.category, "documents")
            self.assertTrue(entry.category_label)
            self.assertIn("Summarize local docs", entry.compact_summary)
            self.assertIn("Extract tables before summarizing.", entry.detailed_summary)

            overview = catalog.overview_summary()
            self.assertIn(entry.category_label, overview)
            self.assertIn("doc-helper Display (doc-helper)", overview)
            self.assertIn("Summarize local docs", overview)
            self.assertNotIn("Implementation Secret", overview)

            category = catalog.category_summary("documents")
            self.assertIn("doc-helper Display (doc-helper)", category)
            self.assertIn("Extract tables before summarizing.", category)

            detail = catalog.format_skill_detail_summary("doc-helper")
            self.assertIn("doc-helper Display (doc-helper)", detail)
            self.assertIn("Extract tables before summarizing.", detail)
            self.assertIn("READ_FULL_SKILL", detail)

            full = catalog.full_skill_context("doc-helper")
            self.assertIn("SKILL.md", full)
            self.assertIn("Implementation Secret: full context only.", full)


if __name__ == "__main__":
    unittest.main()
