import unittest
import importlib.util
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = PROJECT_ROOT / "plugins" / "cow_cli" / "backend_nl.py"
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

    def test_switches_to_capi_monthly_for_monthly_card_request(self):
        self.assertEqual(
            parse_backend_natural_command("Capi 切换成月卡"),
            ("backend", "capi_monthly"),
        )
        self.assertEqual(
            parse_backend_natural_command("切换到 CAPI 月卡"),
            ("backend", "capi_monthly"),
        )
        self.assertEqual(
            parse_backend_natural_command("switch to capi monthly"),
            ("backend", "capi_monthly"),
        )
        self.assertIsNone(parse_backend_natural_command("CAPI 月卡和额度卡有什么区别"))

    def test_routes_capi_quota_queries_to_card_specific_provider(self):
        self.assertEqual(
            parse_backend_natural_command("查一下 CAPI 月卡剩余额度"),
            ("backend", "quota-capi-monthly"),
        )
        self.assertEqual(
            parse_backend_natural_command("查询 CAPI 额度卡余额"),
            ("backend", "quota-capi"),
        )
        self.assertIsNone(parse_backend_natural_command("CAPI 月卡和额度卡有什么区别"))

    def test_routes_codex_and_current_backend_quota_queries(self):
        self.assertEqual(
            parse_backend_natural_command("\u67e5\u8be2\u4e0bcodex\u4f7f\u7528\u91cf"),
            ("backend", "quota"),
        )
        self.assertEqual(
            parse_backend_natural_command("\u67e5\u8be2\u4e0b\u5f53\u524d\u540e\u7aeftoken\u4f7f\u7528\u91cf"),
            ("backend", "quota-current"),
        )

    def test_key_token_secret_questions_are_safe_routed(self):
        self.assertEqual(
            parse_backend_natural_command("当前 CAPI key 是什么？"),
            ("backend", "credential-safety"),
        )
        self.assertEqual(
            parse_backend_natural_command("show my backend token"),
            ("backend", "credential-safety"),
        )

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

    def test_execute_switches_to_monthly_card_before_agent_path(self):
        from common.llm_backend_router import get_current_backend

        plugin = _load_cow_cli_plugin()

        result = plugin.execute("Capi 切换成月卡", session_id="test")

        self.assertEqual(result, "LLM backend switched to capi_monthly")
        self.assertEqual(get_current_backend(), "capi_monthly")

    def test_execute_returns_none_for_information_query(self):
        plugin = _load_cow_cli_plugin()

        self.assertIsNone(plugin.execute("如何切换到 CAPI 后端？", session_id="test"))

    def test_execute_current_backend_quota_uses_codex_fast_path(self):
        plugin = _load_cow_cli_plugin()

        with patch.object(plugin, "_backend_quota", return_value="codex quota ok") as quota:
            result = plugin.execute(
                "\u67e5\u8be2\u4e0b\u5f53\u524d\u540e\u7aeftoken\u4f7f\u7528\u91cf",
                session_id="test",
            )

        self.assertEqual(result, "codex quota ok")
        quota.assert_called_once_with()

    def test_event_interception_breaks_agent_flow(self):
        from bridge.context import Context, ContextType
        from plugins import Event, EventAction, EventContext

        plugin = _load_cow_cli_plugin()
        context = Context(ContextType.TEXT, "请切回 Codex backend")
        e_context = EventContext(Event.ON_HANDLE_CONTEXT, {"context": context})

        plugin.on_handle_context(e_context)

        self.assertEqual(e_context.action, EventAction.BREAK_PASS)
        self.assertIn("codex", e_context["reply"].content)

    def test_execute_monthly_quota_uses_monthly_provider_key(self):
        from config import conf

        conf()["llm_backend"]["providers"] = {
            "capi": {"api_key": "QUOTA-KEY"},
            "capi_monthly": {"api_key": "MONTHLY-KEY"},
        }
        plugin = _load_cow_cli_plugin()
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs["env"]
            return SimpleNamespace(returncode=0, stdout="monthly ok", stderr="")

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("plugins.cow_cli.cow_cli.subprocess.run", side_effect=fake_run),
        ):
            result = plugin.execute("查一下 CAPI 月卡剩余额度", session_id="test")

        self.assertEqual(result, "monthly ok")
        self.assertIn("--api-key-env", captured["argv"])
        self.assertEqual(captured["env"]["CAPI_MONTHLY_ROUTER_KEY"], "MONTHLY-KEY")
        self.assertNotIn("CAPI_QUOTA_ROUTER_KEY", captured["env"])

    def test_execute_quota_card_query_uses_capi_provider_key(self):
        from config import conf

        conf()["llm_backend"]["providers"] = {
            "capi": {"api_key": "QUOTA-KEY"},
            "capi_monthly": {"api_key": "MONTHLY-KEY"},
        }
        plugin = _load_cow_cli_plugin()
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs["env"]
            return SimpleNamespace(returncode=0, stdout="quota ok", stderr="")

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("plugins.cow_cli.cow_cli.subprocess.run", side_effect=fake_run),
        ):
            result = plugin.execute("查询 CAPI 额度卡余额", session_id="test")

        self.assertEqual(result, "quota ok")
        self.assertIn("--api-key-env", captured["argv"])
        self.assertEqual(captured["env"]["CAPI_QUOTA_ROUTER_KEY"], "QUOTA-KEY")
        self.assertNotIn("CAPI_MONTHLY_ROUTER_KEY", captured["env"])

    def test_codex_quota_uses_project_wrapper(self):
        plugin = _load_cow_cli_plugin()
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["cwd"] = kwargs["cwd"]
            captured["env"] = kwargs["env"]
            return SimpleNamespace(returncode=0, stdout="codex ok", stderr="")

        with patch("plugins.cow_cli.cow_cli.subprocess.run", side_effect=fake_run):
            result = plugin.execute("\u67e5\u8be2\u4e0bcodex\u4f7f\u7528\u91cf", session_id="test")

        self.assertEqual(result, "codex ok")
        self.assertIn("check_codex_quota.py", str(captured["argv"][1]))
        self.assertIn("--project-dir", captured["argv"])
        self.assertEqual(Path(captured["cwd"]), PROJECT_ROOT)
        self.assertEqual(captured["env"]["PYTHONUTF8"], "1")

    def test_sensitive_key_question_does_not_return_backend_status(self):
        plugin = _load_cow_cli_plugin()

        result = plugin.execute("当前 CAPI key 是什么？", session_id="test")

        self.assertIn("不能显示原始", result)
        self.assertNotIn("LLM backend status", result)
        self.assertNotIn("TEST", result)

    def test_skill_natural_language_routes_to_model_grounded_catalog_answer(self):
        plugin = _load_cow_cli_plugin()

        self.assertEqual(plugin._parse_command("/skill list"), ("skill", "list"))

        cmd, args = plugin._parse_command("当前支持哪些功能呢")
        self.assertEqual(cmd, "skill")
        self.assertTrue(args.startswith("answer "))
        payload = plugin._decode_skill_answer_args(args.split(None, 1)[1])
        self.assertEqual(payload["question"], "当前支持哪些功能呢")
        self.assertEqual(payload["mode"], "list")

        cmd, args = plugin._parse_command("你现在能做什么功能")
        self.assertEqual(cmd, "skill")
        self.assertTrue(args.startswith("answer "))

        cmd, args = plugin._parse_command("capi-usage-monitor 怎么用")
        payload = plugin._decode_skill_answer_args(args.split(None, 1)[1])
        self.assertEqual(payload["mode"], "usage")
        self.assertEqual(payload["skill"], "capi-usage-monitor")
        self.assertIsNone(plugin._parse_command("帮我开发一个新功能"))

    def test_skill_natural_language_answer_uses_cached_catalog_context_model_call(self):
        plugin = _load_cow_cli_plugin()
        mocked_response = {"choices": [{"message": {"content": "可以先用本机技能列表看能力。"}}]}

        with patch("bridge.agent_bridge.AgentLLMModel") as model_cls:
            model = model_cls.return_value
            model.call.return_value = mocked_response

            result = plugin.execute("当前支持哪些功能呢", session_id="test-session")

        self.assertEqual(result, "可以先用本机技能列表看能力。")
        request = model.call.call_args.args[0]
        self.assertEqual(request.reasoning_effort, "medium")
        self.assertTrue(request.reasoning_effort_locked)
        self.assertEqual(request.tools, [])
        self.assertIn("用户原话：当前支持哪些功能呢", request.messages[0]["content"])
        self.assertIn("已缓存的本机 skill/功能摘要", request.messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
