from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.log import logger
from common.utils import expand_path
from config import conf


RESULT_TTL_SECONDS = 24 * 60 * 60
IMAGE_TTL_SECONDS = 7 * 24 * 60 * 60
RELATED_FOLLOWUP_WINDOW_SECONDS = 15 * 60
LEDGER_FOLLOWUP_WINDOW_SECONDS = 5 * 60
DEFAULT_FOLLOWUP_WAIT_SECONDS = 6.0
DEFAULT_MAX_WORKERS = 2
DEFAULT_MAX_TOKENS = 700
_LEDGER_MODULE = None
DEFAULT_PROMPT = (
    "请用中文识别这张图片，结果用于后续私聊回复。保持自然、简短，不要使用英文，"
    "不要写成报告格式。说明主要主体、可见动作或场景、重要文字/OCR，以及必要的不确定性。"
    "除非图片本身是文档，否则不要使用标题或分段。"
    "如果图片像消费账单、支付账单或订单详情，请直接用你的视觉理解提取日期、金额、平台、商户、商品和付款方式；"
    "金额必须保留截图里看到的精确数字和小数位，不要估算、不要四舍五入成整数；看不清请说明不确定，不要猜。"
    "日期必须尽量解析成 YYYY-MM-DD 或 ISO 8601，不要只写“昨天”“上周”“母亲节”这类自然语言。"
)

_EXPLICIT_IMAGE_QUESTION_MARKERS = (
    "这是什么",
    "这个是什么",
    "图里是什么",
    "图片是什么",
    "照片是什么",
    "帮我识别",
    "识别一下",
    "看一下这张",
    "看看这张",
    "这张图",
    "这张照片",
    "图里有",
    "图上",
    "图片里",
    "照片里",
    "ocr",
    "what is this",
    "what's this",
)
_RELATED_IMAGE_MARKERS = (
    "这是",
    "这个是",
    "这就是",
    "刚拍",
    "刚才发",
    "今天",
    "我的",
    "午餐",
    "午饭",
    "晚餐",
    "晚饭",
    "早餐",
    "车",
    "饭",
    "菜",
)


_NON_IMAGE_TASK_MARKERS = (
    "github",
    "gitlab",
    "git hub",
    "repo",
    "repository",
    "pullrequest",
    "pull-request",
    "issue",
    "token",
    "pat",
    "apikey",
    "api-key",
    "skill",
    "mcp",
    "ssh",
    "\u4ed3\u5e93",  # repository
    "\u8d26\u53f7",  # account
    "\u8d26\u6237",  # account
    "\u6388\u6743",  # authorization
    "\u5de5\u5177",  # tool
    "\u8c03\u7528",  # invoke
)


@dataclass
class ImageRecognitionRecord:
    record_id: str
    session_id: str
    channel_type: str
    image_hash: str
    image_path: str
    is_group: bool
    msg_id: str = ""
    sender_label: str = ""
    status: str = "pending"
    result: str = ""
    error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float = 0.0
    started_new_job: bool = False

    @classmethod
    def from_state(cls, data: Dict[str, Any]) -> "ImageRecognitionRecord":
        return cls(
            record_id=str(data.get("record_id") or data.get("id") or ""),
            session_id=str(data.get("session_id") or ""),
            channel_type=str(data.get("channel_type") or ""),
            image_hash=str(data.get("image_hash") or ""),
            image_path=str(data.get("image_path") or ""),
            is_group=bool(data.get("is_group", False)),
            msg_id=str(data.get("msg_id") or ""),
            sender_label=str(data.get("sender_label") or ""),
            status=str(data.get("status") or "pending"),
            result=str(data.get("result") or ""),
            error=str(data.get("error") or ""),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            completed_at=float(data.get("completed_at") or 0.0),
        )

    def to_state(self) -> Dict[str, Any]:
        return {
            "record_id": self.record_id,
            "session_id": self.session_id,
            "channel_type": self.channel_type,
            "image_hash": self.image_hash,
            "image_path": self.image_path,
            "is_group": self.is_group,
            "msg_id": self.msg_id,
            "sender_label": self.sender_label,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }


