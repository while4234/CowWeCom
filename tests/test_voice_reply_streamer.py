# encoding:utf-8

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel import chat_channel as chat_channel_module
from channel import voice_streamer
from channel.chat_channel import ChatChannel
from channel.voice_streamer import VoiceReplyStreamer, voice_stream_enabled


class FakeChannel:
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


def _context():
    context = Context(ContextType.TEXT, "你好")
    context["input_is_voice"] = True
    context["channel_type"] = "wechatcom_app"
    context["session_id"] = "s1"
    context["receiver"] = "u1"
    return context


def _decision(mode="low_gated", source="local_low", rule="low_greeting", enabled=True):
    return {
        "enabled": enabled,
        "mode": mode,
        "selected_effort": "low",
        "source": source,
        "local_rule": rule,
        "input_is_voice": True,
        "channel": "wechatcom_app",
        "session_id": "s1",
    }


class _NoopPluginManager:
    def emit_event(self, event):
        return event


def _send_reply_channel(monkeypatch):
    channel = object.__new__(ChatChannel)
    sent = []
    channel._send = lambda reply, context: sent.append((reply.type, reply.content)) or True
    monkeypatch.setattr(chat_channel_module, "PluginManager", lambda: _NoopPluginManager())
    return channel, sent


def test_voice_stream_requires_voice_low_local_rule(monkeypatch):
    settings = {
        "grok_voice_streaming_enabled": True,
        "grok_voice_mode_enabled": True,
        "grok_voice_reply_channels": ["wechatcom_app"],
    }
    monkeypatch.setattr(voice_streamer, "conf", lambda: settings)

    assert voice_stream_enabled(_context(), FakeChannel(), _decision()) is True
    assert voice_stream_enabled(_context(), FakeChannel(), _decision(source="disabled")) is False

    text_context = _context()
    text_context["input_is_voice"] = False
    assert voice_stream_enabled(text_context, FakeChannel(), _decision()) is False


def test_voice_stream_conversation_mode_does_not_require_local_low(monkeypatch):
    settings = {
        "grok_voice_streaming_enabled": True,
        "grok_voice_reply_channels": ["wechatcom_app"],
    }
    monkeypatch.setattr(voice_streamer, "conf", lambda: settings)

    assert voice_stream_enabled(
        _context(),
        FakeChannel(),
        _decision(mode="conversation", source="conversation_mode", rule="coding"),
    ) is True


def test_voice_stream_rejects_disallowed_channel(monkeypatch):
    settings = {
        "grok_voice_streaming_enabled": True,
        "grok_voice_reply_channels": ["wecom_bot"],
    }
    monkeypatch.setattr(voice_streamer, "conf", lambda: settings)

    assert voice_stream_enabled(_context(), FakeChannel(), _decision()) is False


def test_streamer_defaults_reduce_native_voice_fragmenting(monkeypatch):
    settings = {
        "grok_voice_streaming_enabled": True,
        "grok_voice_mode_enabled": True,
        "grok_voice_reply_channels": ["wechatcom_app"],
    }
    monkeypatch.setattr(voice_streamer, "conf", lambda: settings)

    streamer = VoiceReplyStreamer.try_create(_context(), FakeChannel(), _decision())
    try:
        assert streamer.max_chars == 180
        assert streamer.min_chars == 18
        assert streamer.idle_seconds == 1.5
    finally:
        streamer.finish(timeout=0.2)


