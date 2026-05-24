import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.capi_monthly_monitor import maybe_check_capi_monthly_after_task
from common.llm_backend_router import BACKEND_CAPI, BACKEND_CAPI_MONTHLY, BACKEND_CODEX, get_current_backend, load_state
from config import conf


def weekly_payload(used_percent=5, resets_at=4102444800):
    return {
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "limitName": "Codex",
                "secondary": {
                    "windowDurationMins": 10080,
                    "usedPercent": used_percent,
                    "resetsAt": resets_at,
                },
            }
        }
    }


class TestCapiMonthlyMonitor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_backend_config = conf().get("llm_backend")
        conf()["llm_backend"] = {
            "current_backend": BACKEND_CAPI_MONTHLY,
            "state_path": str(Path(self.tmp.name) / "state.json"),
            "auto_switch": {
                "enabled": True,
                "fair_share_days": 7,
                "min_remaining_percent": 15,
                "monthly_post_task_check_enabled": True,
                "monthly_min_remaining_percent": 10,
            },
            "providers": {
                "capi_monthly": {"api_key": "TEST-MONTHLY-KEY"},
            },
        }

    def tearDown(self):
        if self.previous_backend_config is None:
            conf().pop("llm_backend", None)
        else:
            conf()["llm_backend"] = self.previous_backend_config
        self.tmp.cleanup()

    def test_noop_when_task_did_not_use_monthly_backend(self):
        with patch("common.capi_monthly_monitor._query_monthly_snapshot") as query:
            state = maybe_check_capi_monthly_after_task(BACKEND_CAPI)

        query.assert_not_called()
        self.assertEqual(state, {})

    def test_keeps_monthly_when_remaining_is_above_threshold(self):
        with patch(
            "common.capi_monthly_monitor._query_monthly_snapshot",
            return_value={"quota": {"mode": "daily", "total": 90, "used": 9, "remaining": 81, "progress": 10}},
        ):
            state = maybe_check_capi_monthly_after_task(BACKEND_CAPI_MONTHLY)

        self.assertEqual(load_state()["monthly_card"]["last_action"], "kept_monthly")
        self.assertEqual(state["monthly_card"]["remaining_percent"], 90)
        self.assertEqual(get_current_backend(), BACKEND_CAPI_MONTHLY)

    def test_low_monthly_quota_switches_to_codex_when_allowed(self):
        with (
            patch(
                "common.capi_monthly_monitor._query_monthly_snapshot",
                return_value={"quota": {"mode": "daily", "total": 90, "used": 82, "remaining": 8, "progress": 91.1}},
            ),
            patch("common.capi_monthly_monitor._query_codex_quota_json", return_value=weekly_payload(used_percent=5)),
        ):
            state = maybe_check_capi_monthly_after_task(BACKEND_CAPI_MONTHLY)

        self.assertEqual(state["current_backend"], BACKEND_CODEX)
        self.assertEqual(state["auto"]["last_decision"], "monthly_low_switched_to_codex")

    def test_low_monthly_quota_switches_to_quota_card_when_codex_is_over_average(self):
        with (
            patch(
                "common.capi_monthly_monitor._query_monthly_snapshot",
                return_value={"quota": {"mode": "daily", "total": 90, "used": 82, "remaining": 8, "progress": 91.1}},
            ),
            patch("common.capi_monthly_monitor._query_codex_quota_json", return_value=weekly_payload(used_percent=30)),
        ):
            state = maybe_check_capi_monthly_after_task(BACKEND_CAPI_MONTHLY)

        self.assertEqual(state["current_backend"], BACKEND_CAPI)
        self.assertEqual(state["auto"]["last_decision"], "monthly_low_switched_to_capi")

    def test_low_monthly_quota_falls_back_to_quota_card_when_codex_query_fails(self):
        with (
            patch(
                "common.capi_monthly_monitor._query_monthly_snapshot",
                return_value={"quota": {"mode": "daily", "total": 90, "used": 82, "remaining": 8, "progress": 91.1}},
            ),
            patch("common.capi_monthly_monitor._query_codex_quota_json", side_effect=RuntimeError("offline")),
        ):
            state = maybe_check_capi_monthly_after_task(BACKEND_CAPI_MONTHLY)

        self.assertEqual(state["current_backend"], BACKEND_CAPI)
        self.assertEqual(state["auto"]["last_reason"], "codex_quota_query_failed")


if __name__ == "__main__":
    unittest.main()
