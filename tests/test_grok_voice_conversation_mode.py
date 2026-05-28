# encoding:utf-8

from types import SimpleNamespace

import pytest

from bridge.context import Context, ContextType
from channel.chat_channel import ChatChannel
from common import grok_voice_mode
from common.grok_voice_mode import append_voice_short_answer_prompt, resolve_grok_voice_mode_decision


@pytest.fixture(autouse=True)
def voice_settings(monkeypatch):
    settings = {
        "grok_voice_reply_enabled": True,
        "grok_voice_mode_enabled": False,
        "grok_voice_conversation_mode_enabled": False,
        "grok_voice_reply_channels": ["wechatcom_app", "wecom_bot"],
        "grok_voice_require_low_reasoning_when_not_conversation_mode": True,
        "grok_voice_force_voice_for_voice_input_in_conversation_mode": True,
        "grok_voice_force_reasoning_effort": "low",
        "grok_voice_low_latency_backend": "",
        "grok_voice_low_latency_model": "",
        "grok_voice_max_output_tokens": 220,
        "grok_voice_short_answer_prompt_enabled": True,
        "text_to_voice": "openai",
    }
    monkeypatch.setattr(grok_voice_mode, "conf", lambda: settings)
    monkeypatch.setattr(grok_voice_mode, "get_current_backend", lambda: "capi")
    return settings


def _model(input_is_voice=True, channel_type="wechatcom_app"):
    return SimpleNamespace(input_is_voice=input_is_voice, channel_type=channel_type)


def _reasoning(effort="low", source="local", rule="low_greeting"):
    return SimpleNamespace(selected_effort=effort, decision_source=source, local_rule=rule)


def test_text_input_conversation_mode_stays_text(voice_settings):
    voice_settings["grok_voice_conversation_mode_enabled"] = True

    decision = resolve_grok_voice_mode_decision(_model(input_is_voice=False), _reasoning("xhigh", rule="coding"))

    assert decision.enabled is False
    assert decision.mode == "disabled"
    assert decision.reason == "input_is_not_voice"


def test_new_total_switch_false_overrides_legacy_alias(voice_settings):
    voice_settings.update({
        "grok_voice_reply_enabled": False,
        "grok_voice_mode_enabled": True,
        "grok_voice_conversation_mode_enabled": True,
    })

    decision = resolve_grok_voice_mode_decision(_model(), _reasoning("low", "local", "low_greeting"))

    assert decision.enabled is False
    assert decision.reason == "grok_voice_reply_disabled"


def test_legacy_voice_mode_alias_still_enables_when_new_switch_missing(voice_settings):
    voice_settings.pop("grok_voice_reply_enabled")
    voice_settings.update({
        "grok_voice_mode_enabled": True,
        "grok_voice_conversation_mode_enabled": True,
    })

    decision = resolve_grok_voice_mode_decision(_model(), _reasoning("low", "local", "low_greeting"))

    assert decision.enabled is True
    assert decision.mode == "conversation"


def test_voice_input_conversation_mode_forces_voice_even_for_complex_reasoning(voice_settings):
    voice_settings.update({
        "grok_voice_conversation_mode_enabled": True,
        "grok_voice_low_latency_backend": "capi",
        "grok_voice_low_latency_model": "fast-current-backend-model",
        "grok_voice_max_output_tokens": 180,
    })

    decision = resolve_grok_voice_mode_decision(_model(), _reasoning("xhigh", rule="coding"))

    assert decision.enabled is True
    assert decision.mode == "conversation"
    assert decision.force_voice_reply is True
    assert decision.selected_effort == "low"
    assert decision.source == "conversation_mode"
    assert decision.selected_backend == "capi"
    assert decision.selected_model == "fast-current-backend-model"
    assert decision.max_output_tokens == 180


def test_conversation_mode_short_prompt_is_strict(voice_settings):
    voice_settings["grok_voice_conversation_mode_enabled"] = True
    decision = resolve_grok_voice_mode_decision(_model(), _reasoning("xhigh", rule="coding"))

    prompt = append_voice_short_answer_prompt("base", decision)

    assert "最多回答 2 到 3 句" in prompt
    assert "120 个中文字符" in prompt


def test_voice_low_latency_model_does_not_cross_backend(voice_settings):
    voice_settings.update({
        "grok_voice_conversation_mode_enabled": True,
        "grok_voice_low_latency_backend": "codex",
        "grok_voice_low_latency_model": "codex-fast-model",
    })

    decision = resolve_grok_voice_mode_decision(_model(), _reasoning("xhigh", rule="coding"))

    assert decision.enabled is True
    assert decision.selected_backend is None
    assert decision.selected_model is None


def test_non_conversation_mode_requires_local_low(voice_settings):
    low_decision = resolve_grok_voice_mode_decision(_model(), _reasoning("low", "local", "low_greeting"))
    medium_decision = resolve_grok_voice_mode_decision(_model(), _reasoning("medium", "local", "daily_expression_advice"))
    default_decision = resolve_grok_voice_mode_decision(_model(), _reasoning("low", "default", "low_greeting"))

    assert low_decision.enabled is True
    assert low_decision.mode == "low_gated"
    assert low_decision.source == "local_low"
    assert medium_decision.enabled is False
    assert default_decision.enabled is False


def test_disallowed_channel_stays_text(voice_settings):
    voice_settings["grok_voice_conversation_mode_enabled"] = True

    decision = resolve_grok_voice_mode_decision(_model(channel_type="weixin"), _reasoning("low", "local", "low_greeting"))

    assert decision.enabled is False
    assert decision.reason == "channel_not_allowed"


def test_legacy_always_reply_voice_is_isolated_from_grok_tts(voice_settings):
    voice_settings["text_to_voice"] = "grok"
    channel = object.__new__(ChatChannel)
    channel.NOT_SUPPORT_REPLYTYPE = []
    context = Context(ContextType.TEXT, "hello")
    context["input_is_voice"] = False

    assert channel._legacy_text_to_voice_allowed(context) is False
