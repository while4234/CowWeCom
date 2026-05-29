# encoding:utf-8

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from common import const


def _template():
    return json.loads(Path("config-template.json").read_text(encoding="utf-8"))


def test_grok_defaults_are_present_but_do_not_enable_text_bot():
    template = _template()

    assert template["bot_type"] == ""
    assert template["grok_model"] == "grok-4.3"
    assert template["grok_wire_api"] == "responses"
    assert template["grok_auth_prefer_oauth"] is True
    assert template["grok_oauth_accept_bare_code"] is False
    assert template["grok_api_key"] == ""
    assert template["grok_proxy"] == ""
    assert template["grok_auth_file"] == ""


def test_grok_media_and_voice_defaults_are_offline_safe():
    template = _template()

    assert template["text_to_image"] == "dall-e-2"
    assert template["video_generation_provider"] == "xai"
    assert template["grok_image_model"] == "grok-imagine-image"
    assert template["grok_video_model"] == "grok-imagine-video"
    assert template["text_to_voice"] == "openai"
    assert template["voice_reply_voice"] is False
    assert template["grok_voice_conversation_mode_enabled"] is True
    assert template["grok_voice_reply_channels"] == ["wechatcom_app", "wecom_bot"]
    assert template["wecom_voice_max_seconds"] == 55
    assert template["wecom_voice_max_bytes"] == 1900000
    assert template["wecom_voice_amr_bitrate"] == "12.2k"


def test_docs_grok_covers_required_pr5_user_guidance():
    text = Path("docs/grok.md").read_text(encoding="utf-8")
    required_markers = [
        "http://127.0.0.1:56121/callback",
        "个人微信当前不新增语音发送能力",
        "语音模式下发送语音均回复语音",
        "只对 `input_is_voice=True` 生效",
        "已成功发送至少一段语音流后，才会抑制最终完整文本",
        "URL 下载只允许公开 HTTPS 地址",
        "tmp/grok_media/",
        "不写回 Hermes auth store",
        "manual paste",
        "AMR",
        "单条 <= 55 秒",
        "单条 <= 1.9 MB",
        "视频生成超时",
        "xAI 429 rate limit",
        "xAI 401 auth error",
    ]
    required_config_keys = [
        "grok_model",
        "grok_api_base",
        "grok_proxy",
        "grok_auth_file",
        "grok_auth_prefer_oauth",
        "grok_import_hermes_auth",
        "grok_wire_api",
        "grok_api_key",
        "text_to_voice",
        "grok_tts_voice_id",
        "grok_tts_language",
        "grok_tts_sample_rate",
        "grok_tts_bit_rate",
        "grok_tts_auto_speech_tags",
        "grok_voice_mode_enabled",
        "grok_voice_reply_enabled",
        "grok_voice_conversation_mode_enabled",
        "grok_voice_reply_channels",
        "grok_voice_streaming_enabled",
        "grok_voice_require_low_reasoning",
        "grok_voice_require_low_reasoning_when_not_conversation_mode",
        "grok_voice_force_voice_for_voice_input_in_conversation_mode",
        "grok_voice_force_reasoning_effort",
        "grok_voice_low_latency_backend",
        "grok_voice_low_latency_model",
        "grok_voice_max_output_tokens",
        "grok_voice_short_answer_prompt_enabled",
        "grok_voice_max_segment_chars",
        "grok_voice_min_segment_chars",
        "grok_voice_flush_idle_ms",
        "grok_voice_tts_queue_size",
        "wecom_voice_max_seconds",
        "wecom_voice_max_bytes",
        "wecom_voice_normalize_enabled",
        "wecom_voice_normalize_target_dbfs",
        "wecom_voice_normalize_headroom_db",
        "wecom_voice_amr_bitrate",
        "reasoning_effort_policy_low_effort",
        "text_to_image",
        "grok_image_model",
        "grok_image_resolution",
        "grok_image_aspect_ratio",
        "grok_image_timeout_seconds",
        "grok_image_download_timeout_seconds",
        "video_generation_provider",
        "video_create_prefix",
        "grok_video_model",
        "grok_video_duration",
        "grok_video_aspect_ratio",
        "grok_video_resolution",
        "grok_video_timeout_seconds",
        "grok_video_poll_interval_seconds",
        "grok_video_download_timeout_seconds",
    ]

    for marker in required_markers + required_config_keys:
        assert marker in text


