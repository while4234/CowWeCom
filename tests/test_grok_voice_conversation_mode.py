# encoding:utf-8

from types import SimpleNamespace

import pytest

from agent.protocol.agent_stream import AgentStreamExecutor
from agent.protocol.models import LLMModel
from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel import chat_channel as chat_channel_module
from channel import voice_streamer
from channel.chat_channel import ChatChannel
from channel.voice_streamer import VoiceReplyStreamer
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
    return settings


def _model(input_is_voice=True, channel_type="wechatcom_app"):
    return SimpleNamespace(input_is_voice=input_is_voice, channel_type=channel_type)


def _reasoning(effort="low", source="local", rule="low_greeting"):
    return SimpleNamespace(selected_effort=effort, decision_source=source, local_rule=rule)


class _FakeAgent:
    memory_manager = None
    skill_manager = None
    max_context_tokens = None
    runtime_info = {}

    def _estimate_message_tokens(self, msg):
        return len(str(msg))

    def _get_model_context_window(self):
        return 100000


class _CaptureStreamModel(LLMModel):
    def __init__(self, input_is_voice=True, channel_type="wechatcom_app"):
        super().__init__(model="default-model")
        self.input_is_voice = input_is_voice
        self.channel_type = channel_type
        self.session_id = "s1"
        self.is_group = False
        self.requests = []

    def call_stream(self, request):
        self.requests.append(request)
        yield {"choices": [{"delta": {"content": "ok"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}


class _NoopPluginManager:
    def emit_event(self, event):
        return event


class _VoiceStreamChannel:
    channel_type = "wechatcom_app"

    def __init__(self, voice_success=True):
        self.voice_success = voice_success
        self.sent = []

    def _send(self, reply, context):
        self.sent.append((reply.type, reply.content))
        if reply.type == ReplyType.VOICE:
            return self.voice_success
        return True

    def _decorate_reply(self, context, reply):
        return reply


def _run_capture_request(model, monkeypatch):
    monkeypatch.setattr(
        "agent.protocol.agent_stream.resolve_reasoning_effort_for_task",
        lambda *_args, **_kwargs: None,
    )
    executor = AgentStreamExecutor(
        agent=_FakeAgent(),
        model=model,
        system_prompt="system",
        tools=[],
        messages=[],
    )
    executor._build_request_context_text = lambda _user_message: ""
    executor._record_project_optimizer_task_start = lambda _user_message: ""
    executor._record_project_optimizer_task_end = lambda *_args, **_kwargs: None

    assert executor.run_stream("hello") == "ok"
    assert len(model.requests) == 1
    return model.requests[0]


def _stream_context(channel_type="wechatcom_app"):
    context = Context(ContextType.TEXT, "hello")
    context["input_is_voice"] = True
    context["channel_type"] = channel_type
    context["session_id"] = "s1"
    context["receiver"] = "u1"
    return context


def _conversation_decision(channel_type="wechatcom_app"):
    return {
        "enabled": True,
        "mode": "conversation",
        "source": "conversation_mode",
        "local_rule": "coding",
        "input_is_voice": True,
        "channel": channel_type,
    }


def _send_reply_channel(monkeypatch):
    channel = object.__new__(ChatChannel)
    sent = []
    channel._send = lambda reply, context: sent.append((reply.type, reply.content)) or True
    monkeypatch.setattr(chat_channel_module, "PluginManager", lambda: _NoopPluginManager())
    return channel, sent


def test_text_input_conversation_mode_stays_text(voice_settings):
    voice_settings["grok_voice_conversation_mode_enabled"] = True

    decision = resolve_grok_voice_mode_decision(_model(input_is_voice=False), _reasoning("xhigh", rule="coding"))

    assert decision.enabled is False
    assert decision.mode == "disabled"
    assert decision.reason == "input_is_not_voice"


def test_conversation_flag_alone_enables_voice(voice_settings):
    voice_settings.update({
        "grok_voice_reply_enabled": False,
        "grok_voice_mode_enabled": False,
        "grok_voice_conversation_mode_enabled": True,
    })

    decision = resolve_grok_voice_mode_decision(_model(), _reasoning("low", "local", "low_greeting"))

    assert decision.enabled is True
    assert decision.mode == "conversation"
    assert decision.source == "conversation_mode"


def test_non_conversation_voice_switch_still_controls_low_gated_mode(voice_settings):
    voice_settings.update({
        "grok_voice_reply_enabled": False,
        "grok_voice_mode_enabled": True,
        "grok_voice_conversation_mode_enabled": False,
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


def test_voice_low_latency_backend_model_can_override_current_backend(voice_settings):
    voice_settings.update({
        "grok_voice_conversation_mode_enabled": True,
        "grok_voice_low_latency_backend": "codex",
        "grok_voice_low_latency_model": "codex-fast-model",
    })

    decision = resolve_grok_voice_mode_decision(_model(), _reasoning("xhigh", rule="coding"))

    assert decision.enabled is True
    assert decision.selected_backend == "codex"
    assert decision.selected_model == "codex-fast-model"


def test_conversation_voice_request_sets_backend_model_for_current_request(voice_settings, monkeypatch):
    voice_settings.update({
        "grok_voice_reply_enabled": False,
        "grok_voice_conversation_mode_enabled": True,
        "grok_voice_low_latency_backend": "codex",
        "grok_voice_low_latency_model": "codex-fast-model",
        "grok_voice_max_output_tokens": 180,
    })

    request = _run_capture_request(_CaptureStreamModel(input_is_voice=True), monkeypatch)

    assert request.backend == "codex"
    assert request.model == "codex-fast-model"
    assert request.max_tokens == 180
    assert request.max_output_tokens == 180
    assert request.reasoning_effort == "low"
    assert request.reasoning_effort_locked is True


def test_text_request_does_not_receive_voice_backend_model_override(voice_settings, monkeypatch):
    voice_settings.update({
        "grok_voice_reply_enabled": False,
        "grok_voice_conversation_mode_enabled": True,
        "grok_voice_low_latency_backend": "codex",
        "grok_voice_low_latency_model": "codex-fast-model",
        "grok_voice_max_output_tokens": 180,
    })

    request = _run_capture_request(_CaptureStreamModel(input_is_voice=False), monkeypatch)

    assert getattr(request, "backend", None) is None
    assert request.model is None
    assert request.max_tokens is None
    assert not hasattr(request, "max_output_tokens")
    assert not hasattr(request, "reasoning_effort_locked")


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


def test_personal_wechat_voice_input_conversation_mode_stays_text(voice_settings):
    voice_settings["grok_voice_conversation_mode_enabled"] = True

    decision = resolve_grok_voice_mode_decision(_model(input_is_voice=True, channel_type="weixin"), _reasoning("low"))

    assert decision.enabled is False
    assert decision.reason == "channel_not_allowed"


def test_wecom_bot_voice_input_conversation_mode_is_allowed(voice_settings):
    voice_settings["grok_voice_conversation_mode_enabled"] = True

    decision = resolve_grok_voice_mode_decision(_model(input_is_voice=True, channel_type="wecom_bot"), _reasoning("xhigh", rule="coding"))

    assert decision.enabled is True
    assert decision.mode == "conversation"


def test_tts_failure_falls_back_to_final_text(voice_settings, monkeypatch):
    voice_settings["grok_voice_conversation_mode_enabled"] = True
    voice_settings["grok_voice_streaming_enabled"] = True
    monkeypatch.setattr(voice_streamer, "conf", lambda: voice_settings)
    monkeypatch.setattr(
        voice_streamer,
        "generate_xai_tts",
        lambda text: (_ for _ in ()).throw(RuntimeError("tts down")),
    )
    context = _stream_context()
    streamer = VoiceReplyStreamer.try_create(context, _VoiceStreamChannel(), _conversation_decision())

    streamer.handle_event({"type": "message_update", "data": {"delta": "你好。"}})
    streamer.handle_event({"type": "message_end", "data": {"tool_calls": []}})

    assert "voice_stream_sent" not in context
    context["channel_type"] = "web"
    channel, sent = _send_reply_channel(monkeypatch)
    assert channel._send_reply(context, Reply(ReplyType.TEXT, "你好。")) is True
    assert sent == [(ReplyType.TEXT, "你好。")]


def test_successful_voice_stream_suppresses_final_text_only_after_segment_sent(monkeypatch):
    context = _stream_context(channel_type="web")
    channel, sent = _send_reply_channel(monkeypatch)

    assert channel._send_reply(context, Reply(ReplyType.TEXT, "fallback")) is True
    assert sent == [(ReplyType.TEXT, "fallback")]

    sent.clear()
    context["voice_stream_sent"] = True
    assert channel._send_reply(context, Reply(ReplyType.TEXT, "final")) is True
    assert sent == []


def test_legacy_always_reply_voice_is_isolated_from_grok_tts(voice_settings):
    voice_settings["text_to_voice"] = "grok"
    channel = object.__new__(ChatChannel)
    channel.NOT_SUPPORT_REPLYTYPE = []
    context = Context(ContextType.TEXT, "hello")
    context["input_is_voice"] = False

    assert channel._legacy_text_to_voice_allowed(context) is False
