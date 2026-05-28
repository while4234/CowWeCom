# encoding:utf-8

from bridge.context import Context, ContextType
from bridge.reply import ReplyType
from channel import voice_streamer
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


def _decision(effort="low", rule="low_greeting"):
    return {
        "selected_effort": effort,
        "source": "local",
        "local_rule": rule,
        "input_is_voice": True,
        "channel": "wechatcom_app",
        "session_id": "s1",
    }


def test_voice_stream_requires_voice_low_local_rule(monkeypatch):
    settings = {
        "grok_voice_streaming_enabled": True,
        "grok_voice_mode_enabled": True,
        "grok_voice_reply_channels": ["wechatcom_app"],
    }
    monkeypatch.setattr(voice_streamer, "conf", lambda: settings)

    assert voice_stream_enabled(_context(), FakeChannel(), _decision()) is True
    assert voice_stream_enabled(_context(), FakeChannel(), _decision(effort="medium")) is False

    text_context = _context()
    text_context["input_is_voice"] = False
    assert voice_stream_enabled(text_context, FakeChannel(), _decision()) is False


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