def test_streamer_speaks_segments_before_final_text(monkeypatch, tmp_path):
    settings = {
        "grok_voice_streaming_enabled": True,
        "grok_voice_mode_enabled": True,
        "grok_voice_reply_channels": ["wechatcom_app"],
        "grok_voice_max_segment_chars": 20,
        "grok_voice_min_segment_chars": 2,
        "grok_voice_flush_idle_ms": 1000,
        "grok_voice_tts_queue_size": 4,
    }
    monkeypatch.setattr(voice_streamer, "conf", lambda: settings)

    generated = []

    def fake_tts(text):
        path = tmp_path / f"{len(generated)}.mp3"
        path.write_bytes(b"audio")
        generated.append(text)
        return str(path)

    monkeypatch.setattr(voice_streamer, "generate_xai_tts", fake_tts)
    context = _context()
    channel = FakeChannel()
    streamer = VoiceReplyStreamer.try_create(context, channel, _decision())

    streamer.handle_event({"type": "message_update", "data": {"delta": "你好。再见。"}})
    streamer.handle_event({"type": "message_end", "data": {"tool_calls": []}})

    assert generated == ["你好。", "再见。"]
    assert context["voice_stream_sent"] is True
    assert [item[0] for item in channel.sent] == [ReplyType.VOICE, ReplyType.VOICE]


def test_streamer_skips_segment_when_runtime_voice_is_disabled(monkeypatch, tmp_path):
    settings = {
        "grok_voice_streaming_enabled": True,
        "grok_voice_mode_enabled": True,
        "grok_voice_reply_channels": ["wechatcom_app"],
        "grok_voice_max_segment_chars": 20,
        "grok_voice_min_segment_chars": 2,
    }
    monkeypatch.setattr(voice_streamer, "conf", lambda: settings)

    generated = []

    def fake_tts(text):
        path = tmp_path / "voice.mp3"
        path.write_bytes(b"audio")
        generated.append(text)
        settings["grok_voice_streaming_enabled"] = False
        return str(path)

    monkeypatch.setattr(voice_streamer, "generate_xai_tts", fake_tts)
    context = _context()
    channel = FakeChannel()
    streamer = VoiceReplyStreamer.try_create(context, channel, _decision())
    try:
        streamer._speak_segment("你好")

        assert generated == ["你好"]
        assert channel.sent == []
        assert context["suppress_final_text_when_voice_stream"] is False
        assert "voice_stream_sent" not in context
    finally:
        streamer.finish(timeout=0.2)


def test_streamer_all_voice_failures_leave_final_text_fallback(monkeypatch, tmp_path):
    settings = {
        "grok_voice_streaming_enabled": True,
        "grok_voice_mode_enabled": True,
        "grok_voice_reply_channels": ["wechatcom_app"],
        "grok_voice_max_segment_chars": 20,
        "grok_voice_min_segment_chars": 4,
        "grok_voice_flush_idle_ms": 1000,
        "grok_voice_tts_queue_size": 4,
    }
    monkeypatch.setattr(voice_streamer, "conf", lambda: settings)

    def fake_tts(text):
        path = tmp_path / "voice.mp3"
        path.write_bytes(b"audio")
        return str(path)

    monkeypatch.setattr(voice_streamer, "generate_xai_tts", fake_tts)
    context = _context()
    channel = FakeChannel(voice_success=False)
    streamer = VoiceReplyStreamer.try_create(context, channel, _decision())

    streamer.handle_event({"type": "message_update", "data": {"delta": "你好。"}})
    streamer.handle_event({"type": "message_end", "data": {"tool_calls": []}})

    assert "voice_stream_sent" not in context
    assert channel.sent == [(ReplyType.VOICE, str(tmp_path / "voice.mp3"))]


def test_final_text_suppressed_after_successful_voice_stream(monkeypatch):
    context = _context()
    context["channel_type"] = "web"
    context["voice_stream_sent"] = True
    channel, sent = _send_reply_channel(monkeypatch)

    assert channel._send_reply(context, Reply(ReplyType.TEXT, "final text")) is True
    assert sent == []


def test_final_text_still_sends_when_voice_stream_never_succeeded(monkeypatch):
    context = _context()
    context["channel_type"] = "web"
    channel, sent = _send_reply_channel(monkeypatch)

    assert channel._send_reply(context, Reply(ReplyType.TEXT, "fallback text")) is True
    assert sent == [(ReplyType.TEXT, "fallback text")]
