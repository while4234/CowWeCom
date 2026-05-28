# encoding:utf-8

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestGrokBotFactory(unittest.TestCase):
    def test_grok_constants_are_in_model_list(self):
        from common import const

        self.assertEqual(const.GROK, "grok")
        self.assertEqual(const.XAI, "xai")
        self.assertEqual(const.GROK_4_3, "grok-4.3")
        self.assertIn(const.GROK, const.MODEL_LIST)
        self.assertIn(const.XAI, const.MODEL_LIST)
        self.assertIn(const.GROK_4_3, const.MODEL_LIST)

    def test_bot_factory_returns_grok_bot_for_grok_and_xai(self):
        from common import const
        from models.bot_factory import create_bot
        from models.grok.grok_bot import GrokBot

        fake_conf = MagicMock()
        fake_conf.get.side_effect = lambda key, default=None: {
            "grok_model": "grok-4.3",
            "model": "grok-4.3",
            "temperature": 0.7,
            "top_p": 1.0,
            "frequency_penalty": 0.0,
            "presence_penalty": 0.0,
            "request_timeout": 60,
        }.get(key, default)

        for bot_type in (const.GROK, const.XAI):
            with self.subTest(bot_type=bot_type):
                with patch("models.grok.grok_bot.conf", return_value=fake_conf):
                    with patch("models.grok.grok_bot.SessionManager"):
                        bot = create_bot(bot_type)
                self.assertIsInstance(bot, GrokBot)

    def test_model_router_maps_grok_models_to_grok_bot(self):
        from common import const
        from common.llm_backend_router import resolve_configured_chat_bot_type

        fake_conf = MagicMock()
        fake_conf.get.side_effect = lambda key, default=None: {
            "use_linkai": False,
            "linkai_api_key": "",
            "bot_type": "",
            "use_azure_chatgpt": False,
        }.get(key, default)

        with patch("config.conf", return_value=fake_conf):
            self.assertEqual(resolve_configured_chat_bot_type("grok-4.3"), const.GROK)
            self.assertEqual(resolve_configured_chat_bot_type("xai"), const.GROK)


if __name__ == "__main__":
    unittest.main()
