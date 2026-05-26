#!/usr/bin/env python3
"""Local China expense ledger helper.

The script is intentionally local-only: it stores user-confirmed text,
vision-extracted fields, and user-provided bill exports in SQLite. It never
logs in to payment or shopping platforms and never stores original screenshots.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable


DEFAULT_CURRENCY = "CNY"
VALID_DIRECTIONS = {"expense", "income", "refund", "transfer", "unknown"}
VALID_SOURCE_TYPES = {"text", "image", "csv", "xlsx", "manual"}
VALID_STATUSES = {"pending", "confirmed", "auto_confirmed", "rejected", "duplicate"}

DEFAULT_CATEGORIES = [
    "餐饮",
    "外卖",
    "商超日用",
    "生鲜买菜",
    "服饰鞋包",
    "数码家电",
    "交通出行",
    "住房物业",
    "水电燃气",
    "通讯网络",
    "医疗健康",
    "教育学习",
    "娱乐休闲",
    "宠物",
    "母婴",
    "旅行住宿",
    "人情红包",
    "转账",
    "收入",
    "退款",
    "其他",
]

DEFAULT_APP_SIGNATURES = [
    {
        "app": "支付宝",
        "app_type": "payment",
        "aliases": ["支付宝", "Alipay"],
        "visual_keywords": ["支付宝", "交易成功", "付款成功"],
        "ocr_keywords": ["支付宝", "交易成功", "付款成功", "账单详情", "交易分类", "付款方式"],
        "negative_keywords": ["微信支付"],
    },
    {
        "app": "微信支付",
        "app_type": "payment",
        "aliases": ["微信支付", "微信", "WeChat Pay"],
        "visual_keywords": ["微信支付", "支付成功"],
        "ocr_keywords": ["微信支付", "支付成功", "当前状态", "商户单号", "交易单号"],
        "negative_keywords": ["支付宝"],
    },
    {
        "app": "淘宝/天猫",
        "app_type": "order",
        "aliases": ["淘宝", "天猫", "淘宝/天猫"],
        "visual_keywords": ["淘宝", "天猫", "订单编号"],
        "ocr_keywords": ["淘宝", "天猫", "订单编号", "实付款", "确认收货", "交易成功"],
        "negative_keywords": [],
    },
    {
        "app": "京东",
        "app_type": "order",
        "aliases": ["京东", "JD"],
        "visual_keywords": ["京东", "订单编号"],
        "ocr_keywords": ["京东", "订单编号", "实付金额", "京豆"],
        "negative_keywords": [],
    },
    {
        "app": "美团外卖",
        "app_type": "order",
        "aliases": ["美团外卖", "美团"],
        "visual_keywords": ["美团外卖", "订单详情"],
        "ocr_keywords": ["美团外卖", "订单详情", "配送费", "骑手", "商家"],
        "negative_keywords": [],
    },
    {
        "app": "盒马",
        "app_type": "order",
        "aliases": ["盒马", "Hema"],
        "visual_keywords": ["盒马", "订单详情"],
        "ocr_keywords": ["盒马", "订单详情", "配送时间", "商品明细"],
        "negative_keywords": [],
    },
    {
        "app": "山姆",
        "app_type": "order",
        "aliases": ["山姆", "Sam's Club", "Sam"],
        "visual_keywords": ["山姆", "Sam's Club"],
        "ocr_keywords": ["山姆", "Sam's Club", "极速达", "订单详情"],
        "negative_keywords": [],
    },
]

CATEGORY_RULES = [
    (
        "外卖",
        ["美团外卖", "饿了么", "外卖", "奶茶", "咖啡", "黄焖鸡", "麻辣烫", "汉堡", "配送费"],
    ),
    ("餐饮", ["餐厅", "饭店", "火锅", "烧烤", "面馆", "小吃", "早餐", "午餐", "晚餐"]),
    ("生鲜买菜", ["盒马", "叮咚买菜", "朴朴", "菜", "肉", "蛋", "奶", "水果", "蔬菜", "生鲜"]),
    ("商超日用", ["山姆", "Sam's Club", "沃尔玛", "永辉", "超市", "便利店", "日用"]),
    ("数码家电", ["数据线", "硬盘", "手机", "电脑", "充电器", "耳机", "家电", "电器"]),
    ("交通出行", ["滴滴", "曹操出行", "T3", "公交", "地铁", "12306", "机票", "打车", "出租车"]),
    ("退款", ["退款", "退货", "退的"]),
    ("收入", ["工资", "奖金", "报销", "收入"]),
    ("人情红包", ["红包"]),
    ("转账", ["转账"]),
]

PLATFORM_KEYWORDS = [
    ("美团外卖", ["美团外卖", "外卖"]),
    ("美团", ["美团"]),
    ("饿了么", ["饿了么"]),
    ("淘宝", ["淘宝"]),
    ("天猫", ["天猫"]),
    ("京东", ["京东"]),
    ("拼多多", ["拼多多"]),
    ("抖音商城", ["抖音商城", "抖音"]),
    ("山姆", ["山姆", "Sam's Club"]),
    ("盒马", ["盒马"]),
    ("滴滴", ["滴滴", "打车"]),
    ("12306", ["12306", "火车票", "高铁"]),
]

PAYMENT_KEYWORDS = [
    ("支付宝", ["支付宝", "Alipay"]),
    ("微信支付", ["微信支付", "微信付", "微信"]),
    ("银行卡", ["银行卡", "储蓄卡", "信用卡"]),
    ("云闪付", ["云闪付"]),
    ("Apple Pay", ["Apple Pay"]),
    ("现金", ["现金"]),
]

TRANSACTION_COLUMNS = [
    "id",
    "user_id",
    "chat_id",
    "occurred_at",
    "direction",
    "amount_cents",
    "currency",
    "category",
    "subcategory",
    "merchant",
    "item_name",
    "item_details_json",
    "source_app",
    "payment_app",
    "order_platform",
    "payment_method",
    "account_hint",
    "order_no_masked",
    "raw_text",
    "source_type",
    "source_hash",
    "confidence",
    "status",
    "created_at",
    "updated_at",
]

LEARNABLE_FIELDS = {
    "category",
    "subcategory",
    "item_name",
    "merchant",
    "order_platform",
    "payment_app",
}


class ImportRow:
    def __init__(self, row_number: int, payload: dict[str, Any]) -> None:
        self.row_number = row_number
        self.payload = payload


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def db_path_from_env() -> Path:
    raw = os.getenv("CHINA_EXPENSE_LEDGER_DB")
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / "cow" / "data" / "china_expense_ledger" / "ledger.db").resolve()


def open_db(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or db_path_from_env()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def fail_json(error: str, **extra: Any) -> None:
    payload = {"ok": False, "error": error}
    payload.update(extra)
    json_print(payload)


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            chat_id TEXT,
            occurred_at TEXT,
            direction TEXT,
            amount_cents INTEGER,
            currency TEXT DEFAULT 'CNY',
            category TEXT,
            subcategory TEXT,
            merchant TEXT,
            item_name TEXT,
            item_details_json TEXT,
            source_app TEXT,
            payment_app TEXT,
            order_platform TEXT,
            payment_method TEXT,
            account_hint TEXT,
            order_no_masked TEXT,
            raw_text TEXT,
            source_type TEXT,
            source_hash TEXT,
            confidence REAL,
            status TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS item_rules (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            item_pattern TEXT,
            normalized_item TEXT,
            category TEXT,
            subcategory TEXT,
            app_hint TEXT,
            confidence REAL,
            hit_count INTEGER,
            last_seen TEXT,
            note TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS merchant_rules (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            merchant_pattern TEXT,
            normalized_merchant TEXT,
            order_platform TEXT,
            payment_app TEXT,
            category TEXT,
            subcategory TEXT,
            confidence REAL,
            hit_count INTEGER,
            last_seen TEXT,
            note TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS app_signatures (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            app TEXT,
            app_type TEXT,
            aliases_json TEXT,
            visual_keywords_json TEXT,
            ocr_keywords_json TEXT,
            negative_keywords_json TEXT,
            confidence REAL,
            hit_count INTEGER,
            last_seen TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS corrections (
            id TEXT PRIMARY KEY,
            transaction_id TEXT,
            user_id TEXT,
            field TEXT,
            old_value TEXT,
            new_value TEXT,
            learned_rule_type TEXT,
            learned_rule_id TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS import_batches (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            source TEXT,
            file_hash TEXT,
            row_count INTEGER,
            inserted_count INTEGER,
            duplicate_count INTEGER,
            created_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_transactions_user_time
            ON transactions(user_id, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_transactions_source_hash
            ON transactions(source_hash);
        CREATE INDEX IF NOT EXISTS idx_transactions_category
            ON transactions(category);
        CREATE INDEX IF NOT EXISTS idx_transactions_status
            ON transactions(status);
        CREATE INDEX IF NOT EXISTS idx_item_rules_user_pattern
            ON item_rules(user_id, item_pattern);
        CREATE INDEX IF NOT EXISTS idx_merchant_rules_user_pattern
            ON merchant_rules(user_id, merchant_pattern);
        """
    )
    seed_default_app_signatures(conn)
    conn.commit()


