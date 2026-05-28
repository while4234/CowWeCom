# encoding:utf-8

import json
import unittest
from pathlib import Path


class TestGrokConfigTemplate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = json.loads(Path("config-template.json").read_text(encoding="utf-8"))

    def test_template_exposes_grok_bot_type_entry_without_changing_default(self):
        self.assertIn("bot_type", self.template)
        self.assertEqual(self.template["bot_type"], "")
        self.assertEqual(self.template["grok_model"], "grok-4.3")

    def test_bare_manual_code_compatibility_is_disabled_by_default(self):
        self.assertIs(self.template["grok_oauth_accept_bare_code"], False)


if __name__ == "__main__":
    unittest.main()
