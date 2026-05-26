import importlib.util
import os
import sqlite3
import tempfile
import unittest
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
                summary = ledger.summarize_transactions(conn, "u1", "today")
                self.assertTrue(summary["cached"])
                self.assertEqual(summary["totals"]["expense_cents"], 2850)
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
