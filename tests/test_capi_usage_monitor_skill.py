import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_capi_usage_module():
    script = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "capi-usage-monitor"
        / "scripts"
        / "capi_usage.py"
    )
    spec = importlib.util.spec_from_file_location("capi_usage_skill", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capi_usage = _load_capi_usage_module()


class TestCapiUsageMonitorSkill(unittest.TestCase):
    def test_snapshot_uses_chatlog_for_filtered_period_usage(self):
        args = capi_usage.build_parser().parse_args([
            "snapshot",
            "--api-key",
            "TEST-CAPI-KEY-000001",
            "--period",
            "today",
            "--summary-page-size",
            "100",
        ])
        user = {
            "account": "TEST-CAPI-KEY-000001",
            "id": 77109,
            "day_score_used": 4.817,
            "score_used": 0,
            "vip": {
                "score": 0,
                "day_score": 0,
                "expire_at": 1783478466212,
            },
        }
        historical_usage = [
            {"time": "2026-05-15 17:00:00", "model": "gpt-5.5", "score": 355.9635},
            {"time": "2026-05-22 14:00:00", "model": "gpt-5.4-mini", "score": 0.2235},
        ]
        today_chatlog = {
            "total": 2,
            "list": [
                {"create_at": 1779432694000, "model": "gpt-5.5", "score": 4.0, "score_used": 4.0},
                {"create_at": 1779433373000, "model": "gpt-5.4-mini", "score": 0.817, "score_used": 4.817},
            ],
        }

        with (
            patch.object(capi_usage, "login", return_value={"token": "test-token"}),
            patch.object(capi_usage, "whoami", return_value=user),
            patch.object(capi_usage, "usages", return_value=historical_usage) as usages_mock,
            patch.object(capi_usage, "chatlog_page", return_value=today_chatlog),
        ):
            result = capi_usage.snapshot(args)

        usages_mock.assert_not_called()
        summary = result["usage_summary"]
        self.assertEqual(summary["source"], "chatlog")
        self.assertEqual(summary["entries"], 2)
        self.assertAlmostEqual(summary["total_cost"], 4.817)
        self.assertEqual(summary["by_model"]["gpt-5.5"], 4.0)
        self.assertEqual(result["chatlog_total"], 2)
        self.assertEqual(result["quota"]["daily"], 90.0)
        self.assertEqual(result["quota"]["mode"], "daily")
        self.assertFalse(result["quota"]["total_mode"])
        self.assertEqual(result["quota"]["used"], 4.817)
        self.assertAlmostEqual(result["quota"]["remaining"], 85.183)

    def test_total_quota_card_uses_score_used_and_remaining_total(self):
        user = {
            "score": 500,
            "score_used": 2043.5407484375066,
            "day_score": 0,
            "day_score_used": 0,
            "vip": {
                "score": 2500,
                "score_used": 1917.304538612506,
                "day_score": 0,
                "expire_at": 1781943430467,
            },
        }

        quota = capi_usage.quota_summary(user)

        self.assertEqual(quota["mode"], "total")
        self.assertTrue(quota["total_mode"])
        self.assertEqual(quota["total"], 2500.0)
        self.assertEqual(quota["used"], 2043.5407484375066)
        self.assertAlmostEqual(quota["remaining"], 456.45925156249336)
        self.assertEqual(quota["daily"], 90.0)
        self.assertEqual(quota["progress"], 81.7)

    def test_text_output_adapts_daily_monthly_card(self):
        result = {
            "account": "650FF3***0997",
            "period": "today",
            "quota": {
                "mode": "daily",
                "total": 90.0,
                "daily": 90.0,
                "used": 9.5,
                "remaining": 80.5,
                "progress": 10.6,
                "expire_at": "2026-07-08T10:41:06+08:00",
            },
            "usage_summary": {"entries": 2, "total_cost": 9.5, "by_model": {"gpt-5.5": 9.5}},
        }

        text = capi_usage.format_snapshot_text(result)

        self.assertIn("card_type: daily/monthly card", text)
        self.assertIn("today_used: 9.5 / 90", text)
        self.assertIn("today_remaining: 80.5", text)
        self.assertIn("daily_reset: 00:00", text)
        self.assertIn("expires_at: 2026-07-08T10:41:06", text)

    def test_text_output_adapts_total_quota_card(self):
        result = {
            "account": "quota-card",
            "period": "today",
            "quota": {
                "mode": "total",
                "total": 2500.0,
                "daily": 90.0,
                "used": 2043.54,
                "remaining": 456.46,
                "progress": 81.7,
            },
            "usage_summary": {"entries": 1, "total_cost": 3.25, "by_model": {}},
        }

        text = capi_usage.format_snapshot_text(result)

        self.assertIn("card_type: quota card", text)
        self.assertIn("total_used: 2043.54 / 2500", text)
        self.assertIn("total_remaining: 456.46", text)
        self.assertIn("daily_reference_quota: 90", text)

    def test_usages_source_is_still_available_for_backend_debugging(self):
        args = capi_usage.build_parser().parse_args([
            "snapshot",
            "--api-key",
            "TEST-CAPI-KEY-000001",
            "--period",
            "today",
            "--usage-source",
            "usages",
        ])

        with (
            patch.object(capi_usage, "login", return_value={"token": "test-token"}),
            patch.object(capi_usage, "whoami", return_value={"vip": {"score": 0, "day_score": 90}, "day_score_used": 1}),
            patch.object(capi_usage, "usages", return_value=[{"model": "gpt-5.5", "score": 360.0}]),
            patch.object(capi_usage, "chatlog_page") as chatlog_mock,
        ):
            result = capi_usage.snapshot(args)

        chatlog_mock.assert_not_called()
        self.assertEqual(result["usage_summary"]["source"], "usages")
        self.assertEqual(result["usage_summary"]["total_cost"], 360.0)

    def test_default_key_source_uses_capi_api_key(self):
        args = capi_usage.build_parser().parse_args(["snapshot", "--period", "today"])

        with patch.dict("os.environ", {"CAPI_API_KEY": "TEST-CAPI-ENV-KEY"}, clear=True):
            self.assertEqual(capi_usage.api_key_from_args(args), "TEST-CAPI-ENV-KEY")

    def test_default_key_source_does_not_use_openai_api_key(self):
        args = capi_usage.build_parser().parse_args(["snapshot", "--period", "today"])

        with patch.dict("os.environ", {"OPENAI_API_KEY": "OPENAI-ENV-KEY"}, clear=True):
            with self.assertRaises(SystemExit) as raised:
                capi_usage.api_key_from_args(args)

        self.assertIn("CAPI_API_KEY", str(raised.exception))
        self.assertNotIn("OPENAI_API_KEY", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
