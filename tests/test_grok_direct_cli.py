import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bridge.context import Context, ContextType
from channel.image_recognition import ImageRecognitionManager, reset_image_recognition_manager
from plugins import Event, EventAction, EventContext


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


def _context(content):
    context = Context(ContextType.TEXT, content)
    context["channel_type"] = "web"
    context["receiver"] = "session"
    context["session_id"] = "session"
    context["request_id"] = "request"
    context["web_authenticated"] = True
    return context


def test_grok_direct_help_is_admin_only():
    plugin = _load_cow_cli_plugin()

    assert plugin._command_access_level("grok-direct") == "admin"
    assert not any("grok-direct" in line for line in plugin._help_sections(False))
    assert any("grok-direct" in line for line in plugin._help_sections(True))


def test_grok_direct_rejects_normal_user():
    plugin = _load_cow_cli_plugin()
    context = Context(ContextType.TEXT, "/grok-direct image -- 一只猫", kwargs={"actor_role": "user"})
    e_context = EventContext(Event.ON_HANDLE_CONTEXT, {"context": context})

    plugin.on_handle_context(e_context)

    assert e_context.action == EventAction.BREAK_PASS
    assert "需要管理员权限" in e_context["reply"].content


def test_grok_direct_image_defaults_to_grok_speed_runtime():
    plugin = _load_cow_cli_plugin()
    manager = CaptureManager()
    context = _context("/grok-direct image -- 一只穿宇航服的橘猫")
    plugin._resolve_grok_direct_profile = lambda ctx: SimpleNamespace(actor_id="actor", memory_user_id="user")

    with patch("agent.tools.image_generation.job_manager.get_image_generation_job_manager", return_value=manager), patch(
        "bridge.bridge.Bridge", lambda: SimpleNamespace(get_agent_bridge=lambda: None)
    ):
        result = plugin._cmd_grok_direct("image -- 一只穿宇航服的橘猫", {"context": context})

    args = manager.submitted[0][0]
    assert "任务 job123" in result
    assert args["prompt"] == "一只穿宇航服的橘猫"
    assert args["runtime"] == "grok"
    assert args["quality"] == "speed"
    assert args["prompt_enhancement"] is False


def test_grok_direct_video_defaults_and_uses_context_image_refs():
    plugin = _load_cow_cli_plugin()
    manager = CaptureManager()
    content = "/grok-direct video -- 参考上面的图片生成视频\n[图片: C:/tmp/old.png]\n[图片: C:/tmp/ref.png]"
    context = _context(content)
    plugin._resolve_grok_direct_profile = lambda ctx: SimpleNamespace(actor_id="actor", memory_user_id="user")

    with patch("agent.tools.video_generation.job_manager.get_grok_video_generation_job_manager", return_value=manager), patch(
        "bridge.bridge.Bridge", lambda: SimpleNamespace(get_agent_bridge=lambda: None)
    ):
        result = plugin._cmd_grok_direct("video -- 参考上面的图片生成视频\n[图片: C:/tmp/old.png]\n[图片: C:/tmp/ref.png]", {"context": context})

    args = manager.submitted[0][0]
    assert "任务 job123" in result
    assert args["prompt"] == "参考上面的图片生成视频"
    assert args["resolution"] == "480p"
    assert "aspect_ratio" not in args
    assert args["duration"] == "6s"
    assert args["image_url"] == "C:/tmp/ref.png"


def test_grok_direct_video_text_only_keeps_default_aspect_ratio():
    plugin = _load_cow_cli_plugin()
    manager = CaptureManager()
    context = _context("/grok-direct video -- 城市天际线延时摄影")
    plugin._resolve_grok_direct_profile = lambda ctx: SimpleNamespace(actor_id="actor", memory_user_id="user")

    with patch("agent.tools.video_generation.job_manager.get_grok_video_generation_job_manager", return_value=manager), patch(
        "bridge.bridge.Bridge", lambda: SimpleNamespace(get_agent_bridge=lambda: None)
    ):
        plugin._cmd_grok_direct("video -- 城市天际线延时摄影", {"context": context})

    args = manager.submitted[0][0]
    assert args["aspect_ratio"] == "16:9"


