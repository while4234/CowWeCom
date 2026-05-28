# encoding:utf-8

"""Streaming Grok TTS replies for WeCom voice-originated requests."""

from __future__ import annotations

import queue
import re
import threading
import time
from typing import Any, Dict, Optional

from bridge.context import Context
from bridge.reply import Reply, ReplyType
from common.log import logger
from config import conf
from integrations.hermes_xai.tts import XaiTtsError, generate_xai_tts


_SENTENCE_END_RE = re.compile(r"[。！？!?…]+|(?<!\d)\.(?!\d)")


class VoiceReplyStreamer:
    """Per-session TTS worker that speaks model deltas as short segments."""

    def __init__(self, context: Context, channel, decision: Dict[str, Any]):
        self.context = context
        self.channel = channel
        self.decision = dict(decision or {})
        self.session_id = str(context.get("session_id") or "")
        self.channel_type = str(context.get("channel_type") or getattr(channel, "channel_type", "") or "")
        self.max_chars = _positive_int(conf().get("grok_voice_max_segment_chars"), 120)
        self.min_chars = min(self.max_chars, _positive_int(conf().get("grok_voice_min_segment_chars"), 12))
        self.idle_seconds = max(0.1, _positive_int(conf().get("grok_voice_flush_idle_ms"), 800) / 1000.0)
        self.queue_size = _positive_int(conf().get("grok_voice_tts_queue_size"), 4)
        self._buffer = ""
        self._text_parts = []
        self._pending_text_fallbacks = []
        self._voice_segments_sent = 0
        self._queue: queue.Queue = queue.Queue(maxsize=self.queue_size)
        self._stop = threading.Event()
        self._done = threading.Event()
        self._lock = threading.RLock()
        self._idle_timer: Optional[threading.Timer] = None
        self._worker = threading.Thread(
            target=self._run_worker,
            name=f"voice-stream-{self.session_id[:12] or 'session'}",
            daemon=True,
        )
        self._worker.start()

    @classmethod
    def try_create(cls, context: Context, channel, decision: Dict[str, Any]):
        if not voice_stream_enabled(context, channel, decision):
            return None
        return cls(context, channel, decision)

    def handle_event(self, event: Dict[str, Any]) -> None:
        event_type = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event_type == "turn_start":
            self._reset_buffer()
            return
        if event_type == "message_update":
            delta = str(data.get("delta") or "")
            if delta:
                self.add_delta(delta)
            return
        if event_type == "message_end":
            self.flush("message_end")
            if not data.get("tool_calls"):
                self.finish()
            return
        if event_type in {"agent_end", "error", "cancelled"}:
            self.finish()

    def add_delta(self, delta: str) -> None:
        with self._lock:
            self._text_parts.append(delta)
            self._buffer += delta
            self._flush_ready_segments_locked()
            self._arm_idle_timer_locked()

    def flush(self, reason: str = "manual") -> None:
        with self._lock:
            self._cancel_idle_timer_locked()
            text = self._buffer.strip()
            self._buffer = ""
        if text:
            self._enqueue_segment(text, reason)

    def finish(self, timeout: float = 30.0) -> None:
        self.flush("finish")
        self._stop.set()
        try:
            self._queue.put(None, timeout=0.2)
        except queue.Full:
            pass
        self._done.wait(timeout=max(0.1, timeout))

    def full_text(self) -> str:
        return "".join(self._text_parts)

    def _reset_buffer(self) -> None:
        with self._lock:
            self._cancel_idle_timer_locked()
            self._buffer = ""

    def _flush_ready_segments_locked(self) -> None:
        while self._buffer:
            split_at = self._sentence_split_index(self._buffer)
            reason = "sentence"
            if split_at <= 0 and len(self._buffer) >= self.max_chars:
                split_at = self._max_char_split_index(self._buffer)
                reason = "max_chars"
            if split_at <= 0:
                return
            segment = self._buffer[:split_at].strip()
            self._buffer = self._buffer[split_at:].lstrip()
            if segment:
                self._enqueue_segment(segment, reason)

    def _sentence_split_index(self, text: str) -> int:
        for match in _SENTENCE_END_RE.finditer(text[: self.max_chars]):
            end = match.end()
            if end >= self.min_chars:
                return end
        return 0

    def _max_char_split_index(self, text: str) -> int:
        if len(text) <= self.max_chars:
            return len(text)
        window = text[: self.max_chars]
        match = None
        for candidate in _SENTENCE_END_RE.finditer(window):
            match = candidate
        return match.end() if match and match.end() >= self.min_chars else self.max_chars

    def _arm_idle_timer_locked(self) -> None:
        self._cancel_idle_timer_locked()
        if not self._buffer.strip():
            return
        timer = threading.Timer(self.idle_seconds, self._flush_idle)
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def _cancel_idle_timer_locked(self) -> None:
        timer = self._idle_timer
        self._idle_timer = None
        if timer:
            timer.cancel()

    def _flush_idle(self) -> None:
        with self._lock:
            if len(self._buffer.strip()) < self.min_chars:
                self._arm_idle_timer_locked()
                return
        self.flush("idle")

    def _enqueue_segment(self, text: str, reason: str) -> None:
        clean = _clean_tts_text(text)
        if not clean:
            return
        try:
            self._queue.put_nowait({"text": clean, "reason": reason})
        except queue.Full:
            logger.warning("[VoiceStreamer] TTS queue full, falling back to text segment")
            self._fallback_segment(clean)

    def _run_worker(self) -> None:
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.2)
                except queue.Empty:
                    if self._stop.is_set():
                        return
                    continue
                if item is None:
                    return
                text = str(item.get("text") or "")
                if not text:
                    continue
                self._speak_segment(text)
        finally:
            self._done.set()

    def _speak_segment(self, text: str) -> None:
        audio_path = ""
        try:
            audio_path = generate_xai_tts(text)
            sent = self._send_voice_segment(audio_path)
            if sent is not False:
                self._voice_segments_sent += 1
                self.context["voice_stream_sent"] = True
                self._flush_pending_text_fallbacks()
                return
            logger.warning("[VoiceStreamer] voice send returned false, falling back to text")
        except XaiTtsError as exc:
            logger.warning("[VoiceStreamer] TTS failed, falling back to text: %s", exc)
        except Exception as exc:
            logger.warning("[VoiceStreamer] voice segment failed, falling back to text: %s", exc)
        self._fallback_segment(text)

    def _fallback_segment(self, text: str) -> None:
        if self._voice_segments_sent <= 0:
            self._pending_text_fallbacks.append(text)
            return
        self._send_text_fallback(text)

    def _flush_pending_text_fallbacks(self) -> None:
        pending = list(self._pending_text_fallbacks)
        self._pending_text_fallbacks = []
        for text in pending:
            self._send_text_fallback(text)

    def _send_voice_segment(self, audio_path: str) -> bool:
        channel_type = str(getattr(self.channel, "channel_type", "") or self.channel_type)
        if channel_type == "wecom_bot" and hasattr(self.channel, "_send_voice"):
            return bool(self.channel._send_voice(
                audio_path,
                self.context.get("receiver", ""),
                bool(self.context.get("isgroup", False)),
                req_id=None,
            ))
        reply = Reply(ReplyType.VOICE, audio_path)
        if hasattr(self.channel, "_send"):
            return bool(self.channel._send(reply, self.context))
        return bool(self.channel.send(reply, self.context))

    def _send_text_fallback(self, text: str) -> bool:
        channel_type = str(getattr(self.channel, "channel_type", "") or self.channel_type)
        if channel_type == "wecom_bot" and hasattr(self.channel, "_send_text"):
            mention_user_ids, mention_display_names = self.channel._reply_mention_target(self.context)
            return bool(self.channel._send_text(
                text,
                self.context.get("receiver", ""),
                bool(self.context.get("isgroup", False)),
                req_id=None,
                mention_user_ids=mention_user_ids,
                mention_display_names=mention_display_names,
            ))
        reply = Reply(ReplyType.TEXT, text)
        if hasattr(self.channel, "_decorate_reply"):
            reply = self.channel._decorate_reply(self.context, reply)
        if hasattr(self.channel, "_send"):
            return bool(self.channel._send(reply, self.context))
        return bool(self.channel.send(reply, self.context))


def voice_stream_enabled(context: Context, channel, decision: Dict[str, Any]) -> bool:
    if not context or not channel or not isinstance(decision, dict):
        return False
    if not bool(conf().get("grok_voice_streaming_enabled", True)):
        return False
    if not bool(conf().get("grok_voice_mode_enabled", False)):
        return False
    if context.get("input_is_voice") is not True:
        return False
    selected_effort = str(decision.get("selected_effort") or "").strip().lower()
    source = str(decision.get("source") or decision.get("decision_source") or "").strip().lower()
    local_rule = str(decision.get("local_rule") or "").strip()
    if selected_effort != "low" or source != "local" or not local_rule.startswith("low_"):
        return False
    channel_type = str(context.get("channel_type") or getattr(channel, "channel_type", "") or "").strip()
    allowed_channels = _configured_channels(conf().get("grok_voice_reply_channels", ["wechatcom_app", "wecom_bot"]))
    return channel_type in allowed_channels


def _configured_channels(value) -> set:
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _clean_tts_text(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"```[\s\S]*?```", " ", value)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"https?://\S+", "", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"[*_#>]+", "", value)
    return re.sub(r"\s+", " ", value).strip()
