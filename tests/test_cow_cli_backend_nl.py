import unittest
import importlib.util
import tempfile
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / "plugins" / "cow_cli" / "backend_nl.py"
_SPEC = importlib.util.spec_from_file_location("cow_cli_backend_nl", _MODULE_PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
parse_backend_natural_command = _MODULE.parse_backend_natural_command


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


class TestCowCliBackendNaturalLanguage(unittest.TestCase):
    def test_switches_to_capi_for_explicit_request(self):
        self.assertEqual(
            parse_backend_natural_command("帮我切换到 CAPI 后端"),
            ("backend", "capi"),
        )
        self.assertEqual(
            parse_backend_natural_command("现在直接用capi回复"),
            ("backend", "capi"),
        )
        self.assertIsNone(parse_backend_natural_command("用 CAPI 写一个调用示例"))

    def test_switches_to_codex_for_explicit_request(self):
        self.assertEqual(
            parse_backend_natural_command("请切回 Codex backend"),
            ("backend", "codex"),
        )

    def test_status_request_does_not_need_slash_command(self):
        self.assertEqual(
            parse_backend_natural_command("现在后端状态是什么"),
            ("backend", "status"),
        )
        self.assertEqual(
            parse_backend_natural_command("当前是 CAPI 吗"),
            ("backend", "status"),
        )

    def test_informational_questions_do_not_switch(self):
        self.assertIsNone(parse_backend_natural_command("如何切换到 CAPI 后端？"))
        self.assertIsNone(parse_backend_natural_command("CAPI 和 Codex 有什么区别"))
        self.assertIsNone(parse_backend_natural_command("使用 CAPI 有什么方法"))
        self.assertIsNone(parse_backend_natural_command("切换到 CAPI 是否可行"))
        self.assertIsNone(parse_backend_natural_command("用 CAPI 有什么好处"))

    def test_negative_requests_do_not_switch(self):
        self.assertIsNone(parse_backend_natural_command("先别切换到 CAPI"))
        self.assertIsNone(parse_backend_natural_command("不要改用 Codex"))

    def test_auto_reset_alias(self):
        self.assertEqual(
            parse_backend_natural_command("帮我重置后端自动切换"),
            ("backend", "auto reset"),
        )


class TestCowCliBackendNaturalLanguageDispatch(unittest.TestCase):
    def setUp(self):
        from config import conf

        self.tmp = tempfile.TemporaryDirectory()
        self.previous_backend_config = conf().get("llm_backend")
        conf()["llm_backend"] = {
            "current_backend": "codex",
            "state_path": str(Path(self.tmp.name) / "state.json"),
            "providers": {"codex": {"model": "gpt-5.5"}},
        }

    def tearDown(self):
        from config import conf

        if self.previous_backend_config is None:
            conf().pop("llm_backend", None)
        else:
            conf()["llm_backend"] = self.previous_backend_config
        self.tmp.cleanup()

    def test_execute_switches_before_agent_path(self):
        from common.llm_backend_router import get_current_backend

        plugin = _load_cow_cli_plugin()

        result = plugin.execute("帮我切换到 CAPI 后端", session_id="test")

        self.assertEqual(result, "LLM backend switched to capi")
        self.assertEqual(get_current_backend(), "capi")

    def test_execute_returns_none_for_information_query(self):
        plugin = _load_cow_cli_plugin()

        self.assertIsNone(plugin.execute("如何切换到 CAPI 后端？", session_id="test"))

    def test_event_interception_breaks_agent_flow(self):
        from bridge.context import Context, ContextType
        from plugins import Event, EventAction, EventContext

        plugin = _load_cow_cli_plugin()
        context = Context(ContextType.TEXT, "请切回 Codex backend")
        e_context = EventContext(Event.ON_HANDLE_CONTEXT, {"context": context})

        plugin.on_handle_context(e_context)

        self.assertEqual(e_context.action, EventAction.BREAK_PASS)
        self.assertIn("codex", e_context["reply"].content)


if __name__ == "__main__":
    unittest.main()
