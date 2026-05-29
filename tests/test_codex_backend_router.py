import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common import const
from common.codex_quota_logic import decide_codex_auto_switch
from common.llm_backend_router import (
    BACKEND_CAPI,
    BACKEND_CAPI_MONTHLY,
    BACKEND_CODEX,
    BACKEND_GROK,
    available_actor_backends,
    check_capi_connectivity,
    evaluate_auto_switch,
    evaluate_midnight_backend_route,
    get_effective_openai_api_config,
    get_effective_chat_bot_type,
    get_current_backend_for_profile,
    get_current_backend,
    get_effective_model,
    is_capi_runtime_fallback_error,
    is_capi_quota_exhausted_error,
    load_state,
    describe_status,
    record_capi_quota_check,
    record_codex_quota_check,
    resolve_configured_chat_bot_type,
    select_capi_runtime_fallback_backend,
    select_backend_after_monthly_quota_low,
    set_current_backend,
    set_user_backend_override,
    save_state,
)
from config import conf
from models.codex.codex_auth import CodexAuthCredentialSource


def weekly_payload(used_percent=5, resets_at=4102444800):
    return {
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "limitName": "Codex",
                "primary": {
                    "windowDurationMins": 60,
                    "usedPercent": 0,
                    "resetsAt": resets_at,
                },
                "secondary": {
                    "windowDurationMins": 10080,
                    "usedPercent": used_percent,
                    "resetsAt": resets_at,
                },
            }
        }
    }


