# encoding:utf-8
"""Runtime state for long-running chat tasks.

This module is intentionally independent from channel implementations so the
WeChat fast lane can read progress and request cancellation without touching
the Agent internals directly.
"""

import re
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from queue import Empty
from typing import Any, Dict, Optional

from common.dequeue import Dequeue
from common.latency import elapsed, format_seconds, monotonic


class TaskCancelled(Exception):
    """Raised when a user requested cooperative task cancellation."""


class TaskPolicy(Enum):
    CONTROL_PROGRESS = "control_progress"
    CONTROL_CANCEL = "control_cancel"
    CONTROL_SKIP = "control_skip"
    QUICK_REPLY = "quick_reply"
    NORMAL = "normal"
    MEDIA = "media"


class CancellationToken:
    def __init__(self):
        self._event = threading.Event()
        self._reason = ""

    def cancel(self, reason: str = "user_cancelled") -> None:
        self._reason = reason or "user_cancelled"
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason


@dataclass
class ProgressSnapshot:
    task_id: str = ""
    task_summary: str = ""
    started_at: Optional[float] = None
    last_update_at: Optional[float] = None
    phase: str = "queued"
    turn: int = 0
    max_turns: int = 0
    last_tool_name: str = ""
    last_tool_status: str = ""
    tool_call_count: int = 0
    llm_call_count: int = 0
    output_chars: int = 0
    last_visible_preview: str = ""
    cancel_requested: bool = False
    pending_count: int = 0
    last_prompt_tokens: int = 0
    last_cached_tokens: int = 0
    last_reasoning_tokens: int = 0
    error: str = ""

    def update(self, event_type: str, data: Optional[Dict[str, Any]] = None) -> None:
        data = data or {}
        self.last_update_at = monotonic()

        if event_type == "agent_start":
            self.phase = "llm_waiting"
        elif event_type == "turn_start":
            self.phase = "llm_waiting"
            self.turn = _safe_int(data.get("turn"), self.turn)
        elif event_type == "message_start":
            self.phase = "llm_waiting"
        elif event_type == "message_update":
            delta = str(data.get("delta", ""))
            self.phase = "llm_streaming"
            self.output_chars += len(delta)
            if delta.strip():
                self.last_visible_preview = sanitize_preview(delta)
        elif event_type == "message_end":
            tool_calls = data.get("tool_calls") or []
            self.phase = "tool_running" if tool_calls else "generating"
        elif event_type == "tool_execution_start":
            self.phase = "tool_running"
            self.last_tool_name = sanitize_identifier(data.get("tool_name", ""))
            self.last_tool_status = "running"
        elif event_type == "tool_execution_end":
            self.phase = "llm_waiting"
            self.last_tool_name = sanitize_identifier(data.get("tool_name", self.last_tool_name))
            self.last_tool_status = sanitize_identifier(data.get("status", "done"))
            self.tool_call_count += 1
        elif event_type == "llm_usage":
            usage = data.get("usage") or {}
            self.llm_call_count += 1
            self.last_prompt_tokens = _safe_int(usage.get("prompt_tokens"), self.last_prompt_tokens)
            self.last_cached_tokens = _safe_int(usage.get("cached_tokens"), self.last_cached_tokens)
            details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
            self.last_reasoning_tokens = _safe_int(
                details.get("reasoning_tokens"), self.last_reasoning_tokens
            )
        elif event_type == "agent_end":
            if not self.cancel_requested:
                self.phase = "done"
        elif event_type == "cancelled":
            self.cancel_requested = True
            self.phase = "cancel_requested"
        elif event_type == "error":
            self.phase = "error"
            self.error = sanitize_preview(data.get("error", ""))

    def mark_cancel_requested(self) -> None:
        self.cancel_requested = True
        self.phase = "cancel_requested"
        self.last_update_at = monotonic()

    def mark_finished(self, phase: str) -> None:
        self.phase = phase
        self.last_update_at = monotonic()


@dataclass
class RunningTask:
    task_id: str
    summary: str
    token: CancellationToken
    started_at: float = field(default_factory=monotonic)


