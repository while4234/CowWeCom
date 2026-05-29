import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bridge.context import Context, ContextType
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
    content = "/grok-direct video -- 让这张图动起来\n[图片: C:/tmp/ref.png]"
    context = _context(content)
    plugin._resolve_grok_direct_profile = lambda ctx: SimpleNamespace(actor_id="actor", memory_user_id="user")

    with patch("agent.tools.video_generation.job_manager.get_grok_video_generation_job_manager", return_value=manager), patch(
        "bridge.bridge.Bridge", lambda: SimpleNamespace(get_agent_bridge=lambda: None)
    ):
        result = plugin._cmd_grok_direct("video -- 让这张图动起来\n[图片: C:/tmp/ref.png]", {"context": context})

    args = manager.submitted[0][0]
    assert "任务 job123" in result
    assert args["prompt"] == "让这张图动起来"
    assert args["resolution"] == "480p"
    assert args["aspect_ratio"] == "16:9"
    assert args["duration"] == "6s"
    assert args["image_url"] == "C:/tmp/ref.png"


def test_grok_direct_image_rejects_reference_image():
    plugin = _load_cow_cli_plugin()
    context = _context("/grok-direct image -- 参考这张图画海报\n[图片: C:/tmp/ref.png]")
    plugin._resolve_grok_direct_profile = lambda ctx: SimpleNamespace(actor_id="actor", memory_user_id="user")

    result = plugin._cmd_grok_direct("image -- 参考这张图画海报\n[图片: C:/tmp/ref.png]", {"context": context})

    assert "只支持文生图" in result