def test_grok_direct_video_without_count_uses_latest_context_ref_even_without_hint():
    plugin = _load_cow_cli_plugin()
    manager = CaptureManager()
    content = "/grok-direct video -- 背景换成火星\n[图片: C:/tmp/old.png]\n[图片: C:/tmp/ref.png]"
    context = _context(content)
    plugin._resolve_grok_direct_profile = lambda ctx: SimpleNamespace(actor_id="actor", memory_user_id="user")

    with patch("agent.tools.video_generation.job_manager.get_grok_video_generation_job_manager", return_value=manager), patch(
        "bridge.bridge.Bridge", lambda: SimpleNamespace(get_agent_bridge=lambda: None)
    ):
        plugin._cmd_grok_direct("video -- 背景换成火星\n[图片: C:/tmp/old.png]\n[图片: C:/tmp/ref.png]", {"context": context})

    args = manager.submitted[0][0]
    assert args["image_url"] == "C:/tmp/ref.png"


def test_grok_direct_video_falls_back_to_recent_image_refs(tmp_path):
    plugin = _load_cow_cli_plugin()
    manager = CaptureManager()
    context = _context("/grok-direct video -- image to video wave")
    plugin._resolve_grok_direct_profile = lambda ctx: SimpleNamespace(actor_id="actor", memory_user_id="user")

    workspace = tmp_path / "workspace"
    source = tmp_path / "ref.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    image_manager = ImageRecognitionManager(workspace_root=str(workspace), max_workers=1)
    reset_image_recognition_manager(image_manager)
    with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
        record = image_manager.register_image(
            session_id="session",
            channel_type="web",
            image_path=str(source),
        )

    with patch("agent.tools.video_generation.job_manager.get_grok_video_generation_job_manager", return_value=manager), patch(
        "bridge.bridge.Bridge", lambda: SimpleNamespace(get_agent_bridge=lambda: None)
    ):
        result = plugin._cmd_grok_direct("video -- image to video wave", {"context": context})

    args = manager.submitted[0][0]
    assert "job123" in result
    assert args["image_url"] == record.image_path
    reset_image_recognition_manager(None)


def test_grok_direct_video_uses_requested_recent_image_count(tmp_path):
    plugin = _load_cow_cli_plugin()
    manager = CaptureManager()
    context = _context("/grok-direct video -- 参考上面2张图片生成产品视频")
    plugin._resolve_grok_direct_profile = lambda ctx: SimpleNamespace(actor_id="actor", memory_user_id="user")

    workspace = tmp_path / "workspace"
    image_manager = ImageRecognitionManager(workspace_root=str(workspace), max_workers=1)
    reset_image_recognition_manager(image_manager)
    records = []
    with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
        for name in ("first.png", "second.png", "third.png"):
            source = tmp_path / name
            source.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode("ascii"))
            records.append(
                image_manager.register_image(
                    session_id="session",
                    channel_type="web",
                    image_path=str(source),
                )
            )

    with patch("agent.tools.video_generation.job_manager.get_grok_video_generation_job_manager", return_value=manager), patch(
        "bridge.bridge.Bridge", lambda: SimpleNamespace(get_agent_bridge=lambda: None)
    ):
        plugin._cmd_grok_direct("video -- 参考上面2张图片生成产品视频", {"context": context})

    args = manager.submitted[0][0]
    assert args["image_url"] == [records[1].image_path, records[2].image_path]
    reset_image_recognition_manager(None)


def test_grok_direct_video_defaults_to_latest_recent_image(tmp_path):
    plugin = _load_cow_cli_plugin()
    manager = CaptureManager()
    context = _context("/grok-direct video -- 参考上面的图片生成产品视频")
    plugin._resolve_grok_direct_profile = lambda ctx: SimpleNamespace(actor_id="actor", memory_user_id="user")

    workspace = tmp_path / "workspace"
    image_manager = ImageRecognitionManager(workspace_root=str(workspace), max_workers=1)
    reset_image_recognition_manager(image_manager)
    records = []
    with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
        for name in ("first.png", "second.png"):
            source = tmp_path / name
            source.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode("ascii"))
            records.append(
                image_manager.register_image(
                    session_id="session",
                    channel_type="web",
                    image_path=str(source),
                )
            )

    with patch("agent.tools.video_generation.job_manager.get_grok_video_generation_job_manager", return_value=manager), patch(
        "bridge.bridge.Bridge", lambda: SimpleNamespace(get_agent_bridge=lambda: None)
    ):
        plugin._cmd_grok_direct("video -- 参考上面的图片生成产品视频", {"context": context})

    args = manager.submitted[0][0]
    assert args["image_url"] == records[1].image_path
    assert "aspect_ratio" not in args
    reset_image_recognition_manager(None)


