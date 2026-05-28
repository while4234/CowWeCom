# encoding:utf-8

from types import SimpleNamespace
from unittest.mock import MagicMock

from bridge.context import Context, ContextType
from bridge.reply import ReplyType
from channel.channel import Channel
from channel.chat_channel import ChatChannel
from channel.wecom_bot.wecom_bot_channel import WecomBotChannel
from models.grok.grok_bot import GrokBot


def _singleton_class(factory):
    for cell in factory.__closure__ or []:
        value = cell.cell_contents
        if isinstance(value, type):
            return value
    raise AssertionError("singleton class not found")


def test_grok_bot_video_create_returns_local_video_reply(monkeypatch, tmp_path):
    video_path = tmp_path / "grok.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x18ftypmp4")
    calls = []

    class FakeProvider:
        def generate(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return str(video_path)

    monkeypatch.setattr("models.grok.grok_video.XAIVideoGenProvider", lambda: FakeProvider())

    context = Context(ContextType.VIDEO_CREATE, "make a red kite video")
    reply = object.__new__(GrokBot).reply(context.content, context)

    assert reply.type == ReplyType.VIDEO
    assert reply.content == str(video_path)
    assert reply.cleanup_after_send is True
    assert calls[0][0] == "make a red kite video"


def test_default_agent_mode_video_create_shortcuts_to_grok_video(monkeypatch):
    context = Context(ContextType.VIDEO_CREATE, "make a cat video")
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "agent": True,
        "video_generation_provider": "xai",
    }.get(key, default)
    called = []

    class TestChannel(Channel):
        channel_type = "wecom_bot"

    def fake_fetch(query, ctx):
        called.append((query, ctx))
        return "video-reply"

    monkeypatch.setattr("channel.channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.channel.Bridge", lambda: SimpleNamespace(fetch_reply_content=fake_fetch))

    assert TestChannel().build_reply_content("make a cat video", context) == "video-reply"
    assert called == [("make a cat video", context)]


def test_non_grok_video_create_keeps_agent_mode(monkeypatch):
    context = Context(ContextType.VIDEO_CREATE, "make normally")
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "agent": True,
        "video_generation_provider": "none",
    }.get(key, default)
    called = []

    class TestChannel(Channel):
        channel_type = "web"

    def fake_agent(query, context, on_event=None, clear_history=False):
        called.append((query, context, clear_history))
        return "agent-reply"

    monkeypatch.setattr("channel.channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.channel.Bridge", lambda: SimpleNamespace(fetch_agent_reply=fake_agent))

    assert TestChannel().build_reply_content("make normally", context) == "agent-reply"
    assert called == [("make normally", context, False)]


def test_video_prefix_is_checked_before_image_prefix(monkeypatch):
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "single_chat_prefix": [""],
        "video_create_prefix": ["画个视频"],
        "image_create_prefix": ["画"],
        "nick_name_black_list": [],
        "always_reply_voice": False,
        "trigger_by_self": True,
    }.get(key, default)
    fake_conf.get_user_data.return_value = {}
    monkeypatch.setattr("channel.chat_channel.conf", lambda: fake_conf)

    channel = object.__new__(ChatChannel)
    channel.channel_type = "wecom_bot"
    channel.user_id = "bot"
    channel.name = "bot"
    msg = SimpleNamespace(
        from_user_id="u1",
        from_user_nickname="User",
        other_user_id="u1",
        other_user_nickname="User",
        to_user_id="bot",
        actual_user_id="u1",
        actual_user_nickname="User",
        is_at=False,
        at_list=[],
        self_display_name="bot",
    )

    context = ChatChannel._compose_context(
        channel,
        ContextType.TEXT,
        "画个视频 让城市动起来",
        msg=msg,
        isgroup=False,
    )

    assert context.type == ContextType.VIDEO_CREATE
    assert context.content == "让城市动起来"


def _wecom_msg():
    return SimpleNamespace(
        input_is_voice=False,
        source_msgtype="text",
        is_group=False,
        from_user_id="u1",
        from_user_nickname="User",
        other_user_id="u1",
        other_user_nickname="User",
        to_user_id="bot",
        actual_user_id="u1",
        actual_user_nickname="User",
    )


def test_wecom_bot_video_prefix_creates_video_context(monkeypatch):
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "video_create_prefix": ["生成视频"],
        "image_create_prefix": ["画图"],
    }.get(key, default)
    monkeypatch.setattr("channel.wecom_bot.wecom_bot_channel.conf", lambda: fake_conf)

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    channel.channel_type = "wecom_bot"

    context = channel._compose_context(
        ContextType.TEXT,
        "生成视频：一只猫跑步",
        msg=_wecom_msg(),
        isgroup=False,
        no_need_at=True,
    )

    assert context.type == ContextType.VIDEO_CREATE
    assert context.content == "一只猫跑步"
    assert context["receiver"] == "u1"
    assert context["session_id"] == "u1"
    assert context["_visible_task_summary"] == "一只猫跑步"


def test_wecom_bot_image_prefix_and_plain_text_still_work(monkeypatch):
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "video_create_prefix": ["生成视频"],
        "image_create_prefix": ["画图"],
    }.get(key, default)
    monkeypatch.setattr("channel.wecom_bot.wecom_bot_channel.conf", lambda: fake_conf)

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    channel.channel_type = "wecom_bot"

    image_context = channel._compose_context(
        ContextType.TEXT,
        "画图：一只猫",
        msg=_wecom_msg(),
        isgroup=False,
        no_need_at=True,
    )
    text_context = channel._compose_context(
        ContextType.TEXT,
        "普通文本",
        msg=_wecom_msg(),
        isgroup=False,
        no_need_at=True,
    )

    assert image_context.type == ContextType.IMAGE_CREATE
    assert image_context.content == "一只猫"
    assert text_context.type == ContextType.TEXT
    assert text_context.content == "普通文本"


def test_grok_video_uses_recent_image_count(monkeypatch, tmp_path):
    first = tmp_path / "a.png"
    second = tmp_path / "b.png"
    third = tmp_path / "c.png"
    for path in (first, second, third):
        path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    captured = []

    class FakeProvider:
        def generate(self, prompt, **kwargs):
            captured.append((prompt, kwargs))
            output = tmp_path / "result.mp4"
            output.write_bytes(b"\x00\x00\x00\x18ftypmp4")
            return str(output)

    monkeypatch.setattr("models.grok.grok_video.XAIVideoGenProvider", lambda: FakeProvider())
    prompt = (
        f"参考上面发的2张图片生成产品视频\n"
        f"[图片: {first}]\n[图片: {second}]\n[图片: {third}]"
    )

    reply = object.__new__(GrokBot).reply(prompt, Context(ContextType.VIDEO_CREATE, prompt))

    assert reply.type == ReplyType.VIDEO
    kwargs = captured[0][1]
    assert kwargs["image_url"] is None
    assert kwargs["reference_image_urls"] == [str(second), str(third)]


def test_grok_video_reference_request_without_image_fails():
    context = Context(ContextType.VIDEO_CREATE, "参考上面发的图片生成视频")
    reply = object.__new__(GrokBot).reply(context.content, context)

    assert reply.type == ReplyType.ERROR
    assert "没有找到可用图片" in reply.content
