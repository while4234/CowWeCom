# encoding:utf-8

from bridge.context import ContextType
from channel.wecom_bot.wecom_bot_channel import WecomBotChannel
from channel.wecom_bot.wecom_bot_message import WecomBotMessage


def _singleton_class(factory):
    for cell in factory.__closure__ or []:
        value = cell.cell_contents
        if isinstance(value, type):
            return value
    raise AssertionError("singleton class not found")


def _voice_body():
    return {
        "msgid": "m1",
        "msgtype": "voice",
        "voice": {"content": "你好"},
        "from": {"userid": "u1", "name": "User"},
        "aibotid": "bot",
        "chatid": "u1",
    }


def test_wecom_bot_message_keeps_voice_origin_marker():
    msg = WecomBotMessage(_voice_body(), is_group=False)

    assert msg.ctype == ContextType.TEXT
    assert msg.content == "你好"
    assert msg.input_is_voice is True
    assert msg.source_msgtype == "voice"
    assert msg.origin_ctype == ContextType.VOICE


def test_wecom_bot_context_preserves_voice_origin(monkeypatch):
    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    channel.channel_type = "wecom_bot"
    msg = WecomBotMessage(_voice_body(), is_group=False)

    context = channel_cls._compose_context(
        channel,
        msg.ctype,
        msg.content,
        msg=msg,
        isgroup=False,
        no_need_at=True,
    )

    assert context["input_is_voice"] is True
    assert context["source_msgtype"] == "voice"
    assert context["origin_ctype"] == ContextType.VOICE
    assert msg.origin_ctype == ContextType.VOICE
    assert context.type == ContextType.TEXT
