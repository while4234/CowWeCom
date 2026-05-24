import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from common.codex_quota_logic import decide_codex_auto_switch
from common.llm_backend_router import (
    BACKEND_CODEX,
    evaluate_auto_switch,
    get_current_backend,
    load_state,
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
