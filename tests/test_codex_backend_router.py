import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from common.codex_quota_logic import decide_codex_auto_switch
from common.llm_backend_router import (
    BACKEND_CAPI,
    BACKEND_CAPI_MONTHLY,
    BACKEND_CODEX,
    evaluate_auto_switch,
    evaluate_midnight_backend_route,
    get_effective_openai_api_config,
    get_current_backend,
    load_state,
    select_backend_after_monthly_quota_low,
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
        }

    def tearDown(self):
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

        state = evaluate_midnight_backend_route(quota_payload=weekly_payload(used_percent=5), now=now)

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

        state = evaluate_midnight_backend_route(quota_payload=weekly_payload(used_percent=30), now=now)

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

        state = evaluate_midnight_backend_route(quota_payload=weekly_payload(used_percent=99), now=now)

        self.assertEqual(state["current_backend"], BACKEND_CAPI_MONTHLY)
        self.assertFalse(state["manual_override_active"])
        self.assertEqual(state["auto"]["last_decision"], "switched_to_capi_monthly")
        self.assertEqual(get_current_backend(), BACKEND_CAPI_MONTHLY)

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


if __name__ == "__main__":
    unittest.main()
