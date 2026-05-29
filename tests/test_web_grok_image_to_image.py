# encoding:utf-8

import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bridge.context import Context, ContextType
from channel.image_recognition import ImageRecognitionManager, reset_image_recognition_manager
from channel.web import web_channel
from plugins import Event, EventContext


def _singleton_class(factory):
    for cell in factory.__closure__ or []:
        value = cell.cell_contents
        if isinstance(value, type):
            return value
    raise AssertionError("singleton class not found")


def _load_cow_cli_plugin():
    import importlib
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


def _fake_conf(workspace):
    fake = SimpleNamespace()
    fake.get = lambda key, default=None: {
        "agent_workspace": str(workspace),
        "single_chat_prefix": [""],
        "group_chat_prefix": [],
        "image_create_prefix": [],
        "video_create_prefix": [],
        "background_image_recognition_enabled": True,
        "image_recognition_image_create_auto_ref_window_seconds": 600,
    }.get(key, default)
    fake.get_user_data = lambda user: {}
    return fake


def test_web_upload_image_registers_recent_image(monkeypatch, tmp_path):
    manager = ImageRecognitionManager(workspace_root=str(tmp_path / "recognition"), max_workers=1)
    reset_image_recognition_manager(manager)
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()

    monkeypatch.setattr(web_channel, "_get_upload_dir", lambda: str(upload_dir))
    monkeypatch.setattr(
        web_channel,
        "_raw_web_input",
        lambda: {
            "file": SimpleNamespace(filename="ref.png", file=io.BytesIO(b"\x89PNG\r\n\x1a\nimage")),
            "session_id": "web-session",
        },
    )
    with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
        channel = object.__new__(_singleton_class(web_channel.WebChannel))
        result = json.loads(channel.upload_file())

    refs = manager.recent_image_refs_for_session("web-session", limit=1, max_age_seconds=600)
    assert result["status"] == "success"
    assert result["file_type"] == "image"
    assert len(refs) == 1
    assert Path(refs[0]).exists()
    reset_image_recognition_manager(None)


def test_web_same_message_attachment_adds_image_reference(monkeypatch, tmp_path):
    fake_conf = _fake_conf(tmp_path)
    monkeypatch.setattr(web_channel, "conf", lambda: fake_conf)
    monkeypatch.setattr("channel.chat_channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.chat_channel.active_backend_is_grok_for_context", lambda context: True)
    monkeypatch.setattr(web_channel, "_apply_web_admin_context", lambda context: None)

    captured = []

    class ImmediateThread:
        def __init__(self, target, args=()):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(web_channel.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        web_channel.web,
        "data",
        lambda: json.dumps({
            "session_id": "web-session",
            "message": "edit this image into a poster",
            "stream": False,
            "attachments": [{
                "file_type": "image",
                "file_path": str(tmp_path / "ref.png"),
            }],
        }).encode("utf-8"),
    )

    channel = object.__new__(_singleton_class(web_channel.WebChannel))
    channel.channel_type = "web"
    channel.name = "bot"
    channel.user_id = "bot"
    channel.request_to_session = {}
    channel.session_queues = {}
    channel.sse_queues = {}
    channel._generate_request_id = lambda: "request"
    channel._generate_msg_id = lambda: "message"
    channel._make_sse_callback = lambda request_id: None
    channel.produce = lambda context: captured.append(context)

    response = json.loads(channel.post_message())

    assert response["status"] == "success"
    assert captured
    assert captured[0].type == ContextType.IMAGE_CREATE
    assert str(tmp_path / "ref.png") in captured[0].content


def test_web_split_grok_direct_image_uses_recent_upload(monkeypatch, tmp_path):
    manager = ImageRecognitionManager(workspace_root=str(tmp_path / "recognition"), max_workers=1)
    reset_image_recognition_manager(manager)
    source = tmp_path / "ref.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
        record = manager.register_image(
            session_id="web-session",
            channel_type="web",
            image_path=str(source),
        )

    plugin = _load_cow_cli_plugin()
    capture = CaptureManager()
    context = Context(ContextType.TEXT, "/grok-direct image -- change it into movie poster style")
    context["channel_type"] = "web"
    context["session_id"] = "web-session"
    context["receiver"] = "web-session"
    context["web_authenticated"] = True
    plugin._resolve_grok_direct_profile = lambda ctx: SimpleNamespace(actor_id="actor", memory_user_id="user")

    with patch("agent.tools.image_generation.job_manager.get_image_generation_job_manager", return_value=capture), patch(
        "bridge.bridge.Bridge", lambda: SimpleNamespace(get_agent_bridge=lambda: None)
    ):
        result = plugin._cmd_grok_direct(
            "image -- change it into movie poster style",
            EventContext(Event.ON_HANDLE_CONTEXT, {"context": context}),
        )

    assert "job123" in result
    assert capture.submitted[0][0]["image_url"] == record.image_path
    reset_image_recognition_manager(None)
