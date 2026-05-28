# encoding:utf-8

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestGrokWebGray(unittest.TestCase):
    def _fake_conf(self, values=None):
        data = {"grok_gray_enabled": False}
        if values:
            data.update(values)
        fake_conf = MagicMock()
        fake_conf.get.side_effect = lambda key, default=None: data.get(key, default)
        return fake_conf

    def test_grok_provider_hidden_by_default(self):
        from channel.web.web_channel import ConfigHandler

        providers = ConfigHandler()._visible_provider_models(self._fake_conf())

        self.assertNotIn("grok", providers)
        self.assertIn("openai", providers)
        self.assertIn("deepseek", providers)

    def test_grok_provider_visible_when_gray_enabled(self):
        from channel.web.web_channel import ConfigHandler

        providers = ConfigHandler()._visible_provider_models(
            self._fake_conf({"grok_gray_enabled": True})
        )

        self.assertIn("grok", providers)


if __name__ == "__main__":
    unittest.main()
