# encoding:utf-8

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from bridge.context import Context, ContextType
from channel.chat_channel import ChatChannel
from channel.discord.discord_channel import DiscordChannel
from common.grok_image_prompt_rewriter import RANDOM_PROMPT_MODE_IMAGE_TO_IMAGE


def _singleton_class(factory):
    for cell in factory.__closure__ or []:
        value = cell.cell_contents
        if isinstance(value, type):
            return value
    raise AssertionError("singleton class not found")


def _discord_channel():
    cls = _singleton_class(DiscordChannel)
    channel = object.__new__(cls)
    channel.channel_type = "discord"
    return channel


def _message(content, attachments=None):
    return SimpleNamespace(
        id="m1",
        content=content,
        author=SimpleNamespace(id="100", display_name="Admin", name="admin"),
        channel=SimpleNamespace(id="300", name="bot"),
        guild=SimpleNamespace(id="200"),
        attachments=attachments or [],
    )


def _patch_conf(monkeypatch):
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "video_create_prefix": ["生成视频"],
        "image_create_prefix": ["生图"],
        "background_image_recognition_enabled": True,
        "image_recognition_recent_video_ref_window_seconds": 600,
        "image_recognition_image_create_auto_ref_window_seconds": 600,
    }.get(key, default)
    monkeypatch.setattr("channel.discord.discord_channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.chat_channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.chat_channel.active_backend_is_grok_for_context", lambda context: True)


def test_discord_message_video_prefix_creates_video_context(monkeypatch):
    _patch_conf(monkeypatch)
    channel = _discord_channel()
    context = channel._build_message_context(_message("生成视频 一只猫跑步"), ContextType.TEXT, "生成视频 一只猫跑步")

    channel._promote_grok_media_context_for_message(context)

    assert context.type == ContextType.VIDEO_CREATE
    assert context.content == "一只猫跑步"


def test_discord_message_image_prefix_creates_image_context(monkeypatch):
    _patch_conf(monkeypatch)
    channel = _discord_channel()
    context = channel._build_message_context(_message("生图 一只猫"), ContextType.TEXT, "生图 一只猫")

    channel._promote_grok_media_context_for_message(context)

    assert context.type == ContextType.IMAGE_CREATE
    assert context.content == "一只猫"


def test_discord_random_image_prompt_request_stays_text(monkeypatch):
    _patch_conf(monkeypatch)
    channel = _discord_channel()
    context = channel._build_message_context(
        _message("随机给我个NSFW图生图提示词"),
        ContextType.TEXT,
        "随机给我个NSFW图生图提示词",
    )

    channel._promote_grok_media_context_for_message(context)

    assert context.type == ContextType.TEXT
    assert context.content == "随机给我个NSFW图生图提示词"


def test_random_image_prompt_text_reply_uses_deterministic_formatter(monkeypatch):
    monkeypatch.setattr(
        "common.grok_image_prompt_rewriter.build_grok_random_image_prompt",
        lambda prompt: {
            "prompt_mode": RANDOM_PROMPT_MODE_IMAGE_TO_IMAGE,
            "enhanced_prompt": "reference-preserving prompt",
            "chinese_prompt": "保留参考图的提示词",
        },
    )

    reply = ChatChannel._generate_random_grok_prompt_reply(
        Context(ContextType.TEXT, "随机给我个NSFW图生图提示词")
    )

    assert reply.content.startswith("随机图生图提示词：")
    assert "English Prompt:\nreference-preserving prompt" in reply.content
    assert "中文翻译：\n保留参考图的提示词" in reply.content


def test_discord_message_with_image_attachment_keeps_inline_image_ref_for_video(monkeypatch, tmp_path):
    _patch_conf(monkeypatch)
    channel = _discord_channel()
    image_path = str(tmp_path / "ref.png")
    context = channel._build_message_context(
        _message("生成视频 让它动起来"),
        ContextType.TEXT,
        f"生成视频 让它动起来\n[image: {image_path}]",
    )

    channel._promote_grok_media_context_for_message(context)

    assert context.type == ContextType.VIDEO_CREATE
    assert context.content == f"让它动起来\n[image: {image_path}]"


def test_discord_handle_message_promotes_text_and_preserves_attachment_ref(monkeypatch, tmp_path):
    _patch_conf(monkeypatch)
    channel = _discord_channel()
    channel.admin_user_id = "100"
    channel.guild_id = "200"
    channel.allowed_channel_ids = {"300"}
    captured = {}

    class Attachment:
        filename = "ref.png"
        content_type = "image/png"

        async def save(self, path):
            captured["saved_path"] = path

    async def save_attachment(_attachment, _bucket_id):
        return str(tmp_path / "ref.png")

    def produce(context):
        captured["context"] = context

    channel._save_interaction_image_attachment = save_attachment
    channel.produce = produce

    asyncio.run(channel._handle_message(_message("生成视频 让它动起来", [Attachment()])))

    context = captured["context"]
    assert context.type == ContextType.VIDEO_CREATE
    assert "[image:" in context.content
    assert str(tmp_path / "ref.png") in context.content
