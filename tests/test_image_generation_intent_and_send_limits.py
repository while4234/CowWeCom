from types import SimpleNamespace
from pathlib import Path

import pytest

from bridge.context import ContextType
from channel.wecom_bot.wecom_bot_channel import WecomBotChannel
from channel.weixin.weixin_channel import WeixinChannel
from common.image_generation_routing import (
    explicit_image_generation_requested,
    explicit_video_generation_requested,
    looks_like_media_generation_status_question,
    match_image_create_prefix,
)
from common.image_send_limits import prepare_image_for_send


def _singleton_class(factory):
    for cell in factory.__closure__ or []:
        value = cell.cell_contents
        if isinstance(value, type):
            return value
    raise AssertionError("singleton class not found")


def test_legacy_look_and_find_prefixes_do_not_trigger_image_generation():
    prefixes = ["画", "看", "找"]

    assert match_image_create_prefix("看这张图是什么\n[图片: C:\\tmp\\a.png]", prefixes) is None
    assert match_image_create_prefix("找一下这张图的来源\n[图片: C:\\tmp\\a.png]", prefixes) is None
    assert match_image_create_prefix("画面里有什么", prefixes) is None
    assert match_image_create_prefix("画一只猫", prefixes) == "画"
    assert match_image_create_prefix("画猫", prefixes) == "画"
    assert match_image_create_prefix("生成图片：一只猫", prefixes) == "生成图片"
    assert match_image_create_prefix("imagine too large", prefixes) is None


def test_explicit_image_generation_intent_excludes_image_qa():
    assert explicit_image_generation_requested("请生成图片：一只猫")
    assert explicit_image_generation_requested("draw an image of a cat")
    assert not explicit_image_generation_requested("这张图是什么")
    assert not explicit_image_generation_requested("what is in this image")
    assert looks_like_media_generation_status_question("刚才生成图片失败了吗")


def test_explicit_video_generation_intent_excludes_status_questions():
    assert explicit_video_generation_requested("请用 grok 生成视频：一只猫跑步")
    assert explicit_video_generation_requested("generate a video of a cat running")
    assert not explicit_video_generation_requested("这个视频里是什么")
    assert looks_like_media_generation_status_question("刚才生成视频失败了吗")


def test_weixin_quoted_image_question_stays_text(monkeypatch):
    fake_conf = SimpleNamespace(get=lambda key, default=None: {"image_create_prefix": ["画", "看", "找"]}.get(key, default))
    monkeypatch.setattr("channel.weixin.weixin_channel.conf", lambda: fake_conf)

    channel = object.__new__(WeixinChannel)
    channel.channel_type = "weixin"
    msg = SimpleNamespace(from_user_id="u1", other_user_id="u1")

    context = channel._compose_context(
        ContextType.TEXT,
        "看这张图是什么\n[图片: C:\\tmp\\input.png]",
        msg=msg,
    )

    assert context.type == ContextType.TEXT
    assert context.content.startswith("看这张图是什么")


def test_wecom_quoted_image_question_stays_text_but_explicit_prefix_still_generates(monkeypatch):
    fake_conf = SimpleNamespace(
        get=lambda key, default=None: {
            "image_create_prefix": ["画", "看", "找"],
            "video_create_prefix": ["生成视频"],
        }.get(key, default)
    )
    monkeypatch.setattr("channel.wecom_bot.wecom_bot_channel.conf", lambda: fake_conf)

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    channel.channel_type = "wecom_bot"
    msg = SimpleNamespace(
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

    question_context = channel._compose_context(
        ContextType.TEXT,
        "看这张图是什么\n[图片: C:\\tmp\\input.png]",
        msg=msg,
        isgroup=False,
        no_need_at=True,
    )
    create_context = channel._compose_context(
        ContextType.TEXT,
        "画图：一只猫",
        msg=msg,
        isgroup=False,
        no_need_at=True,
    )

    assert question_context.type == ContextType.TEXT
    assert create_context.type == ContextType.IMAGE_CREATE
    assert create_context.content == "一只猫"


def test_wecom_active_grok_promotes_natural_image_and_video_generation(monkeypatch):
    fake_conf = SimpleNamespace(
        get=lambda key, default=None: {
            "image_create_prefix": ["画图"],
            "video_create_prefix": ["生成视频"],
        }.get(key, default)
    )
    monkeypatch.setattr("channel.wecom_bot.wecom_bot_channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.chat_channel.active_backend_is_grok_for_context", lambda context: True)

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    channel.channel_type = "wecom_bot"
    msg = SimpleNamespace(
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

    image_context = channel._compose_context(
        ContextType.TEXT,
        "请用 grok 生成图片：一只猫",
        msg=msg,
        isgroup=False,
        no_need_at=True,
    )
    video_context = channel._compose_context(
        ContextType.TEXT,
        "请用 grok 生成视频：一只猫跑步",
        msg=msg,
        isgroup=False,
        no_need_at=True,
    )
    status_context = channel._compose_context(
        ContextType.TEXT,
        "刚才生成图片失败了吗",
        msg=msg,
        isgroup=False,
        no_need_at=True,
    )

    assert image_context.type == ContextType.IMAGE_CREATE
    assert image_context.content == "请用 grok 生成图片：一只猫"
    assert video_context.type == ContextType.VIDEO_CREATE
    assert video_context.content == "请用 grok 生成视频：一只猫跑步"
    assert status_context.type == ContextType.TEXT


def test_prepare_image_for_send_enforces_dimensions(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    source = tmp_path / "wide.jpg"
    Image.new("RGB", (3000, 1000), (20, 120, 200)).save(source, "JPEG", quality=95)

    prepared = prepare_image_for_send(
        str(source),
        max_bytes=2 * 1024 * 1024,
        max_width=512,
        max_height=512,
        prefix="test_send",
    )

    assert prepared
    assert prepared != str(source)
    with Image.open(prepared) as img:
        assert img.width <= 512
        assert img.height <= 512
    Path(prepared).unlink(missing_ok=True)


def test_prepare_image_for_send_uses_system_tempdir(monkeypatch, tmp_path):
    Image = pytest.importorskip("PIL.Image")
    source = tmp_path / "large.png"
    temp_dir = tmp_path / "system-temp"
    temp_dir.mkdir()
    Image.new("RGB", (1200, 900), (220, 80, 30)).save(source, "PNG")
    monkeypatch.setattr("common.image_send_limits.tempfile.gettempdir", lambda: str(temp_dir))

    prepared = prepare_image_for_send(
        str(source),
        max_bytes=128 * 1024,
        max_width=320,
        max_height=320,
        prefix="test_send",
    )

    assert prepared
    assert Path(prepared).parent == temp_dir
    Path(prepared).unlink(missing_ok=True)
