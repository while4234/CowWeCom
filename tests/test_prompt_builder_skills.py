import unittest
from types import SimpleNamespace

from agent.prompt.builder import _build_skills_section


class PromptBuilderSkillsTest(unittest.TestCase):
    def test_skills_prompt_allows_complementary_skill_pairing(self):
        class FakeSkillManager:
            def build_skills_prompt(self):
                return (
                    "<available_skills>\n"
                    "  <skill><name>travel-manager</name><description>Travel planning</description></skill>\n"
                    "  <skill><name>amap-cowwechat</name><description>AMap route and traffic</description></skill>\n"
                    "</available_skills>"
                )

        prompt = "\n".join(
            _build_skills_section(
                FakeSkillManager(),
                tools=[SimpleNamespace(name="read")],
                language="zh",
            )
        )

        self.assertIn("如果多个技能能互补完成同一任务", prompt)
        self.assertIn("旅行/一日游规划", prompt)
        self.assertIn("地图/高德路线类 Skill", prompt)
        self.assertIn("另一个技能明确互补", prompt)
        self.assertNotIn("永远不要一次性读取多个技能", prompt)


if __name__ == "__main__":
    unittest.main()
