# encoding:utf-8

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from bridge.context import ContextType
from channel.image_recognition import ImageRecognitionManager, ImageRecognitionRecord, reset_image_recognition_manager
from channel.weixin.weixin_channel import WeixinChannel


def _weixin_channel():
    channel = object.__new__(WeixinChannel)
    channel.channel_type = "weixin"
    return channel


def _weixin_msg():
    return SimpleNamespace(
        from_user_id="u1",
        from_user_nickname="User",
        other_user_id="u1",
        other_user_nickname="User",
    )


def _patch_conf(monkeypatch, *, background=True):
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "video_create_prefix": ["生成视频"],
        "image_create_prefix": ["生图"],
        "background_image_recognition_enabled": background,
        "image_recognition_recent_video_ref_window_seconds": 600,
        "image_recognition_image_create_auto_ref_window_seconds": 600,
    }.get(key, default)
    monkeypatch.setattr("channel.weixin.weixin_channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.chat_channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.chat_channel.active_backend_is_grok_for_context", lambda context: True)
    return fake_conf


def _register_recent_image(tmp_path):
    source = tmp_path / "ref.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    manager = ImageRecognitionManager(workspace_root=str(tmp_path / "recognition"), max_workers=1)
    reset_image_recognition_manager(manager)
    now = time.time()
    record = ImageRecognitionRecord(
        record_id="record1",
        session_id="u1",
        channel_type="weixin",
        image_hash="hash1",
        image_path=str(source),
        is_group=False,
        status="done",
        result="summary",
        created_at=now,
        updated_at=now,
        completed_at=now,
    )
    manager._records[record.record_id] = record
    manager._latest_by_session[record.session_id] = record.record_id
    return record


def test_weixin_video_prefix_creates_video_context(monkeypatch):
    _patch_conf(monkeypatch, background=False)

    context = _weixin_channel()._compose_context(
        ContextType.TEXT,
        "生成视频 一只猫在月球奔跑",
        msg=_weixin_msg(),
    )

    assert context.type == ContextType.VIDEO_CREATE
    assert context.content == "一只猫在月球奔跑"


def test_weixin_image_prefix_still_creates_image_context(monkeypatch):
    _patch_conf(monkeypatch, background=False)

    context = _weixin_channel()._compose_context(
        ContextType.TEXT,
        "生图 一只猫",
        msg=_weixin_msg(),
    )

    assert context.type == ContextType.IMAGE_CREATE
    assert context.content == "一只猫"


def test_weixin_image_to_video_followup_attaches_recent_image(monkeypatch, tmp_path):
    _patch_conf(monkeypatch)
    record = _register_recent_image(tmp_path)

    context = _weixin_channel()._compose_context(
        ContextType.TEXT,
        "让上图动起来",
        msg=_weixin_msg(),
    )

    assert context.type == ContextType.VIDEO_CREATE
    assert record.image_path in context.content
    assert "[image:" in context.content
    reset_image_recognition_manager(None)
