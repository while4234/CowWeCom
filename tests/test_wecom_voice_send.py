# encoding:utf-8

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.wecom_bot import wecom_bot_channel
from channel.wecom_bot.wecom_bot_channel import WecomBotChannel
from channel.wechatcom.wechatcomapp_channel import WechatComAppChannel


def _singleton_class(factory):
    for cell in factory.__closure__ or []:
        value = cell.cell_contents
        if isinstance(value, type):
            return value
    raise AssertionError("singleton class not found")


def test_wecom_bot_voice_uploads_voice_media(monkeypatch, tmp_path):
    source = tmp_path / "source.mp3"
    segment = tmp_path / "segment.amr"
    source.write_bytes(b"audio")
    segment.write_bytes(b"#!AMR\n123")

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    sent_payloads = []
    upload_calls = []

    monkeypatch.setattr(
        wecom_bot_channel,
        "split_audio_by_wecom_voice_limits",
        lambda path: (1000, [str(segment)]),
    )
    monkeypatch.setattr(channel, "_upload_media", lambda path, media_type: upload_calls.append((path, media_type)) or "mid")
    monkeypatch.setattr(channel, "_ws_send", lambda payload: sent_payloads.append(payload) or True)
    monkeypatch.setattr(channel, "_gen_req_id", lambda: "rid")

    assert channel._send_voice(str(source), "chat", False, req_id=None) is True
    assert upload_calls == [(str(segment), "voice")]
    assert sent_payloads[0]["body"]["msgtype"] == "voice"
    assert sent_payloads[0]["body"]["voice"]["media_id"] == "mid"


def test_wecom_bot_declares_voice_reply_supported():
    channel_cls = _singleton_class(WecomBotChannel)

    assert ReplyType.VOICE not in channel_cls.NOT_SUPPORT_REPLYTYPE


def test_wechatcom_app_voice_splits_uploads_and_sends(monkeypatch, tmp_path):
    source = tmp_path / "source.mp3"
    segment = tmp_path / "segment.amr"
    source.write_bytes(b"audio")
    segment.write_bytes(b"#!AMR\n123")

    class FakeMedia:
        def __init__(self):
            self.uploads = []

        def upload(self, media_type, handle):
            self.uploads.append((media_type, handle.name))
            return {"media_id": "mid"}

    class FakeMessage:
        def __init__(self):
            self.sent = []

        def send_voice(self, agent_id, receiver, media_id):
            self.sent.append((agent_id, receiver, media_id))

    channel_cls = _singleton_class(WechatComAppChannel)
    channel = object.__new__(channel_cls)
    channel.agent_id = "agent"
    channel.client = type("FakeClient", (), {"media": FakeMedia(), "message": FakeMessage()})()
    context = Context(ContextType.TEXT, "")
    context["receiver"] = "user"

    monkeypatch.setattr(
        "channel.wechatcom.wechatcomapp_channel.split_audio_by_wecom_voice_limits",
        lambda path: (1000, [str(segment)]),
    )

    assert channel.send(Reply(ReplyType.VOICE, str(source)), context) is True
    assert channel.client.media.uploads == [("voice", str(segment))]
    assert channel.client.message.sent == [("agent", "user", "mid")]
