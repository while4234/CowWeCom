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
SUMMARY_STATUSES = {"confirmed", "auto_confirmed"}
SUMMARY_PERIOD_TYPES = {"day", "week", "month"}
BILL_CONTEXT_STATUSES = {"needs_clarification", "auto_recorded", "confirmed", "rejected"}
DATE_DETAIL_FIELDS = ("occurred_at_text", "occurred_at_resolution", "occurred_at_assumed")

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

# Curated category extensions are deliberately small. We only promote a new
# primary category when the signal is stable enough to be useful across users.
CURATED_MAJOR_CATEGORY_RULES = [
    (
        "AI工具",
        [
            "中转API",
            "中转api",
            "api token",
            "API token",
            "apitoken",
            "API额度",
            "api额度",
            "额度卡",
            "token",
            "Token",
            "API密钥",
            "api key",
            "apikey",
        ],
    ),
]

PLATFORM_KEYWORDS = [
    ("闲鱼", ["闲鱼", "咸鱼", "xianyu"]),
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

BILL_KEYWORDS = [
    "支付成功",
    "付款成功",
    "交易成功",
    "账单",
    "订单",
    "实付款",
    "实付",
    "收款方",
    "付款方式",
    "商户单号",
    "交易单号",
    "订单编号",
    "¥",
    "￥",
]

STRONG_BILL_KEYWORDS = [
    "支付成功",
    "付款成功",
    "交易成功",
    "账单详情",
    "账单截图",
    "当前状态",
    "商户单号",
    "交易单号",
    "订单编号",
    "付款方式",
    "收款方",
]

BILL_DETAIL_KEYWORDS = [
    "账单",
    "订单详情",
    "订单截图",
    "实付款",
    "实付金额",
    "实际支付",
    "支付金额",
    "订单金额",
    "已支付",
    "交易时间",
    "下单时间",
    "转账时间",
    "收款时间",
    "支付方式",
    "转账单号",
    "对方已收钱",
]

TRANSFER_BILL_KEYWORDS = [
    "微信转账",
    "支付宝转账",
    "转账账单",
    "转账截图",
    "转账时间",
    "收款时间",
    "转账单号",
    "对方已收钱",
    "转给",
    "转账给",
]

NON_BILL_PRICE_CONTEXTS = [
    "菜单",
    "价目表",
    "价格表",
    "商品列表",
    "优惠券",
    "满减",
    "配送范围",
    "加入购物车",
]

BROAD_SHOPPING_PLATFORMS = {"闲鱼", "淘宝", "天猫", "京东", "拼多多", "抖音商城"}

SIGNATURE_KEYWORDS = [
    "支付宝",
    "微信支付",
    "闲鱼",
    "咸鱼",
    "淘宝",
    "天猫",
    "京东",
    "拼多多",
    "抖音",
    "美团",
    "美团外卖",
    "饿了么",
    "盒马",
    "山姆",
    "订单",
    "订单详情",
    "订单编号",
    "账单详情",
    "交易成功",
    "支付成功",
    "付款成功",
    "实付款",
    "实付",
    "付款方式",
    "交易单号",
    "商户单号",
]


class ImportRow:
    def __init__(self, row_number: int, payload: dict[str, Any]) -> None:
        self.row_number = row_number
        self.payload = payload


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class InvalidOccurredAtError(ValueError):
    """Raised when Agent-provided occurred_at is not a standard date value."""

    def __init__(self, value: object):
        super().__init__("occurred_at must be ISO 8601 or YYYY-MM-DD; parse natural dates with the model before calling ledger.py")
        self.value = value
        self.error = "invalid_occurred_at"


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


def result_for_exception(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, InvalidOccurredAtError):
        return {
            "ok": False,
            "error": exc.error,
            "field": "occurred_at",
            "value": normalize_text(exc.value),
            "message": str(exc),
        }
    return {"ok": False, "error": type(exc).__name__, "message": str(exc)}


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

        CREATE TABLE IF NOT EXISTS bill_contexts (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            chat_id TEXT,
            record_id TEXT,
            source_hash TEXT,
            ui_signature TEXT,
            raw_text TEXT,
            payload_json TEXT,
            transaction_id TEXT,
            status TEXT,
            clarification_json TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS screenshot_rules (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            ui_signature TEXT,
            app_hint TEXT,
            source_app TEXT,
            payment_app TEXT,
            order_platform TEXT,
            category TEXT,
            subcategory TEXT,
            item_name TEXT,
            merchant TEXT,
            confidence REAL,
            hit_count INTEGER,
            last_seen TEXT,
            note TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS summary_cache (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            period_type TEXT,
            period_start TEXT,
            period_end TEXT,
            currency TEXT,
            expense_cents INTEGER,
            income_cents INTEGER,
            refund_cents INTEGER,
            transfer_cents INTEGER,
            unknown_cents INTEGER,
            transaction_count INTEGER,
            by_category_json TEXT,
            by_direction_json TEXT,
            updated_at TEXT,
            UNIQUE(user_id, period_type, period_start, currency)
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
        CREATE INDEX IF NOT EXISTS idx_bill_contexts_user_chat_status
            ON bill_contexts(user_id, chat_id, status, created_at);
        CREATE INDEX IF NOT EXISTS idx_bill_contexts_record
            ON bill_contexts(record_id);
        CREATE INDEX IF NOT EXISTS idx_screenshot_rules_user_signature
            ON screenshot_rules(user_id, ui_signature);
        CREATE INDEX IF NOT EXISTS idx_summary_cache_user_period
            ON summary_cache(user_id, period_type, period_start);
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


def normalize_occurred_at(value: object, default_timestamp: str | None = None) -> tuple[str, bool]:
    text = normalize_text(value)
    if not text:
        return default_timestamp or now_iso(), True
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise InvalidOccurredAtError(value) from exc
        return parsed.astimezone().isoformat(timespec="seconds"), False
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidOccurredAtError(value) from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.isoformat(timespec="seconds"), False


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


def extract_amount_cents_from_text(raw_text: str) -> int | None:
    text = normalize_text(raw_text)
    if not text:
        return None
    preferred_patterns = (
        r"(?:实付款|实付金额|实付|实际支付|支付金额|订单金额|付款金额|成交价|合计|金额)\s*[:：]?\s*[¥￥]?\s*(-?\d+(?:\.\d+)?)",
        r"[¥￥]\s*(-?\d+(?:\.\d+)?)",
        r"(-?\d+(?:\.\d+)?)\s*(?:元|CNY|人民币)",
    )
    for pattern in preferred_patterns:
        matches = re.findall(pattern, text, flags=re.IGNORECASE)
        if matches:
            return amount_to_cents(matches[0])
    numbers = re.findall(r"-?\d+(?:\.\d+)?", text)
    money_like = [number for number in numbers if "." in number or len(number) <= 5]
    if money_like:
        return amount_to_cents(money_like[-1])
    return None


def amount_cents_value(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = str(value).strip().replace(",", "")
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return amount_to_cents(value)


def payload_amount_cents(payload: dict[str, Any]) -> int | None:
    raw_text_amount = extract_amount_cents_from_text(normalize_text(payload.get("raw_text")))
    if _should_prefer_raw_text_amount(payload, raw_text_amount):
        return raw_text_amount

    amount_cents = amount_cents_value(payload.get("amount_cents"))
    if amount_cents is not None:
        return amount_cents
    amount = amount_to_cents(payload.get("amount"))
    if amount is not None:
        return amount
    return raw_text_amount


def _should_prefer_raw_text_amount(payload: dict[str, Any], raw_text_amount: int | None) -> bool:
    if raw_text_amount is None:
        return False
    if normalize_text(payload.get("source_type")).lower() != "image":
        return False
    answer_text = normalize_text(payload.get("answer_text"))
    if answer_text and answer_text_has_explicit_amount(answer_text):
        return False
    return True


def mask_order_no(value: object) -> str | None:
    text = normalize_text(value)
    if not text:
        return None
    compact = re.sub(r"\s+", "", text)
    if re.search(r"\d{9,}", compact):
        compact = re.sub(
            r"\d{9,}",
            lambda match: f"{match.group(0)[:4]}***{match.group(0)[-4:]}",
            compact,
            count=1,
        )
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
    category_names = set(DEFAULT_CATEGORIES)
    category_names.update(category for category, _keywords in CURATED_MAJOR_CATEGORY_RULES)
    if explicit in category_names:
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
    for category, keywords in CURATED_MAJOR_CATEGORY_RULES:
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


def item_details_with_date_metadata(payload: dict[str, Any], occurred_at: str, assumed: bool) -> str | None:
    base_value = payload.get("item_details_json") or payload.get("item_details")
    details: dict[str, Any] = {}
    if isinstance(base_value, dict):
        details.update(base_value)
    elif isinstance(base_value, str) and base_value.strip():
        try:
            parsed = json.loads(base_value)
            if isinstance(parsed, dict):
                details.update(parsed)
            else:
                details["value"] = parsed
        except json.JSONDecodeError:
            details["text"] = base_value.strip()
    elif base_value not in (None, ""):
        details["value"] = base_value

    date_meta = {
        "occurred_at": occurred_at,
        "occurred_at_assumed": bool(assumed or payload.get("occurred_at_assumed")),
    }
    for field in ("occurred_at_text", "occurred_at_resolution"):
        value = normalize_optional_text(payload.get(field))
        if value:
            date_meta[field] = value
    if date_meta["occurred_at_assumed"] or len(date_meta) > 2:
        details["date_resolution"] = date_meta
    return normalize_item_details(details) if details else None


def date_notice_for_payload(payload: dict[str, Any], occurred_at: str, assumed: bool) -> str:
    date_text = occurred_at[:10]
    original = normalize_optional_text(payload.get("occurred_at_text"))
    resolution = normalize_optional_text(payload.get("occurred_at_resolution"))
    if assumed or payload.get("occurred_at_assumed"):
        return f"已默认记为今天 {date_text}，如需修改日期请告诉我。"
    if original:
        if resolution:
            return f"“{original}”已按 {date_text} 记录（{resolution}），如需修改日期请告诉我。"
        return f"“{original}”已按 {date_text} 记录，如需修改日期请告诉我。"
    return ""


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


def lower_text(value: object) -> str:
    return normalize_text(value).lower()


def contains_any(haystack: str, needles: Iterable[str]) -> bool:
    lowered = haystack.lower()
    return any(needle.lower() in lowered for needle in needles)


def ui_signature_for_text(raw_text: str) -> str:
    compact = " ".join(normalize_text(raw_text).split())
    matched = [keyword for keyword in SIGNATURE_KEYWORDS if keyword.lower() in compact.lower()]
    if matched:
        material = "|".join(matched)
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
        return f"kw:{digest}:{'|'.join(matched[:10])}"
    digest = hashlib.sha256(compact[:300].encode("utf-8")).hexdigest()[:20]
    return f"text:{digest}"


def apply_screenshot_rules(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    ui_signature = normalize_text(payload.get("ui_signature"))
    user_id = normalize_optional_text(payload.get("user_id"))
    if not ui_signature:
        return
    row = conn.execute(
        """
        SELECT * FROM screenshot_rules
        WHERE ui_signature = ? AND (user_id IS ? OR user_id IS NULL)
        ORDER BY confidence DESC, hit_count DESC
        LIMIT 1
        """,
        (ui_signature, user_id),
    ).fetchone()
    if not row:
        return
    for field in (
        "source_app",
        "payment_app",
        "order_platform",
        "category",
        "subcategory",
        "item_name",
        "merchant",
    ):
        if not normalize_text(payload.get(field)) and row[field]:
            payload[field] = row[field]
    timestamp = now_iso()
    conn.execute(
        "UPDATE screenshot_rules SET hit_count = hit_count + 1, last_seen = ?, updated_at = ? WHERE id = ?",
        (timestamp, timestamp, row["id"]),
    )


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


def is_bill_like_text(raw_text: str) -> bool:
    text = normalize_text(raw_text)
    if not text:
        return False
    has_amount = extract_amount_cents_from_text(text) is not None
    if not has_amount:
        return False
    has_strong_marker = contains_any(text, STRONG_BILL_KEYWORDS)
    has_detail_marker = contains_any(text, BILL_DETAIL_KEYWORDS)
    has_platform_marker = infer_order_platform({"raw_text": text}) is not None
    has_payment_marker = infer_payment_app({"raw_text": text}) is not None
    looks_like_menu = contains_any(text, NON_BILL_PRICE_CONTEXTS) and not has_strong_marker and not has_detail_marker
    if looks_like_menu:
        return False
    if looks_like_transfer_bill_text(text):
        return True
    return has_strong_marker or (has_detail_marker and (has_platform_marker or has_payment_marker))


def looks_like_transfer_bill_text(raw_text: str) -> bool:
    text = normalize_text(raw_text)
    if not text:
        return False
    has_amount = extract_amount_cents_from_text(text) is not None
    if not has_amount:
        return False
    has_transfer_marker = contains_any(text, TRANSFER_BILL_KEYWORDS)
    if not has_transfer_marker:
        return False
    has_payment_marker = infer_payment_app({"raw_text": text}) is not None or contains_any(text, ("零钱", "银行卡"))
    has_bill_marker = contains_any(text, ("账单", "截图", "单号", "时间", "已收钱", "收款"))
    return has_payment_marker and has_bill_marker


def extract_field_from_text(raw_text: str, labels: Iterable[str]) -> str | None:
    text = normalize_text(raw_text)
    for label in labels:
        pattern = rf"{re.escape(label)}\s*[:：]\s*([^\n\r，,。；;]+)"
        match = re.search(pattern, text)
        if match:
            value = match.group(1).strip()
            value = re.split(r"\s{2,}", value)[0].strip()
            if value:
                return value[:80]
    return None


def extract_transfer_counterparty_from_text(raw_text: str) -> str | None:
    text = normalize_text(raw_text)
    if not text:
        return None
    patterns = (
        r"(?:转账给|转给)\s*([^，,。；;\n\r]+)",
        r"(?:收款方|交易对方)\s*[:：]?\s*([^，,。；;\n\r]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        value = re.sub(r"\s+", " ", match.group(1)).strip(" -:：")
        if value:
            return value[:80]
    return None


def extract_order_no_from_text(raw_text: str) -> str | None:
    text = normalize_text(raw_text)
    match = re.search(r"(?:订单编号|订单号|商户单号|交易单号)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9_-]{5,})", text)
    if not match:
        return None
    return match.group(1).strip()[:80]


def extract_unlabeled_merchant_from_bill_text(raw_text: str) -> str | None:
    text = normalize_text(raw_text)
    if not text:
        return None
    cleaned = text
    cleaned = re.sub(r"[¥￥]\s*\d+(?:\.\d{1,2})?", " ", cleaned)
    cleaned = re.sub(r"\b\d+(?:\.\d{1,2})?\s*元\b", " ", cleaned)
    cleaned = re.sub(r"(?:订单编号|订单号|商户单号|交易单号)\s*[:：]?\s*[A-Za-z0-9_-]{6,}", " ", cleaned)
    cleaned = re.sub(r"(?:支付金额|付款金额|实付金额|成交价|实付款|金额)\s*[:：]?\s*", " ", cleaned)
    cleaned = re.sub(r"(?:付款方式|交易方式|支付方式)\s*[:：]?\s*[^，,。；;\n\r]+", " ", cleaned)
    for app_name, keywords in PAYMENT_KEYWORDS:
        cleaned = cleaned.replace(app_name, " ")
        for keyword in keywords:
            cleaned = cleaned.replace(keyword, " ")
    for platform_name, keywords in PLATFORM_KEYWORDS:
        cleaned = cleaned.replace(platform_name, " ")
        for keyword in keywords:
            cleaned = cleaned.replace(keyword, " ")
    cleaned = re.sub(r"(支付成功|付款成功|交易成功|账单详情|当前状态|已支付|获得[^，,。；;\n\r]*|首页|完成)", " ", cleaned)
    candidates = [
        part.strip()
        for part in re.split(r"[，,。；;\n\r]+|\s{2,}", cleaned)
        if part.strip()
    ]
    for candidate in candidates:
        candidate = re.sub(r"\s+", " ", candidate).strip(" -:：")
        if not candidate or len(candidate) < 2:
            continue
        if any(token in candidate for token in ("余额宝", "银行卡", "红包", "优惠", "评价", "参与")):
            continue
        if re.fullmatch(r"[A-Za-z0-9_-]+", candidate) or re.fullmatch(r"\d+(?:\.\d+)?", candidate):
            continue
        return candidate[:80]
    return None


def infer_payload_from_bill_text(payload: dict[str, Any]) -> dict[str, Any]:
    inferred = dict(payload)
    raw_text = normalize_text(inferred.get("raw_text") or inferred.get("text") or inferred.get("ocr_text"))
    inferred["raw_text"] = raw_text
    inferred.setdefault("source_type", "image")
    inferred.setdefault("direction", default_direction_for_bill_text(raw_text))
    if not normalize_text(inferred.get("amount_cents")) and not normalize_text(inferred.get("amount")):
        amount = extract_amount_cents_from_text(raw_text)
        if amount is not None:
            inferred["amount_cents"] = abs(amount)
    if not normalize_text(inferred.get("source_app")):
        inferred["source_app"] = infer_payment_app({"raw_text": raw_text}) or infer_order_platform({"raw_text": raw_text})
    if not normalize_text(inferred.get("payment_app")):
        payment_app = infer_payment_app({"raw_text": raw_text})
        if payment_app:
            inferred["payment_app"] = payment_app
    if not normalize_text(inferred.get("order_platform")):
        order_platform = infer_order_platform({"raw_text": raw_text})
        if order_platform:
            inferred["order_platform"] = order_platform
    if not normalize_text(inferred.get("merchant")):
        merchant = extract_field_from_text(raw_text, ("商户", "商家", "交易对方", "收款方", "店铺", "卖家"))
        if not merchant:
            merchant = extract_transfer_counterparty_from_text(raw_text)
        if not merchant:
            merchant = extract_unlabeled_merchant_from_bill_text(raw_text)
        if merchant:
            inferred["merchant"] = merchant
    if not normalize_text(inferred.get("item_name")):
        item_name = extract_field_from_text(raw_text, ("商品", "商品名称", "商品说明", "订单商品", "物品", "标题"))
        if item_name:
            inferred["item_name"] = item_name
    if not normalize_text(inferred.get("category")):
        category = infer_category(inferred, infer_direction(inferred))
        if category and category != "其他":
            inferred["category"] = category
    for date_field in DATE_DETAIL_FIELDS:
        if date_field in payload and payload.get(date_field) not in (None, ""):
            inferred[date_field] = payload.get(date_field)
    if not normalize_text(inferred.get("order_no_masked")) and not normalize_text(inferred.get("order_no")):
        order_no = extract_order_no_from_text(raw_text) or extract_field_from_text(raw_text, ("订单编号", "订单号", "商户单号", "交易单号"))
        if order_no:
            inferred["order_no"] = order_no
    inferred.setdefault("confidence", 0.82)
    inferred["ui_signature"] = normalize_text(inferred.get("ui_signature")) or ui_signature_for_text(raw_text)
    return inferred


def default_direction_for_bill_text(raw_text: str) -> str:
    text = normalize_text(raw_text)
    if "退款" in text:
        return "refund"
    if contains_any(text, ("红包", "转账", "转给", "收钱")):
        return "unknown"
    return "expense"


def fields_from_answer_text(answer_text: str) -> dict[str, Any]:
    text = normalize_text(answer_text)
    if not text:
        return {}
    fields: dict[str, Any] = {"answer_text": text}
    decision_markers = ("仍要记账", "还是记账", "新增一笔", "新增记账", "不是同一笔", "不是同一个订单", "单独记一笔")
    if any(marker in text for marker in decision_markers):
        fields["force_new_transaction"] = True
        fields["duplicate_decision"] = "add_new"
        remainder = text
        for marker in decision_markers:
            remainder = remainder.replace(marker, "")
        if not re.sub(r"[，,。；;：:\s]+", "", remainder):
            return fields
    amount = extract_amount_cents_from_text(text) if answer_text_has_explicit_amount(text) else None
    if amount is not None:
        fields["amount_cents"] = amount
    category = next(
        (name for name in [*DEFAULT_CATEGORIES, *(name for name, _ in CURATED_MAJOR_CATEGORY_RULES)] if name in text),
        None,
    )
    if category:
        fields["category"] = category
    platform = infer_order_platform({"raw_text": text})
    if platform:
        fields["order_platform"] = platform
    payment_app = infer_payment_app({"raw_text": text})
    if payment_app:
        fields["payment_app"] = payment_app
    merchant = extract_field_from_text(text, ("商户", "商家", "店铺", "卖家", "收款方"))
    if merchant:
        fields["merchant"] = merchant
    item_name = extract_field_from_text(text, ("商品", "商品名称", "物品", "买的是", "买了", "买的"))
    if not item_name:
        item_match = re.search(r"(?:买的是|买了|买的)\s*([^，,。；;]+)", text)
        if item_match:
            item_name = item_match.group(1).strip()
    if not item_name:
        cleaned = text
        for category_name in [*DEFAULT_CATEGORIES, *(name for name, _ in CURATED_MAJOR_CATEGORY_RULES)]:
            cleaned = cleaned.replace(category_name, "")
        for platform_name, keywords in PLATFORM_KEYWORDS:
            cleaned = cleaned.replace(platform_name, "")
            for keyword in keywords:
                cleaned = cleaned.replace(keyword, "")
        for app_name, keywords in PAYMENT_KEYWORDS:
            cleaned = cleaned.replace(app_name, "")
            for keyword in keywords:
                cleaned = cleaned.replace(keyword, "")
        cleaned = re.sub(r"(分类|类别|是|一个|这个|这笔|这张|截图|账单|订单|消费|记到|归到|归类为|买的是|买了|买的|商品|商户|商家|店铺|卖家|收款方)", " ", cleaned)
        cleaned = re.sub(r"[，,。；;：:\s]+", " ", cleaned).strip()
        if cleaned and len(cleaned) <= 40 and not re.fullmatch(r"\d+(?:\.\d+)?", cleaned):
            item_name = cleaned
    if item_name:
        fields["item_name"] = item_name[:80]
    if not fields.get("category"):
        inferred_category = infer_category(
            {
                "raw_text": text,
                "item_name": fields.get("item_name"),
                "merchant": fields.get("merchant"),
                "order_platform": fields.get("order_platform"),
            },
            "expense",
        )
        if inferred_category:
            fields["category"] = inferred_category
    return fields


def answer_text_has_explicit_amount(text: str) -> bool:
    compact = normalize_text(text)
    if not compact:
        return False
    if re.fullmatch(r"[¥￥]?\s*-?\d+(?:\.\d+)?\s*(?:元|块|块钱|CNY|人民币)?", compact, flags=re.IGNORECASE):
        return True
    amount_markers = (
        "金额", "价钱", "价格", "实付", "付款", "支付", "成交价", "合计", "改成", "更正为",
        "¥", "￥",
    )
    return contains_any(compact, amount_markers)


def bill_missing_fields(payload: dict[str, Any], transaction: dict[str, Any] | None = None) -> list[str]:
    data = {**payload, **(transaction or {})}
    missing = []
    if payload_amount_cents(data) is None:
        missing.append("amount")
    payment_app = normalize_text(data.get("payment_app"))
    order_platform = normalize_text(data.get("order_platform"))
    if not payment_app and not order_platform:
        missing.append("app_or_platform")
    category = normalize_text(data.get("category"))
    item_name = normalize_text(data.get("item_name"))
    merchant = normalize_text(data.get("merchant"))
    if not category or (category == "其他" and not item_name and not merchant):
        missing.append("category")
    if order_platform in BROAD_SHOPPING_PLATFORMS and not item_name:
        missing.append("item_name")
    if data.get("direction") in {"unknown", "transfer"}:
        missing.append("direction")
    return sorted(set(missing))


def clarification_text_for_fields(fields: Iterable[str]) -> list[str]:
    messages = {
        "amount": "金额看不清，请补充实际支付金额。",
        "app_or_platform": "这个截图像账单，但没看清是哪个 App 或平台，请补充一下。",
        "category": "这笔消费分类不确定，请告诉我是餐饮、购物、交通还是其他分类。",
        "item_name": "买的东西看不清，请补充商品或服务名称。",
        "direction": "这笔是消费、退款、收入还是个人转账？请确认一下。",
    }
    return [messages[field] for field in fields if field in messages]


def create_bill_context(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    status: str,
    clarification: Iterable[str] | None = None,
    transaction_id: str | None = None,
) -> dict[str, Any]:
    timestamp = now_iso()
    context = {
        "id": uuid.uuid4().hex,
        "user_id": normalize_optional_text(payload.get("user_id")),
        "chat_id": normalize_optional_text(payload.get("chat_id")),
        "record_id": normalize_optional_text(payload.get("record_id")),
        "source_hash": source_hash_for(payload),
        "ui_signature": normalize_optional_text(payload.get("ui_signature")),
        "raw_text": normalize_optional_text(payload.get("raw_text")),
        "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        "transaction_id": transaction_id,
        "status": status if status in BILL_CONTEXT_STATUSES else "needs_clarification",
        "clarification_json": json.dumps(list(clarification or []), ensure_ascii=False),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    columns = list(context.keys())
    conn.execute(
        f"INSERT INTO bill_contexts ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        [context[column] for column in columns],
    )
    return context


def learn_screenshot_rule(conn: sqlite3.Connection, payload: dict[str, Any], transaction: dict[str, Any]) -> str | None:
    ui_signature = normalize_text(payload.get("ui_signature") or transaction.get("ui_signature"))
    user_id = normalize_optional_text(transaction.get("user_id") or payload.get("user_id"))
    if not ui_signature or not user_id:
        return None
    timestamp = now_iso()
    existing = conn.execute(
        """
        SELECT * FROM screenshot_rules
        WHERE user_id IS ? AND ui_signature = ?
        LIMIT 1
        """,
        (user_id, ui_signature),
    ).fetchone()
    values = {
        "user_id": user_id,
        "ui_signature": ui_signature,
        "app_hint": normalize_optional_text(transaction.get("order_platform") or transaction.get("source_app")),
        "source_app": normalize_optional_text(transaction.get("source_app")),
        "payment_app": normalize_optional_text(transaction.get("payment_app")),
        "order_platform": normalize_optional_text(transaction.get("order_platform")),
        "category": normalize_optional_text(transaction.get("category")),
        "subcategory": normalize_optional_text(transaction.get("subcategory")),
        "item_name": normalize_optional_text(transaction.get("item_name")),
        "merchant": normalize_optional_text(transaction.get("merchant")),
        "confidence": 0.9,
        "last_seen": timestamp,
        "updated_at": timestamp,
    }
    if existing:
        updates = dict(values)
        updates["hit_count"] = int(existing["hit_count"] or 0) + 1
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE screenshot_rules SET {assignments} WHERE id = ?",
            [*updates.values(), existing["id"]],
        )
        return existing["id"]
    rule_id = uuid.uuid4().hex
    insert_values = {
        "id": rule_id,
        **values,
        "hit_count": 1,
        "note": "learned from bill screenshot confirmation",
        "created_at": timestamp,
    }
    columns = list(insert_values.keys())
    conn.execute(
        f"INSERT INTO screenshot_rules ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        [insert_values[column] for column in columns],
    )
    return rule_id


def learn_item_rule_from_transaction(conn: sqlite3.Connection, transaction: dict[str, Any]) -> str | None:
    item_name = normalize_text(transaction.get("item_name"))
    user_id = normalize_optional_text(transaction.get("user_id"))
    if not item_name:
        return None
    timestamp = now_iso()
    existing = conn.execute(
        """
        SELECT * FROM item_rules
        WHERE user_id IS ? AND item_pattern = ?
        LIMIT 1
        """,
        (user_id, item_name),
    ).fetchone()
    values = {
        "normalized_item": item_name,
        "category": normalize_optional_text(transaction.get("category")),
        "subcategory": normalize_optional_text(transaction.get("subcategory")),
        "app_hint": normalize_optional_text(transaction.get("order_platform") or transaction.get("source_app")),
        "last_seen": timestamp,
        "updated_at": timestamp,
    }
    if existing:
        updates = dict(values)
        updates["hit_count"] = int(existing["hit_count"] or 0) + 1
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(f"UPDATE item_rules SET {assignments} WHERE id = ?", [*updates.values(), existing["id"]])
        return existing["id"]
    rule_id = uuid.uuid4().hex
    row = {
        "id": rule_id,
        "user_id": user_id,
        "item_pattern": item_name,
        **values,
        "confidence": 0.9,
        "hit_count": 1,
        "note": "learned from bill confirmation",
        "created_at": timestamp,
    }
    columns = list(row.keys())
    conn.execute(
        f"INSERT INTO item_rules ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        [row[column] for column in columns],
    )
    return rule_id


def learn_merchant_rule_from_transaction(conn: sqlite3.Connection, transaction: dict[str, Any]) -> str | None:
    merchant = normalize_text(transaction.get("merchant"))
    user_id = normalize_optional_text(transaction.get("user_id"))
    if not merchant:
        return None
    timestamp = now_iso()
    existing = conn.execute(
        """
        SELECT * FROM merchant_rules
        WHERE user_id IS ? AND merchant_pattern = ?
        LIMIT 1
        """,
        (user_id, merchant),
    ).fetchone()
    values = {
        "normalized_merchant": merchant,
        "order_platform": normalize_optional_text(transaction.get("order_platform")),
        "payment_app": normalize_optional_text(transaction.get("payment_app")),
        "category": normalize_optional_text(transaction.get("category")),
        "subcategory": normalize_optional_text(transaction.get("subcategory")),
        "last_seen": timestamp,
        "updated_at": timestamp,
    }
    if existing:
        updates = dict(values)
        updates["hit_count"] = int(existing["hit_count"] or 0) + 1
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(f"UPDATE merchant_rules SET {assignments} WHERE id = ?", [*updates.values(), existing["id"]])
        return existing["id"]
    rule_id = uuid.uuid4().hex
    row = {
        "id": rule_id,
        "user_id": user_id,
        "merchant_pattern": merchant,
        **values,
        "confidence": 0.9,
        "hit_count": 1,
        "note": "learned from bill confirmation",
        "created_at": timestamp,
    }
    columns = list(row.keys())
    conn.execute(
        f"INSERT INTO merchant_rules ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        [row[column] for column in columns],
    )
    return rule_id


def learn_bill_rules(conn: sqlite3.Connection, payload: dict[str, Any], transaction: dict[str, Any]) -> dict[str, str | None]:
    return {
        "screenshot_rule_id": learn_screenshot_rule(conn, payload, transaction),
        "item_rule_id": learn_item_rule_from_transaction(conn, transaction),
        "merchant_rule_id": learn_merchant_rule_from_transaction(conn, transaction),
    }


def build_transaction(conn: sqlite3.Connection, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    payload = dict(payload)
    apply_screenshot_rules(conn, payload)
    apply_item_rules(conn, payload)
    apply_merchant_rules(conn, payload)

    timestamp = now_iso()
    direction = infer_direction(payload)
    source_type = normalize_text(payload.get("source_type")) or "manual"
    if source_type not in VALID_SOURCE_TYPES:
        source_type = "manual"
    status = status_for_payload(payload, direction)
    category = infer_category(payload, direction)

    amount_cents = payload_amount_cents(payload)
    if amount_cents is None:
        raise ValueError("amount_cents or amount is required")

    occurred_at, occurred_at_assumed = normalize_occurred_at(payload.get("occurred_at"), timestamp)
    transaction = {
        "id": normalize_text(payload.get("id")) or uuid.uuid4().hex,
        "user_id": normalize_optional_text(payload.get("user_id")),
        "chat_id": normalize_optional_text(payload.get("chat_id")),
        "occurred_at": occurred_at,
        "direction": direction,
        "amount_cents": amount_cents,
        "currency": normalize_text(payload.get("currency")) or DEFAULT_CURRENCY,
        "category": category,
        "subcategory": normalize_optional_text(payload.get("subcategory")),
        "merchant": normalize_optional_text(payload.get("merchant")),
        "item_name": normalize_optional_text(payload.get("item_name")),
        "item_details_json": item_details_with_date_metadata(payload, occurred_at, occurred_at_assumed),
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
    return insert_transaction_with_options(conn, transaction, force_insert=False)


def insert_transaction_with_options(
    conn: sqlite3.Connection,
    transaction: dict[str, Any],
    force_insert: bool = False,
) -> tuple[bool, dict[str, Any]]:
    if not force_insert:
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


def row_to_dict(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    return dict(row)


def matching_text(a: object, b: object) -> bool:
    left = normalize_text(a)
    right = normalize_text(b)
    return bool(left and right and left == right)


def duplicate_match_reasons(transaction: dict[str, Any], existing: sqlite3.Row | dict[str, Any]) -> list[str]:
    reasons = []
    if matching_text(transaction.get("order_no_masked"), existing["order_no_masked"]):
        reasons.append("order_no")
    if matching_text(transaction.get("merchant"), existing["merchant"]):
        reasons.append("merchant")
    if matching_text(transaction.get("item_name"), existing["item_name"]):
        reasons.append("item_name")
    if any(
        matching_text(transaction.get(field), existing[field])
        for field in ("source_app", "payment_app", "order_platform")
    ):
        reasons.append("app_or_platform")
    return reasons


def has_conflicting_duplicate_details(transaction: dict[str, Any], existing: sqlite3.Row | dict[str, Any]) -> bool:
    for field in ("order_no_masked", "merchant", "item_name"):
        left = normalize_text(transaction.get(field))
        right = normalize_text(existing[field])
        if left and right and left != right:
            return True
    return False


def find_possible_duplicate_transaction(
    conn: sqlite3.Connection,
    transaction: dict[str, Any],
) -> tuple[dict[str, Any], list[str]] | None:
    user_id = normalize_optional_text(transaction.get("user_id"))
    occurred_at = parse_date(str(transaction.get("occurred_at") or ""))
    category = normalize_optional_text(transaction.get("category"))
    amount_cents = amount_cents_value(transaction.get("amount_cents"))
    if not user_id or occurred_at is None or not category or amount_cents is None:
        return None
    day_start = occurred_at.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    rows = conn.execute(
        """
        SELECT * FROM transactions
        WHERE user_id = ?
          AND amount_cents = ?
          AND occurred_at >= ?
          AND occurred_at < ?
          AND category = ?
          AND direction = ?
          AND status IN (?, ?)
        ORDER BY occurred_at DESC, created_at DESC
        LIMIT 20
        """,
        (
            user_id,
            amount_cents,
            iso_bound(day_start),
            iso_bound(day_end),
            category,
            transaction.get("direction") or "expense",
            "confirmed",
            "auto_confirmed",
        ),
    ).fetchall()
    for row in rows:
        if row["id"] == transaction.get("id"):
            continue
        if has_conflicting_duplicate_details(transaction, row):
            continue
        reasons = duplicate_match_reasons(transaction, row)
        if reasons:
            return dict(row), reasons
    return None


def describe_transaction_brief(transaction: dict[str, Any] | sqlite3.Row) -> str:
    date_text = str(transaction["occurred_at"] or "")[:10]
    amount = int(transaction["amount_cents"] or 0)
    category = transaction["category"] or "未分类"
    merchant = normalize_optional_text(transaction["merchant"]) or normalize_optional_text(transaction["item_name"])
    merchant_text = f" {merchant}" if merchant else ""
    return f"{date_text} ¥{amount / 100:.2f} {category}{merchant_text}"


def period_bounds_datetimes(
    period: str,
    start: str | None = None,
    end: str | None = None,
    reference: datetime | None = None,
) -> tuple[datetime | None, datetime | None]:
    now = reference or datetime.now().astimezone()
    start_dt = parse_date(start)
    end_dt = parse_date(end)
    if period == "today":
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = start_dt + timedelta(days=1)
    elif period == "week":
        start_dt = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = start_dt + timedelta(days=7)
    elif period == "month":
        start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_dt = month_after(start_dt)
    elif period == "last_month":
        this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start_dt = month_before(this_month)
        end_dt = this_month
    elif period == "all":
        pass
    return start_dt, end_dt


def month_before(value: datetime) -> datetime:
    year = value.year
    month = value.month - 1
    if month < 1:
        year -= 1
        month = 12
    return value.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


def month_after(value: datetime) -> datetime:
    year = value.year
    month = value.month + 1
    if month > 12:
        year += 1
        month = 1
    return value.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


def iso_bound(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def summary_periods_for_transaction(transaction: dict[str, Any] | sqlite3.Row) -> list[tuple[str, datetime, datetime]]:
    occurred = parse_date(str(transaction["occurred_at"] or ""))
    if not occurred:
        return []
    day_start = occurred.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = (occurred - timedelta(days=occurred.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = occurred.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return [
        ("day", day_start, day_start + timedelta(days=1)),
        ("week", week_start, week_start + timedelta(days=7)),
        ("month", month_start, month_after(month_start)),
    ]


def empty_summary_totals() -> dict[str, int]:
    return {
        "expense_cents": 0,
        "income_cents": 0,
        "refund_cents": 0,
        "transfer_cents": 0,
        "unknown_cents": 0,
    }


def accumulate_summary_rows(rows: Iterable[dict[str, Any] | sqlite3.Row]) -> tuple[dict[str, int], dict[str, int], dict[str, int], int]:
    totals = empty_summary_totals()
    by_category: dict[str, int] = {}
    by_direction: dict[str, int] = {}
    count = 0
    for row in rows:
        count += 1
        amount = int(row["amount_cents"] or 0)
        direction = row["direction"] or "unknown"
        key = f"{direction}_cents"
        totals[key if key in totals else "unknown_cents"] += amount
        by_direction[direction] = by_direction.get(direction, 0) + amount
        category = row["category"] or "其他"
        by_category[category] = by_category.get(category, 0) + amount
    return totals, by_category, by_direction, count


def refresh_summary_cache(
    conn: sqlite3.Connection,
    user_id: str | None,
    period_type: str,
    period_start: datetime,
    period_end: datetime,
    currency: str = DEFAULT_CURRENCY,
) -> dict[str, Any]:
    if period_type not in SUMMARY_PERIOD_TYPES or not user_id:
        return {}
    rows = conn.execute(
        """
        SELECT * FROM transactions
        WHERE user_id = ?
          AND currency = ?
          AND occurred_at >= ?
          AND occurred_at < ?
          AND status IN (?, ?)
        """,
        (
            user_id,
            currency,
            iso_bound(period_start),
            iso_bound(period_end),
            "confirmed",
            "auto_confirmed",
        ),
    ).fetchall()
    totals, by_category, by_direction, count = accumulate_summary_rows(rows)
    timestamp = now_iso()
    values = {
        "id": uuid.uuid4().hex,
        "user_id": user_id,
        "period_type": period_type,
        "period_start": iso_bound(period_start),
        "period_end": iso_bound(period_end),
        "currency": currency,
        **totals,
        "transaction_count": count,
        "by_category_json": json.dumps(by_category, ensure_ascii=False, sort_keys=True),
        "by_direction_json": json.dumps(by_direction, ensure_ascii=False, sort_keys=True),
        "updated_at": timestamp,
    }
    conn.execute(
        """
        INSERT INTO summary_cache (
            id, user_id, period_type, period_start, period_end, currency,
            expense_cents, income_cents, refund_cents, transfer_cents, unknown_cents,
            transaction_count, by_category_json, by_direction_json, updated_at
        ) VALUES (
            :id, :user_id, :period_type, :period_start, :period_end, :currency,
            :expense_cents, :income_cents, :refund_cents, :transfer_cents, :unknown_cents,
            :transaction_count, :by_category_json, :by_direction_json, :updated_at
        )
        ON CONFLICT(user_id, period_type, period_start, currency) DO UPDATE SET
            period_end = excluded.period_end,
            expense_cents = excluded.expense_cents,
            income_cents = excluded.income_cents,
            refund_cents = excluded.refund_cents,
            transfer_cents = excluded.transfer_cents,
            unknown_cents = excluded.unknown_cents,
            transaction_count = excluded.transaction_count,
            by_category_json = excluded.by_category_json,
            by_direction_json = excluded.by_direction_json,
            updated_at = excluded.updated_at
        """,
        values,
    )
    return values


def refresh_summary_caches_for_transaction(
    conn: sqlite3.Connection,
    transaction: dict[str, Any] | sqlite3.Row | None,
) -> None:
    if not transaction or not transaction["user_id"]:
        return
    currency = transaction["currency"] or DEFAULT_CURRENCY
    for period_type, period_start, period_end in summary_periods_for_transaction(transaction):
        refresh_summary_cache(conn, transaction["user_id"], period_type, period_start, period_end, currency)


def record_payload(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        transaction, clarification = build_transaction(conn, payload)
    except InvalidOccurredAtError as exc:
        return result_for_exception(exc)
    possible_duplicate = None if payload.get("force_new_transaction") else find_possible_duplicate_transaction(conn, transaction)
    if possible_duplicate:
        existing, reasons = possible_duplicate
        clarification_text = (
            f"这笔账单和已有记录很像：{describe_transaction_brief(existing)}。"
            "我先不新增，请确认是“仍要记账/新增一笔”，还是要撤销或修改已有账单。"
        )
        context = create_bill_context(
            conn,
            {
                **payload,
                "possible_duplicate": True,
                "duplicate_candidate_transaction_id": existing["id"],
                "duplicate_reasons": reasons,
            },
            "needs_clarification",
            [clarification_text],
            transaction_id=existing["id"],
        )
        conn.commit()
        return {
            "ok": True,
            "action": "record-json",
            "status": "needs_clarification",
            "possible_duplicate": True,
            "inserted": False,
            "duplicate": False,
            "context_id": context["id"],
            "transaction": existing,
            "proposed_transaction": transaction,
            "needs_clarification": [clarification_text],
        }
    inserted, row = insert_transaction_with_options(
        conn,
        transaction,
        force_insert=bool(payload.get("force_new_transaction")),
    )
    if inserted:
        refresh_summary_caches_for_transaction(conn, row)
    conn.commit()
    result = {
        "ok": True,
        "action": "record-json",
        "inserted": inserted,
        "duplicate": not inserted,
        "transaction": row,
        "date_notice": date_notice_for_payload(payload, row["occurred_at"], not normalize_text(payload.get("occurred_at"))),
    }
    if clarification:
        result["needs_clarification"] = clarification
    return result


def analyze_bill_payload(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    inferred = infer_payload_from_bill_text(payload)
    raw_text = normalize_text(inferred.get("raw_text"))
    if not is_bill_like_text(raw_text):
        return {"ok": True, "action": "analyze-bill", "is_bill": False}

    apply_screenshot_rules(conn, inferred)
    apply_item_rules(conn, inferred)
    apply_merchant_rules(conn, inferred)
    try:
        transaction, record_clarification = build_transaction(conn, {**inferred, "status": "auto_confirmed"})
    except InvalidOccurredAtError as exc:
        return {"action": "analyze-bill", "is_bill": True, **result_for_exception(exc)}
    except ValueError:
        fields = bill_missing_fields(inferred)
        clarification = clarification_text_for_fields(fields or ["amount"])
        context = create_bill_context(conn, inferred, "needs_clarification", clarification)
        conn.commit()
        return {
            "ok": True,
            "action": "analyze-bill",
            "is_bill": True,
            "status": "needs_clarification",
            "context_id": context["id"],
            "needs_clarification": clarification,
        }

    missing = bill_missing_fields(inferred, transaction)
    if missing or record_clarification:
        clarification = clarification_text_for_fields(missing)
        clarification.extend(item for item in record_clarification if item not in clarification)
        context = create_bill_context(conn, inferred, "needs_clarification", clarification)
        conn.commit()
        return {
            "ok": True,
            "action": "analyze-bill",
            "is_bill": True,
            "status": "needs_clarification",
            "context_id": context["id"],
            "needs_clarification": clarification,
            "ui_signature": inferred.get("ui_signature"),
        }

    possible_duplicate = None if inferred.get("force_new_transaction") else find_possible_duplicate_transaction(conn, transaction)
    if possible_duplicate:
        existing, reasons = possible_duplicate
        duplicate_payload = {
            **inferred,
            "possible_duplicate": True,
            "duplicate_candidate_transaction_id": existing["id"],
            "duplicate_reasons": reasons,
        }
        clarification = [
            f"这张账单和已有记录很像：{describe_transaction_brief(existing)}。我先不新增，请回复“仍要记账/新增一笔”、“撤销这笔”，或重新发送/引用账单截图再修改。"
        ]
        context = create_bill_context(
            conn,
            duplicate_payload,
            "needs_clarification",
            clarification,
            transaction_id=existing["id"],
        )
        conn.commit()
        return {
            "ok": True,
            "action": "analyze-bill",
            "is_bill": True,
            "status": "needs_clarification",
            "possible_duplicate": True,
            "context_id": context["id"],
            "needs_clarification": clarification,
            "transaction": existing,
            "proposed_transaction": transaction,
        }

    inserted, row = insert_transaction(conn, transaction)
    context = create_bill_context(
        conn,
        inferred,
        "auto_recorded" if inserted else "confirmed",
        [],
        transaction_id=row["id"],
    )
    learned = {}
    if inserted:
        learned = learn_bill_rules(conn, inferred, row)
        refresh_summary_caches_for_transaction(conn, row)
    conn.commit()
    return {
        "ok": True,
        "action": "analyze-bill",
        "is_bill": True,
        "status": "auto_recorded" if inserted else "duplicate",
        "inserted": inserted,
        "duplicate": not inserted,
        "context_id": context["id"],
        "transaction": row,
        "learned": learned,
        "message": ledger_recorded_message(row, date_notice_for_payload(inferred, row["occurred_at"], not normalize_text(inferred.get("occurred_at")))),
    }


def ledger_recorded_message(
    transaction: dict[str, Any] | sqlite3.Row,
    date_notice: str = "",
    suffix: str = "如果不需要记账，请回复“不记账”或“撤销记账”，我会撤销这笔。",
) -> str:
    date_text = str(transaction["occurred_at"] or "")[:10]
    amount = int(transaction["amount_cents"] or 0)
    category = transaction["category"] or "未分类"
    suffix_parts = []
    if date_notice:
        suffix_parts.append(date_notice)
    if suffix:
        suffix_parts.append(suffix)
    return f"已记账：{date_text} ¥{amount / 100:.2f} {category}。" + "".join(suffix_parts)


def latest_bill_context(
    conn: sqlite3.Connection,
    user_id: str | None,
    chat_id: str | None,
    statuses: Iterable[str],
    max_age_seconds: int | None = None,
) -> sqlite3.Row | None:
    status_list = [status for status in statuses if status in BILL_CONTEXT_STATUSES]
    if not status_list:
        return None
    where = [f"status IN ({', '.join('?' for _ in status_list)})"]
    params: list[Any] = [*status_list]
    if user_id:
        where.append("user_id = ?")
        params.append(user_id)
    if chat_id:
        where.append("chat_id = ?")
        params.append(chat_id)
    sql = "SELECT * FROM bill_contexts WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT 1"
    context = conn.execute(sql, params).fetchone()
    if context and max_age_seconds is not None and bill_context_is_stale(context, max_age_seconds):
        return None
    return context


def bill_context_is_stale(context: sqlite3.Row, max_age_seconds: int) -> bool:
    created_at = parse_date(str(context["created_at"] or ""))
    if not created_at:
        return True
    now = datetime.now().astimezone()
    if created_at.tzinfo is None:
        created_at = created_at.astimezone()
    age_seconds = (now - created_at).total_seconds()
    return age_seconds > max(0, int(max_age_seconds))


def confirm_bill_context(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    context_id = normalize_text(payload.get("context_id"))
    if context_id:
        context = conn.execute("SELECT * FROM bill_contexts WHERE id = ?", (context_id,)).fetchone()
    else:
        context = latest_bill_context(
            conn,
            normalize_optional_text(payload.get("user_id")),
            normalize_optional_text(payload.get("chat_id")),
            ["needs_clarification"],
        )
    if not context:
        return {"ok": False, "error": "bill_context_not_found"}

    stored_payload = json.loads(context["payload_json"] or "{}")
    merged = {**stored_payload, **{key: value for key, value in payload.items() if value not in (None, "")}}
    merged.setdefault("source_type", "image")
    merged.setdefault("status", "auto_confirmed")
    if stored_payload.get("possible_duplicate") and not merged.get("force_new_transaction"):
        clarification = json.loads(context["clarification_json"] or "[]")
        return {
            "ok": True,
            "action": "confirm-bill",
            "status": "needs_clarification",
            "possible_duplicate": True,
            "context_id": context["id"],
            "transaction_id": context["transaction_id"],
            "needs_clarification": clarification,
        }
    try:
        transaction, clarification = build_transaction(conn, {**merged, "status": "auto_confirmed"})
    except InvalidOccurredAtError as exc:
        return {"action": "confirm-bill", "context_id": context["id"], **result_for_exception(exc)}
    missing = bill_missing_fields(merged, transaction)
    if missing or clarification:
        clarification = clarification_text_for_fields(missing) + [
            item for item in clarification if item not in clarification_text_for_fields(missing)
        ]
        conn.execute(
            "UPDATE bill_contexts SET payload_json = ?, clarification_json = ?, updated_at = ? WHERE id = ?",
            (
                json.dumps(merged, ensure_ascii=False, sort_keys=True),
                json.dumps(clarification, ensure_ascii=False),
                now_iso(),
                context["id"],
            ),
        )
        conn.commit()
        return {
            "ok": True,
            "action": "confirm-bill",
            "status": "needs_clarification",
            "context_id": context["id"],
            "needs_clarification": clarification,
        }

    inserted, row = insert_transaction_with_options(
        conn,
        transaction,
        force_insert=bool(merged.get("force_new_transaction")),
    )
    learned = {}
    if inserted:
        learned = learn_bill_rules(conn, merged, row)
        refresh_summary_caches_for_transaction(conn, row)
    timestamp = now_iso()
    conn.execute(
        """
        UPDATE bill_contexts
        SET payload_json = ?, transaction_id = ?, status = ?, clarification_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            json.dumps(merged, ensure_ascii=False, sort_keys=True),
            row["id"],
            "confirmed",
            "[]",
            timestamp,
            context["id"],
        ),
    )
    conn.commit()
    return {
        "ok": True,
        "action": "confirm-bill",
        "status": "confirmed" if inserted else "duplicate",
        "inserted": inserted,
        "duplicate": not inserted,
        "context_id": context["id"],
        "transaction": row,
        "learned": learned,
        "message": ledger_recorded_message(
            row,
            date_notice_for_payload(merged, row["occurred_at"], not normalize_text(merged.get("occurred_at"))),
            "已记住这类截图和商品规则。",
        ),
    }


def undo_bill_transaction(conn: sqlite3.Connection, user_id: str | None, chat_id: str | None, transaction_id: str | None) -> dict[str, Any]:
    if transaction_id:
        transaction = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
        context = None
    else:
        context = latest_bill_context(conn, user_id, chat_id, ["auto_recorded", "confirmed"])
        if not context or not context["transaction_id"]:
            return {"ok": False, "error": "auto_recorded_bill_not_found"}
        transaction = conn.execute("SELECT * FROM transactions WHERE id = ?", (context["transaction_id"],)).fetchone()
    if not transaction:
        return {"ok": False, "error": "transaction_not_found"}

    timestamp = now_iso()
    conn.execute(
        "UPDATE transactions SET status = ?, updated_at = ? WHERE id = ?",
        ("rejected", timestamp, transaction["id"]),
    )
    if transaction_id:
        conn.execute(
            "UPDATE bill_contexts SET status = ?, updated_at = ? WHERE transaction_id = ?",
            ("rejected", timestamp, transaction_id),
        )
    elif context:
        conn.execute(
            "UPDATE bill_contexts SET status = ?, updated_at = ? WHERE id = ?",
            ("rejected", timestamp, context["id"]),
        )
    refreshed = dict(transaction)
    refreshed["status"] = "rejected"
    refresh_summary_caches_for_transaction(conn, refreshed)
    conn.commit()
    return {
        "ok": True,
        "action": "undo-bill",
        "transaction_id": transaction["id"],
        "status": "rejected",
        "message": "已撤销这笔记账。",
    }


def reject_bill_context(conn: sqlite3.Connection, context_id: str) -> dict[str, Any]:
    context_id = normalize_text(context_id)
    if not context_id:
        return {"ok": False, "error": "bill_context_not_found"}
    timestamp = now_iso()
    cursor = conn.execute(
        "UPDATE bill_contexts SET status = ?, updated_at = ? WHERE id = ?",
        ("rejected", timestamp, context_id),
    )
    conn.commit()
    if cursor.rowcount <= 0:
        return {"ok": False, "error": "bill_context_not_found"}
    return {
        "ok": True,
        "action": "reject-bill-context",
        "context_id": context_id,
        "status": "rejected",
        "message": "好的，这次不新增记录，已有账单保留。",
    }


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
        try:
            transaction, _ = build_transaction(conn, parsed.payload)
        except InvalidOccurredAtError:
            continue
        was_inserted, row = insert_transaction(conn, transaction)
        if was_inserted:
            inserted += 1
            transaction_ids.append(row["id"])
            refresh_summary_caches_for_transaction(conn, row)
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
    start_dt, end_dt = period_bounds_datetimes(period, start, end)
    return iso_bound(start_dt), iso_bound(end_dt)


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
    where.append("status != ?")
    params.append("rejected")
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
    if user_id and period in {"today", "week", "month", "last_month"} and not start and not end:
        period_type = "day" if period == "today" else "month" if period == "last_month" else period
        start_dt, end_dt = period_bounds_datetimes(period)
        if start_dt and end_dt:
            start_iso = iso_bound(start_dt)
            cached = conn.execute(
                """
                SELECT * FROM summary_cache
                WHERE user_id = ? AND period_type = ? AND period_start = ? AND currency = ?
                LIMIT 1
                """,
                (user_id, period_type, start_iso, DEFAULT_CURRENCY),
            ).fetchone()
            cache_hit = cached is not None
            if cached is None:
                cached_values = refresh_summary_cache(conn, user_id, period_type, start_dt, end_dt)
                conn.commit()
            else:
                cached_values = dict(cached)
            totals = {
                "expense_cents": int(cached_values.get("expense_cents") or 0),
                "income_cents": int(cached_values.get("income_cents") or 0),
                "refund_cents": int(cached_values.get("refund_cents") or 0),
                "transfer_cents": int(cached_values.get("transfer_cents") or 0),
                "unknown_cents": int(cached_values.get("unknown_cents") or 0),
            }
            return {
                "ok": True,
                "action": "summary",
                "period": period,
                "period_start": start_iso,
                "period_end": iso_bound(end_dt),
                "count": int(cached_values.get("transaction_count") or 0),
                "totals": totals,
                "by_direction": json.loads(cached_values.get("by_direction_json") or "{}"),
                "by_category": json.loads(cached_values.get("by_category_json") or "{}"),
                "cached": True,
                "cache_hit": cache_hit,
            }

    rows = [
        row
        for row in query_transactions(conn, user_id, period, 100000, start, end)
        if row.get("status") in SUMMARY_STATUSES
    ]
    totals, by_category, by_direction, count = accumulate_summary_rows(rows)
    return {
        "ok": True,
        "action": "summary",
        "period": period,
        "count": count,
        "totals": totals,
        "by_direction": by_direction,
        "by_category": by_category,
        "cached": False,
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
    if field == "occurred_at":
        try:
            new_value, _ = normalize_occurred_at(new_value)
        except InvalidOccurredAtError as exc:
            return result_for_exception(exc)
    timestamp = now_iso()
    conn.execute(
        f"UPDATE transactions SET {field} = ?, updated_at = ? WHERE id = ?",
        (new_value, timestamp, args.transaction_id),
    )
    learned_type, learned_id = update_learning_rule(conn, transaction, field, str(new_value))
    refresh_summary_caches_for_transaction(conn, transaction)
    updated_transaction = conn.execute(
        "SELECT * FROM transactions WHERE id = ?",
        (args.transaction_id,),
    ).fetchone()
    refresh_summary_caches_for_transaction(conn, updated_transaction)
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
        "bill_contexts",
        "screenshot_rules",
        "summary_cache",
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

    analyze = sub.add_parser("analyze-bill", help="Analyze vision-extracted bill text and auto-record clear private bills.")
    analyze.add_argument("--json", dest="json_text", help="JSON object payload.")
    analyze.add_argument("--file", help="Path to a JSON payload file. Reads stdin when omitted.")
    analyze.set_defaults(func=cmd_analyze_bill)

    confirm_bill = sub.add_parser("confirm-bill", help="Confirm a pending screenshot bill and learn its rules.")
    confirm_bill.add_argument("--json", dest="json_text", help="JSON object payload.")
    confirm_bill.add_argument("--file", help="Path to a JSON payload file. Reads stdin when omitted.")
    confirm_bill.set_defaults(func=cmd_confirm_bill)

    undo_bill = sub.add_parser("undo-bill", help="Reject the latest auto-recorded bill transaction.")
    undo_bill.add_argument("--user-id")
    undo_bill.add_argument("--chat-id")
    undo_bill.add_argument("--transaction-id")
    undo_bill.set_defaults(func=cmd_undo_bill)

    import_parser = sub.add_parser("import-file", help="Import a user-provided CSV bill export.")
    import_parser.add_argument("--path", required=True, help="Path to CSV/XLS/XLSX export.")
    import_parser.add_argument("--source", choices=["alipay", "wechat", "auto"], default="auto")
    import_parser.add_argument("--user-id")
    import_parser.set_defaults(func=cmd_import_file)

    query = sub.add_parser("query", help="Query local transactions.")
    query.add_argument("--user-id")
    query.add_argument("--period", choices=["today", "week", "month", "last_month", "all"], default="all")
    query.add_argument("--start")
    query.add_argument("--end")
    query.add_argument("--limit", type=int, default=20)
    query.set_defaults(func=cmd_query)

    summary = sub.add_parser("summary", help="Summarize local transactions.")
    summary.add_argument("--user-id")
    summary.add_argument("--period", choices=["today", "week", "month", "last_month", "all"], default="month")
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
    export.add_argument("--period", choices=["today", "week", "month", "last_month", "all"], default="all")
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


def cmd_analyze_bill(args: argparse.Namespace) -> None:
    conn, _ = connection_for_args(args)
    try:
        init_db(conn)
        payload = read_json_arg(args)
        json_print(analyze_bill_payload(conn, payload))
    finally:
        conn.close()


def cmd_confirm_bill(args: argparse.Namespace) -> None:
    conn, _ = connection_for_args(args)
    try:
        init_db(conn)
        payload = read_json_arg(args)
        json_print(confirm_bill_context(conn, payload))
    finally:
        conn.close()


def cmd_undo_bill(args: argparse.Namespace) -> None:
    conn, _ = connection_for_args(args)
    try:
        init_db(conn)
        json_print(undo_bill_transaction(conn, args.user_id, args.chat_id, args.transaction_id))
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