class SessionRuntime:
    def __init__(self, concurrency: int = 1):
        self.queue = Dequeue()
        self.semaphore = threading.BoundedSemaphore(max(1, int(concurrency or 1)))
        self.lock = threading.RLock()
        self.running_task: Optional[RunningTask] = None
        self.progress = ProgressSnapshot()
        self.last_notice_at = 0.0
        self.last_visible_output_at = 0.0
        self.last_visible_output_source = ""
        self.last_silence_notice_at = 0.0
        self.silence_notice_count = 0

    def start_task(self, summary: str, max_turns: int = 0) -> CancellationToken:
        with self.lock:
            task_id = uuid.uuid4().hex[:12]
            token = CancellationToken()
            clean_summary = sanitize_preview(summary, limit=80)
            now = monotonic()
            self.running_task = RunningTask(
                task_id=task_id,
                summary=clean_summary,
                token=token,
                started_at=now,
            )
            self.progress = ProgressSnapshot(
                task_id=task_id,
                task_summary=clean_summary,
                started_at=now,
                last_update_at=now,
                phase="llm_waiting",
                max_turns=max_turns,
                pending_count=self.queue.qsize(),
            )
            self.last_visible_output_at = now
            self.last_visible_output_source = "task_start"
            self.last_silence_notice_at = 0.0
            self.silence_notice_count = 0
            return token

    def finish_task(self, phase: str = "done") -> None:
        with self.lock:
            if self.running_task and self.running_task.token.is_cancelled():
                phase = "cancel_requested"
            self.progress.pending_count = self.queue.qsize()
            self.progress.mark_finished(phase)
            self.running_task = None

    def update_progress(self, event_type: str, data: Optional[Dict[str, Any]] = None) -> None:
        with self.lock:
            self.progress.pending_count = self.queue.qsize()
            self.progress.update(event_type, data)

    def mark_visible_output(self, source: str = "") -> None:
        with self.lock:
            self.last_visible_output_at = monotonic()
            self.last_visible_output_source = sanitize_identifier(source or "visible_output")
            self.last_notice_at = self.last_visible_output_at
            if self.last_visible_output_source != "silence_notice":
                self.last_silence_notice_at = 0.0
                self.silence_notice_count = 0

    def claim_silence_notice(
        self,
        first_notice_seconds: float = 45.0,
        repeat_notice_seconds: float = 120.0,
    ) -> Optional[str]:
        with self.lock:
            if not self.running_task:
                return None
            if self.running_task.token.is_cancelled() or self.progress.cancel_requested:
                return None
            now = monotonic()
            visible_at = self.last_visible_output_at or self.running_task.started_at
            notice_at = self.last_silence_notice_at
            if notice_at:
                silence_for = now - max(visible_at, notice_at)
                required = max(0.0, float(repeat_notice_seconds))
            else:
                silence_for = now - visible_at
                required = max(0.0, float(first_notice_seconds))
            if silence_for < required:
                return None
            self.last_silence_notice_at = now
            self.silence_notice_count += 1
            return self._silence_notice_text(now)

    def cancel_running(self, reason: str = "user_cancelled") -> bool:
        with self.lock:
            if not self.running_task:
                return False
            self.running_task.token.cancel(reason)
            self.progress.mark_cancel_requested()
            return True

    def clear_pending(self) -> int:
        cleared = 0
        with self.lock:
            while True:
                try:
                    self.queue.get_nowait()
                    cleared += 1
                except Empty:
                    break
            self.progress.pending_count = self.queue.qsize()
            return cleared

    def has_running(self) -> bool:
        with self.lock:
            return self.running_task is not None

    def should_send_queue_notice(self, interval_seconds: float = 30.0) -> bool:
        with self.lock:
            if not self.running_task:
                return False
            now = monotonic()
            if now - self.last_notice_at < interval_seconds:
                return False
            self.last_notice_at = now
            return True

    def status_text(self, include_eta_note: bool = False) -> str:
        with self.lock:
            running = self.running_task is not None
            progress = self.progress
            pending = self.queue.qsize()

            if not running:
                if progress.phase in {"done", "error", "cancel_requested"} and progress.started_at:
                    state = _phase_text(progress.phase)
                    text = (
                        f"当前没有运行中的任务。\n"
                        f"最近任务状态：{state}。\n"
                        f"待处理队列：{pending} 条。"
                    )
                else:
                    text = f"当前没有运行中的任务。\n待处理队列：{pending} 条。"
                if include_eta_note:
                    text += "\n无法精确估算剩余时间。"
                return text

            parts = [
                f"当前任务已运行 {format_seconds(elapsed(progress.started_at))}。",
                f"阶段：{_phase_text(progress.phase)}。",
            ]
            if include_eta_note:
                parts.insert(0, "无法精确估算剩余时间，只能显示当前阶段和已耗时。")
                if progress.last_update_at:
                    parts.append(f"当前阶段已持续约 {format_seconds(elapsed(progress.last_update_at))}。")
            if progress.turn:
                turn_text = f"第 {progress.turn} 轮"
                if progress.max_turns:
                    turn_text += f"/最多 {progress.max_turns} 轮"
                parts.append(turn_text + "。")
            if progress.last_tool_name:
                status = f"（{progress.last_tool_status}）" if progress.last_tool_status else ""
                parts.append(f"最近工具：{progress.last_tool_name}{status}。")
            if progress.llm_call_count:
                parts.append(f"模型调用：{progress.llm_call_count} 次。")
            if progress.output_chars:
                parts.append(f"已生成约 {progress.output_chars} 字。")
            if progress.last_visible_preview:
                parts.append(f"最近输出：{progress.last_visible_preview}")
            if pending:
                parts.append(f"队列中还有 {pending} 条消息。")
            if progress.cancel_requested:
                parts.append("已收到取消请求，正在等待当前步骤结束。")
            return "\n".join(parts)

    def failure_notice_text(self, reason: str = "error") -> str:
        with self.lock:
            reason = sanitize_identifier(reason or "error")
            progress = self.progress
            reason_text = {
                "max_steps": "这轮已经达到单次尝试的步骤上限，我先停止继续尝试，避免继续空转。",
                "context_overflow": "这轮被对话上下文长度卡住了，我先停止本轮尝试。",
                "rate_limit": "这轮被模型限流或服务繁忙卡住了，我先停止本轮尝试。",
                "model_error": "这轮模型调用没有稳定完成，我先停止本轮尝试。",
                "error": "这轮处理没有稳定完成，我先停止本轮尝试。",
            }.get(reason, "这轮处理没有稳定完成，我先停止本轮尝试。")
            parts = [reason_text]
            if progress.task_summary:
                parts.append(f"任务：{progress.task_summary}。")
            if progress.started_at:
                parts.append(f"已尝试 {format_seconds(elapsed(progress.started_at))}。")
            if progress.turn:
                turn_text = f"已进行到第 {progress.turn} 轮"
                if progress.max_turns:
                    turn_text += f"/最多 {progress.max_turns} 轮"
                parts.append(turn_text + "。")
            if progress.last_tool_name:
                parts.append(f"最近工具：{progress.last_tool_name}。")
            if progress.error:
                parts.append(f"当前卡点：{progress.error}。")
            parts.append("建议把需求拆成更小一步，或换一种描述方式让我继续尝试。")
            return "\n".join(parts)

    def _silence_notice_text(self, now: float) -> str:
        progress = self.progress
        parts = ["还在处理这条需求，只是刚才一段时间没有新的可见输出，我先报个进度。"]
        if progress.task_summary:
            parts.append(f"任务：{progress.task_summary}。")
        if progress.started_at:
            parts.append(f"已运行 {format_seconds(elapsed(progress.started_at, now))}，阶段：{_phase_text(progress.phase)}。")
        else:
            parts.append(f"阶段：{_phase_text(progress.phase)}。")
        if progress.turn:
            turn_text = f"第 {progress.turn} 轮"
            if progress.max_turns:
                turn_text += f"/最多 {progress.max_turns} 轮"
            parts.append(turn_text + "。")
        if progress.last_tool_name:
            status = f"（{progress.last_tool_status}）" if progress.last_tool_status else ""
            parts.append(f"最近工具：{progress.last_tool_name}{status}。")
        if progress.last_visible_preview:
            parts.append(f"最近输出：{progress.last_visible_preview}")
        pending = self.queue.qsize()
        if pending:
            parts.append(f"队列中还有 {pending} 条消息。")
        parts.append("可以发送 /状态 查看进展，或发送 /取消 取消当前任务。")
        return "\n".join(parts)


_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|authorization|cookie|auth)\s*[:=]\s*\S+"
)
_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"'<>]+")
_POSIX_PATH_RE = re.compile(r"(?<!\w)/(?:[\w.-]+/){2,}[\w.-]+")


def sanitize_preview(value: Any, limit: int = 80) -> str:
    text = str(value or "")
    text = _SECRET_RE.sub(r"\1=<redacted>", text)
    text = _WINDOWS_PATH_RE.sub("<path>", text)
    text = _POSIX_PATH_RE.sub("<path>", text)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def sanitize_identifier(value: Any, limit: int = 64) -> str:
    text = str(value or "")
    text = re.sub(r"[^0-9A-Za-z_.:-]+", "_", text).strip("_")
    return text[:limit] or "unknown"


def _phase_text(phase: str) -> str:
    return {
        "queued": "排队中",
        "llm_waiting": "等待模型返回",
        "llm_streaming": "模型正在输出",
        "tool_running": "正在执行工具",
        "generating": "正在整理最终回复",
        "cancel_requested": "取消中",
        "done": "已完成",
        "error": "出错",
    }.get(phase or "", phase or "未知")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default
