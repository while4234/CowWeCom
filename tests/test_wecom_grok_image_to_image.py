# encoding:utf-8

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bridge.context import Context, ContextType
from channel.image_recognition import ImageRecognitionManager, reset_image_recognition_manager
from channel.wecom_bot.wecom_bot_channel import WecomBotChannel


def _singleton_class(factory):
    for cell in factory.__closure__ or []:
        value = cell.cell_contents
        if isinstance(value, type):
            return value
    raise AssertionError("singleton class not found")


def _load_cow_cli_plugin():
    from plugins import PluginManager

    manager = PluginManager()
    previous_path = manager.current_plugin_path
    manager.current_plugin_path = str(Path(__file__).resolve().parents[1] / "plugins" / "cow_cli")
    try:
        importlib.import_module("plugins.cow_cli.cow_cli")
    finally:
        manager.current_plugin_path = previous_path
    return manager.plugins["COW_CLI"]()


class CaptureManager:
    def __init__(self):
        self.submitted = []

    def submit(self, args, context, profile):
        self.submitted.append((dict(args), context, profile))
        return SimpleNamespace(job_id="job123")

    def queue_position(self, job):
        return 0


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


def _patch_conf(monkeypatch):
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "image_create_prefix": [],
        "video_create_prefix": [],
        "background_image_recognition_enabled": True,
        "image_recognition_image_create_auto_ref_window_seconds": 600,
        "image_recognition_recent_video_ref_window_seconds": 600,
    }.get(key, default)
    monkeypatch.setattr("channel.wecom_bot.wecom_bot_channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.chat_channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.chat_channel.active_backend_is_grok_for_context", lambda context: True)


def _register_recent_image(tmp_path):
    source = tmp_path / "ref.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    manager = ImageRecognitionManager(workspace_root=str(tmp_path / "recognition"), max_workers=1)
    reset_image_recognition_manager(manager)
    with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
        return manager.register_image(
            session_id="u1",
            channel_type="wecom_bot",
            image_path=str(source),
        )


def test_wecom_natural_grok_image_to_image_attaches_recent_image(monkeypatch, tmp_path):
    _patch_conf(monkeypatch)
    record = _register_recent_image(tmp_path)

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    channel.channel_type = "wecom_bot"

    context = channel._compose_context(
        ContextType.TEXT,
        "edit this image into a poster",
        msg=_wecom_msg(),
        isgroup=False,
        no_need_at=True,
    )

    assert context.type == ContextType.IMAGE_CREATE
    assert record.image_path in context.content
    reset_image_recognition_manager(None)


def test_wecom_grok_direct_image_uses_recent_image(monkeypatch, tmp_path):
    _patch_conf(monkeypatch)
    record = _register_recent_image(tmp_path)
    plugin = _load_cow_cli_plugin()
    capture = CaptureManager()
    context = Context(ContextType.TEXT, "/grok-direct image -- change it into movie poster style")
    context["channel_type"] = "wecom_bot"
    context["session_id"] = "u1"
    context["receiver"] = "u1"
    plugin._resolve_grok_direct_profile = lambda ctx: SimpleNamespace(actor_id="actor", memory_user_id="user")

    with patch("agent.tools.image_generation.job_manager.get_image_generation_job_manager", return_value=capture), patch(
        "bridge.bridge.Bridge", lambda: SimpleNamespace(get_agent_bridge=lambda: None)
    ):
        plugin._cmd_grok_direct("image -- change it into movie poster style", {"context": context})

    assert capture.submitted[0][0]["image_url"] == record.image_path
    reset_image_recognition_manager(None)


def test_wecom_grok_direct_video_still_uses_recent_image(monkeypatch, tmp_path):
    _patch_conf(monkeypatch)
    record = _register_recent_image(tmp_path)
    plugin = _load_cow_cli_plugin()
    capture = CaptureManager()
    context = Context(ContextType.TEXT, "/grok-direct video -- image to video wave")
    context["channel_type"] = "wecom_bot"
    context["session_id"] = "u1"
    context["receiver"] = "u1"
    plugin._resolve_grok_direct_profile = lambda ctx: SimpleNamespace(actor_id="actor", memory_user_id="user")

    with patch("agent.tools.video_generation.job_manager.get_grok_video_generation_job_manager", return_value=capture), patch(
        "bridge.bridge.Bridge", lambda: SimpleNamespace(get_agent_bridge=lambda: None)
    ):
        plugin._cmd_grok_direct("video -- image to video wave", {"context": context})

    assert capture.submitted[0][0]["image_url"] == record.image_path
    reset_image_recognition_manager(None)
