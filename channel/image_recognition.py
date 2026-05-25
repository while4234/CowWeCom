from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from common.log import logger
from common.utils import expand_path
from config import conf


RESULT_TTL_SECONDS = 24 * 60 * 60
IMAGE_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_FOLLOWUP_WAIT_SECONDS = 6.0
DEFAULT_MAX_WORKERS = 2
DEFAULT_MAX_TOKENS = 700
DEFAULT_PROMPT = (
    "Identify this image for a later chat follow-up. Keep it natural and short. "
    "Mention the main subject, visible action or scene, important text/OCR, and "
    "uncertainty if needed. Do not use report headings or formal sections unless "
    "the image itself is a document where key text matters."
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
        intent = self._classify_followup_intent(user_text)
        session_id = str(context.get("session_id") or "").strip()
        record = self.latest_for_session(session_id)
        if not record:
            return False

        if intent in {"explicit", "related"}:
            self.suppress_auto_reply(record.record_id)
            self._ensure_record_current(record)
            record = self.latest_for_session(session_id) or record
            if record.status == "done" and self._record_result_is_fresh(record, time.time()):
                channel._send_plain_text(context, self.format_public_reply(record, intent, user_text))
                return True
            if record.status == "pending":
                def _send_followup(done_record) -> None:
                    if not done_record:
                        return
                    channel._send_plain_text(
                        context,
                        self.format_public_reply(done_record, intent, user_text),
                    )

                if not self.add_done_callback(record, _send_followup):
                    latest = self.latest_for_session(session_id) or record
                    if latest.status in {"done", "error"}:
                        _send_followup(latest)
                return True
            return False

        return False

    def classify_followup_intent(self, content: str) -> str:
        return self._classify_followup_intent(content)

    def public_reply_for(self, record: Optional[ImageRecognitionRecord]) -> str:
        return self.format_public_reply(record, "default", "")

    def format_public_reply(
        self,
        record: Optional[ImageRecognitionRecord],
        intent: str = "default",
        user_text: str = "",
    ) -> str:
        if not record:
            return ""
        latest = self.get_record(record.record_id) or record
        if latest.status == "done" and latest.result:
            result = self._short_result(latest.result)
            if intent == "explicit":
                return f"我看了下，这张图大概是：{result}"
            if intent == "related":
                prefix = user_text.strip(" ，,。")
                if prefix:
                    return f"{prefix}。我这边也记下了：{result}"
                return f"我记下了：{result}"
            return f"我看了下，这张图像是{result}"
        if latest.status == "error":
            return "这张图我刚才没识别清楚，你可以再问我一次，我会换个方式处理。"
        return ""

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
