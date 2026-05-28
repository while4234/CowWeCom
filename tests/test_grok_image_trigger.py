# encoding:utf-8

from types import SimpleNamespace
from unittest.mock import MagicMock

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.channel import Channel
from channel.wecom_bot.wecom_bot_channel import WecomBotChannel
from integrations.hermes_xai.media_download import new_generated_media_path
from models.grok.grok_bot import GrokBot


def _singleton_class(factory):
    for cell in factory.__closure__ or []:
        value = cell.cell_contents
        if isinstance(value, type):
            return value
    raise AssertionError("singleton class not found")


def test_grok_bot_image_create_returns_local_image_reply(monkeypatch, tmp_path):
    image_path = tmp_path / "grok.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")

    class FakeProvider:
        def generate(self, prompt):
            assert prompt == "draw a red kite"
            return str(image_path)

    monkeypatch.setattr("models.grok.grok_image.XAIImageGenProvider", lambda: FakeProvider())

    context = Context(ContextType.IMAGE_CREATE, "draw a red kite")
    reply = object.__new__(GrokBot).reply(context.content, context)

    assert reply.type == ReplyType.IMAGE
    assert reply.content == str(image_path)
    assert not reply.content.startswith(("http://", "https://"))
    assert reply.cleanup_after_send is True


def test_default_agent_mode_image_create_shortcuts_to_grok_image(monkeypatch):
    context = Context(ContextType.IMAGE_CREATE, "draw a cat")
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "agent": True,
        "text_to_image": "xai",
    }.get(key, default)
    called = []

    class TestChannel(Channel):
        channel_type = "wecom_bot"

    def fake_fetch(query, ctx):
        called.append((query, ctx))
        return "image-reply"

    monkeypatch.setattr("channel.channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.channel.Bridge", lambda: SimpleNamespace(fetch_reply_content=fake_fetch))

    assert TestChannel().build_reply_content("draw a cat", context) == "image-reply"
    assert called == [("draw a cat", context)]


def test_non_grok_image_create_keeps_agent_mode(monkeypatch):
    context = Context(ContextType.IMAGE_CREATE, "draw normally")
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "agent": True,
        "text_to_image": "dall-e-2",
    }.get(key, default)
    called = []

    class TestChannel(Channel):
        channel_type = "web"

    def fake_agent(query, context, on_event=None, clear_history=False):
        called.append((query, context, clear_history))
        return "agent-reply"

    monkeypatch.setattr("channel.channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.channel.Bridge", lambda: SimpleNamespace(fetch_agent_reply=fake_agent))

    assert TestChannel().build_reply_content("draw normally", context) == "agent-reply"
    assert called == [("draw normally", context, False)]


def test_wecom_bot_declares_image_reply_supported():
    channel_cls = _singleton_class(WecomBotChannel)

    assert ReplyType.IMAGE not in channel_cls.NOT_SUPPORT_REPLYTYPE


def test_wecom_bot_sends_local_image_reply(monkeypatch, tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage")

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    sent_payloads = []
    upload_calls = []

    monkeypatch.setattr(channel, "_ensure_image_format", lambda path: path)
    monkeypatch.setattr(channel, "_upload_media", lambda path, media_type: upload_calls.append((path, media_type)) or "mid")
    monkeypatch.setattr(channel, "_ws_send", lambda payload: sent_payloads.append(payload) or True)

    assert channel._send_image(str(source), "chat", False, req_id="req") is True
    assert upload_calls == [(str(source), "image")]
    assert sent_payloads[0]["body"]["msgtype"] == "image"
    assert sent_payloads[0]["body"]["image"]["media_id"] == "mid"


def _wecom_context():
    context = Context(ContextType.TEXT, "")
    context["receiver"] = "chat"
    context["isgroup"] = False
    return context


def test_wecom_bot_generated_image_is_deleted_after_success(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    source = new_generated_media_path("xai_test", ".png")
    tmp_path.__class__(source).write_bytes(b"\x89PNG\r\n\x1a\nimage")

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    reply = Reply(ReplyType.IMAGE, source)
    reply.cleanup_after_send = True

    monkeypatch.setattr(channel, "_reply_mention_target", lambda context: ([], []))
    monkeypatch.setattr(channel, "_ensure_image_format", lambda path: path)
    monkeypatch.setattr(channel, "_upload_media", lambda path, media_type: "mid")
    monkeypatch.setattr(channel, "_ws_send", lambda payload: True)
    monkeypatch.setattr(channel, "_gen_req_id", lambda: "rid")

    assert channel.send(reply, _wecom_context()) is True
    assert not tmp_path.__class__(source).exists()


def test_wecom_bot_generated_image_is_deleted_after_upload_failure(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    source = new_generated_media_path("xai_test", ".png")
    tmp_path.__class__(source).write_bytes(b"\x89PNG\r\n\x1a\nimage")

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    reply = Reply(ReplyType.IMAGE, source)
    reply.cleanup_after_send = True

    monkeypatch.setattr(channel, "_reply_mention_target", lambda context: ([], []))
    monkeypatch.setattr(channel, "_ensure_image_format", lambda path: path)
    monkeypatch.setattr(channel, "_upload_media", lambda path, media_type: "")
    monkeypatch.setattr(channel, "_send_text", lambda *args, **kwargs: True)

    assert channel.send(reply, _wecom_context()) is False
    assert not tmp_path.__class__(source).exists()
