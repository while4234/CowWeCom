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

    def test_finds_and_summarizes_multiple_categories_from_user_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtin = root / "builtin"
            custom = root / "custom"
            write_skill(
                builtin / "amap-cowwechat",
                "amap-cowwechat",
                "使用高德地图提供出行、路线、路况和通勤分析。",
                body="## 支持命令\n高德 上班\n",
            )
            write_skill(
                builtin / "shopping-helper",
                "shopping-helper",
                "帮助用户做购物比价、商品筛选和优惠券查询。",
                body="## 支持命令\n购物 比价 iPhone\n",
            )
            write_skill(
                builtin / "stock-analysis",
                "stock-analysis",
                "Analyze stocks and cryptocurrencies using Yahoo Finance data.",
                body="## Usage\npython stock.py quote AAPL\n",
            )

            catalog = SkillCatalogCache(str(builtin), str(custom))
            categories = catalog.find_categories_in_text("当前有没有购物和出行相关的功能呢")
            summary = catalog.multi_category_summary(categories)

            self.assertEqual(categories, ["travel_location", "shopping_food"])
            self.assertIn("amap-cowwechat", summary)
            self.assertIn("shopping-helper", summary)
            self.assertNotIn("stock-analysis", summary)

    def test_travel_day_trip_query_maps_to_travel_location_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = SkillCatalogCache(str(root / "builtin"), str(root / "custom"))

            categories = catalog.find_categories_in_text("有没有规划成都明日一日游的 skill")

            self.assertEqual(categories, ["travel_location"])

    def test_travel_category_surfaces_travel_manager_and_amap_pairing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtin = root / "builtin"
            custom = root / "custom"
            write_skill(
                builtin / "amap-cowwechat",
                "amap-cowwechat",
                "使用高德地图提供路线、路况、ETA 和旅游路线分析。",
                body="## 协同\n高德负责路线、ETA、路况和交通方式对比。\n",
            )
            write_skill(
                builtin / "travel-manager",
                "travel-manager",
                "Comprehensive travel planning for city one-day trips. Pair with amap-cowwechat for route and traffic decisions.",
                body="## 协同\ntravel-manager 负责行程结构，amap-cowwechat 负责路线证据。\n",
            )

            catalog = SkillCatalogCache(str(builtin), str(custom))
            summary = catalog.category_summary("travel_location")

            self.assertIn("amap-cowwechat", summary)
            self.assertIn("travel-manager", summary)
            self.assertIn("行程结构", summary)
            self.assertIn("路线证据", summary)

    def test_codex_quota_summary_mentions_analysis_strategy_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtin = root / "builtin"
            custom = root / "custom"
            write_skill(
                builtin / "codex-quota-query",
                "codex-quota-query",
                (
                    "Query and analyze Codex quota, fair-share overuse, "
                    "and follow-up usage strategy. For analysis requests, "
                    "run the decision command before answering."
                ),
                body=(
                    "## Analysis Workflow\n"
                    "Run codex_quota.py decision --format json first.\n"
                ),
            )

            catalog = SkillCatalogCache(str(builtin), str(custom))
            detail = catalog.format_skill_detail_summary("codex-quota-query")

            self.assertIn("follow-up usage strategy", detail)
            self.assertIn("codex_quota.py decision", detail)

    def test_generic_skill_word_does_not_match_system_dev_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = SkillCatalogCache(str(root / "builtin"), str(root / "custom"))

            self.assertEqual(catalog.find_categories_in_text("当前支持哪些 skill 呢"), [])
            self.assertEqual(catalog.find_category_in_text("当前支持哪些 skill 呢"), "")

    def test_inventory_summary_uses_chinese_skill_names_and_followup_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtin = root / "builtin"
            custom = root / "custom"
            write_skill(
                builtin / "frontend-design",
                "frontend-design",
                "Expert frontend design guidelines for creating beautiful, modern UIs.",
                body="## Usage\nBuild a dashboard.\n",
            )

            catalog = SkillCatalogCache(str(builtin), str(custom))
            summary = catalog.inventory_summary_zh()
            entry = catalog.find_entry_in_text("前端界面设计怎么用")

            self.assertIn("前端界面设计", summary)
            self.assertIn("现代化网页", summary)
            self.assertIn("想看某个 Skill 的详细用法", summary)
            self.assertNotIn("Expert frontend design guidelines", summary)
            self.assertIsNotNone(entry)
            self.assertEqual(entry.name, "frontend-design")

    def test_category_summary_reports_no_installed_skill_for_empty_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtin = root / "builtin"
            custom = root / "custom"
            write_skill(
                builtin / "amap-cowwechat",
                "amap-cowwechat",
                "使用高德地图提供出行、路线、路况和通勤分析。",
                body="## 支持命令\n高德 上班\n",
            )

            catalog = SkillCatalogCache(str(builtin), str(custom))
            summary = catalog.category_summary("shopping_food")
            options = catalog.category_options_summary()

            self.assertIn("购物餐饮", summary)
            self.assertIn("暂无匹配", summary)
            self.assertIn("shopping_food", options)
            self.assertIn("已安装 0 个", options)

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
