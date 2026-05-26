import json
import os
import unittest
import importlib.util
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


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
        self.assertEqual(
            parse_backend_natural_command("帮我切换后端到capi额度卡"),
            ("backend", "capi"),
        )
        self.assertEqual(
            parse_backend_natural_command("帮我切换后端到额度卡"),
            ("backend", "capi"),
        )
        self.assertEqual(
            parse_backend_natural_command("switch backend to quota card"),
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
        self.assertEqual(
            parse_backend_natural_command("\u67e5\u8be2\u4e0b\u5f53\u524d\u540e\u7aef\u989d\u5ea6"),
            ("backend", "quota-current"),
        )

    def test_codex_quota_analysis_request_stays_on_agent_path(self):
        self.assertIsNone(
            parse_backend_natural_command(
                "分析一下Codex当前的平均用量超了多少，后续的使用策略如何分配更合适"
            )
        )
        self.assertIsNone(parse_backend_natural_command("请分析 codex 额度是否超了以及后续策略"))

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

    def test_execute_switches_to_quota_card_before_quota_query_path(self):
        from common.llm_backend_router import get_current_backend, set_current_backend

        plugin = _load_cow_cli_plugin()

        with patch.object(plugin, "_backend_capi_quota") as quota:
            result = plugin.execute("帮我切换后端到capi额度卡", session_id="test")

        self.assertEqual(result, "LLM backend switched to capi")
        self.assertEqual(get_current_backend(), "capi")
        quota.assert_not_called()

        set_current_backend("codex", manual=True, reason="test")
        result = plugin.execute("帮我切换到额度卡后端", session_id="test")

        self.assertEqual(result, "LLM backend switched to capi")
        self.assertEqual(get_current_backend(), "capi")
        quota.assert_not_called()

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

    def test_execute_current_backend_quota_uses_capi_quota_card_when_active(self):
        from config import conf

        conf()["llm_backend"]["current_backend"] = "capi"
        plugin = _load_cow_cli_plugin()

        with patch.object(plugin, "_backend_capi_quota", return_value="capi quota ok") as quota:
            result = plugin.execute(
                "\u67e5\u8be2\u4e0b\u5f53\u524d\u540e\u7aef\u989d\u5ea6",
                session_id="test",
            )

        self.assertEqual(result, "capi quota ok")
        quota.assert_called_once_with("capi")

    def test_execute_current_backend_quota_uses_monthly_card_when_active(self):
        from config import conf

        conf()["llm_backend"]["current_backend"] = "capi_monthly"
        plugin = _load_cow_cli_plugin()

        with patch.object(plugin, "_backend_capi_quota", return_value="monthly quota ok") as quota:
            result = plugin.execute(
                "\u67e5\u8be2\u4e0b\u5f53\u524d\u540e\u7aef\u989d\u5ea6",
                session_id="test",
            )

        self.assertEqual(result, "monthly quota ok")
        quota.assert_called_once_with("capi_monthly")

    def test_event_interception_breaks_agent_flow(self):
        from bridge.context import Context, ContextType
        from plugins import Event, EventAction, EventContext

        plugin = _load_cow_cli_plugin()
        context = Context(ContextType.TEXT, "请切回 Codex backend", kwargs={"actor_role": "admin"})
        e_context = EventContext(Event.ON_HANDLE_CONTEXT, {"context": context})

        plugin.on_handle_context(e_context)

        self.assertEqual(e_context.action, EventAction.BREAK_PASS)
        self.assertIn("codex", e_context["reply"].content)

    def test_high_risk_cli_command_requires_admin(self):
        from bridge.context import Context, ContextType
        from plugins import Event, EventAction, EventContext

        plugin = _load_cow_cli_plugin()
        context = Context(ContextType.TEXT, "/backend capi", kwargs={"actor_role": "user"})
        e_context = EventContext(Event.ON_HANDLE_CONTEXT, {"context": context})

        plugin.on_handle_context(e_context)

        self.assertEqual(e_context.action, EventAction.BREAK_PASS)
        self.assertIn("需要管理员权限", e_context["reply"].content)

    def test_high_risk_natural_command_requires_admin(self):
        from bridge.context import Context, ContextType
        from plugins import Event, EventAction, EventContext

        plugin = _load_cow_cli_plugin()
        context = Context(ContextType.TEXT, "请切回 Codex backend", kwargs={"actor_role": "user"})
        e_context = EventContext(Event.ON_HANDLE_CONTEXT, {"context": context})

        plugin.on_handle_context(e_context)

        self.assertEqual(e_context.action, EventAction.BREAK_PASS)
        self.assertIn("需要管理员权限", e_context["reply"].content)

    def test_public_cli_command_remains_available_to_normal_user(self):
        from bridge.context import Context, ContextType
        from plugins import Event, EventAction, EventContext

        plugin = _load_cow_cli_plugin()
        context = Context(ContextType.TEXT, "/backend status", kwargs={"actor_role": "user"})
        e_context = EventContext(Event.ON_HANDLE_CONTEXT, {"context": context})

        with patch("common.llm_backend_router.describe_status", return_value="backend status ok"):
            plugin.on_handle_context(e_context)

        self.assertEqual(e_context.action, EventAction.BREAK_PASS)
        self.assertEqual(e_context["reply"].content, "backend status ok")

    def test_public_backend_quota_subcommands_remain_available_to_normal_user(self):
        plugin = _load_cow_cli_plugin()

        self.assertEqual(plugin._command_access_level("backend", "quota capi"), "public")
        self.assertEqual(plugin._command_access_level("backend", "capi quota"), "public")
        self.assertEqual(plugin._command_access_level("backend", "capi-monthly quota"), "public")
        self.assertEqual(plugin._command_access_level("backend", "capi"), "admin")

    def test_help_filters_commands_by_user_role(self):
        from bridge.context import Context, ContextType
        from plugins import Event, EventAction, EventContext

        plugin = _load_cow_cli_plugin()
        user_context = Context(ContextType.TEXT, "/help", kwargs={"actor_role": "user"})
        user_event = EventContext(Event.ON_HANDLE_CONTEXT, {"context": user_context})
        admin_context = Context(ContextType.TEXT, "/help", kwargs={"actor_role": "admin"})
        admin_event = EventContext(Event.ON_HANDLE_CONTEXT, {"context": admin_context})

        plugin.on_handle_context(user_event)
        plugin.on_handle_context(admin_event)

        user_help = user_event["reply"].content
        admin_help = admin_event["reply"].content
        self.assertIn("/status", user_help)
        self.assertIn("查询本月账单", user_help)
        self.assertIn("管理员命令已隐藏", user_help)
        self.assertNotIn("/backend capi：切到 CAPI 额度卡", user_help)
        self.assertNotIn("/skill install <名称>：安装技能", user_help)
        self.assertNotIn("/config <key> <val>：修改配置", user_help)
        self.assertIn("/backend capi：切到 CAPI 额度卡", admin_help)
        self.assertIn("/skill install <名称>：安装技能", admin_help)
        self.assertIn("/config <key> <val>：修改配置", admin_help)

    def test_new_cli_commands_default_to_admin_only(self):
        plugin = _load_cow_cli_plugin()

        self.assertEqual(plugin._command_access_level("future-command"), "admin")

    def test_group_sender_can_be_admin_for_cli_commands(self):
        from config import conf
        from bridge.context import Context, ContextType
        from plugins import Event, EventAction, EventContext

        previous_admin_users = conf().get("agent_admin_users")
        conf()["agent_admin_users"] = ["wecom_bot:sender-a"]
        try:
            plugin = _load_cow_cli_plugin()
            context = Context(
                ContextType.TEXT,
                "/backend capi",
                kwargs={
                    "actor_role": "user",
                    "actor_id": "wecom_bot:group:chat-a",
                    "channel_type": "wecom_bot",
                    "group_sender_id": "sender-a",
                },
            )
            e_context = EventContext(Event.ON_HANDLE_CONTEXT, {"context": context})

            plugin.on_handle_context(e_context)
        finally:
            if previous_admin_users is None:
                conf().pop("agent_admin_users", None)
            else:
                conf()["agent_admin_users"] = previous_admin_users

        self.assertEqual(e_context.action, EventAction.BREAK_PASS)
        self.assertIn("switched to capi", e_context["reply"].content)

    def test_ledger_natural_query_uses_context_memory_user_id(self):
        from bridge.context import Context, ContextType
        from plugins import Event, EventAction, EventContext

        plugin = _load_cow_cli_plugin()
        context = Context(
            ContextType.TEXT,
            "查询本日账单",
            kwargs={
                "memory_user_id": "weixin-user-memory",
                "channel_type": "weixin",
                "session_id": "weixin-session",
            },
        )
        e_context = EventContext(Event.ON_HANDLE_CONTEXT, {"context": context})
        summary = {
            "count": 1,
            "totals": {
                "expense_cents": 9988,
                "income_cents": 0,
                "refund_cents": 0,
                "transfer_cents": 0,
            },
            "by_category": {"AI工具": 9988},
        }
        fake_ledger = SimpleNamespace(
            db_path_from_env=lambda: Path("ledger.db"),
            open_db=lambda path: SimpleNamespace(close=lambda: None),
            init_db=lambda conn: None,
            summarize_transactions=lambda conn, user_id, period: summary,
        )

        with patch.object(plugin, "_load_china_expense_ledger", return_value=fake_ledger) as load_ledger:
            plugin.on_handle_context(e_context)

        self.assertEqual(e_context.action, EventAction.BREAK_PASS)
        self.assertIn("本地账单", e_context["reply"].content)
        self.assertIn("¥99.88", e_context["reply"].content)
        self.assertIn("AI工具", e_context["reply"].content)
        load_ledger.assert_called_once()

    def test_ledger_today_phrase_queries_real_ledger_for_current_user(self):
        from bridge.context import Context, ContextType
        from plugins import Event, EventAction, EventContext

        ledger_path = PROJECT_ROOT / "skills" / "china-expense-ledger" / "scripts" / "ledger.py"
        spec = importlib.util.spec_from_file_location("china_expense_ledger_test", ledger_path)
        ledger = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ledger)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ledger.db"
            with patch.dict(os.environ, {"CHINA_EXPENSE_LEDGER_DB": str(db_path)}):
                conn = ledger.open_db(db_path)
                try:
                    ledger.init_db(conn)
                    result = ledger.analyze_bill_payload(
                        conn,
                        {
                            "user_id": "wecom-user-memory",
                            "chat_id": "private-chat",
                            "record_id": "image-record-1",
                            "source_type": "image",
                            "source_hash": "hash-1",
                            "raw_text": "微信支付 支付成功 餐饮 支付金额 ¥16.00 交易单号 123456789012",
                            "amount_cents": 1600,
                            "direction": "expense",
                            "category": "餐饮",
                            "merchant": "餐厅",
                        },
                    )
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["status"], "auto_recorded")
                finally:
                    conn.close()

                plugin = _load_cow_cli_plugin()
                context = Context(
                    ContextType.TEXT,
                    "今日账单",
                    kwargs={
                        "memory_user_id": "wecom-user-memory",
                        "channel_type": "wecom_bot",
                        "session_id": "private-chat",
                    },
                )
                e_context = EventContext(Event.ON_HANDLE_CONTEXT, {"context": context})

                plugin.on_handle_context(e_context)

        self.assertEqual(e_context.action, EventAction.BREAK_PASS)
        self.assertIn("本地账单", e_context["reply"].content)
        self.assertIn("¥16.00", e_context["reply"].content)
        self.assertIn("餐饮", e_context["reply"].content)

    def test_ledger_natural_query_requires_context_memory_user_id(self):
        from bridge.context import Context, ContextType
        from plugins import Event, EventAction, EventContext

        plugin = _load_cow_cli_plugin()
        context = Context(ContextType.TEXT, "查询本月消费", kwargs={"channel_type": "wecom_bot"})
        e_context = EventContext(Event.ON_HANDLE_CONTEXT, {"context": context})

        plugin.on_handle_context(e_context)

        self.assertEqual(e_context.action, EventAction.BREAK_PASS)
        self.assertIn("没有识别到当前用户的记账身份", e_context["reply"].content)

    def test_direct_social_skill_request_bypasses_cow_cli_fast_answer(self):
        plugin = _load_cow_cli_plugin()
        question = (
            "\u5c06\u81ea\u52a9\u8bb0\u8d26\u8fd9\u4e2a\u529f\u80fd"
            "\u544a\u8bc9\u6211\u8001\u5a46\uff0c\u8ba9\u5979\u660e\u767d\u600e\u4e48\u7528"
        )

        self.assertIsNone(plugin._parse_command(question))

    def test_direct_social_skill_request_bypasses_for_general_recipient(self):
        plugin = _load_cow_cli_plugin()

        self.assertIsNone(plugin._parse_command("把自助记账这个功能发给小王，告诉他怎么用"))
        self.assertIsNone(plugin._parse_command("帮我转述自助记账这个功能给我妈，通俗一点"))
        self.assertIsNone(plugin._parse_command("告诉我妈自助记账这个功能怎么用"))
        self.assertIsNone(plugin._parse_command("通知群里自助记账这个功能已经可以用了"))

    def test_tell_me_skill_request_still_uses_fast_answer(self):
        plugin = _load_cow_cli_plugin()

        parsed = plugin._parse_command("告诉我自助记账这个功能怎么用")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0], "skill")

    def test_send_howto_question_does_not_force_direct_relay_bypass(self):
        plugin = _load_cow_cli_plugin()

        self.assertEqual(plugin._direct_social_target("这个功能怎么发给小王"), "")

    def test_recommendation_question_can_still_use_updates_fast_answer(self):
        plugin = _load_cow_cli_plugin()
        question = "\u4eca\u5929\u66f4\u65b0\u4e86\u54ea\u4e9b\u529f\u80fd\u9002\u5408\u63a8\u9001\u7ed9\u6211\u8001\u5a46\u7684"

        parsed = plugin._parse_command(question)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0], "updates")

    def test_skill_fast_answer_marks_followup_context(self):
        from bridge.context import Context, ContextType
        from plugins import Event, EventAction, EventContext

        plugin = _load_cow_cli_plugin()
        question = "\u81ea\u52a9\u8bb0\u8d26\u8fd9\u4e2a\u529f\u80fd\u600e\u4e48\u7528"
        answer = "\u53d1\u4ed8\u6b3e\u622a\u56fe\u5c31\u80fd\u81ea\u52a8\u8bb0\u8d26\u3002"
        context = Context(
            ContextType.TEXT,
            question,
            kwargs={"channel_type": "wecom_bot", "session_id": "LiuHao"},
        )
        e_context = EventContext(Event.ON_HANDLE_CONTEXT, {"context": context})

        with patch.object(plugin, "_call_skill_answer_model", return_value=answer):
            plugin.on_handle_context(e_context)

        self.assertEqual(e_context.action, EventAction.BREAK_PASS)
        self.assertEqual(context["_cow_cli_followup_context"]["user_text"], question)
        self.assertEqual(context["_cow_cli_followup_context"]["assistant_text"], answer)

    def test_chat_channel_remembers_marked_cow_cli_reply_after_send(self):
        from bridge.context import Context, ContextType
        from bridge.reply import Reply, ReplyType
        from channel.chat_channel import ChatChannel

        context = Context(ContextType.TEXT, "ignored")
        context["_cow_cli_followup_context"] = {
            "source": "cow_cli",
            "user_text": "自助记账这个功能怎么用",
            "assistant_text": "发付款截图就能自动记账。",
        }
        agent_bridge = SimpleNamespace(remember_external_visible_reply=Mock())
        bridge = SimpleNamespace(get_agent_bridge=lambda: agent_bridge)

        with patch("bridge.bridge.Bridge", return_value=bridge):
            ChatChannel._remember_cow_cli_followup_context(
                context,
                Reply(ReplyType.TEXT, "已发送给用户的最终回复"),
            )

        agent_bridge.remember_external_visible_reply.assert_called_once()
        call = agent_bridge.remember_external_visible_reply.call_args.kwargs
        self.assertEqual(call["context"], context)
        self.assertEqual(call["user_text"], "自助记账这个功能怎么用")
        self.assertEqual(call["assistant_text"], "发付款截图就能自动记账。")

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
            return SimpleNamespace(
                returncode=0,
                stdout='{"quota":{"mode":"daily","total":90,"daily":90,"used":10,"remaining":80,"progress":11.1}}',
                stderr="",
            )

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("common.capi_quota_query.subprocess.run", side_effect=fake_run),
        ):
            result = plugin.execute("查一下 CAPI 月卡剩余额度", session_id="test")

        self.assertIn("today_remaining: 80", result)
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
            return SimpleNamespace(
                returncode=0,
                stdout='{"quota":{"mode":"total","total":500,"used":20,"remaining":480,"progress":4}}',
                stderr="",
            )

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("common.capi_quota_query.subprocess.run", side_effect=fake_run),
        ):
            result = plugin.execute("查询 CAPI 额度卡余额", session_id="test")

        self.assertIn("total_remaining: 480", result)
        self.assertIn("--api-key-env", captured["argv"])
        self.assertEqual(captured["env"]["CAPI_QUOTA_ROUTER_KEY"], "QUOTA-KEY")
        self.assertNotIn("CAPI_MONTHLY_ROUTER_KEY", captured["env"])

    def test_execute_quota_query_updates_backend_status_cache(self):
        from common.llm_backend_router import describe_status, load_state
        from config import conf

        conf()["llm_backend"]["current_backend"] = "capi"
        conf()["llm_backend"]["providers"] = {"capi": {"api_key": "QUOTA-KEY"}}
        plugin = _load_cow_cli_plugin()

        def fake_run(_argv, **_kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout='{"quota":{"mode":"total","total":500,"used":20,"remaining":480,"progress":4}}',
                stderr="",
            )

        with patch("common.capi_quota_query.subprocess.run", side_effect=fake_run):
            result = plugin.execute("\u67e5\u8be2 CAPI \u989d\u5ea6\u5361\u4f59\u989d", session_id="test")

        self.assertIn("total_remaining: 480", result)
        self.assertEqual(load_state()["backend_quota"]["capi"]["remaining"], 480)
        self.assertIn("- current_quota_remaining: 480.0/500.0", describe_status())

    def test_codex_quota_uses_project_wrapper(self):
        plugin = _load_cow_cli_plugin()
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["cwd"] = kwargs["cwd"]
            captured["env"] = kwargs["env"]
            payload = {
                "ok": True,
                "source": "codex-app-server",
                "account": {"label": "p***@example.com", "plan_type": "plus", "type": "chatgpt"},
                "summary": {"blocked": False},
                "rate_limits": [],
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

        with patch("common.codex_quota_query.subprocess.run", side_effect=fake_run):
            result = plugin.execute("\u67e5\u8be2\u4e0bcodex\u4f7f\u7528\u91cf", session_id="test")

        self.assertIn("GPT/Codex quota", result)
        self.assertIn("check_codex_quota.py", str(captured["argv"][1]))
        self.assertIn("--project-dir", captured["argv"])
        self.assertEqual(Path(captured["cwd"]), PROJECT_ROOT)

    def test_sensitive_key_question_does_not_return_backend_status(self):
        plugin = _load_cow_cli_plugin()

        result = plugin.execute("当前 CAPI key 是什么？", session_id="test")

        self.assertIn("不能显示原始", result)
        self.assertNotIn("LLM backend status", result)
        self.assertNotIn("TEST", result)

    def test_skill_natural_language_routes_to_model_grounded_catalog_answer(self):
        plugin = _load_cow_cli_plugin()

        self.assertEqual(plugin._parse_command("/skill list"), ("skill", "list"))

        cmd, args = plugin._parse_command("帮我总结下今天更新的功能适合推送给我老婆使用的有哪些")
        self.assertEqual(cmd, "updates")
        update_payload = plugin._decode_project_update_args(args)
        self.assertEqual(update_payload["period"], "today")
        self.assertEqual(update_payload["question"], "帮我总结下今天更新的功能适合推送给我老婆使用的有哪些")

        cmd, args = plugin._parse_command("当前支持哪些功能呢")
        self.assertEqual(cmd, "skill")
        self.assertTrue(args.startswith("answer "))
        payload = plugin._decode_skill_answer_args(args.split(None, 1)[1])
        self.assertEqual(payload["question"], "当前支持哪些功能呢")
        self.assertEqual(payload["mode"], "list")

        cmd, args = plugin._parse_command("当前支持哪些 skill 呢")
        self.assertEqual(cmd, "skill")
        payload = plugin._decode_skill_answer_args(args.split(None, 1)[1])
        self.assertEqual(payload["mode"], "list")
        self.assertEqual(payload["category"], "")
        self.assertEqual(payload["categories"], [])

        cmd, args = plugin._parse_command("你有哪些出行地图功能")
        self.assertEqual(cmd, "skill")
        payload = plugin._decode_skill_answer_args(args.split(None, 1)[1])
        self.assertEqual(payload["mode"], "category")
        self.assertEqual(payload["category"], "travel_location")

        cmd, args = plugin._parse_command("你现在能做什么功能")
        self.assertEqual(cmd, "skill")
        self.assertTrue(args.startswith("answer "))

        cmd, args = plugin._parse_command("capi-usage-monitor 怎么用")
        payload = plugin._decode_skill_answer_args(args.split(None, 1)[1])
        self.assertEqual(payload["mode"], "usage")
        self.assertEqual(payload["skill"], "capi-usage-monitor")
        self.assertIsNone(plugin._parse_command("帮我开发一个新功能"))

    def test_today_project_update_summary_uses_git_log_not_skill_catalog(self):
        plugin = _load_cow_cli_plugin()

        class FakeProc:
            returncode = 0
            stderr = ""
            stdout = "\n".join([
                "abc123\t2026-05-25\tfix: reconnect stalled wecom bot subscribe",
                "def456\t2026-05-25\tfeat: upgrade travel manager orchestration",
                "aaa111\t2026-05-25\tfix: finish long tasks after progress notices",
                "bbb222\t2026-05-25\tfix: bound stale image followups and monthly routing",
            ])

        with (
            patch("plugins.cow_cli.cow_cli.subprocess.run", return_value=FakeProc()) as run,
            patch.object(plugin, "_skill_catalog") as skill_catalog,
            patch("bridge.agent_bridge.AgentLLMModel") as model_cls,
        ):
            model = model_cls.return_value
            model.call.return_value = {
                "choices": [{"message": {"content": "适合推给你老婆的：旅行规划、企业微信稳定性和图片追问稳定。"}}],
            }
            with patch.object(plugin, "_read_readme_update_entries_for_today", return_value=[]):
                result = plugin.execute("帮我总结下今天更新的功能适合推送给我老婆使用的有哪些", session_id="test")

        run.assert_called_once()
        skill_catalog.assert_not_called()
        self.assertIn("适合推给你老婆", result)
        request = model.call.call_args.args[0]
        self.assertEqual(request.cache_shape_metadata["request_kind"], "cow_cli_project_update_summary")
        self.assertEqual(request.tools, [])
        self.assertEqual(request.reasoning_effort, "medium")
        self.assertTrue(request.reasoning_effort_locked)
        self.assertIn("帮我总结下今天更新的功能适合推送给我老婆使用的有哪些", request.messages[0]["content"])
        self.assertIn("fix: reconnect stalled wecom bot subscribe", request.messages[0]["content"])

    def test_today_project_update_summary_prefers_readme_update_log(self):
        plugin = _load_cow_cli_plugin()

        class FakeProc:
            returncode = 0
            stderr = ""
            stdout = "abc123\t2026-05-25\tchore: internal maintenance"

        readme_entries = [
            "加固企业微信智能机器人长连接，避免企业微信消息长时间无回复",
            "优化旅行规划技能，夜间和过夜行程默认提示酒店/住宿安排",
        ]

        with (
            patch("plugins.cow_cli.cow_cli.subprocess.run", return_value=FakeProc()),
            patch.object(plugin, "_read_readme_update_entries_for_today", return_value=readme_entries),
            patch.object(plugin, "_skill_catalog") as skill_catalog,
            patch("bridge.agent_bridge.AgentLLMModel") as model_cls,
        ):
            model = model_cls.return_value
            model.call.return_value = {
                "choices": [{"message": {"content": "适合推给你老婆的：旅行规划和企业微信稳定性。"}}],
            }
            result = plugin.execute("帮我总结下今天更新的功能适合推送给我老婆使用的有哪些", session_id="test")

        skill_catalog.assert_not_called()
        self.assertIn("适合推给你老婆", result)
        request = model.call.call_args.args[0]
        self.assertIn("加固企业微信智能机器人长连接", request.messages[0]["content"])
        self.assertIn("优化旅行规划技能", request.messages[0]["content"])

    def test_today_project_update_summary_fallback_keeps_spouse_target(self):
        plugin = _load_cow_cli_plugin()

        class FakeProc:
            returncode = 0
            stderr = ""
            stdout = "abc123\t2026-05-25\tfeat: upgrade travel manager orchestration"

        encoded_args = plugin._encode_project_update_args(
            "帮我总结下今天更新的功能适合推送给我老婆使用的有哪些",
            "today",
        )

        with (
            patch("plugins.cow_cli.cow_cli.subprocess.run", return_value=FakeProc()),
            patch.object(plugin, "_read_readme_update_entries_for_today", return_value=[]),
            patch.object(plugin, "_call_project_update_summary_model", side_effect=RuntimeError("model down")),
        ):
            result = plugin._cmd_updates(encoded_args, e_context=None, session_id="test")

        self.assertIn("适合推荐给你老婆", result)
        self.assertNotIn("日常使用者", result)
        self.assertIn("旅行规划更适合直接用", result)

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

    def test_skill_answer_falls_back_to_full_skill_when_summary_is_insufficient(self):
        plugin = _load_cow_cli_plugin()
        responses = [
            {"choices": [{"message": {"content": "[[READ_FULL_SKILL]]"}}]},
            {"choices": [{"message": {"content": "完整 Skill 里说明可以运行 snapshot。"}}]},
        ]

        with patch("bridge.agent_bridge.AgentLLMModel") as model_cls:
            model = model_cls.return_value
            model.call.side_effect = responses

            args = plugin._encode_skill_answer_args(
                "capi-usage-monitor 的 snapshot 参数怎么用",
                "usage",
                "capi-usage-monitor",
            )
            result = plugin.execute(f"/skill {args}", session_id="test-session")

        self.assertEqual(result, "完整 Skill 里说明可以运行 snapshot。")
        self.assertEqual(model.call.call_count, 2)
        first_request = model.call.call_args_list[0].args[0]
        second_request = model.call.call_args_list[1].args[0]
        self.assertIn("单个 Skill 详细摘要", first_request.messages[0]["content"])
        self.assertIn("完整 Skill 内容", second_request.messages[0]["content"])


    def test_specific_skill_answer_context_uses_detailed_summary(self):
        plugin = _load_cow_cli_plugin()
        catalog = SimpleNamespace(
            format_skill_detail_summary=lambda name, **_: f"DETAILED SUMMARY: {name}",
            format_skill_usage=lambda name: f"LEGACY USAGE PREVIEW: {name}",
        )

        with patch.object(plugin, "_skill_catalog", return_value=catalog):
            context = plugin._skill_answer_context(mode="usage", skill_name="deep-skill")

        self.assertEqual(context, "DETAILED SUMMARY: deep-skill")

    def test_skill_natural_query_supports_single_and_multi_category(self):
        plugin = _load_cow_cli_plugin()
        catalog = SimpleNamespace(
            find_categories_in_text=lambda text: ["shopping_food", "travel_location"]
            if "购物" in text and "出行" in text
            else (["shopping_food"] if "购物" in text else []),
            find_category_in_text=lambda text: "shopping_food" if "购物" in text else "",
        )

        with patch.object(plugin, "_skill_catalog", return_value=catalog):
            single = plugin._parse_command("当前有没有购物类的功能呢")
            multiple = plugin._parse_command("当前有没有购物和出行相关的功能呢")

        self.assertEqual(single[0], "skill")
        single_payload = plugin._decode_skill_answer_args(single[1].split(None, 1)[1])
        self.assertEqual(single_payload["mode"], "category")
        self.assertEqual(single_payload["categories"], ["shopping_food"])

        self.assertEqual(multiple[0], "skill")
        multiple_payload = plugin._decode_skill_answer_args(multiple[1].split(None, 1)[1])
        self.assertEqual(multiple_payload["mode"], "category")
        self.assertEqual(multiple_payload["categories"], ["shopping_food", "travel_location"])

    def test_category_answer_context_uses_multi_category_summary(self):
        plugin = _load_cow_cli_plugin()
        catalog = SimpleNamespace(
            multi_category_summary=lambda categories, **_: f"MULTI CATEGORY: {','.join(categories)}",
            category_summary=lambda category, **_: f"SINGLE CATEGORY: {category}",
            overview_summary=lambda **_: "OVERVIEW",
        )

        with patch.object(plugin, "_skill_catalog", return_value=catalog):
            context = plugin._skill_answer_context(
                mode="category",
                category="shopping_food,travel_location",
                categories=["shopping_food", "travel_location"],
            )

        self.assertEqual(context, "MULTI CATEGORY: shopping_food,travel_location")

    def test_skill_answer_uses_medium_model_router_for_unlisted_category_phrasing(self):
        plugin = _load_cow_cli_plugin()
        catalog = SimpleNamespace(
            find_categories_in_text=lambda text: [],
            category_options_summary=lambda: (
                "可选 Skill 分类：\n"
                "- shopping_food: 购物餐饮；已安装 0 个；常见说法示例：购物、商品\n"
                "- travel_location: 出行地图；已安装 1 个；常见说法示例：路线、交通"
            ),
            multi_category_summary=lambda categories, **_: f"SUMMARY: {','.join(categories)}",
            category_summary=lambda category, **_: f"SUMMARY: {category}",
            overview_summary=lambda **_: "OVERVIEW",
            format_local_list=lambda: "LOCAL LIST",
        )
        encoded_args = plugin._encode_skill_answer_args(
            "有没有帮我下单找折扣这种功能",
            "list",
        ).split(None, 1)[1]
        responses = [
            {"choices": [{"message": {"content": "{\"categories\":[\"shopping_food\"]}"}}]},
            {"choices": [{"message": {"content": "当前没有看到购物类 Skill。"}}]},
        ]

        with (
            patch.object(plugin, "_skill_catalog", return_value=catalog),
            patch("bridge.agent_bridge.AgentLLMModel") as model_cls,
        ):
            model = model_cls.return_value
            model.call.side_effect = responses
            result = plugin._skill_answer(encoded_args, e_context=None, session_id="test-session")

        self.assertEqual(result, "当前没有看到购物类 Skill。")
        self.assertEqual(model.call.call_count, 2)
        router_request = model.call.call_args_list[0].args[0]
        answer_request = model.call.call_args_list[1].args[0]
        self.assertEqual(router_request.reasoning_effort, "medium")
        self.assertTrue(router_request.reasoning_effort_locked)
        self.assertEqual(router_request.tools, [])
        self.assertEqual(router_request.cache_shape_metadata["request_kind"], "cow_cli_skill_category_router")
        self.assertIn("下单找折扣", router_request.messages[0]["content"])
        self.assertIn("SUMMARY: shopping_food", answer_request.messages[0]["content"])

    def test_explicit_skill_inventory_bypasses_model_and_returns_full_overview(self):
        plugin = _load_cow_cli_plugin()
        catalog = SimpleNamespace(
            inventory_summary_zh=lambda **_: (
                "当前已启用的本机 Skill 共 5 个：\n"
                "- 技能甲\n"
                "- 技能乙\n"
                "想看某个 Skill 的详细用法，直接问“某某怎么用”。"
            ),
            overview_summary=lambda **_: (
                "本机技能/功能总览：\n"
                "- Skill A\n"
                "- Skill B\n"
                "- Skill C\n"
                "- Skill D\n"
                "- Skill E"
            )
        )
        encoded_args = plugin._encode_skill_answer_args(
            "当前支持哪些 skill 呢",
            "list",
        ).split(None, 1)[1]

        with (
            patch.object(plugin, "_skill_catalog", return_value=catalog),
            patch.object(plugin, "_call_skill_answer_model") as model_call,
            patch.object(plugin, "_infer_skill_categories_with_model") as router_call,
        ):
            result = plugin._skill_answer(encoded_args, e_context=None, session_id="test-session")

        self.assertIn("技能甲", result)
        self.assertIn("想看某个 Skill 的详细用法", result)
        model_call.assert_not_called()
        router_call.assert_not_called()

    def test_insufficient_summary_marker_falls_back_to_full_skill_context(self):
        plugin = _load_cow_cli_plugin()
        catalog = SimpleNamespace(
            format_skill_detail_summary=lambda name, **_: f"SUMMARY ONLY: {name}",
            full_skill_context=lambda name, **_: f"FULL SKILL CONTEXT: {name}\nretry policy details",
            format_skill_usage=lambda name: f"FULL USAGE FALLBACK: {name}\nretry policy details",
        )
        encoded_args = plugin._encode_skill_answer_args(
            "How does deep-skill handle retry policy details?",
            "usage",
            "deep-skill",
        ).split(None, 1)[1]

        with (
            patch.object(plugin, "_skill_catalog", return_value=catalog),
            patch.object(
                plugin,
                "_call_skill_answer_model",
                side_effect=["[[READ_FULL_SKILL:deep-skill]]", "Use retry policy details."],
            ) as model_call,
        ):
            result = plugin._skill_answer(encoded_args, e_context=None, session_id="test-session")

        acceptable_results = {
            "Use retry policy details.",
            "FULL USAGE FALLBACK: deep-skill\nretry policy details",
        }
        self.assertIn(result, acceptable_results)
        if result == "Use retry policy details.":
            self.assertEqual(model_call.call_count, 2)
            first_context = model_call.call_args_list[0].kwargs["catalog_context"]
            second_context = model_call.call_args_list[1].kwargs["catalog_context"]
            self.assertIn("SUMMARY ONLY: deep-skill", first_context)
            self.assertIn("FULL SKILL CONTEXT: deep-skill", second_context)
        else:
            self.assertEqual(model_call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
