import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEDGER_SCRIPT = PROJECT_ROOT / "skills" / "china-expense-ledger" / "scripts" / "ledger.py"
EXAMPLES_DIR = PROJECT_ROOT / "skills" / "china-expense-ledger" / "resources" / "examples"


def load_ledger_module():
    spec = importlib.util.spec_from_file_location("china_expense_ledger", LEDGER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ledger = load_ledger_module()


class ChinaExpenseLedgerTest(unittest.TestCase):
    def open_temp_db(self, root: Path) -> sqlite3.Connection:
        conn = ledger.open_db(root / "ledger.db")
        ledger.init_db(conn)
        return conn

    def test_init_creates_core_tables_and_app_signatures(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                for table in (
                    "transactions",
                    "item_rules",
                    "merchant_rules",
                    "app_signatures",
                    "corrections",
                    "import_batches",
                    "bill_contexts",
                    "screenshot_rules",
                    "summary_cache",
                ):
                    self.assertTrue(ledger.table_exists(conn, table), table)

                apps = {
                    row["app"]
                    for row in conn.execute("SELECT app FROM app_signatures").fetchall()
                }
                self.assertIn("支付宝", apps)
                self.assertIn("微信支付", apps)
                self.assertIn("美团外卖", apps)
            finally:
                conn.close()

    def test_db_path_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = Path(tmp) / "custom" / "ledger.db"
            with patch.dict(os.environ, {"CHINA_EXPENSE_LEDGER_DB": str(expected)}):
                self.assertEqual(ledger.db_path_from_env(), expected.resolve())

    def test_record_text_taobao_alipay_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                result = ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "source_type": "text",
                        "raw_text": "刚才在淘宝买了数据线 19.9，支付宝付的",
                        "amount_cents": 1990,
                        "direction": "expense",
                        "order_platform": "淘宝",
                        "payment_app": "支付宝",
                        "item_name": "数据线",
                    },
                )

                transaction = result["transaction"]
                self.assertTrue(result["inserted"])
                self.assertEqual(transaction["amount_cents"], 1990)
                self.assertEqual(transaction["currency"], "CNY")
                self.assertEqual(transaction["direction"], "expense")
                self.assertEqual(transaction["order_platform"], "淘宝")
                self.assertEqual(transaction["payment_app"], "支付宝")
                self.assertEqual(transaction["category"], "数码家电")
            finally:
                conn.close()

    def test_amount_number_is_yuan_while_amount_cents_is_cents(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                yuan_result = ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "source_type": "text",
                        "raw_text": "今天京东买硬盘 499",
                        "amount": 499,
                        "direction": "expense",
                        "item_name": "硬盘",
                    },
                )
                cents_result = ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "source_type": "text",
                        "raw_text": "数据线",
                        "amount_cents": 1990,
                        "direction": "expense",
                        "item_name": "数据线",
                    },
                )

                self.assertEqual(yuan_result["transaction"]["amount_cents"], 49900)
                self.assertEqual(cents_result["transaction"]["amount_cents"], 1990)
            finally:
                conn.close()

    def test_bill_amount_prefers_exact_deal_price_over_later_rounded_summary(self):
        self.assertEqual(ledger.extract_amount_cents_from_text("成交价 99.88 元，约 100 元"), 9988)
        self.assertEqual(ledger.extract_amount_cents_from_text("成交价 ¥99.88 订单编号 123456789"), 9988)
        self.assertEqual(ledger.extract_amount_cents_from_text("支付金额 ¥99.88"), 9988)

    def test_bill_confirmation_does_not_override_amount_without_explicit_amount_marker(self):
        fields = ledger.fields_from_answer_text("这是一个咸鱼账单，我买的是 100G 中转API的token")

        self.assertNotIn("amount_cents", fields)

        explicit = ledger.fields_from_answer_text("金额是 100")
        self.assertEqual(explicit["amount_cents"], 10000)

    def test_image_payload_prefers_exact_raw_text_amount_over_rounded_model_amount(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                result = ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "source_type": "image",
                        "raw_text": "卖家已发货 待确认收货 成交价 ¥99.88 约100元 订单编号 3303779739048007681",
                        "amount_cents": 10000,
                        "direction": "expense",
                        "order_platform": "闲鱼",
                        "payment_app": "支付宝",
                        "item_name": "中转API额度卡",
                        "category": "AI工具",
                    },
                )

                self.assertTrue(result["ok"])
                self.assertEqual(result["transaction"]["amount_cents"], 9988)
            finally:
                conn.close()

    def test_record_accepts_model_normalized_occurred_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                result = ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "source_type": "text",
                        "raw_text": "昨天买了咖啡 18",
                        "occurred_at": "2026-05-23",
                        "occurred_at_text": "昨天",
                        "occurred_at_resolution": "模型按用户发送时间解析为 2026-05-23",
                        "amount": "18",
                        "direction": "expense",
                        "category": "餐饮",
                    },
                )

                transaction = result["transaction"]
                self.assertTrue(result["inserted"])
                self.assertTrue(transaction["occurred_at"].startswith("2026-05-23"))
                details = json.loads(transaction["item_details_json"])
                self.assertEqual(details["date_resolution"]["occurred_at_text"], "昨天")
                self.assertEqual(details["date_resolution"]["occurred_at_assumed"], False)
            finally:
                conn.close()

    def test_record_defaults_missing_occurred_at_to_today_with_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                result = ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "source_type": "image",
                        "raw_text": "微信支付 支付成功 商品: 咖啡 支付金额 ¥18.00",
                        "amount": "18",
                        "direction": "expense",
                        "category": "餐饮",
                        "occurred_at_assumed": True,
                    },
                )

                transaction = result["transaction"]
                today = ledger.now_iso()[:10]
                self.assertTrue(transaction["occurred_at"].startswith(today))
                details = json.loads(transaction["item_details_json"])
                self.assertTrue(details["date_resolution"]["occurred_at_assumed"])
            finally:
                conn.close()

    def test_record_rejects_natural_language_occurred_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                result = ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "source_type": "text",
                        "raw_text": "今年母亲节买花 88",
                        "occurred_at": "今年母亲节",
                        "amount": "88",
                        "direction": "expense",
                        "category": "其他",
                    },
                )

                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "invalid_occurred_at")
                count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                conn.close()

    def test_read_json_file_accepts_utf8_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload_file = Path(tmp) / "payload.json"
            payload_file.write_text('{"amount": "19.90"}', encoding="utf-8-sig")
            args = type("Args", (), {"json_text": None, "file": str(payload_file)})()

            payload = ledger.read_json_arg(args)

            self.assertEqual(payload["amount"], "19.90")

    def test_record_vision_wechat_meituan_transaction_needs_item_clarification(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                result = ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "source_type": "image",
                        "source_app": "微信支付",
                        "payment_app": "微信支付",
                        "order_platform": "美团外卖",
                        "merchant": "美团外卖",
                        "amount_cents": 2850,
                        "direction": "expense",
                        "raw_text": "微信支付 支付成功 美团外卖 ¥28.50",
                        "needs_clarification": ["截图中商品名不清楚，请补充。"],
                    },
                )

                transaction = result["transaction"]
                self.assertEqual(transaction["payment_app"], "微信支付")
                self.assertEqual(transaction["order_platform"], "美团外卖")
                self.assertEqual(transaction["merchant"], "美团外卖")
                self.assertIsNone(transaction["item_name"])
                self.assertEqual(transaction["category"], "外卖")
                self.assertEqual(transaction["source_type"], "image")
                self.assertEqual(transaction["status"], "pending")
                self.assertIn("needs_clarification", result)
            finally:
                conn.close()

    def test_refund_is_not_recorded_as_income(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                result = ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "source_type": "text",
                        "raw_text": "收到退款 88，淘宝退的",
                        "amount": "88",
                    },
                )

                transaction = result["transaction"]
                self.assertEqual(transaction["direction"], "refund")
                self.assertEqual(transaction["category"], "退款")
                self.assertNotEqual(transaction["direction"], "income")
            finally:
                conn.close()

    def test_unclear_transfer_remains_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                result = ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "source_type": "text",
                        "raw_text": "给小李转账 200",
                        "amount": "200",
                        "direction": "transfer",
                        "merchant": "小李",
                    },
                )

                transaction = result["transaction"]
                self.assertEqual(transaction["direction"], "transfer")
                self.assertEqual(transaction["status"], "pending")
                self.assertEqual(transaction["category"], "转账")
                self.assertIn("needs_clarification", result)
            finally:
                conn.close()

    def test_alipay_csv_import_and_duplicate_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                sample = EXAMPLES_DIR / "alipay_csv_sample.csv"
                first = ledger.import_file(conn, sample, "alipay", "u1")
                second = ledger.import_file(conn, sample, "alipay", "u1")

                self.assertTrue(first["ok"])
                self.assertEqual(first["batch"]["row_count"], 3)
                self.assertEqual(first["batch"]["inserted_count"], 3)
                self.assertEqual(first["batch"]["duplicate_count"], 0)
                self.assertEqual(second["batch"]["inserted_count"], 0)
                self.assertEqual(second["batch"]["duplicate_count"], 3)
            finally:
                conn.close()

    def test_wechat_csv_import_parses_payment_and_refund(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                sample = EXAMPLES_DIR / "wechat_csv_sample.csv"
                result = ledger.import_file(conn, sample, "wechat", "u1")
                rows = ledger.query_transactions(conn, "u1", "all", 10)

                self.assertTrue(result["ok"])
                self.assertEqual(result["batch"]["inserted_count"], 3)
                self.assertTrue(all(row["payment_app"] == "微信支付" for row in rows))
                self.assertTrue(any(row["direction"] == "refund" for row in rows))
                self.assertTrue(any(row["amount_cents"] == 2850 for row in rows))
            finally:
                conn.close()

    def test_xlsx_import_returns_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_xlsx = root / "bill.xlsx"
            fake_xlsx.write_bytes(b"not-a-real-workbook")
            conn = self.open_temp_db(root)
            try:
                result = ledger.import_file(conn, fake_xlsx, "auto", "u1")

                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "unsupported_format")
                self.assertEqual(result["format"], "xlsx")
            finally:
                conn.close()

    def test_correct_records_correction_and_updates_learning_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                result = ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "source_type": "text",
                        "raw_text": "盒马买菜 67.2",
                        "amount": "67.2",
                        "merchant": "盒马鲜生",
                        "item_name": "买菜",
                    },
                )
                args = type(
                    "Args",
                    (),
                    {
                        "transaction_id": result["transaction"]["id"],
                        "field": "category",
                        "new_value": "生鲜买菜",
                    },
                )()
                correction = ledger.correct_transaction(conn, args)

                self.assertTrue(correction["ok"])
                self.assertEqual(correction["field"], "category")
                self.assertEqual(correction["new_value"], "生鲜买菜")
                self.assertEqual(correction["learned_rule_type"], "merchant_rules")
                correction_count = conn.execute("SELECT COUNT(*) FROM corrections").fetchone()[0]
                rule_count = conn.execute("SELECT COUNT(*) FROM merchant_rules").fetchone()[0]
                self.assertEqual(correction_count, 1)
                self.assertEqual(rule_count, 1)
            finally:
                conn.close()

    def test_summary_groups_by_direction_and_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "source_type": "text",
                        "raw_text": "美团外卖黄焖鸡 28.5",
                        "amount": "28.5",
                        "direction": "expense",
                        "category": "外卖",
                    },
                )
                ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "source_type": "text",
                        "raw_text": "收到退款 8",
                        "amount": "8",
                    },
                )

                summary = ledger.summarize_transactions(conn, "u1", "all")

                self.assertEqual(summary["count"], 2)
                self.assertEqual(summary["totals"]["expense_cents"], 2850)
                self.assertEqual(summary["totals"]["refund_cents"], 800)
                self.assertEqual(summary["by_category"]["外卖"], 2850)
                self.assertEqual(summary["by_category"]["退款"], 800)
            finally:
                conn.close()

    def test_analyze_bill_auto_records_clear_private_bill_and_updates_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                result = ledger.analyze_bill_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "chat_id": "chat-a",
                        "record_id": "image-a",
                        "occurred_at": "2026-05-23",
                        "raw_text": "微信支付 支付成功 美团外卖 商品: 黄焖鸡 支付金额 ¥28.50 交易单号 123456789012",
                    },
                )

                self.assertTrue(result["ok"])
                self.assertTrue(result["is_bill"])
                self.assertEqual(result["status"], "auto_recorded")
                self.assertEqual(result["transaction"]["amount_cents"], 2850)
                self.assertEqual(result["transaction"]["payment_app"], "微信支付")
                self.assertEqual(result["transaction"]["order_platform"], "美团外卖")
                self.assertEqual(result["transaction"]["category"], "外卖")
                self.assertTrue(result["transaction"]["occurred_at"].startswith("2026-05-23"))
                may_summary = ledger.summarize_transactions(
                    conn,
                    "u1",
                    "all",
                    start="2026-05-01",
                    end="2026-06-01",
                )
                self.assertEqual(may_summary["totals"]["expense_cents"], 2850)
            finally:
                conn.close()

    def test_analyze_bill_recognizes_wechat_transfer_screenshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                raw_text = (
                    "这是一张微信转账账单截图：转给 小王，金额 -50.00 元，"
                    "状态为对方已收钱。转账时间 2026-05-30 17:04:13，"
                    "收款时间 2026-05-30 17:06:47，支付方式是零钱，"
                    "转账单号 1000050001202605300035652311060。"
                )

                result = ledger.analyze_bill_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "chat_id": "chat-a",
                        "record_id": "image-transfer",
                        "raw_text": raw_text,
                    },
                )

                self.assertTrue(ledger.is_bill_like_text(raw_text))
                self.assertTrue(result["ok"])
                self.assertTrue(result["is_bill"])
                self.assertEqual(result["status"], "needs_clarification")
                self.assertIn("这笔是消费、退款、收入还是个人转账？请确认一下。", result["needs_clarification"])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)

                context = ledger.latest_bill_context(conn, "u1", "chat-a", ["needs_clarification"])
                self.assertIsNotNone(context)
                payload = json.loads(context["payload_json"])
                self.assertEqual(payload["amount_cents"], 5000)
                self.assertEqual(payload["direction"], "unknown")
                self.assertEqual(payload["merchant"], "小王")
                self.assertEqual(payload["category"], "转账")

                answer_fields = ledger.fields_from_answer_text("消费")
                self.assertEqual(answer_fields["direction"], "expense")
                self.assertNotIn("category", answer_fields)
                confirmed = ledger.confirm_bill_context(
                    conn,
                    {
                        **answer_fields,
                        "context_id": result["context_id"],
                    },
                )
                self.assertTrue(confirmed["ok"])
                self.assertEqual(confirmed["status"], "confirmed")
                self.assertEqual(confirmed["transaction"]["amount_cents"], 5000)
                self.assertEqual(confirmed["transaction"]["direction"], "expense")
            finally:
                conn.close()

    def test_analyze_bill_recognizes_meituan_order_screenshot_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                raw_text = (
                    "美团外卖账单截图，今日订单，下单时间 2026-05-30 12:10，"
                    "商家：黄焖鸡米饭，金额 28.50 元，支付方式 微信支付。"
                )

                result = ledger.analyze_bill_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "chat_id": "chat-a",
                        "record_id": "image-meituan",
                        "raw_text": raw_text,
                    },
                )

                self.assertTrue(ledger.is_bill_like_text(raw_text))
                self.assertTrue(result["ok"])
                self.assertTrue(result["is_bill"])
                self.assertEqual(result["status"], "auto_recorded")
                self.assertEqual(result["transaction"]["amount_cents"], 2850)
                self.assertEqual(result["transaction"]["order_platform"], "美团外卖")
                self.assertEqual(result["transaction"]["payment_app"], "微信支付")
                self.assertEqual(result["transaction"]["category"], "外卖")
            finally:
                conn.close()

    def test_correct_occurred_at_refreshes_old_and_new_summary_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                result = ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "source_type": "text",
                        "raw_text": "咖啡 18",
                        "occurred_at": "2026-05-20",
                        "amount": "18",
                        "direction": "expense",
                        "category": "餐饮",
                    },
                )
                before = ledger.summarize_transactions(
                    conn,
                    "u1",
                    "all",
                    start="2026-05-20",
                    end="2026-05-21",
                )
                self.assertEqual(before["totals"]["expense_cents"], 1800)

                args = type(
                    "Args",
                    (),
                    {
                        "transaction_id": result["transaction"]["id"],
                        "field": "occurred_at",
                        "new_value": "2026-05-21",
                    },
                )()
                correction = ledger.correct_transaction(conn, args)

                self.assertTrue(correction["ok"])
                old_day = ledger.summarize_transactions(
                    conn,
                    "u1",
                    "all",
                    start="2026-05-20",
                    end="2026-05-21",
                )
                new_day = ledger.summarize_transactions(
                    conn,
                    "u1",
                    "all",
                    start="2026-05-21",
                    end="2026-05-22",
                )
                self.assertEqual(old_day["totals"]["expense_cents"], 0)
                self.assertEqual(new_day["totals"]["expense_cents"], 1800)
            finally:
                conn.close()

    def test_menu_with_prices_is_not_treated_as_bill(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                result = ledger.analyze_bill_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "raw_text": "美团商家菜单 黄焖鸡 28 元 可乐 5 元 加入购物车 满 30 减 5",
                    },
                )

                self.assertTrue(result["ok"])
                self.assertFalse(result["is_bill"])
                count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                conn.close()

    def test_unclear_bill_asks_once_then_learns_screenshot_and_item_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                first = ledger.analyze_bill_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "chat_id": "chat-a",
                        "record_id": "image-b",
                        "raw_text": "支付宝 交易成功 闲鱼 支付金额 ¥88.00 订单编号 987654321000",
                    },
                )
                self.assertEqual(first["status"], "needs_clarification")
                self.assertIn("needs_clarification", first)

                confirmed = ledger.confirm_bill_context(
                    conn,
                    {
                        "context_id": first["context_id"],
                        "item_name": "二手键盘",
                        "category": "数码家电",
                    },
                )
                self.assertTrue(confirmed["ok"])
                self.assertEqual(confirmed["status"], "confirmed")
                self.assertEqual(confirmed["transaction"]["item_name"], "二手键盘")
                self.assertEqual(confirmed["transaction"]["category"], "数码家电")
                self.assertGreater(conn.execute("SELECT COUNT(*) FROM screenshot_rules").fetchone()[0], 0)
                self.assertGreater(conn.execute("SELECT COUNT(*) FROM item_rules").fetchone()[0], 0)

                second = ledger.analyze_bill_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "chat_id": "chat-a",
                        "record_id": "image-c",
                        "raw_text": "支付宝 交易成功 闲鱼 支付金额 ¥88.00 订单编号 987654321999",
                    },
                )
                self.assertEqual(second["status"], "auto_recorded")
                self.assertEqual(second["transaction"]["item_name"], "二手键盘")
                self.assertEqual(second["transaction"]["category"], "数码家电")
            finally:
                conn.close()

    def test_possible_duplicate_same_day_amount_category_and_merchant_asks_before_insert(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                first = ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "chat_id": "chat-a",
                        "source_type": "image",
                        "raw_text": "支付宝 支付成功 成都软件园 C8 餐厅 支付金额 ¥31.00",
                        "occurred_at": "2026-05-26T12:35:27+08:00",
                        "amount": "31.00",
                        "direction": "expense",
                        "category": "餐饮",
                        "merchant": "成都软件园 C8 餐厅",
                        "payment_app": "支付宝",
                    },
                )
                self.assertTrue(first["inserted"])

                duplicate = ledger.analyze_bill_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "chat_id": "chat-a",
                        "record_id": "image-dup",
                        "raw_text": "支付宝 支付成功 成都软件园 C8 餐厅 付款方式 余额宝 支付金额 ¥31.00",
                        "occurred_at": "2026-05-26T12:35:27+08:00",
                    },
                )

                self.assertEqual(duplicate["status"], "needs_clarification")
                self.assertTrue(duplicate["possible_duplicate"])
                self.assertIn("我先不新增", duplicate["needs_clarification"][0])
                count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
                self.assertEqual(count, 1)

                forced = ledger.confirm_bill_context(
                    conn,
                    {
                        "context_id": duplicate["context_id"],
                        "force_new_transaction": True,
                    },
                )

                self.assertTrue(forced["ok"])
                self.assertEqual(forced["status"], "confirmed")
                count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
                self.assertEqual(count, 2)
            finally:
                conn.close()

    def test_record_json_possible_duplicate_asks_before_insert(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                first = ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "chat_id": "chat-a",
                        "source_type": "manual",
                        "occurred_at": "2026-05-26T12:35:27+08:00",
                        "amount": "31.00",
                        "direction": "expense",
                        "category": "餐饮",
                        "merchant": "成都软件园 C8 餐厅",
                        "payment_app": "支付宝",
                    },
                )
                self.assertTrue(first["inserted"])

                second = ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "chat_id": "chat-a",
                        "source_type": "image",
                        "occurred_at": "2026-05-26T12:36:00+08:00",
                        "amount": "31.00",
                        "direction": "expense",
                        "category": "餐饮",
                        "merchant": "成都软件园 C8 餐厅",
                        "payment_app": "支付宝",
                    },
                )

                self.assertEqual(second["status"], "needs_clarification")
                self.assertTrue(second["possible_duplicate"])
                count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
                self.assertEqual(count, 1)
            finally:
                conn.close()

    def test_possible_duplicate_with_same_app_but_missing_merchant_asks_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                first = ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "chat_id": "chat-a",
                        "source_type": "image",
                        "occurred_at": "2026-05-26T12:35:27+08:00",
                        "amount": "31.00",
                        "direction": "expense",
                        "category": "餐饮",
                        "payment_app": "支付宝",
                    },
                )
                self.assertTrue(first["inserted"])

                second = ledger.record_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "chat_id": "chat-a",
                        "source_type": "image",
                        "occurred_at": "2026-05-26T12:40:00+08:00",
                        "amount": "31.00",
                        "direction": "expense",
                        "category": "餐饮",
                        "payment_app": "支付宝",
                    },
                )

                self.assertEqual(second["status"], "needs_clarification")
                self.assertTrue(second["possible_duplicate"])
                count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
                self.assertEqual(count, 1)
            finally:
                conn.close()

    def test_xianyu_api_token_answer_auto_classifies_and_confirms(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                first = ledger.analyze_bill_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "chat_id": "chat-a",
                        "record_id": "image-xianyu",
                        "raw_text": "卖家已发货 待确认收货 成交价 ¥99.88 订单编号 3303779739048007681",
                    },
                )
                self.assertEqual(first["status"], "needs_clarification")

                answer_fields = ledger.fields_from_answer_text("这是一个咸鱼账单，我买的是中转API的token")
                self.assertEqual(answer_fields["order_platform"], "闲鱼")
                self.assertEqual(answer_fields["category"], "AI工具")
                self.assertEqual(answer_fields["item_name"], "中转API的token")

                confirmed = ledger.confirm_bill_context(
                    conn,
                    {
                        **answer_fields,
                        "context_id": first["context_id"],
                    },
                )

                self.assertTrue(confirmed["ok"])
                self.assertEqual(confirmed["status"], "confirmed")
                self.assertEqual(confirmed["transaction"]["amount_cents"], 9988)
                self.assertEqual(confirmed["transaction"]["order_platform"], "闲鱼")
                self.assertEqual(confirmed["transaction"]["category"], "AI工具")
                self.assertEqual(confirmed["transaction"]["item_name"], "中转API的token")
            finally:
                conn.close()

    def test_bill_confirmation_keeps_exact_raw_amount_when_model_amount_was_rounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                first = ledger.analyze_bill_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "chat_id": "chat-a",
                        "record_id": "image-rounded",
                        "source_type": "image",
                        "raw_text": "闲鱼 卖家已发货 待确认收货 成交价 ¥99.88 约100元 订单编号 3303779739048007681",
                        "amount_cents": 10000,
                    },
                )
                self.assertEqual(first["status"], "needs_clarification")

                answer_fields = ledger.fields_from_answer_text("这个账单是中转api额度卡")
                self.assertNotIn("amount_cents", answer_fields)
                confirmed = ledger.confirm_bill_context(
                    conn,
                    {
                        **answer_fields,
                        "context_id": first["context_id"],
                    },
                )

                self.assertTrue(confirmed["ok"])
                self.assertEqual(confirmed["status"], "confirmed")
                self.assertEqual(confirmed["transaction"]["amount_cents"], 9988)
                self.assertEqual(confirmed["transaction"]["category"], "AI工具")
            finally:
                conn.close()

    def test_partial_bill_clarification_is_saved_for_next_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                first = ledger.analyze_bill_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "chat_id": "chat-a",
                        "record_id": "image-partial",
                        "raw_text": "卖家已发货 待确认收货 成交价 ¥66.00 订单编号 987654321000",
                    },
                )
                self.assertEqual(first["status"], "needs_clarification")

                partial = ledger.confirm_bill_context(
                    conn,
                    {
                        "context_id": first["context_id"],
                        "item_name": "神秘服务",
                    },
                )
                self.assertEqual(partial["status"], "needs_clarification")

                confirmed = ledger.confirm_bill_context(
                    conn,
                    {
                        "context_id": first["context_id"],
                        "order_platform": "闲鱼",
                    },
                )

                self.assertTrue(confirmed["ok"])
                self.assertEqual(confirmed["status"], "confirmed")
                self.assertEqual(confirmed["transaction"]["order_platform"], "闲鱼")
                self.assertEqual(confirmed["transaction"]["item_name"], "神秘服务")
                self.assertEqual(confirmed["transaction"]["category"], "其他")
            finally:
                conn.close()

    def test_latest_bill_context_honors_followup_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                first = ledger.analyze_bill_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "chat_id": "chat-a",
                        "record_id": "image-old",
                        "raw_text": "卖家已发货 待确认收货 成交价 ¥66.00 订单编号 987654321000",
                    },
                )
                self.assertEqual(first["status"], "needs_clarification")
                old_time = (datetime.now().astimezone() - timedelta(minutes=10)).isoformat(timespec="seconds")
                conn.execute(
                    "UPDATE bill_contexts SET created_at = ?, updated_at = ? WHERE id = ?",
                    (old_time, old_time, first["context_id"]),
                )
                conn.commit()

                stale = ledger.latest_bill_context(conn, "u1", "chat-a", ["needs_clarification"], max_age_seconds=300)
                unbounded = ledger.latest_bill_context(conn, "u1", "chat-a", ["needs_clarification"])

                self.assertIsNone(stale)
                self.assertIsNotNone(unbounded)
            finally:
                conn.close()

    def test_undo_bill_rejects_latest_auto_recorded_transaction_and_refreshes_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.open_temp_db(Path(tmp))
            try:
                recorded = ledger.analyze_bill_payload(
                    conn,
                    {
                        "user_id": "u1",
                        "chat_id": "chat-a",
                        "raw_text": "微信支付 支付成功 美团外卖 商品: 黄焖鸡 支付金额 ¥28.50 交易单号 123456789012",
                    },
                )
                self.assertEqual(ledger.summarize_transactions(conn, "u1", "today")["totals"]["expense_cents"], 2850)

                undone = ledger.undo_bill_transaction(conn, "u1", "chat-a", None)

                self.assertTrue(undone["ok"])
                row = conn.execute(
                    "SELECT status FROM transactions WHERE id = ?",
                    (recorded["transaction"]["id"],),
                ).fetchone()
                self.assertEqual(row["status"], "rejected")
                self.assertEqual(ledger.summarize_transactions(conn, "u1", "today")["totals"]["expense_cents"], 0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