class TestCodexBackendRouter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = str(Path(self.tmp.name) / "state.json")
        self.reset_bridge_cache = patch("common.llm_backend_router._reset_bridge_cache")
        self.reset_bridge_cache.start()
        self.env_patch = patch.dict(
            "os.environ",
            {
                "CAPI_API_KEY": "",
                "CAPI_MONTHLY_API_KEY": "",
                "OPENAI_API_BASE": "",
            },
            clear=False,
        )
        self.env_patch.start()
        conf()["llm_backend"] = {
            "current_backend": "capi",
            "state_path": self.state_path,
            "auto_switch": {
                "enabled": True,
                "fair_share_days": 7,
                "min_remaining_percent": 15,
                "respect_manual_override": True,
            },
            "providers": {"codex": {"model": "gpt-5.5"}},
            "restricted_backends": {
                "enabled": True,
                "allowed_backends": ["grok"],
                "whitelist": ["山海入梦来"],
            },
        }

    def tearDown(self):
        self.env_patch.stop()
        self.reset_bridge_cache.stop()
        self.tmp.cleanup()
        conf().pop("llm_backend", None)

    def test_day_two_under_fair_share_switches(self):
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)
        decision = decide_codex_auto_switch(weekly_payload(used_percent=5), now=now)

        self.assertTrue(decision.should_switch)
        self.assertEqual(decision.completed_days, 1)
        self.assertAlmostEqual(decision.allowed_used_percent, 100 / 7)

    def test_above_fair_share_does_not_switch(self):
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)
        decision = decide_codex_auto_switch(weekly_payload(used_percent=20), now=now)

        self.assertFalse(decision.should_switch)
        self.assertEqual(decision.reason, "used_above_fair_share")

    def test_auto_switch_writes_latched_codex_state_once(self):
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)
        state = evaluate_auto_switch(weekly_payload(used_percent=5), now=now)

        self.assertEqual(state["current_backend"], BACKEND_CODEX)
        self.assertTrue(state["auto_switch_latched"])
        self.assertEqual(load_state()["auto"]["last_decision"], "switched_to_codex")
        self.assertEqual(get_current_backend(), BACKEND_CODEX)

    def test_manual_override_blocks_auto_switch(self):
        save_state({
            "current_backend": "capi",
            "manual_override_active": True,
            "auto_switch_latched": False,
        })
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)
        state = evaluate_auto_switch(weekly_payload(used_percent=5), now=now)

        self.assertEqual(state["auto"]["last_decision"], "skipped")
        self.assertEqual(state["auto"]["last_reason"], "manual_override_active")

    def test_midnight_auto_ignores_manual_override_for_global_daily_switch(self):
        save_state({
            "current_backend": "capi",
            "manual_override_active": True,
            "auto_switch_latched": False,
        })
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)

        state = evaluate_midnight_backend_route(
            quota_payload=weekly_payload(used_percent=5),
            capi_connectivity_checker=lambda backend: True,
            now=now,
        )

        self.assertEqual(state["current_backend"], BACKEND_CODEX)
        self.assertFalse(state["manual_override_active"])
        self.assertEqual(state["auto"]["last_decision"], "switched_to_codex")

    def test_midnight_kept_route_clears_manual_override_for_all_sessions(self):
        save_state({
            "current_backend": "capi",
            "manual_override_active": True,
            "auto_switch_latched": False,
        })
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)

        state = evaluate_midnight_backend_route(
            quota_payload=weekly_payload(used_percent=30),
            capi_connectivity_checker=lambda backend: True,
            now=now,
        )

        self.assertEqual(state["current_backend"], BACKEND_CAPI)
        self.assertFalse(state["manual_override_active"])
        self.assertEqual(state["current_backend_source"], "auto")
        self.assertEqual(state["auto"]["last_decision"], "kept")

    def test_auto_switch_runs_only_once_per_day(self):
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)
        save_state({
            "current_backend": "capi",
            "auto": {
                "last_checked_date": now.date().isoformat(),
                "last_decision": "kept",
            },
        })

        state = evaluate_auto_switch(weekly_payload(used_percent=5), now=now)

        self.assertEqual(state["current_backend"], "capi")
        self.assertEqual(state["auto"]["last_decision"], "kept")

    def test_midnight_prefers_monthly_capi_backend_when_configured(self):
        conf()["llm_backend"]["providers"]["capi_monthly"] = {"api_key": "TEST-MONTHLY-KEY"}
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)

        state = evaluate_midnight_backend_route(
            quota_payload=weekly_payload(used_percent=99),
            capi_connectivity_checker=lambda backend: True,
            now=now,
        )

        self.assertEqual(state["current_backend"], BACKEND_CAPI_MONTHLY)
        self.assertFalse(state["manual_override_active"])
        self.assertEqual(state["auto"]["last_decision"], "switched_to_capi_monthly")
        self.assertEqual(get_current_backend(), BACKEND_CAPI_MONTHLY)

    def test_midnight_monthly_reset_replaces_stale_low_quota_status(self):
        conf()["llm_backend"]["providers"]["capi_monthly"] = {
            "api_key": "TEST-MONTHLY-KEY",
            "default_daily_quota": 90,
        }
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)
        save_state({
            "current_backend": BACKEND_CAPI,
            "monthly_card": {
                "total": 90,
                "used": 90,
                "remaining": 0,
                "remaining_percent": 0,
                "progress": 100,
                "last_action": "monthly_quota_low",
            },
        })

        state = evaluate_midnight_backend_route(
            quota_payload=weekly_payload(used_percent=99),
            capi_connectivity_checker=lambda backend: True,
            now=now,
        )

        monthly = state["monthly_card"]
        self.assertEqual(state["current_backend"], BACKEND_CAPI_MONTHLY)
        self.assertEqual(state["auto"]["last_reason"], "daily_monthly_card_reset")
        self.assertEqual(monthly["last_action"], "daily_monthly_card_reset")
        self.assertEqual(monthly["remaining"], 90.0)
        self.assertEqual(monthly["total"], 90.0)
        self.assertEqual(state["backend_quota"][BACKEND_CAPI_MONTHLY]["remaining"], 90.0)

    def test_status_uses_latest_recorded_quota_for_current_backend(self):
        save_state({"current_backend": BACKEND_CAPI})
        record_capi_quota_check(
            BACKEND_CAPI,
            {"quota": {"mode": "total", "total": 500, "used": 120, "remaining": 380, "progress": 24}},
            action="manual_quota_query",
            now=datetime(2026, 5, 26, 8, 30),
        )

        text = describe_status()

        self.assertIn("- current_backend: capi", text)
        self.assertIn("- current_quota_backend: capi", text)
        self.assertIn("- current_quota_remaining: 380.0/500.0", text)
        self.assertIn("- current_quota_last_action: manual_quota_query", text)

    def test_status_does_not_show_stale_monthly_quota_for_non_monthly_backend(self):
        save_state({
            "current_backend": BACKEND_CAPI,
            "monthly_card": {
                "total": 90,
                "remaining": 12,
                "last_action": "kept_monthly",
            },
        })

        text = describe_status()

        self.assertIn("- current_backend: capi", text)
        self.assertNotIn("monthly_remaining", text)
        self.assertNotIn("current_quota_remaining", text)

    def test_status_uses_latest_recorded_codex_percent_quota(self):
        save_state({"current_backend": BACKEND_CODEX})
        record_codex_quota_check(
            weekly_payload(used_percent=41.2),
            action="manual_quota_query",
            now=datetime(2026, 5, 26, 8, 30),
        )

        text = describe_status()

        self.assertIn("- current_backend: codex", text)
        self.assertIn("- current_quota_backend: codex", text)
        self.assertIn("- current_quota_remaining_percent: 58.8", text)
        self.assertIn("- current_quota_used_percent: 41.2", text)

    def test_midnight_uses_codex_when_monthly_capi_probe_fails(self):
        conf()["llm_backend"]["providers"]["capi_monthly"] = {"api_key": "TEST-MONTHLY-KEY"}
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)
        checked = []

        state = evaluate_midnight_backend_route(
            quota_payload=weekly_payload(used_percent=99),
            capi_connectivity_checker=lambda backend: checked.append(backend) or False,
            now=now,
        )

        self.assertEqual(checked, [BACKEND_CAPI_MONTHLY])
        self.assertEqual(state["current_backend"], BACKEND_CODEX)
        self.assertEqual(state["auto"]["last_decision"], "switched_to_codex")
        self.assertEqual(state["auto"]["last_reason"], f"capi_connectivity_failed:{BACKEND_CAPI_MONTHLY}")

    def test_midnight_uses_codex_when_regular_capi_probe_fails_before_quota(self):
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)

        state = evaluate_midnight_backend_route(
            quota_payload=weekly_payload(used_percent=99),
            capi_connectivity_checker=lambda backend: False,
            now=now,
        )

        self.assertEqual(state["current_backend"], BACKEND_CODEX)
        self.assertTrue(state["auto_switch_latched"])
        self.assertEqual(state["auto"]["last_reason"], f"capi_connectivity_failed:{BACKEND_CAPI}")

    def test_midnight_probe_exception_falls_back_without_leaking_message(self):
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)

        def failing_checker(_backend):
            raise RuntimeError("SECRET-API-KEY")

        state = evaluate_midnight_backend_route(
            quota_payload=weekly_payload(used_percent=99),
            capi_connectivity_checker=failing_checker,
            now=now,
        )

        self.assertEqual(state["current_backend"], BACKEND_CODEX)
        self.assertEqual(state["auto"]["last_reason"], f"capi_connectivity_failed:{BACKEND_CAPI}")
        self.assertNotIn("SECRET", json.dumps(state, ensure_ascii=False))

    def test_capi_runtime_fallback_error_classifier_covers_riko_failure(self):
        self.assertTrue(is_capi_runtime_fallback_error("{'raw': 'Internal Server Error'} (Status: 500)"))
        self.assertTrue(is_capi_runtime_fallback_error("provider_network_error: ConnectionResetError(10054)"))
        self.assertTrue(is_capi_runtime_fallback_error("ConnectError: getaddrinfo failed"))
        self.assertTrue(is_capi_runtime_fallback_error("RemoteDisconnected: remote end closed connection"))
        self.assertTrue(is_capi_runtime_fallback_error("ReadTimeout: request timed out"))
        self.assertTrue(is_capi_runtime_fallback_error("Stream interrupted: ChunkedEncodingError"))
        self.assertTrue(is_capi_runtime_fallback_error("stream_read_error (Status: N/A, Code: stream_read_error, Type: upstream_error)"))
        self.assertTrue(is_capi_runtime_fallback_error("Concurrency limit exceeded for account (Status: 429)"))
        self.assertTrue(is_capi_runtime_fallback_error("Payment Required: monthly quota exhausted (Status: 402)"))
        self.assertFalse(is_capi_runtime_fallback_error("invalid_request_error (Status: 400)"))

    def test_capi_quota_exhausted_classifier_covers_monthly_card_errors(self):
        self.assertTrue(is_capi_quota_exhausted_error("insufficient_quota: monthly quota exhausted (Status: 402)"))
        self.assertTrue(is_capi_quota_exhausted_error("余额不足，月卡额度用完"))
        self.assertTrue(is_capi_quota_exhausted_error({"error": {"code": "quota_exceeded", "message": "billing hard limit"}}))
        self.assertFalse(is_capi_quota_exhausted_error("invalid api key (Status: 401)"))

    def test_monthly_runtime_quota_fallback_prefers_codex_when_under_fair_share(self):
        conf()["llm_backend"]["providers"] = {
            "capi": {"api_key": "QUOTA-KEY"},
            "capi_monthly": {"api_key": "MONTHLY-KEY"},
        }
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)

        backend = select_capi_runtime_fallback_backend(
            BACKEND_CAPI_MONTHLY,
            "insufficient_quota: monthly quota exhausted (Status: 402)",
            quota_payload=weekly_payload(used_percent=5),
            now=now,
        )

        self.assertEqual(backend, BACKEND_CODEX)

    def test_monthly_runtime_quota_fallback_uses_configured_custom_before_codex(self):
        conf()["llm_backend"]["providers"] = {
            "capi": {"api_key": "QUOTA-KEY"},
            "capi_monthly": {"api_key": "MONTHLY-KEY"},
            "custom_fast": {
                "model": "gpt-custom",
                "api_base": "https://custom.example/v1",
                "api_key": "CUSTOM-KEY",
            },
        }
        conf()["llm_backend"]["auto_switch"]["fallback_backends"] = ["custom_fast"]
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)

        with patch("common.llm_backend_router.check_capi_connectivity", return_value=True):
            backend = select_capi_runtime_fallback_backend(
                BACKEND_CAPI_MONTHLY,
                "insufficient_quota: monthly quota exhausted (Status: 402)",
                quota_payload=weekly_payload(used_percent=5),
                now=now,
            )

        self.assertEqual(backend, "custom_fast")

    def test_monthly_runtime_quota_fallback_uses_quota_card_when_codex_over_average(self):
        conf()["llm_backend"]["providers"] = {
            "capi": {"api_key": "QUOTA-KEY"},
            "capi_monthly": {"api_key": "MONTHLY-KEY"},
        }
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)

        backend = select_capi_runtime_fallback_backend(
            BACKEND_CAPI_MONTHLY,
            "insufficient_quota: monthly quota exhausted (Status: 402)",
            quota_payload=weekly_payload(used_percent=30),
            now=now,
        )

        self.assertEqual(backend, BACKEND_CAPI)

    def test_monthly_runtime_quota_fallback_uses_codex_when_quota_card_missing(self):
        conf()["llm_backend"]["providers"] = {
            "capi": {"api_key": ""},
            "capi_monthly": {"api_key": "MONTHLY-KEY"},
        }
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)

        backend = select_capi_runtime_fallback_backend(
            BACKEND_CAPI_MONTHLY,
            "insufficient_quota: monthly quota exhausted (Status: 402)",
            quota_payload=weekly_payload(used_percent=30),
            now=now,
        )

        self.assertEqual(backend, BACKEND_CODEX)

    def test_monthly_low_quota_falls_back_to_codex_when_under_fair_share(self):
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)

        state = select_backend_after_monthly_quota_low(weekly_payload(used_percent=5), now=now)

        self.assertEqual(state["current_backend"], BACKEND_CODEX)
        self.assertEqual(state["auto"]["last_decision"], "monthly_low_switched_to_codex")

    def test_monthly_low_quota_falls_back_to_quota_capi_when_codex_over_average(self):
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)

        state = select_backend_after_monthly_quota_low(weekly_payload(used_percent=30), now=now)

        self.assertEqual(state["current_backend"], BACKEND_CAPI)
        self.assertEqual(state["auto"]["last_decision"], "monthly_low_switched_to_capi")

    def test_monthly_backend_uses_monthly_provider_key(self):
        conf()["llm_backend"]["current_backend"] = BACKEND_CAPI_MONTHLY
        conf()["llm_backend"]["providers"] = {
            "capi": {
                "api_key": "QUOTA-KEY",
                "api_base": "https://quota.example/v1",
                "wire_api": "responses",
            },
            "capi_monthly": {
                "api_key": "MONTHLY-KEY",
                "api_base": "https://monthly.example/v1",
                "model": "gpt-5.5",
            },
        }
        with patch.dict("os.environ", {}, clear=True):
            routed = get_effective_openai_api_config()

        self.assertEqual(routed["backend"], BACKEND_CAPI_MONTHLY)
        self.assertEqual(routed["api_key"], "MONTHLY-KEY")
        self.assertEqual(routed["api_base"], "https://monthly.example/v1")

    def test_monthly_backend_inherits_blank_capi_provider_options(self):
        conf()["llm_backend"]["current_backend"] = BACKEND_CAPI_MONTHLY
        conf()["llm_backend"]["providers"] = {
            "capi": {
                "api_key": "QUOTA-KEY",
                "api_base": "https://quota.example/v1",
                "wire_api": "responses",
                "model": "gpt-5.5",
            },
            "capi_monthly": {
                "api_key": "MONTHLY-KEY",
                "api_base": "",
                "wire_api": "",
                "model": "",
            },
        }
        with patch.dict("os.environ", {}, clear=True):
            routed = get_effective_openai_api_config()

        self.assertEqual(routed["backend"], BACKEND_CAPI_MONTHLY)
        self.assertEqual(routed["api_key"], "MONTHLY-KEY")
        self.assertEqual(routed["api_base"], "https://quota.example/v1")
        self.assertEqual(routed["wire_api"], "responses")
        self.assertEqual(routed["model"], "gpt-5.5")
        self.assertEqual(routed["request_timeout_seconds"], 120.0)

    def test_capi_connectivity_uses_streaming_responses_probe(self):
        conf()["llm_backend"]["current_backend"] = BACKEND_CAPI_MONTHLY
        conf()["llm_backend"]["providers"] = {
            "capi": {
                "api_base": "https://quota.example/openai",
                "wire_api": "responses",
                "model": "gpt-5.5",
            },
            "capi_monthly": {
                "api_key": "MONTHLY-KEY",
            },
        }
        calls = []

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            def responses(self, **kwargs):
                calls.append(kwargs)
                return iter([{"type": "response.created"}])

        with patch("models.openai.openai_http_client.OpenAIHTTPClient", FakeClient):
            ok = check_capi_connectivity(BACKEND_CAPI_MONTHLY, timeout_seconds=3)

        self.assertTrue(ok)
        self.assertEqual(calls[0]["api_key"], "MONTHLY-KEY")
        self.assertEqual(calls[0]["api_base"], "https://quota.example/openai")
        self.assertEqual(calls[0]["stream"], True)
        self.assertEqual(calls[0]["timeout"], 3.0)

    def test_capi_connectivity_reports_stream_error_as_failure(self):
        conf()["llm_backend"]["current_backend"] = BACKEND_CAPI_MONTHLY
        conf()["llm_backend"]["providers"] = {
            "capi": {
                "api_base": "https://quota.example/openai",
                "wire_api": "responses",
                "model": "gpt-5.5",
            },
            "capi_monthly": {
                "api_key": "MONTHLY-KEY",
            },
        }

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            def responses(self, **_kwargs):
                return iter([{
                    "error": {"message": "upstream unavailable"},
                    "message": "upstream unavailable",
                    "status_code": 503,
                }])

        with patch("models.openai.openai_http_client.OpenAIHTTPClient", FakeClient):
            ok = check_capi_connectivity(BACKEND_CAPI_MONTHLY, timeout_seconds=3)

        self.assertFalse(ok)

    def test_quota_backend_defaults_to_capi_api_key_env(self):
        conf()["llm_backend"]["current_backend"] = BACKEND_CAPI
        conf()["llm_backend"]["providers"] = {"capi": {"model": "gpt-5.5"}}

        with patch.dict("os.environ", {"CAPI_API_KEY": "ENV-QUOTA-KEY"}, clear=True):
            routed = get_effective_openai_api_config()

        self.assertEqual(routed["backend"], BACKEND_CAPI)
        self.assertEqual(routed["api_key"], "ENV-QUOTA-KEY")

    def test_quota_backend_does_not_fallback_to_openai_api_key(self):
        previous_key = conf().get("open_ai_api_key")
        try:
            conf()["open_ai_api_key"] = "OPENAI-KEY"
            conf()["llm_backend"]["current_backend"] = BACKEND_CAPI
            conf()["llm_backend"]["providers"] = {"capi": {"model": "gpt-5.5"}}

            with patch.dict("os.environ", {"OPENAI_API_KEY": "OPENAI-ENV-KEY"}, clear=True):
                routed = get_effective_openai_api_config()
        finally:
            if previous_key is None:
                conf().pop("open_ai_api_key", None)
            else:
                conf()["open_ai_api_key"] = previous_key

        self.assertEqual(routed["backend"], BACKEND_CAPI)
        self.assertEqual(routed["api_key"], "")

    def test_missing_quota_payload_records_reason_without_switching(self):
        now = datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60)

        state = evaluate_auto_switch({}, now=now)

        self.assertEqual(state["auto"]["last_decision"], "kept")
        self.assertEqual(state["auto"]["last_reason"], "quota_window_missing")
        self.assertNotEqual(state.get("current_backend"), BACKEND_CODEX)

    def test_codex_auth_source_reads_token_and_account_id(self):
        auth_path = Path(self.tmp.name) / "auth.json"
        auth_path.write_text(
            json.dumps({
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "not-a-real-token",
                    "account_id": "acct_test",
                    "expires_at": 4102444800,
                },
            }),
            encoding="utf-8",
        )

        tokens = CodexAuthCredentialSource(str(auth_path)).resolve_access_tokens()

        self.assertEqual(tokens["access_token"], "not-a-real-token")
        self.assertEqual(tokens["account_id"], "acct_test")

    def test_restricted_grok_backend_override_is_per_whitelisted_user_only(self):
        save_state({"current_backend": BACKEND_CAPI})
        whitelisted = SimpleNamespace(
            actor_id="wecom_bot:u1",
            raw_user_id="u1",
            memory_user_id="m1",
            display_name="山海入梦来",
            role="user",
            is_admin=False,
        )
        normal = SimpleNamespace(
            actor_id="wecom_bot:u2",
            raw_user_id="u2",
            memory_user_id="m2",
            display_name="普通用户",
            role="user",
            is_admin=False,
        )

        state = set_user_backend_override(whitelisted, BACKEND_GROK, reason="unit_test")

        self.assertEqual(state["current_backend"], BACKEND_CAPI)
        self.assertEqual(get_current_backend(), BACKEND_CAPI)
        self.assertEqual(get_current_backend_for_profile(whitelisted), BACKEND_GROK)
        self.assertEqual(get_effective_model(BACKEND_GROK), "grok-4.3")
        self.assertEqual(get_current_backend_for_profile(normal), BACKEND_CAPI)

    def test_actor_status_reports_personal_grok_before_shared_backend(self):
        admin = SimpleNamespace(
            actor_id="web:admin",
            raw_user_id="web:admin",
            memory_user_id="admin",
            display_name="Admin",
            role="admin",
            is_admin=True,
        )
        save_state({"current_backend": BACKEND_CAPI_MONTHLY})
        set_user_backend_override(admin, BACKEND_GROK, reason="unit_test")

        text = describe_status(admin)

        self.assertIn("- current_backend: grok", text)
        self.assertIn("- effective_model: grok-4.3", text)
        self.assertIn("- shared_gpt_backend: capi_monthly", text)
        self.assertIn("- personal_backend_override: True", text)
        self.assertNotIn("- current_backend: capi_monthly", text)

    def test_normal_actor_status_reports_shared_backend_without_personal_lines(self):
        normal = SimpleNamespace(
            actor_id="wecom_bot:u2",
            raw_user_id="u2",
            memory_user_id="m2",
            display_name="普通用户",
            role="user",
            is_admin=False,
        )
        save_state({"current_backend": BACKEND_CAPI_MONTHLY})

        text = describe_status(normal)

        self.assertIn("- current_backend: capi_monthly", text)
        self.assertNotIn("shared_gpt_backend", text)
        self.assertNotIn("personal_backend_override", text)

    def test_midnight_auto_switch_keeps_grok_user_override_out_of_global_route(self):
        whitelisted = SimpleNamespace(
            actor_id="wecom_bot:u1",
            raw_user_id="u1",
            memory_user_id="m1",
            display_name="山海入梦来",
            role="user",
            is_admin=False,
        )
        save_state({"current_backend": BACKEND_CAPI})
        set_user_backend_override(whitelisted, BACKEND_GROK, reason="unit_test")

        state = evaluate_midnight_backend_route(
            quota_payload=weekly_payload(used_percent=5),
            capi_connectivity_checker=lambda backend: True,
            now=datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60),
        )

        self.assertEqual(state["current_backend"], BACKEND_CODEX)
        self.assertEqual(get_current_backend_for_profile(whitelisted), BACKEND_GROK)

    def test_admin_custom_backend_override_uses_saved_provider_without_global_switch(self):
        admin = SimpleNamespace(
            actor_id="admin",
            raw_user_id="admin",
            memory_user_id="admin",
            display_name="Admin",
            role="admin",
            is_admin=True,
        )
        conf()["llm_backend"]["providers"]["custom_fast"] = {
            "model": "gpt-custom",
            "api_base": "https://custom.example/v1",
            "wire_api": "responses",
            "api_key": "CUSTOM-KEY",
        }
        save_state({"current_backend": BACKEND_CAPI})

        set_user_backend_override(admin, "custom_fast", reason="unit_test")
        routed = get_effective_openai_api_config("custom_fast")

        self.assertEqual(get_current_backend(), BACKEND_CAPI)
        self.assertEqual(get_current_backend_for_profile(admin), "custom_fast")
        self.assertIn("custom_fast", available_actor_backends(admin))
        self.assertEqual(routed["backend"], "custom_fast")
        self.assertEqual(routed["model"], "gpt-custom")

    def test_global_custom_backend_uses_saved_provider(self):
        conf()["llm_backend"]["providers"]["custom_fast"] = {
            "label": "Custom Fast",
            "model": "gpt-custom",
            "api_base": "https://custom.example/v1",
            "wire_api": "responses",
            "api_key": "CUSTOM-KEY",
        }

        set_current_backend("custom_fast", reason="unit_test")
        routed = get_effective_openai_api_config()

        self.assertEqual(get_current_backend(), "custom_fast")
        self.assertEqual(get_effective_model(), "gpt-custom")
        self.assertEqual(routed["backend"], "custom_fast")
        self.assertEqual(routed["api_key"], "CUSTOM-KEY")
        self.assertEqual(routed["api_base"], "https://custom.example/v1")

    def test_unknown_backend_selection_is_rejected(self):
        with self.assertRaises(ValueError):
            set_current_backend("missing_provider", reason="unit_test")

        self.assertEqual(get_current_backend(), BACKEND_CAPI)

    def test_unknown_backend_config_lookup_is_rejected(self):
        with self.assertRaises(ValueError):
            get_effective_openai_api_config("missing_provider")

    def test_unknown_backend_model_lookup_and_probe_fail_closed(self):
        with self.assertRaises(ValueError):
            get_effective_model("missing_provider")

        self.assertFalse(check_capi_connectivity("missing_provider"))

    def test_global_grok_bot_type_is_ignored_without_restricted_backend(self):
        previous_bot_type = conf().get("bot_type")
        previous_model = conf().get("model")
        try:
            conf()["bot_type"] = "grok"
            conf()["model"] = "grok-4.3"

            self.assertEqual(resolve_configured_chat_bot_type(), const.OPENAI)
            self.assertEqual(get_effective_chat_bot_type("grok-4.3", BACKEND_CAPI), const.OPENAI)
        finally:
            if previous_bot_type is None:
                conf().pop("bot_type", None)
            else:
                conf()["bot_type"] = previous_bot_type
            if previous_model is None:
                conf().pop("model", None)
            else:
                conf()["model"] = previous_model

    def test_custom_provider_does_not_inherit_capi_credentials(self):
        conf()["llm_backend"]["providers"] = {
            "capi": {
                "api_key": "CAPI-KEY",
                "api_base": "https://capi.example/v1",
                "model": "gpt-capi",
            },
            "custom_fast": {
                "model": "gpt-custom",
            },
        }

        with patch.dict("os.environ", {}, clear=True):
            routed = get_effective_openai_api_config("custom_fast")

        self.assertEqual(routed["backend"], "custom_fast")
        self.assertEqual(routed["api_key"], "")
        self.assertEqual(routed["api_base"], "")
        self.assertEqual(routed["model"], "gpt-custom")

    def test_midnight_connectivity_failure_uses_configured_custom_fallback(self):
        conf()["llm_backend"]["providers"]["custom_fast"] = {
            "label": "Custom Fast",
            "model": "gpt-custom",
            "api_base": "https://custom.example/v1",
            "api_key": "CUSTOM-KEY",
        }
        conf()["llm_backend"]["auto_switch"]["fallback_backends"] = ["custom_fast"]
        checked = []

        state = evaluate_midnight_backend_route(
            quota_payload=weekly_payload(used_percent=99),
            capi_connectivity_checker=lambda backend: checked.append(backend) or backend == "custom_fast",
            now=datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60),
        )

        self.assertEqual(checked, [BACKEND_CAPI, "custom_fast"])
        self.assertEqual(state["current_backend"], "custom_fast")
        self.assertEqual(state["auto"]["last_decision"], "switched_to_custom_fast")
        self.assertEqual(state["auto"]["last_reason"], f"capi_connectivity_failed:{BACKEND_CAPI}")

    def test_monthly_low_quota_uses_configured_custom_fallback(self):
        conf()["llm_backend"]["providers"]["custom_fast"] = {
            "label": "Custom Fast",
            "model": "gpt-custom",
            "api_base": "https://custom.example/v1",
            "api_key": "CUSTOM-KEY",
        }
        conf()["llm_backend"]["auto_switch"]["fallback_backends"] = ["custom_fast"]

        with patch("common.llm_backend_router.check_capi_connectivity", return_value=True):
            state = select_backend_after_monthly_quota_low(
                weekly_payload(used_percent=5),
                now=datetime.fromtimestamp(4102444800 - 6 * 24 * 60 * 60),
            )

        self.assertEqual(state["current_backend"], "custom_fast")
        self.assertEqual(state["auto"]["last_decision"], "monthly_low_switched_to_custom_fast")

    def test_chatgpt_backend_override_uses_custom_provider_model(self):
        previous_model = conf().get("model")
        try:
            conf()["model"] = "global-model"
            conf()["llm_backend"]["providers"]["custom_fast"] = {
                "model": "provider-model",
                "api_base": "https://custom.example/v1",
                "api_key": "CUSTOM-KEY",
            }

            from models.chatgpt.chat_gpt_bot import ChatGPTBot

            bot = ChatGPTBot(backend_override="custom_fast")

            self.assertEqual(bot.args["model"], "provider-model")
        finally:
            if previous_model is None:
                conf().pop("model", None)
            else:
                conf()["model"] = previous_model

    def test_chatgpt_custom_backend_does_not_use_global_openai_credentials(self):
        previous_key = conf().get("open_ai_api_key")
        previous_base = conf().get("open_ai_api_base")
        try:
            conf()["open_ai_api_key"] = "GLOBAL-OPENAI-KEY"
            conf()["open_ai_api_base"] = "https://global.example/v1"
            conf()["llm_backend"]["providers"]["custom_fast"] = {
                "model": "provider-model",
            }

            from models.chatgpt.chat_gpt_bot import ChatGPTBot

            bot = ChatGPTBot(backend_override="custom_fast")

            self.assertEqual(bot._api_key, "")
            self.assertIsNone(bot._api_base)
        finally:
            if previous_key is None:
                conf().pop("open_ai_api_key", None)
            else:
                conf()["open_ai_api_key"] = previous_key
            if previous_base is None:
                conf().pop("open_ai_api_base", None)
            else:
                conf()["open_ai_api_base"] = previous_base

    def test_chatgpt_custom_backend_with_key_requires_explicit_base(self):
        conf()["llm_backend"]["providers"]["custom_fast"] = {
            "model": "provider-model",
            "api_key": "CUSTOM-KEY",
        }

        from models.chatgpt.chat_gpt_bot import ChatGPTBot

        bot = ChatGPTBot(backend_override="custom_fast")
        with patch.object(bot, "_get_http_client") as client_factory:
            result = bot.call_with_tools(
                messages=[{"role": "user", "content": "hi"}],
                stream=False,
                model="provider-model",
            )

        self.assertTrue(result["error"])
        self.assertIn("requires api_base", result["message"])
        client_factory.assert_not_called()

    def test_chatgpt_custom_backend_plain_chat_requires_explicit_base(self):
        conf()["llm_backend"]["providers"]["custom_fast"] = {
            "model": "provider-model",
            "api_key": "CUSTOM-KEY",
        }

        from models.chatgpt.chat_gpt_bot import ChatGPTBot

        bot = ChatGPTBot(backend_override="custom_fast")
        with self.assertRaisesRegex(ValueError, "requires api_base"):
            bot._create_model_response(
                messages=[{"role": "user", "content": "hi"}],
                model="provider-model",
            )


if __name__ == "__main__":
    unittest.main()