def test_grok_direct_video_without_hint_uses_latest_recent_image(tmp_path):
    plugin = _load_cow_cli_plugin()
    manager = CaptureManager()
    context = _context("/grok-direct video -- 10s 让镜头轻微推进")
    plugin._resolve_grok_direct_profile = lambda ctx: SimpleNamespace(actor_id="actor", memory_user_id="user")

    workspace = tmp_path / "workspace"
    image_manager = ImageRecognitionManager(workspace_root=str(workspace), max_workers=1)
    reset_image_recognition_manager(image_manager)
    records = []
    with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
        for name in ("first.png", "second.png"):
            source = tmp_path / name
            source.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode("ascii"))
            records.append(
                image_manager.register_image(
                    session_id="session",
                    channel_type="web",
                    image_path=str(source),
                )
            )

    with patch("agent.tools.video_generation.job_manager.get_grok_video_generation_job_manager", return_value=manager), patch(
        "bridge.bridge.Bridge", lambda: SimpleNamespace(get_agent_bridge=lambda: None)
    ):
        plugin._cmd_grok_direct("video -- 10s 让镜头轻微推进", {"context": context})

    args = manager.submitted[0][0]
    assert args["image_url"] == records[1].image_path
    assert "aspect_ratio" not in args
    reset_image_recognition_manager(None)


def test_grok_direct_video_text_to_video_opt_out_ignores_recent_image(tmp_path):
    plugin = _load_cow_cli_plugin()
    manager = CaptureManager()
    context = _context("/grok-direct video -- 文生视频，一只猫在月球奔跑")
    plugin._resolve_grok_direct_profile = lambda ctx: SimpleNamespace(actor_id="actor", memory_user_id="user")

    workspace = tmp_path / "workspace"
    image_manager = ImageRecognitionManager(workspace_root=str(workspace), max_workers=1)
    reset_image_recognition_manager(image_manager)
    source = tmp_path / "ref.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
        image_manager.register_image(
            session_id="session",
            channel_type="web",
            image_path=str(source),
        )

    with patch("agent.tools.video_generation.job_manager.get_grok_video_generation_job_manager", return_value=manager), patch(
        "bridge.bridge.Bridge", lambda: SimpleNamespace(get_agent_bridge=lambda: None)
    ):
        plugin._cmd_grok_direct("video -- 文生视频，一只猫在月球奔跑", {"context": context})

    args = manager.submitted[0][0]
    assert "image_url" not in args
    assert args["aspect_ratio"] == "16:9"
    reset_image_recognition_manager(None)


def test_grok_direct_image_rejects_reference_image():
    plugin = _load_cow_cli_plugin()
    context = _context("/grok-direct image -- 参考这张图画海报\n[图片: C:/tmp/ref.png]")
    plugin._resolve_grok_direct_profile = lambda ctx: SimpleNamespace(actor_id="actor", memory_user_id="user")

    result = plugin._cmd_grok_direct("image -- 参考这张图画海报\n[图片: C:/tmp/ref.png]", {"context": context})

    assert "只支持文生图" in result


def test_slash_command_suggestions_include_admin_web_commands():
    plugin = _load_cow_cli_plugin()

    commands = {item["cmd"] for item in plugin.slash_command_suggestions(is_admin=True)}

    assert "/grok-direct image -- 一只穿宇航服的橘猫，电影海报风格" in commands
    assert "/grok-direct video -- 城市天际线延时摄影" in commands
    assert "/backend grok" in commands
    assert "/voice on" in commands
    assert "/memory rebuild-index" in commands
    assert "/knowledge off" in commands
    assert "/install-browser" in commands
    assert "/start" not in commands
    assert "/stop" not in commands
    assert "/restart" not in commands


def test_module_slash_command_suggestions_uses_registered_plugin():
    _load_cow_cli_plugin()
    from plugins.cow_cli import cow_cli

    commands = {item["cmd"] for item in cow_cli.slash_command_suggestions(is_admin=True)}

    assert "/grok-direct image -- 一只穿宇航服的橘猫，电影海报风格" in commands