def seed_default_app_signatures(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT app FROM app_signatures WHERE user_id IS NULL"
    ).fetchall()
    existing_apps = {row["app"] for row in existing}
    timestamp = now_iso()
    for signature in DEFAULT_APP_SIGNATURES:
        if signature["app"] in existing_apps:
            continue
        conn.execute(
            """
            INSERT INTO app_signatures (
                id, user_id, app, app_type, aliases_json, visual_keywords_json,
                ocr_keywords_json, negative_keywords_json, confidence, hit_count,
                last_seen, created_at, updated_at
            ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                signature["app"],
                signature["app_type"],
                json.dumps(signature["aliases"], ensure_ascii=False),
                json.dumps(signature["visual_keywords"], ensure_ascii=False),
                json.dumps(signature["ocr_keywords"], ensure_ascii=False),
                json.dumps(signature["negative_keywords"], ensure_ascii=False),
                0.8,
                0,
                timestamp,
                timestamp,
                timestamp,
            ),
        )


def normalize_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_optional_text(value: object) -> str | None:
    text = normalize_text(value)
    return text or None


def amount_to_cents(value: object) -> int | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    text = text.replace("人民币", "").replace("CNY", "").replace("¥", "").replace("￥", "")
    text = text.replace(",", "").replace("元", "").strip()
    if not text:
        return None
    # Keep the first number because bill exports sometimes contain "28.50元".
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        amount = Decimal(match.group(0))
    except InvalidOperation:
        return None
    cents = (amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def amount_cents_value(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = str(value).strip().replace(",", "")
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return amount_to_cents(value)


def mask_order_no(value: object) -> str | None:
    text = normalize_text(value)
    if not text:
        return None
    compact = re.sub(r"\s+", "", text)
    if len(compact) <= 8:
        return compact
    return f"{compact[:4]}***{compact[-4:]}"


def infer_direction(payload: dict[str, Any]) -> str:
    explicit = normalize_text(payload.get("direction")).lower()
    if explicit in VALID_DIRECTIONS:
        return explicit

    raw_text = " ".join(
        normalize_text(payload.get(key))
        for key in ("raw_text", "item_name", "merchant", "category")
    )
    if any(token in raw_text for token in ("退款", "退货", "退的")):
        return "refund"
    if any(token in raw_text for token in ("收入", "工资", "奖金")):
        return "income"
    if any(token in raw_text for token in ("红包", "转账")):
        return "unknown"

    income_expense = normalize_text(payload.get("income_expense"))
    if income_expense in ("收入", "收", "+"):
        return "income"
    if income_expense in ("支出", "支", "-"):
        return "expense"
    return "expense"


def infer_category(payload: dict[str, Any], direction: str) -> str:
    explicit = normalize_text(payload.get("category"))
    if explicit in DEFAULT_CATEGORIES:
        return explicit
    if direction == "refund":
        return "退款"
    if direction == "income":
        return "收入"

    haystack = " ".join(
        normalize_text(payload.get(key))
        for key in ("raw_text", "merchant", "item_name", "order_platform", "source_app")
    )
    for category, keywords in CATEGORY_RULES:
        if any(keyword.lower() in haystack.lower() for keyword in keywords):
            return category
    return explicit or "其他"


def infer_order_platform(payload: dict[str, Any]) -> str | None:
    explicit = normalize_optional_text(payload.get("order_platform"))
    if explicit:
        return explicit
    haystack = " ".join(
        normalize_text(payload.get(key))
        for key in ("raw_text", "merchant", "item_name", "source_app")
    )
    for platform, keywords in PLATFORM_KEYWORDS:
        if any(keyword.lower() in haystack.lower() for keyword in keywords):
            return platform
    return None


def infer_payment_app(payload: dict[str, Any]) -> str | None:
    explicit = normalize_optional_text(payload.get("payment_app"))
    if explicit:
        return explicit
    haystack = " ".join(
        normalize_text(payload.get(key))
        for key in ("raw_text", "source_app", "payment_method")
    )
    for app, keywords in PAYMENT_KEYWORDS:
        if any(keyword.lower() in haystack.lower() for keyword in keywords):
            return app
    return None


def needs_clarification(payload: dict[str, Any], direction: str, status: str) -> list[str]:
    if status in {"confirmed", "auto_confirmed", "rejected"}:
        return []
    missing = []
    if payload.get("needs_clarification"):
        value = payload["needs_clarification"]
        if isinstance(value, list):
            missing.extend(str(item) for item in value if str(item).strip())
        elif isinstance(value, str):
            missing.append(value)

    if direction in {"unknown", "transfer"}:
        raw = normalize_text(payload.get("raw_text"))
        if any(token in raw for token in ("红包", "转账")):
            missing.append("请确认这是消费、收入、退款，还是个人转账。")

    if payload.get("source_type") == "image":
        if not normalize_text(payload.get("item_name")) and not normalize_text(payload.get("merchant")):
            missing.append("截图中商品名或商户名不清楚，请补充。")
    return sorted(set(missing))


def status_for_payload(payload: dict[str, Any], direction: str) -> str:
    explicit = normalize_text(payload.get("status"))
    if explicit in VALID_STATUSES:
        return explicit
    if direction in {"unknown", "transfer"}:
        return "pending"
    if payload.get("needs_clarification"):
        return "pending"
    return "auto_confirmed"


def normalize_item_details(value: object) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            return json.dumps({"text": text}, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def source_hash_for(payload: dict[str, Any]) -> str:
    explicit = normalize_text(payload.get("source_hash"))
    if explicit:
        return explicit
    stable = {
        "user_id": normalize_text(payload.get("user_id")),
        "occurred_at": normalize_text(payload.get("occurred_at")),
        "direction": normalize_text(payload.get("direction")),
        "amount_cents": amount_cents_value(payload.get("amount_cents"))
        if payload.get("amount_cents") not in (None, "")
        else amount_to_cents(payload.get("amount")),
        "merchant": normalize_text(payload.get("merchant")),
        "item_name": normalize_text(payload.get("item_name")),
        "payment_app": normalize_text(payload.get("payment_app")),
        "order_platform": normalize_text(payload.get("order_platform")),
        "order_no_masked": normalize_text(payload.get("order_no_masked") or payload.get("order_no")),
        "source_type": normalize_text(payload.get("source_type")),
        "raw_text": normalize_text(payload.get("raw_text"))[:500],
    }
    data = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def apply_item_rules(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    user_id = normalize_optional_text(payload.get("user_id"))
    haystack = " ".join(
        normalize_text(payload.get(key)) for key in ("raw_text", "item_name", "merchant")
    )
    if not haystack:
        return
    rows = conn.execute(
        """
        SELECT * FROM item_rules
        WHERE (user_id = ? OR user_id IS NULL)
        ORDER BY confidence DESC, hit_count DESC
        """,
        (user_id,),
    ).fetchall()
    timestamp = now_iso()
    for row in rows:
        pattern = normalize_text(row["item_pattern"])
        if not pattern or pattern not in haystack:
            continue
        for field in ("category", "subcategory"):
            if not normalize_text(payload.get(field)) and row[field]:
                payload[field] = row[field]
        if not normalize_text(payload.get("item_name")) and row["normalized_item"]:
            payload["item_name"] = row["normalized_item"]
        conn.execute(
            "UPDATE item_rules SET hit_count = hit_count + 1, last_seen = ?, updated_at = ? WHERE id = ?",
            (timestamp, timestamp, row["id"]),
        )
        return


def apply_merchant_rules(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    user_id = normalize_optional_text(payload.get("user_id"))
    haystack = " ".join(
        normalize_text(payload.get(key)) for key in ("raw_text", "merchant", "item_name")
    )
    if not haystack:
        return
    rows = conn.execute(
        """
        SELECT * FROM merchant_rules
        WHERE (user_id = ? OR user_id IS NULL)
        ORDER BY confidence DESC, hit_count DESC
        """,
        (user_id,),
    ).fetchall()
    timestamp = now_iso()
    for row in rows:
        pattern = normalize_text(row["merchant_pattern"])
        if not pattern or pattern not in haystack:
            continue
        for field in ("order_platform", "payment_app", "category", "subcategory"):
            if not normalize_text(payload.get(field)) and row[field]:
                payload[field] = row[field]
        if not normalize_text(payload.get("merchant")) and row["normalized_merchant"]:
            payload["merchant"] = row["normalized_merchant"]
        conn.execute(
            "UPDATE merchant_rules SET hit_count = hit_count + 1, last_seen = ?, updated_at = ? WHERE id = ?",
            (timestamp, timestamp, row["id"]),
        )
        return


def build_transaction(conn: sqlite3.Connection, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    payload = dict(payload)
    apply_item_rules(conn, payload)
    apply_merchant_rules(conn, payload)

    timestamp = now_iso()
    direction = infer_direction(payload)
    source_type = normalize_text(payload.get("source_type")) or "manual"
    if source_type not in VALID_SOURCE_TYPES:
        source_type = "manual"
    status = status_for_payload(payload, direction)
    category = infer_category(payload, direction)

    amount_cents = amount_cents_value(payload.get("amount_cents"))
    if amount_cents is None:
        amount_cents = amount_to_cents(payload.get("amount"))
    if amount_cents is None:
        raise ValueError("amount_cents or amount is required")

    transaction = {
        "id": normalize_text(payload.get("id")) or uuid.uuid4().hex,
        "user_id": normalize_optional_text(payload.get("user_id")),
        "chat_id": normalize_optional_text(payload.get("chat_id")),
        "occurred_at": normalize_optional_text(payload.get("occurred_at")) or timestamp,
        "direction": direction,
        "amount_cents": amount_cents,
        "currency": normalize_text(payload.get("currency")) or DEFAULT_CURRENCY,
        "category": category,
        "subcategory": normalize_optional_text(payload.get("subcategory")),
        "merchant": normalize_optional_text(payload.get("merchant")),
        "item_name": normalize_optional_text(payload.get("item_name")),
        "item_details_json": normalize_item_details(payload.get("item_details_json") or payload.get("item_details")),
        "source_app": normalize_optional_text(payload.get("source_app")),
        "payment_app": infer_payment_app(payload),
        "order_platform": infer_order_platform(payload),
        "payment_method": normalize_optional_text(payload.get("payment_method")),
        "account_hint": normalize_optional_text(payload.get("account_hint")),
        "order_no_masked": mask_order_no(payload.get("order_no_masked") or payload.get("order_no")),
        "raw_text": normalize_optional_text(payload.get("raw_text")),
        "source_type": source_type,
        "source_hash": None,
        "confidence": float(payload.get("confidence", 0.7) or 0.0),
        "status": status,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    hash_payload = dict(payload)
    hash_payload.update(transaction)
    transaction["source_hash"] = source_hash_for(hash_payload)
    clarification = needs_clarification({**payload, **transaction}, direction, status)
    if clarification:
        transaction["status"] = "pending"
    return transaction, clarification


def insert_transaction(conn: sqlite3.Connection, transaction: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    existing = conn.execute(
        """
        SELECT * FROM transactions
        WHERE user_id IS ? AND source_hash = ?
        LIMIT 1
        """,
        (transaction.get("user_id"), transaction["source_hash"]),
    ).fetchone()
    if existing is not None:
        duplicate = dict(existing)
        duplicate["status"] = "duplicate"
        return False, duplicate

    conn.execute(
        f"""
        INSERT INTO transactions ({", ".join(TRANSACTION_COLUMNS)})
        VALUES ({", ".join("?" for _ in TRANSACTION_COLUMNS)})
        """,
        [transaction.get(column) for column in TRANSACTION_COLUMNS],
    )
    return True, transaction


def record_payload(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    transaction, clarification = build_transaction(conn, payload)
    inserted, row = insert_transaction(conn, transaction)
    conn.commit()
    result = {
        "ok": True,
        "action": "record-json",
        "inserted": inserted,
        "duplicate": not inserted,
        "transaction": row,
    }
    if clarification:
        result["needs_clarification"] = clarification
    return result


def read_json_arg(args: argparse.Namespace) -> dict[str, Any]:
    if args.json_text:
        data = args.json_text
    elif args.file:
        data = Path(args.file).read_text(encoding="utf-8-sig")
    else:
        data = sys.stdin.read()
    payload = json.loads(data)
    if not isinstance(payload, dict):
        raise ValueError("JSON payload must be an object")
    return payload


def detect_file_encoding(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            raw.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "utf-8-sig"


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    encoding = detect_file_encoding(path)
    with path.open("r", encoding=encoding, newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(handle, dialect=dialect)
        return [dict(row) for row in reader if any(normalize_text(v) for v in row.values())]


def row_value(row: dict[str, Any], *names: str) -> str | None:
    normalized = {normalize_text(key).lower(): value for key, value in row.items()}
    for name in names:
        key = normalize_text(name).lower()
        if key in normalized:
            return normalize_optional_text(normalized[key])
    for key, value in row.items():
        cleaned_key = normalize_text(key)
        for name in names:
            if name in cleaned_key:
                return normalize_optional_text(value)
    return None


def parse_income_expense(text: str | None, amount_text: str | None = None) -> str | None:
    value = normalize_text(text)
    if value in ("支出", "支", "付款", "消费", "-"):
        return "支出"
    if value in ("收入", "收", "收款", "+"):
        return "收入"
    amount = normalize_text(amount_text)
    if amount.startswith("-"):
        return "支出"
    if amount.startswith("+"):
        return "收入"
    return None


def alipay_rows(rows: list[dict[str, str]], user_id: str | None) -> list[ImportRow]:
    parsed = []
    for index, row in enumerate(rows, start=1):
        amount_text = row_value(row, "金额", "交易金额", "金额（元）")
        income_expense = parse_income_expense(row_value(row, "收/支", "收支"), amount_text)
        raw_text = " ".join(normalize_text(value) for value in row.values() if normalize_text(value))
        direction = "refund" if "退款" in raw_text else infer_direction({"income_expense": income_expense, "raw_text": raw_text})
        payload = {
            "user_id": user_id,
            "occurred_at": row_value(row, "交易时间", "付款时间", "创建时间"),
            "direction": direction,
            "amount": amount_text,
            "merchant": row_value(row, "交易对方", "对方", "商户"),
            "item_name": row_value(row, "商品说明", "商品", "商品名称", "交易说明"),
            "source_app": "支付宝",
            "payment_app": "支付宝",
            "order_platform": row_value(row, "交易分类", "业务类型"),
            "payment_method": row_value(row, "付款方式", "支付方式"),
            "order_no": row_value(row, "商户订单号", "交易号", "订单号"),
            "raw_text": raw_text,
            "source_type": "csv",
            "confidence": 0.85,
            "status": "auto_confirmed",
        }
        parsed.append(ImportRow(index, payload))
    return parsed


def wechat_rows(rows: list[dict[str, str]], user_id: str | None) -> list[ImportRow]:
    parsed = []
    for index, row in enumerate(rows, start=1):
        amount_text = row_value(row, "金额", "金额(元)", "交易金额")
        income_expense = parse_income_expense(row_value(row, "收/支", "收支"), amount_text)
        raw_text = " ".join(normalize_text(value) for value in row.values() if normalize_text(value))
        direction = "refund" if "退款" in raw_text else infer_direction({"income_expense": income_expense, "raw_text": raw_text})
        payload = {
            "user_id": user_id,
            "occurred_at": row_value(row, "交易时间", "支付时间"),
            "direction": direction,
            "amount": amount_text,
            "merchant": row_value(row, "交易对方", "商户", "收款方"),
            "item_name": row_value(row, "商品", "商品名称", "交易说明"),
            "source_app": "微信支付",
            "payment_app": "微信支付",
            "order_platform": row_value(row, "交易类型", "交易分类"),
            "payment_method": row_value(row, "支付方式", "付款方式"),
            "order_no": row_value(row, "商户单号", "交易单号", "订单号"),
            "raw_text": raw_text,
            "source_type": "csv",
            "confidence": 0.85,
            "status": "auto_confirmed",
        }
        parsed.append(ImportRow(index, payload))
    return parsed


def auto_source_from_headers(path: Path, rows: list[dict[str, str]]) -> str:
    suffix = path.suffix.lower()
    if suffix in {".xls", ".xlsx"}:
        return "excel"
    if not rows:
        return "unknown"
    headers = " ".join(rows[0].keys())
    if "交易对方" in headers and ("商品说明" in headers or "商户订单号" in headers):
        return "alipay"
    if "微信" in path.name or "商户单号" in headers or "交易单号" in headers:
        return "wechat"
    return "unknown"


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def import_file(conn: sqlite3.Connection, path: Path, source: str, user_id: str | None) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in {".xls", ".xlsx"}:
        return {
            "ok": False,
            "error": "unsupported_format",
            "format": suffix.lstrip("."),
            "message": "MVP only imports CSV. Convert XLS/XLSX to CSV before importing.",
        }
    if suffix != ".csv":
        return {"ok": False, "error": "unsupported_format", "format": suffix.lstrip(".") or "unknown"}

    rows = read_csv_rows(path)
    resolved_source = source if source != "auto" else auto_source_from_headers(path, rows)
    if resolved_source == "alipay":
        parsed_rows = alipay_rows(rows, user_id)
    elif resolved_source == "wechat":
        parsed_rows = wechat_rows(rows, user_id)
    else:
        return {"ok": False, "error": "unsupported_format", "source": resolved_source}

    inserted = 0
    duplicates = 0
    transaction_ids = []
    for parsed in parsed_rows:
        transaction, _ = build_transaction(conn, parsed.payload)
        was_inserted, row = insert_transaction(conn, transaction)
        if was_inserted:
            inserted += 1
            transaction_ids.append(row["id"])
        else:
            duplicates += 1

    batch = {
        "id": uuid.uuid4().hex,
        "user_id": user_id,
        "source": resolved_source,
        "file_hash": file_hash(path),
        "row_count": len(parsed_rows),
        "inserted_count": inserted,
        "duplicate_count": duplicates,
        "created_at": now_iso(),
    }
    conn.execute(
        """
        INSERT INTO import_batches (
            id, user_id, source, file_hash, row_count, inserted_count,
            duplicate_count, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch["id"],
            batch["user_id"],
            batch["source"],
            batch["file_hash"],
            batch["row_count"],
            batch["inserted_count"],
            batch["duplicate_count"],
            batch["created_at"],
        ),
    )
    conn.commit()
    return {
        "ok": True,
        "action": "import-file",
        "source": resolved_source,
        "batch": batch,
        "transaction_ids": transaction_ids,
    }