def test_grok_startup_modules_import_without_network_or_tokens():
    from common import grok_voice_mode
    from models.grok import grok_bot, grok_image, grok_video

    assert grok_bot.DEFAULT_GROK_MODEL == "grok-4.3"
    assert grok_voice_mode.DEFAULT_VOICE_CHANNELS == ["wechatcom_app", "wecom_bot"]
    assert grok_image.is_grok_image_provider("grok") is True
    assert grok_image.is_grok_image_provider("xai") is True
    assert grok_image.is_grok_image_provider("openai") is False
    assert grok_video.is_grok_video_provider("grok") is True
    assert grok_video.is_grok_video_provider("xai") is True
    assert grok_video.is_grok_video_provider("openai") is False


def test_basic_model_routing_maps_grok_aliases_to_grok_bot():
    from common.llm_backend_router import resolve_configured_chat_bot_type

    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "use_linkai": False,
        "linkai_api_key": "",
        "bot_type": "",
        "use_azure_chatgpt": False,
    }.get(key, default)

    with patch("config.conf", return_value=fake_conf):
        assert resolve_configured_chat_bot_type("grok-4.3") == const.GROK
        assert resolve_configured_chat_bot_type("grok-beta") == const.GROK
        assert resolve_configured_chat_bot_type("xai") == const.GROK


def test_bot_factory_can_construct_grok_without_resolving_credentials():
    from models.bot_factory import create_bot
    from models.grok.grok_bot import GrokBot

    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "grok_model": "grok-4.3",
        "temperature": 0.7,
        "top_p": 1.0,
        "frequency_penalty": 0.0,
        "presence_penalty": 0.0,
        "request_timeout": 60,
    }.get(key, default)

    with patch("models.grok.grok_bot.conf", return_value=fake_conf), \
            patch("models.grok.grok_bot.SessionManager") as session_manager, \
            patch("models.grok.grok_bot.resolve_xai_http_credentials") as resolve_credentials:
        for bot_type in (const.GROK, const.XAI):
            bot = create_bot(bot_type)
            assert isinstance(bot, GrokBot)
            assert bot.args["model"] == "grok-4.3"

    assert session_manager.call_count == 2
    resolve_credentials.assert_not_called()


def test_openai_bot_still_constructs_and_replies_to_plain_text_offline():
    from bridge.context import Context, ContextType
    from bridge.reply import ReplyType
    from models.bot_factory import create_bot
    from models.openai.open_ai_bot import OpenAIBot

    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "open_ai_api_key": "test-openai-key",
        "open_ai_api_base": "https://api.openai.com/v1",
        "proxy": "",
        "model": "gpt-4o-mini",
        "temperature": 0.7,
        "frequency_penalty": 0.0,
        "presence_penalty": 0.0,
        "request_timeout": 60,
    }.get(key, default)

    with patch("models.openai.open_ai_bot.conf", return_value=fake_conf):
        bot = create_bot(const.OPEN_AI)

    assert isinstance(bot, OpenAIBot)
    context = Context(ContextType.TEXT, "你好")
    context["session_id"] = "session-1"
    with patch.object(
        bot,
        "reply_text",
        return_value={"total_tokens": 3, "completion_tokens": 1, "content": "你好"},
    ):
        reply = bot.reply("你好", context)

    assert reply.type == ReplyType.TEXT
    assert reply.content == "你好"


def test_default_voice_conversation_mode_only_routes_allowed_voice_inputs():
    from common.grok_voice_mode import resolve_grok_voice_mode_decision

    settings = _template()
    reasoning = SimpleNamespace(selected_effort="xhigh", decision_source="local", local_rule="coding")
    voice_model = SimpleNamespace(input_is_voice=True, channel_type="wechatcom_app")
    text_model = SimpleNamespace(input_is_voice=False, channel_type="wechatcom_app")
    other_channel_model = SimpleNamespace(input_is_voice=True, channel_type="weixin")

    with patch("common.grok_voice_mode.conf", lambda: settings):
        voice_decision = resolve_grok_voice_mode_decision(voice_model, reasoning)
        text_decision = resolve_grok_voice_mode_decision(text_model, reasoning)
        other_channel_decision = resolve_grok_voice_mode_decision(other_channel_model, reasoning)

    assert voice_decision.enabled is True
    assert voice_decision.mode == "conversation"
    assert voice_decision.force_voice_reply is True
    assert voice_decision.selected_effort == "low"
    assert voice_decision.max_output_tokens == 220
    assert text_decision.enabled is False
    assert text_decision.reason == "input_is_not_voice"
    assert other_channel_decision.enabled is False
    assert other_channel_decision.reason == "channel_not_allowed"