class ImageRecognitionManager:
    def __init__(self, workspace_root: Optional[str] = None, max_workers: Optional[int] = None):
        self.workspace_root = Path(expand_path(workspace_root or conf().get("agent_workspace", "~/cow")))
        self.root = self.workspace_root / "data" / "image-recognition-cache"
        self.images_dir = self.root / "images"
        self.state_path = self.root / "state.json"
        self.result_ttl_seconds = self._int_conf("image_recognition_result_ttl_seconds", RESULT_TTL_SECONDS)
        self.image_ttl_seconds = self._int_conf("image_recognition_image_ttl_seconds", IMAGE_TTL_SECONDS)
        self.related_followup_window_seconds = self._int_conf(
            "image_recognition_related_followup_window_seconds",
            RELATED_FOLLOWUP_WINDOW_SECONDS,
        )
        self.ledger_followup_window_seconds = self._int_conf(
            "china_expense_ledger_followup_window_seconds",
            LEDGER_FOLLOWUP_WINDOW_SECONDS,
        )
        worker_count = max_workers or self._int_conf("image_recognition_workers", DEFAULT_MAX_WORKERS)
        self.executor = ThreadPoolExecutor(max_workers=max(1, int(worker_count)), thread_name_prefix="image-recognition")
        self._lock = threading.RLock()
        self._records: Dict[str, ImageRecognitionRecord] = {}
        self._latest_by_session: Dict[str, str] = {}
        self._pending_by_key: Dict[str, Future] = {}
        self._suppressed_auto_replies = set()
        self._loaded = False

    @staticmethod
    def _int_conf(key: str, default: int) -> int:
        try:
            return int(conf().get(key, default) or default)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _float_conf(key: str, default: float) -> float:
        try:
            return float(conf().get(key, default) or default)
        except (TypeError, ValueError):
            return default

    def register_image(
        self,
        *,
        session_id: str,
        channel_type: str,
        image_path: str,
        is_group: bool = False,
        msg_id: str = "",
        sender_label: str = "",
    ) -> Optional[ImageRecognitionRecord]:
        session_id = str(session_id or "").strip()
        source = str(image_path or "").strip()
        if not session_id or not source:
            return None
        source_path = Path(expand_path(source))
        if not source_path.is_file():
            logger.warning("[ImageRecognition] image not found: %s", source)
            return None

        now = time.time()
        image_hash = self._hash_file(source_path)
        record_id = self._record_id(session_id, image_hash)
        pending_key = self._pending_key(session_id, image_hash)
        copied_path = self._copy_image(source_path, session_id, image_hash)

        with self._lock:
            self._load_state_locked()
            self.cleanup_locked(now)
            record = self._records.get(record_id)
            if record and self._record_result_is_fresh(record, now):
                record.image_path = copied_path
                record.updated_at = now
                record.started_new_job = False
                self._latest_by_session[session_id] = record_id
                self._save_state_locked()
                return self._copy_record(record)

            if record is None:
                record = ImageRecognitionRecord(
                    record_id=record_id,
                    session_id=session_id,
                    channel_type=channel_type,
                    image_hash=image_hash,
                    image_path=copied_path,
                    is_group=is_group,
                    msg_id=msg_id,
                    sender_label=sender_label,
                    created_at=now,
                    updated_at=now,
                )
                self._records[record_id] = record
            else:
                record.channel_type = channel_type or record.channel_type
                record.image_path = copied_path
                record.is_group = is_group
                record.msg_id = msg_id or record.msg_id
                record.sender_label = sender_label or record.sender_label
                record.status = "pending"
                record.error = ""
                record.updated_at = now

            self._latest_by_session[session_id] = record_id
            future = self._pending_by_key.get(pending_key)
            if future and not future.done():
                record.started_new_job = False
                self._save_state_locked()
                return self._copy_record(record)

            future = self.executor.submit(self._recognize_and_store, record_id)
            self._pending_by_key[pending_key] = future
            record.started_new_job = True
            self._save_state_locked()
            return self._copy_record(record)

    def add_done_callback(self, record: ImageRecognitionRecord, callback) -> bool:
        if not record:
            return False
        pending_key = self._pending_key(record.session_id, record.image_hash)
        with self._lock:
            future = self._pending_by_key.get(pending_key)
        if not future:
            return False
        future.add_done_callback(lambda fut: callback(self.get_record(record.record_id)))
        return True

    def get_record(self, record_id: str) -> Optional[ImageRecognitionRecord]:
        with self._lock:
            self._load_state_locked()
            record = self._records.get(record_id)
            return self._copy_record(record) if record else None

    def latest_for_session(self, session_id: str) -> Optional[ImageRecognitionRecord]:
        with self._lock:
            self._load_state_locked()
            record_id = self._latest_by_session.get(str(session_id or "").strip())
            record = self._records.get(record_id or "")
            return self._copy_record(record) if record else None

    def recent_image_refs_for_session(
        self,
        session_id: str,
        *,
        limit: int = 7,
        max_age_seconds: Optional[float] = None,
    ) -> List[str]:
        session_id = str(session_id or "").strip()
        if not session_id:
            return []
        try:
            max_refs = max(1, min(int(limit), 7))
        except (TypeError, ValueError):
            max_refs = 7
        if max_age_seconds is None:
            max_age_seconds = self.related_followup_window_seconds
        try:
            max_age = float(max_age_seconds)
        except (TypeError, ValueError):
            max_age = float(RELATED_FOLLOWUP_WINDOW_SECONDS)

        now = time.time()
        rows: list[tuple[float, str]] = []
        with self._lock:
            self._load_state_locked()
            self.cleanup_locked(now)
            for record in self._records.values():
                if record.session_id != session_id or not record.image_path:
                    continue
                image_path = str(record.image_path)
                if not Path(image_path).exists():
                    continue
                ts = max(
                    float(record.completed_at or 0),
                    float(record.updated_at or 0),
                    float(record.created_at or 0),
                )
                if max_age > 0 and ts > 0 and now - ts > max_age:
                    continue
                rows.append((ts, image_path))
        rows.sort(key=lambda item: item[0])
        refs: list[str] = []
        for _, image_path in rows[-max_refs:]:
            if image_path not in refs:
                refs.append(image_path)
        return refs

    def build_followup_context(self, session_id: str, wait_seconds: Optional[float] = None) -> str:
        session_id = str(session_id or "").strip()
        if not session_id:
            return ""
        wait = self._float_conf("image_recognition_followup_wait_seconds", DEFAULT_FOLLOWUP_WAIT_SECONDS)
        if wait_seconds is not None:
            wait = max(0.0, float(wait_seconds))

        record = self.latest_for_session(session_id)
        if not record:
            return ""

        if record.status == "pending":
            self._wait_for_record(record, wait)
            record = self.latest_for_session(session_id) or record

        if record.status == "done" and record.result:
            return self._format_done_context(record)
        if record.status == "error":
            return self._format_error_context(record)
        return self._format_pending_context(record)

    def handle_text(self, channel, context, content: str) -> bool:
        """Handle explicit image follow-up without starting another foreground vision job."""
        if not context:
            return False

        raw_content = str(content or "")
        user_text = raw_content.split("[Recent image context]", 1)[0].strip()
        if self._handle_ledger_undo_text(channel, context, user_text):
            return True
        looks_like_ledger_confirmation = self._looks_like_ledger_confirmation_text(user_text)
        if self._handle_ledger_confirmation_text(channel, context, user_text):
            return True
        if looks_like_ledger_confirmation:
            return False
        intent = self._classify_followup_intent(user_text)
        session_id = str(context.get("session_id") or "").strip()
        record = self.latest_for_session(session_id)
        if not record:
            return False
        if intent == "related" and not self.related_followup_is_current(record):
            return False

        if intent in {"explicit", "related"}:
            self.suppress_auto_reply(record.record_id)
            self._ensure_record_current(record)
            record = self.latest_for_session(session_id) or record
            if record.status == "done" and self._record_result_is_fresh(record, time.time()):
                channel._send_plain_text(context, self.public_reply_for(record, intent, user_text, context=context))
                return True
            if record.status == "pending":
                def _send_followup(done_record) -> None:
                    if not done_record:
                        return
                    channel._send_plain_text(
                        context,
                        self.public_reply_for(done_record, intent, user_text, context=context),
                    )

                if not self.add_done_callback(record, _send_followup):
                    latest = self.latest_for_session(session_id) or record
                    if latest.status in {"done", "error"}:
                        _send_followup(latest)
                return True
            return False

        return False

    def _handle_ledger_confirmation_text(self, channel, context, user_text: str) -> bool:
        if not user_text or self._context_is_group(context):
            return False
        compact = "".join(user_text.split())
        if len(compact) > 80:
            return False
        if not self._looks_like_ledger_confirmation_text(user_text):
            return False
        try:
            ledger = self._load_ledger_module()
            if not ledger:
                return False
            user_id = (
                self._context_get(context, "memory_user_id")
                or self._context_get(context, "from_user_id")
                or self._context_get(context, "session_id")
            )
            chat_id = (
                self._context_get(context, "chat_id")
                or self._context_get(context, "conversation_id")
                or self._context_get(context, "session_id")
            )
            answer_fields = ledger.fields_from_answer_text(user_text)
            if not answer_fields:
                return False
            conn = ledger.open_db(ledger.db_path_from_env())
            try:
                ledger.init_db(conn)
                pending_context = ledger.latest_bill_context(
                    conn,
                    user_id,
                    chat_id,
                    ["needs_clarification"],
                    max_age_seconds=self.ledger_followup_window_seconds,
                )
                if not pending_context:
                    return False
                result = ledger.confirm_bill_context(
                    conn,
                    {
                        **answer_fields,
                        "context_id": pending_context["id"],
                        "user_id": user_id,
                        "chat_id": chat_id,
                    },
                )
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("[ImageRecognition] ledger bill confirmation skipped: %s", exc)
            return False
        if not result.get("ok"):
            return False
        if result.get("status") == "needs_clarification":
            reply = self._format_ledger_result({"ok": True, "is_bill": True, **result})
            if reply:
                channel._send_plain_text(context, reply)
                return True
            return False
        channel._send_plain_text(context, result.get("message") or "已记账，并记住这类规则。")
        return True

    @classmethod
    def _looks_like_ledger_confirmation_text(cls, user_text: str) -> bool:
        compact = "".join(str(user_text or "").split())
        if not compact:
            return False
        if cls._looks_like_non_ledger_task_text(compact):
            return False
        strong_markers = (
            "仍要记账",
            "还是记账",
            "新增一笔",
            "新增记账",
            "不是同一笔",
            "不是同一个订单",
            "单独记一笔",
            "买的是",
            "买了",
            "买的",
            "商品",
            "分类",
            "类别",
            "归类",
            "记到",
            "商户",
            "商家",
            "店铺",
            "卖家",
            "收款方",
            "平台",
            "付款",
            "支付",
        )
        context_markers = ("账单", "订单", "这笔", "这张", "截图", "刚才", "上面", "那个", "这个消费")
        known_answers = (
            "支付宝",
            "微信支付",
            "闲鱼",
            "咸鱼",
            "淘宝",
            "京东",
            "美团",
            "美团外卖",
            "餐饮",
            "外卖",
            "购物",
            "交通",
            "AI工具",
            "其他",
        )
        if any(marker in compact for marker in strong_markers):
            return True
        if any(marker in compact for marker in context_markers) and any(answer in compact for answer in known_answers):
            return True
        if len(compact) <= 24 and any(answer == compact or answer in compact for answer in known_answers):
            return True
        if len(compact) <= 32 and any(marker in compact.lower() for marker in ("api", "token")):
            return True
        return False

    @staticmethod
    def _looks_like_non_ledger_task_text(compact: str) -> bool:
        lowered = compact.lower()
        if any(marker in lowered for marker in ("backend", "github", "gitlab", "mcp", "ssh")):
            return True
        system_context = ("后端", "模型", "llm", "LLM", "配置", "密钥", "秘钥", "key")
        query_context = ("查", "查询", "查看", "统计", "汇总", "多少", "当前", "现在", "状态", "使用量", "用量", "消耗", "额度")
        token_context = ("token", "api", "API", "额度", "用量", "消耗")
        if any(marker in compact for marker in system_context) and any(marker in compact for marker in query_context):
            return True
        if any(marker in compact for marker in query_context) and any(marker in compact for marker in token_context):
            return True
        return False

    def _handle_ledger_undo_text(self, channel, context, user_text: str) -> bool:
        if not user_text or self._context_is_group(context):
            return False
        compact = "".join(user_text.lower().split())
        explicit_delete_markers = ("撤销记账", "撤销这笔", "撤销该笔", "删除这笔", "删掉这笔")
        undo_markers = (
            "不记账",
            "撤销记账",
            "取消记账",
            "这笔不要记",
            "不用记账",
            "不要记账",
            "撤销这笔",
            "撤销该笔",
            "删除这笔",
            "删掉这笔",
        )
        if not any(marker in compact for marker in undo_markers):
            return False
        try:
            ledger = self._load_ledger_module()
            if not ledger:
                return False
            conn = ledger.open_db(ledger.db_path_from_env())
            try:
                ledger.init_db(conn)
                user_id = self._context_get(context, "memory_user_id") or self._context_get(context, "from_user_id") or self._context_get(context, "session_id")
                chat_id = self._context_get(context, "chat_id") or self._context_get(context, "conversation_id") or self._context_get(context, "session_id")
                recent_context = ledger.latest_bill_context(
                    conn,
                    user_id,
                    chat_id,
                    ["needs_clarification"],
                    max_age_seconds=self.ledger_followup_window_seconds,
                )
                if recent_context:
                    try:
                        stored_payload = json.loads(recent_context["payload_json"] or "{}")
                    except Exception:
                        stored_payload = {}
                    if not stored_payload.get("possible_duplicate") or not recent_context["transaction_id"]:
                        return False
                    if any(marker in compact for marker in explicit_delete_markers):
                        result = ledger.undo_bill_transaction(conn, user_id, chat_id, recent_context["transaction_id"])
                    else:
                        result = ledger.reject_bill_context(conn, recent_context["id"])
                else:
                    recent_context = ledger.latest_bill_context(
                        conn,
                        user_id,
                        chat_id,
                        ["auto_recorded", "confirmed"],
                        max_age_seconds=self.ledger_followup_window_seconds,
                    )
                    if not recent_context:
                        return False
                    result = ledger.undo_bill_transaction(conn, user_id, chat_id, recent_context["transaction_id"])
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("[ImageRecognition] ledger undo skipped: %s", exc)
            return False
        if result.get("ok"):
            channel._send_plain_text(context, result.get("message") or "已撤销这笔记账。")
        else:
            channel._send_plain_text(context, "我没找到刚才那笔可撤销的自动记账。")
        return True

    def classify_followup_intent(self, content: str) -> str:
        return self._classify_followup_intent(content)

    def should_use_followup_context(self, session_id: str, content: str) -> bool:
        intent = self._classify_followup_intent(content)
        if intent == "none":
            return False
        record = self.latest_for_session(session_id)
        if not record:
            return False
        if intent == "related":
            return self.related_followup_is_current(record)
        return True

    def related_followup_is_current(self, record: Optional[ImageRecognitionRecord], now: Optional[float] = None) -> bool:
        if not record:
            return False
        try:
            window = float(self.related_followup_window_seconds)
        except (TypeError, ValueError):
            window = float(RELATED_FOLLOWUP_WINDOW_SECONDS)
        if window <= 0:
            return False
        reference = max(
            float(record.completed_at or 0),
            float(record.updated_at or 0),
            float(record.created_at or 0),
        )
        return reference > 0 and float(now or time.time()) - reference <= window

    def proactive_private_reply_for(
        self,
        record: Optional[ImageRecognitionRecord],
        intent: str = "default",
        user_text: str = "",
        context: Any = None,
        allow_non_bill: bool = False,
    ) -> str:
        if not record:
            return ""
        latest = self.get_record(record.record_id) or record
        if latest.status == "done" and latest.result:
            ledger_reply = self._ledger_bill_reply(latest, context)
            if ledger_reply:
                return ledger_reply
            if not allow_non_bill:
                return ""
        elif not allow_non_bill:
            return ""
        return self.format_public_reply(latest, intent, user_text, context=context)

    def public_reply_for(
        self,
        record: Optional[ImageRecognitionRecord],
        intent: str = "default",
        user_text: str = "",
        context: Any = None,
    ) -> str:
        return self.format_public_reply(record, intent, user_text, context=context)

    def format_public_reply(
        self,
        record: Optional[ImageRecognitionRecord],
        intent: str = "default",
        user_text: str = "",
        context: Any = None,
    ) -> str:
        if not record:
            return ""
        latest = self.get_record(record.record_id) or record
        if latest.status == "done" and latest.result:
            ledger_reply = self._ledger_bill_reply(latest, context)
            if ledger_reply:
                return ledger_reply
            if intent in {"default", "related"}:
                synthesized = self._synthesize_casual_reply(latest, intent, user_text, context)
                if synthesized:
                    return synthesized
            result = self._short_result(latest.result)
            if intent == "explicit":
                return f"这张图里主要是：{result}"
            if intent == "related":
                prefix = user_text.strip(" ，,。")
                if prefix:
                    return f"{prefix}，我记下了。看起来还有这些细节：{result}"
                return f"我记下了，这张里看起来是{result}"
            return f"这张看起来是{result}"
        if latest.status == "error":
            return "这张图我刚才没识别清楚，你可以再问我一次，我会换个方式处理。"
        return ""

    def _ledger_bill_reply(self, record: ImageRecognitionRecord, context: Any = None) -> str:
        if record.is_group:
            return ""
        if not bool(conf().get("china_expense_ledger_private_auto", True)):
            return ""
        try:
            ledger = self._load_ledger_module()
            if not ledger:
                return ""
            if not ledger.is_bill_like_text(record.result):
                return ""
            db_path = getattr(ledger, "db_path_from_env")()
            conn = ledger.open_db(db_path)
            try:
                ledger.init_db(conn)
                payload = {
                    "user_id": self._ledger_user_id(record, context),
                    "chat_id": self._context_get(context, "chat_id")
                    or self._context_get(context, "conversation_id")
                    or self._context_get(context, "session_id")
                    or record.session_id,
                    "record_id": record.record_id,
                    "source_type": "image",
                    "source_app": "",
                    "raw_text": record.result,
                    "source_hash": record.image_hash,
                }
                result = ledger.analyze_bill_payload(conn, payload)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("[ImageRecognition] ledger bill analysis skipped: %s", exc)
            return ""
        return self._format_ledger_result(result)

    @staticmethod
    def _format_ledger_result(result: Dict[str, Any]) -> str:
        if not isinstance(result, dict) or not result.get("ok") or not result.get("is_bill"):
            return ""
        if result.get("status") == "needs_clarification":
            questions = result.get("needs_clarification") or []
            text = "；".join(str(item).strip("。") for item in questions if str(item).strip())
            if text:
                if result.get("possible_duplicate"):
                    return text
                return f"这张像账单，但还有点不确定：{text}"
            return "这张像账单，但有些字段看不清，补充一下我再记。"
        if result.get("status") in {"auto_recorded", "duplicate"}:
            message = str(result.get("message") or "").strip()
            if message and not result.get("duplicate"):
                return message
            transaction = result.get("transaction") or {}
            amount = transaction.get("amount_cents")
            category = transaction.get("category") or "未分类"
            date_text = str(transaction.get("occurred_at") or "")[:10]
            amount_text = ""
            try:
                amount_text = f"¥{int(amount) / 100:.2f}"
            except (TypeError, ValueError):
                pass
            parts = [part for part in (date_text, amount_text, category) if part]
            detail = "：" + " ".join(parts) if parts else f"：{category}"
            suffix = "如果不需要记账，请回复“不记账”或“撤销记账”，我会撤销这笔。"
            if result.get("duplicate"):
                return f"这张账单看起来已经记过了{detail}。{suffix}"
            return f"已记账{detail}。{suffix}"
        return ""

    @classmethod
    def _load_ledger_module(cls):
        global _LEDGER_MODULE
        if _LEDGER_MODULE is not None:
            return _LEDGER_MODULE
        candidates = [
            Path(__file__).resolve().parents[1] / "skills" / "china-expense-ledger" / "scripts" / "ledger.py",
            Path(expand_path("~/cow")) / "skills" / "china-expense-ledger" / "scripts" / "ledger.py",
        ]
        for candidate in candidates:
            if not candidate.is_file():
                continue
            spec = importlib.util.spec_from_file_location("china_expense_ledger_runtime", candidate)
            if not spec or not spec.loader:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _LEDGER_MODULE = module
            return module
        return None

    def _ledger_user_id(self, record: ImageRecognitionRecord, context: Any = None) -> str:
        return (
            self._context_get(context, "memory_user_id")
            or self._context_get(context, "from_user_id")
            or self._context_get(context, "session_id")
            or record.session_id
        )

    def _synthesize_casual_reply(
        self,
        record: ImageRecognitionRecord,
        intent: str,
        user_text: str,
        context: Any = None,
    ) -> str:
        try:
            from agent.protocol import LLMRequest
            from bridge.agent_bridge import AgentLLMModel
            from bridge.bridge import Bridge

            llm = AgentLLMModel(Bridge())
            session_id = self._context_get(context, "conversation_id") or self._context_get(context, "session_id")
            if session_id:
                llm.session_id = session_id
            llm.channel_type = self._context_get(context, "channel_type") or record.channel_type
            llm.user_id = self._context_get(context, "memory_user_id") or self._context_get(context, "from_user_id")
            llm.user_label = (
                self._context_get(context, "actual_user_nickname")
                or self._context_get(context, "from_user_nickname")
                or record.sender_label
            )

            memory_context = self._collect_memory_context(context)
            conversation_context = self._collect_recent_conversation_context(context)
            system = (
                "你是一个在私聊里回复朋友照片的助手。"
                "图片事实已经由后台识图给出，你不要再声称自己重新识图，也不要写报告、不要中英混杂、不要说“识别结果/这张图像是/我看了下”。"
                "结合用户长期记忆和最近对话，用像朋友随口聊天一样的中文回复。"
                "如果用户只是随手发图且没有文字，回复 1-2 句，轻松自然，可以有一点幽默或关心。"
                "如果用户补了一句相关文字，就接住用户那句话自然回应。"
                "不要把菜品、物体逐项机械列完；不要编造看不见的细节。"
            )
            user = (
                f"用户发图后的文字意图：{intent}\n"
                f"用户补充文字：{user_text.strip() or '（没有补充文字，像随手发图）'}\n"
                f"后台识图事实：{record.result.strip()}\n\n"
                f"长期记忆摘要：\n{memory_context or '未检索到'}\n\n"
                f"最近短期对话：\n{conversation_context or '未检索到'}\n\n"
                "请直接给要发给用户的一小段自然回复。"
            )
            response = llm.call(
                LLMRequest(
                    messages=[{"role": "user", "content": user}],
                    system=system,
                    max_tokens=220,
                    temperature=0.7,
                    tools=[],
                    request_timeout=45,
                    reasoning_effort="medium",
                    reasoning_effort_locked=True,
                    cache_shape_metadata={"request_kind": "private_image_casual_reply"},
                )
            )
            return self._extract_model_text(response)
        except Exception as exc:
            logger.debug("[ImageRecognition] casual reply synthesis failed: %s", exc)
            return ""

    def _collect_memory_context(self, context: Any, limit: int = 2200) -> str:
        memory_user_id = self._context_get(context, "memory_user_id") or self._context_get(context, "from_user_id")
        if not memory_user_id:
            return ""
        workspace = Path(expand_path(conf().get("agent_workspace", "~/cow") or "~/cow"))
        user_dir = workspace / "memory" / "users" / str(memory_user_id)
        profile = self._read_file_tail(user_dir / "USER.md", min(900, limit))
        remaining = max(0, limit - len(profile))
        memory = self._read_file_tail(user_dir / "MEMORY.md", remaining) if remaining else ""
        return "\n".join(part for part in (profile, memory) if part).strip()

    def _collect_recent_conversation_context(self, context: Any, max_turns: int = 4, limit: int = 2200) -> str:
        session_id = self._context_get(context, "conversation_id") or self._context_get(context, "session_id")
        if not session_id:
            return ""
        try:
            from agent.memory import get_conversation_store

            messages = get_conversation_store().load_messages(str(session_id), max_turns=max_turns)
        except Exception as exc:
            logger.debug("[ImageRecognition] recent conversation context unavailable: %s", exc)
            return ""

        lines: List[str] = []
        for message in messages[-(max_turns * 2):]:
            role = str(message.get("role") or "").strip()
            text = self._message_text(message.get("content"))
            if not text or "[Recent image context]" in text:
                continue
            label = "用户" if role == "user" else "助手"
            lines.append(f"{label}: {text}")
        text = "\n".join(lines)
        return text[-limit:]

    @staticmethod
    def _message_text(content: Any, limit: int = 500) -> str:
        if isinstance(content, str):
            return " ".join(content.split())[:limit]
        if isinstance(content, list):
            parts = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            return " ".join(" ".join(parts).split())[:limit]
        if isinstance(content, dict):
            return " ".join(str(content.get("text") or content.get("content") or "").split())[:limit]
        return ""

    @staticmethod
    def _context_get(context: Any, key: str, default: str = "") -> str:
        if context is None:
            return default
        try:
            value = context.get(key, default)
        except Exception:
            value = default
        return str(value or default).strip()

    @staticmethod
    def _context_is_group(context: Any) -> bool:
        if context is None:
            return False
        try:
            value = context.get("isgroup", False)
        except Exception:
            return False
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "y"}

    @staticmethod
    def _read_file_tail(path: Path, limit: int) -> str:
        if limit <= 0 or not path.exists() or not path.is_file():
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
        return text[-limit:].strip()

    @staticmethod
    def _extract_model_text(response: Any) -> str:
        if not isinstance(response, dict) or response.get("error"):
            return ""
        choices = response.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text") or item.get("content")
                        if text:
                            parts.append(str(text))
                    elif item:
                        parts.append(str(item))
                return "\n".join(parts).strip()
        return str(response.get("content") or "").strip()

    def suppress_auto_reply(self, record_id: str) -> None:
        if record_id:
            with self._lock:
                self._suppressed_auto_replies.add(record_id)

    def is_auto_reply_suppressed(self, record_id: str) -> bool:
        with self._lock:
            return record_id in self._suppressed_auto_replies

    def _wait_for_record(self, record: ImageRecognitionRecord, timeout: float) -> None:
        pending_key = self._pending_key(record.session_id, record.image_hash)
        with self._lock:
            future = self._pending_by_key.get(pending_key)
        if not future or timeout <= 0:
            return
        try:
            future.result(timeout=timeout)
        except TimeoutError:
            return
        except Exception as e:
            logger.debug("[ImageRecognition] wait failed: %s", e)

    def _ensure_record_current(self, record: ImageRecognitionRecord) -> None:
        now = time.time()
        if record.status == "done" and self._record_result_is_fresh(record, now):
            return
        if not record.image_path or not Path(record.image_path).exists():
            return
        with self._lock:
            current = self._records.get(record.record_id)
            if not current:
                return
            pending_key = self._pending_key(current.session_id, current.image_hash)
            future = self._pending_by_key.get(pending_key)
            if future and not future.done():
                return
            current.status = "pending"
            current.error = ""
            current.updated_at = now
            self._pending_by_key[pending_key] = self.executor.submit(self._recognize_and_store, current.record_id)
            self._save_state_locked()

    def _recognize_and_store(self, record_id: str) -> ImageRecognitionRecord:
        with self._lock:
            self._load_state_locked()
            record = self._records.get(record_id)
            if not record:
                raise KeyError(record_id)
            image_path = record.image_path
            prompt = str(conf().get("image_recognition_prompt") or DEFAULT_PROMPT)
            max_tokens = self._int_conf("image_recognition_max_tokens", DEFAULT_MAX_TOKENS)

        try:
            content = self._recognize_image(image_path, prompt, max_tokens=max_tokens)
            status = "done"
            error = ""
        except Exception as e:
            logger.warning("[ImageRecognition] recognition failed for %s: %s", record_id, e)
            content = ""
            status = "error"
            error = str(e)

        with self._lock:
            record = self._records.get(record_id)
            if not record:
                raise KeyError(record_id)
            now = time.time()
            record.status = status
            record.result = content.strip()
            record.error = error
            record.updated_at = now
            record.completed_at = now if status == "done" else 0.0
            record.started_new_job = False
            self._pending_by_key.pop(self._pending_key(record.session_id, record.image_hash), None)
            self._save_state_locked()
            return self._copy_record(record)

    @staticmethod
    def _recognize_image(image_path: str, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        from agent.tools.vision.vision import Vision
        from bridge.agent_bridge import AgentLLMModel
        from bridge.bridge import Bridge

        tool = Vision(
            config={
                "reasoning_effort": "low",
                "reasoning_effort_locked": True,
                "max_tokens": max_tokens,
            }
        )
        tool.model = AgentLLMModel(Bridge())
        result = tool.execute(
            {
                "image": image_path,
                "question": prompt,
                "reasoning_effort": "low",
                "reasoning_effort_locked": True,
            }
        )
        if not result or result.status != "success":
            raise RuntimeError(getattr(result, "result", "vision failed"))
        payload = result.result
        if isinstance(payload, dict):
            content = str(payload.get("content") or "").strip()
        else:
            content = str(payload or "").strip()
        if not content:
            raise RuntimeError("empty vision result")
        return content

    @staticmethod
    def _format_done_context(record: ImageRecognitionRecord) -> str:
        return (
            "\n\n[Recent image context]\n"
            "The user recently sent an image. Use this for follow-up questions. "
            "Answer naturally and briefly unless the user explicitly asks for a detailed report.\n"
            f"Image file for tools: [image: {record.image_path}]\n"
            f"Recognition result: {record.result.strip()}\n"
            "[/Recent image context]"
        )

    @staticmethod
    def _format_pending_context(record: ImageRecognitionRecord) -> str:
        return (
            "\n\n[Recent image context]\n"
            "The user recently sent an image, and background recognition is still running. "
            "If the image matters, say that it is still being analyzed or use the image file directly.\n"
            f"Image file for tools: [image: {record.image_path}]\n"
            "[/Recent image context]"
        )

    @staticmethod
    def _format_error_context(record: ImageRecognitionRecord) -> str:
        return (
            "\n\n[Recent image context]\n"
            "The user recently sent an image, but background recognition did not finish successfully. "
            "Use the image file directly if the user asks about it.\n"
            f"Image file for tools: [image: {record.image_path}]\n"
            "[/Recent image context]"
        )

    @staticmethod
    def _classify_followup_intent(content: str) -> str:
        compact = "".join(str(content or "").lower().split())
        if not compact:
            return "none"
        if any(marker.lower() in compact for marker in _EXPLICIT_IMAGE_QUESTION_MARKERS):
            return "explicit"
        if any(marker in compact for marker in _NON_IMAGE_TASK_MARKERS):
            return "none"
        if len(compact) <= 120 and any(marker in compact for marker in _RELATED_IMAGE_MARKERS):
            return "related"
        return "none"

    @staticmethod
    def _short_result(content: str, limit: int = 180) -> str:
        text = " ".join(str(content or "").split())
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + "..."

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _safe_session(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def _record_id(cls, session_id: str, image_hash: str) -> str:
        return f"{cls._safe_session(session_id)}-{image_hash[:24]}"

    @classmethod
    def _pending_key(cls, session_id: str, image_hash: str) -> str:
        return cls._record_id(session_id, image_hash)

    def _copy_image(self, source_path: Path, session_id: str, image_hash: str) -> str:
        suffix = source_path.suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            suffix = ".jpg"
        target_dir = self.images_dir / self._safe_session(session_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{image_hash[:24]}{suffix}"
        if not target.exists() or target.stat().st_size != source_path.stat().st_size:
            shutil.copy2(str(source_path), str(target))
        now = time.time()
        os.utime(str(target), (now, now))
        return str(target)

    def _record_result_is_fresh(self, record: ImageRecognitionRecord, now: float) -> bool:
        return (
            record.status == "done"
            and bool(record.result)
            and record.completed_at > 0
            and now - record.completed_at <= self.result_ttl_seconds
            and bool(record.image_path)
            and Path(record.image_path).exists()
        )

    def cleanup_locked(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        expired = []
        for record_id, record in self._records.items():
            result_expired = (
                record.completed_at > 0
                and now - record.completed_at > self.result_ttl_seconds
            )
            image_missing = bool(record.image_path) and not Path(record.image_path).exists()
            if result_expired or image_missing:
                expired.append(record_id)
        for record_id in expired:
            record = self._records.pop(record_id, None)
            if record and self._latest_by_session.get(record.session_id) == record_id:
                self._latest_by_session.pop(record.session_id, None)

        if self.images_dir.exists():
            for path in self.images_dir.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    if now - path.stat().st_mtime > self.image_ttl_seconds:
                        path.unlink()
                except OSError:
                    logger.debug("[ImageRecognition] failed to clean image copy: %s", path)

    def _load_state_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("[ImageRecognition] failed to read state: %s", e)
            return
        sessions = data.get("sessions", {}) if isinstance(data, dict) else {}
        if not isinstance(sessions, dict):
            return
        for session_id, session_data in sessions.items():
            if not isinstance(session_data, dict):
                continue
            latest = str(session_data.get("latest_record_id") or "")
            records = session_data.get("records", [])
            if latest:
                self._latest_by_session[str(session_id)] = latest
            if not isinstance(records, list):
                continue
            for item in records:
                if not isinstance(item, dict):
                    continue
                record = ImageRecognitionRecord.from_state(item)
                if not record.record_id:
                    continue
                if record.status == "pending":
                    record.status = "error"
                    record.error = "recognition was interrupted before completion"
                self._records[record.record_id] = record

    def _save_state_locked(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        by_session: Dict[str, Dict[str, Any]] = {}
        for record in self._records.values():
            session = by_session.setdefault(
                record.session_id,
                {
                    "latest_record_id": self._latest_by_session.get(record.session_id, ""),
                    "records": [],
                },
            )
            session["records"].append(record.to_state())
        payload = {"version": 1, "sessions": by_session}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    @staticmethod
    def _copy_record(record: Optional[ImageRecognitionRecord]) -> Optional[ImageRecognitionRecord]:
        if record is None:
            return None
        copied = ImageRecognitionRecord.from_state(record.to_state())
        copied.started_new_job = record.started_new_job
        return copied


_manager: Optional[ImageRecognitionManager] = None
_manager_lock = threading.Lock()


def get_image_recognition_manager() -> ImageRecognitionManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = ImageRecognitionManager()
        return _manager


def reset_image_recognition_manager(manager: Optional[ImageRecognitionManager] = None) -> None:
    global _manager
    with _manager_lock:
        if _manager is not None and _manager is not manager:
            try:
                _manager.executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                _manager.executor.shutdown(wait=False)
            except Exception:
                pass
        _manager = manager