def parse_date(value: str | None) -> datetime | None:
    text = normalize_text(value)
    if not text:
        return None
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return datetime.fromisoformat(text)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def period_bounds(period: str, start: str | None = None, end: str | None = None) -> tuple[str | None, str | None]:
    now = datetime.now().astimezone()
    start_dt = parse_date(start)
    end_dt = parse_date(end)
    if period == "today":
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = start_dt + timedelta(days=1)
    elif period == "month":
        start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_dt = None
    elif period == "all":
        pass
    return (
        start_dt.isoformat(timespec="seconds") if start_dt else None,
        end_dt.isoformat(timespec="seconds") if end_dt else None,
    )


def query_transactions(
    conn: sqlite3.Connection,
    user_id: str | None,
    period: str,
    limit: int,
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    where = []
    params: list[Any] = []
    if user_id:
        where.append("user_id = ?")
        params.append(user_id)
    start_bound, end_bound = period_bounds(period, start, end)
    if start_bound:
        where.append("occurred_at >= ?")
        params.append(start_bound)
    if end_bound:
        where.append("occurred_at < ?")
        params.append(end_bound)
    sql = "SELECT * FROM transactions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY occurred_at DESC, created_at DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def summarize_transactions(
    conn: sqlite3.Connection,
    user_id: str | None,
    period: str,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    rows = query_transactions(conn, user_id, period, 100000, start, end)
    totals = {
        "expense_cents": 0,
        "income_cents": 0,
        "refund_cents": 0,
        "transfer_cents": 0,
        "unknown_cents": 0,
    }
    by_category: dict[str, int] = {}
    by_direction: dict[str, int] = {}
    for row in rows:
        amount = int(row["amount_cents"] or 0)
        direction = row["direction"] or "unknown"
        key = f"{direction}_cents"
        totals[key if key in totals else "unknown_cents"] += amount
        by_direction[direction] = by_direction.get(direction, 0) + amount
        category = row["category"] or "其他"
        by_category[category] = by_category.get(category, 0) + amount
    return {
        "ok": True,
        "action": "summary",
        "period": period,
        "count": len(rows),
        "totals": totals,
        "by_direction": by_direction,
        "by_category": by_category,
    }


def update_learning_rule(
    conn: sqlite3.Connection,
    transaction: sqlite3.Row,
    field: str,
    new_value: str,
) -> tuple[str | None, str | None]:
    if field not in LEARNABLE_FIELDS:
        return None, None
    timestamp = now_iso()
    user_id = transaction["user_id"]

    if field in {"merchant", "order_platform", "payment_app", "category", "subcategory"} and transaction["merchant"]:
        existing = conn.execute(
            """
            SELECT * FROM merchant_rules
            WHERE user_id IS ? AND merchant_pattern = ?
            LIMIT 1
            """,
            (user_id, transaction["merchant"]),
        ).fetchone()
        if existing:
            rule_id = existing["id"]
            updates = {
                field: new_value,
                "updated_at": timestamp,
                "last_seen": timestamp,
                "hit_count": int(existing["hit_count"] or 0) + 1,
            }
            assignments = ", ".join(f"{key} = ?" for key in updates)
            conn.execute(
                f"UPDATE merchant_rules SET {assignments} WHERE id = ?",
                [*updates.values(), rule_id],
            )
        else:
            rule_id = uuid.uuid4().hex
            values = {
                "id": rule_id,
                "user_id": user_id,
                "merchant_pattern": transaction["merchant"],
                "normalized_merchant": new_value if field == "merchant" else transaction["merchant"],
                "order_platform": new_value if field == "order_platform" else transaction["order_platform"],
                "payment_app": new_value if field == "payment_app" else transaction["payment_app"],
                "category": new_value if field == "category" else transaction["category"],
                "subcategory": new_value if field == "subcategory" else transaction["subcategory"],
                "confidence": 0.9,
                "hit_count": 1,
                "last_seen": timestamp,
                "note": f"learned from correction field={field}",
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            columns = list(values.keys())
            conn.execute(
                f"INSERT INTO merchant_rules ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [values[column] for column in columns],
            )
        return "merchant_rules", rule_id

    if transaction["item_name"]:
        existing = conn.execute(
            """
            SELECT * FROM item_rules
            WHERE user_id IS ? AND item_pattern = ?
            LIMIT 1
            """,
            (user_id, transaction["item_name"]),
        ).fetchone()
        if existing:
            rule_id = existing["id"]
            updates = {
                "updated_at": timestamp,
                "last_seen": timestamp,
                "hit_count": int(existing["hit_count"] or 0) + 1,
            }
            if field == "item_name":
                updates["normalized_item"] = new_value
            elif field in {"category", "subcategory"}:
                updates[field] = new_value
            assignments = ", ".join(f"{key} = ?" for key in updates)
            conn.execute(
                f"UPDATE item_rules SET {assignments} WHERE id = ?",
                [*updates.values(), rule_id],
            )
        else:
            rule_id = uuid.uuid4().hex
            values = {
                "id": rule_id,
                "user_id": user_id,
                "item_pattern": transaction["item_name"],
                "normalized_item": new_value if field == "item_name" else transaction["item_name"],
                "category": new_value if field == "category" else transaction["category"],
                "subcategory": new_value if field == "subcategory" else transaction["subcategory"],
                "app_hint": transaction["order_platform"] or transaction["source_app"],
                "confidence": 0.9,
                "hit_count": 1,
                "last_seen": timestamp,
                "note": f"learned from correction field={field}",
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            columns = list(values.keys())
            conn.execute(
                f"INSERT INTO item_rules ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [values[column] for column in columns],
            )
        return "item_rules", rule_id
    return None, None


def correct_transaction(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    field = args.field
    if field not in TRANSACTION_COLUMNS:
        return {"ok": False, "error": "unsupported_field", "field": field}
    transaction = conn.execute(
        "SELECT * FROM transactions WHERE id = ?",
        (args.transaction_id,),
    ).fetchone()
    if not transaction:
        return {"ok": False, "error": "transaction_not_found", "transaction_id": args.transaction_id}
    old_value = transaction[field]
    new_value: Any = args.new_value
    if field == "amount_cents":
        new_value = amount_cents_value(new_value)
    timestamp = now_iso()
    conn.execute(
        f"UPDATE transactions SET {field} = ?, updated_at = ? WHERE id = ?",
        (new_value, timestamp, args.transaction_id),
    )
    learned_type, learned_id = update_learning_rule(conn, transaction, field, str(new_value))
    correction_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO corrections (
            id, transaction_id, user_id, field, old_value, new_value,
            learned_rule_type, learned_rule_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            correction_id,
            args.transaction_id,
            transaction["user_id"],
            field,
            None if old_value is None else str(old_value),
            None if new_value is None else str(new_value),
            learned_type,
            learned_id,
            timestamp,
        ),
    )
    conn.commit()
    return {
        "ok": True,
        "action": "correct",
        "correction_id": correction_id,
        "transaction_id": args.transaction_id,
        "field": field,
        "old_value": old_value,
        "new_value": new_value,
        "learned_rule_type": learned_type,
        "learned_rule_id": learned_id,
    }


def export_json(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    rows = query_transactions(conn, args.user_id, args.period, args.limit, args.start, args.end)
    payload = {"ok": True, "action": "export-json", "count": len(rows), "transactions": rows}
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["output"] = str(output)
    return payload


def doctor(conn: sqlite3.Connection, db_path: Path) -> dict[str, Any]:
    tables = [
        "transactions",
        "item_rules",
        "merchant_rules",
        "app_signatures",
        "corrections",
        "import_batches",
    ]
    return {
        "ok": True,
        "action": "doctor",
        "db_path": str(db_path),
        "tables": {table: table_exists(conn, table) for table in tables},
        "adapters": {
            "alipay_csv": "supported",
            "wechat_csv": "supported",
            "xls": "unsupported",
            "xlsx": "unsupported",
            "official_platform_api": "design_placeholder_disabled",
        },
    }


def add_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", dest="db_path", help="Override SQLite database path for this command.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local China expense ledger helper.")
    add_db_arg(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize the local ledger database.")
    init.set_defaults(func=cmd_init)

    record = sub.add_parser("record-json", help="Record one structured transaction JSON object.")
    record.add_argument("--json", dest="json_text", help="JSON object payload.")
    record.add_argument("--file", help="Path to a JSON payload file. Reads stdin when omitted.")
    record.set_defaults(func=cmd_record_json)

    import_parser = sub.add_parser("import-file", help="Import a user-provided CSV bill export.")
    import_parser.add_argument("--path", required=True, help="Path to CSV/XLS/XLSX export.")
    import_parser.add_argument("--source", choices=["alipay", "wechat", "auto"], default="auto")
    import_parser.add_argument("--user-id")
    import_parser.set_defaults(func=cmd_import_file)

    query = sub.add_parser("query", help="Query local transactions.")
    query.add_argument("--user-id")
    query.add_argument("--period", choices=["today", "month", "all"], default="all")
    query.add_argument("--start")
    query.add_argument("--end")
    query.add_argument("--limit", type=int, default=20)
    query.set_defaults(func=cmd_query)

    summary = sub.add_parser("summary", help="Summarize local transactions.")
    summary.add_argument("--user-id")
    summary.add_argument("--period", choices=["today", "month", "all"], default="month")
    summary.add_argument("--start")
    summary.add_argument("--end")
    summary.set_defaults(func=cmd_summary)

    correct = sub.add_parser("correct", help="Correct a field and learn a local rule.")
    correct.add_argument("--transaction-id", required=True)
    correct.add_argument("--field", required=True)
    correct.add_argument("--new-value", required=True)
    correct.set_defaults(func=cmd_correct)

    export = sub.add_parser("export-json", help="Export transactions as JSON.")
    export.add_argument("--user-id")
    export.add_argument("--period", choices=["today", "month", "all"], default="all")
    export.add_argument("--start")
    export.add_argument("--end")
    export.add_argument("--limit", type=int, default=100000)
    export.add_argument("--output")
    export.set_defaults(func=cmd_export_json)

    doctor_parser = sub.add_parser("doctor", help="Check database and adapter status.")
    doctor_parser.set_defaults(func=cmd_doctor)
    return parser


def connection_for_args(args: argparse.Namespace) -> tuple[sqlite3.Connection, Path]:
    db_path = Path(args.db_path).expanduser().resolve() if args.db_path else db_path_from_env()
    conn = open_db(db_path)
    return conn, db_path


def cmd_init(args: argparse.Namespace) -> None:
    conn, db_path = connection_for_args(args)
    try:
        init_db(conn)
        json_print({"ok": True, "action": "init", "db_path": str(db_path)})
    finally:
        conn.close()


def cmd_record_json(args: argparse.Namespace) -> None:
    conn, _ = connection_for_args(args)
    try:
        init_db(conn)
        payload = read_json_arg(args)
        json_print(record_payload(conn, payload))
    finally:
        conn.close()


def cmd_import_file(args: argparse.Namespace) -> None:
    conn, _ = connection_for_args(args)
    try:
        init_db(conn)
        json_print(import_file(conn, Path(args.path).expanduser(), args.source, args.user_id))
    finally:
        conn.close()


def cmd_query(args: argparse.Namespace) -> None:
    conn, _ = connection_for_args(args)
    try:
        init_db(conn)
        rows = query_transactions(conn, args.user_id, args.period, args.limit, args.start, args.end)
        json_print({"ok": True, "action": "query", "count": len(rows), "transactions": rows})
    finally:
        conn.close()


def cmd_summary(args: argparse.Namespace) -> None:
    conn, _ = connection_for_args(args)
    try:
        init_db(conn)
        json_print(summarize_transactions(conn, args.user_id, args.period, args.start, args.end))
    finally:
        conn.close()


def cmd_correct(args: argparse.Namespace) -> None:
    conn, _ = connection_for_args(args)
    try:
        init_db(conn)
        json_print(correct_transaction(conn, args))
    finally:
        conn.close()


def cmd_export_json(args: argparse.Namespace) -> None:
    conn, _ = connection_for_args(args)
    try:
        init_db(conn)
        json_print(export_json(conn, args))
    finally:
        conn.close()


def cmd_doctor(args: argparse.Namespace) -> None:
    conn, db_path = connection_for_args(args)
    try:
        init_db(conn)
        json_print(doctor(conn, db_path))
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
        return 0
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        fail_json(type(exc).__name__, message=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
