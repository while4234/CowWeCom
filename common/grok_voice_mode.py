# encoding:utf-8

"""Decision policy for controlled Grok/xAI voice replies."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from common.llm_backend_router import get_current_backend, normalize_backend
from config import conf


VOICE_MODE_CONVERSATION = "conversation"
VOICE_MODE_LOW_GATED = "low_gated"
VOICE_MODE_DISABLED = "disabled"

VOICE_SOURCE_CONVERSATION = "conversation_mode"
VOICE_SOURCE_LOCAL_LOW = "local_low"
VOICE_SOURCE_DISABLED = "disabled"
VOICE_SOURCE_DEFAULT = "default"

DEFAULT_VOICE_CHANNELS = ["wechatcom_app", "wecom_bot"]
DEFAULT_MAX_OUTPUT_TOKENS = 220
SHORT_ANSWER_PROMPT = (
    "你正在语音会话中。请用简短、口语化、可直接听懂的方式回答。"
    "复杂问题先给结论和下一步建议，不要展开长篇分析。"
    "最多回答 2 到 3 句，总字数尽量控制在 120 个中文字符以内。"
)


@dataclass
class VoiceModeDecision:
    enabled: bool
    mode: str
    input_is_voice: bool
    channel: str
    force_voice_reply: bool
    selected_effort: Optional[str]
    source: str
    local_rule: Optional[str]
    selected_backend: Optional[str]
    selected_model: Optional[str]
    max_output_tokens: Optional[int]
    reason: str

    def to_event_payload(self) -> Dict[str, Any]:
        return asdict(self)


def resolve_grok_voice_mode_decision(
    model_adapter: Any,
    reasoning_decision: Any = None,
) -> VoiceModeDecision:
    channel = str(getattr(model_adapter, "channel_type", "") or "").strip()
    input_is_voice = bool(getattr(model_adapter, "input_is_voice", False))
    selected_effort = _safe_text(getattr(reasoning_decision, "selected_effort", None)) or None
    decision_source = _safe_text(getattr(reasoning_decision, "decision_source", None))
    local_rule = _safe_text(getattr(reasoning_decision, "local_rule", None)) or None

    if not _voice_reply_enabled():
        return _disabled(input_is_voice, channel, selected_effort, local_rule, "grok_voice_reply_disabled")

    if not input_is_voice:
        return _disabled(False, channel, selected_effort, local_rule, "input_is_not_voice")

    if channel not in _configured_channels(conf().get("grok_voice_reply_channels", DEFAULT_VOICE_CHANNELS)):
        return _disabled(input_is_voice, channel, selected_effort, local_rule, "channel_not_allowed")

    if _config_bool(conf().get("grok_voice_conversation_mode_enabled"), False):
        if not _config_bool(conf().get("grok_voice_force_voice_for_voice_input_in_conversation_mode"), True):
            return _disabled(input_is_voice, channel, selected_effort, local_rule, "conversation_force_voice_disabled")
        return VoiceModeDecision(
            enabled=True,
            mode=VOICE_MODE_CONVERSATION,
            input_is_voice=True,
            channel=channel,
            force_voice_reply=True,
            selected_effort=_forced_effort(),
            source=VOICE_SOURCE_CONVERSATION,
            local_rule=local_rule,
            selected_backend=_selected_backend(),
            selected_model=_selected_model(),
            max_output_tokens=_max_output_tokens(),
            reason="conversation_voice_input_allowed_channel",
        )

    if selected_effort == "low" and decision_source == "local" and str(local_rule or "").startswith("low_"):
        return VoiceModeDecision(
            enabled=True,
            mode=VOICE_MODE_LOW_GATED,
            input_is_voice=True,
            channel=channel,
            force_voice_reply=True,
            selected_effort=_forced_effort(),
            source=VOICE_SOURCE_LOCAL_LOW,
            local_rule=local_rule,
            selected_backend=_selected_backend(),
            selected_model=_selected_model(),
            max_output_tokens=_max_output_tokens(),
            reason="local_low_voice_rule",
        )

    return _disabled(
        input_is_voice,
        channel,
        selected_effort,
        local_rule,
        "not_local_low_voice_rule",
    )


def append_voice_short_answer_prompt(system_prompt: str, decision: Optional[VoiceModeDecision]) -> str:
    if not decision or not decision.enabled or decision.mode != VOICE_MODE_CONVERSATION:
        return system_prompt
    if not _config_bool(conf().get("grok_voice_short_answer_prompt_enabled"), True):
        return system_prompt
    current = str(system_prompt or "").strip()
    return f"{current}\n\n{SHORT_ANSWER_PROMPT}".strip() if current else SHORT_ANSWER_PROMPT


def is_grok_text_to_voice_provider(value: Any = None) -> bool:
    provider = str(conf().get("text_to_voice") if value is None else value or "").strip().lower()
    return provider in {"xai", "grok"}


def _disabled(
    input_is_voice: bool,
    channel: str,
    selected_effort: Optional[str],
    local_rule: Optional[str],
    reason: str,
) -> VoiceModeDecision:
    return VoiceModeDecision(
        enabled=False,
        mode=VOICE_MODE_DISABLED,
        input_is_voice=bool(input_is_voice),
        channel=channel,
        force_voice_reply=False,
        selected_effort=selected_effort,
        source=VOICE_SOURCE_DISABLED,
        local_rule=local_rule,
        selected_backend=None,
        selected_model=None,
        max_output_tokens=None,
        reason=reason,
    )


def _voice_reply_enabled() -> bool:
    configured = conf().get("grok_voice_reply_enabled")
    if configured is not None:
        return _config_bool(configured, False)
    return _config_bool(conf().get("grok_voice_mode_enabled"), False)


def _selected_backend() -> Optional[str]:
    configured_backend = str(conf().get("grok_voice_low_latency_backend") or "").strip()
    if not configured_backend:
        return None
    current_backend = get_current_backend()
    normalized = normalize_backend(configured_backend)
    return normalized if normalized == current_backend else None


def _selected_model() -> Optional[str]:
    current_backend = get_current_backend()
    configured_backend = str(conf().get("grok_voice_low_latency_backend") or "").strip()
    if configured_backend and normalize_backend(configured_backend) != current_backend:
        return None
    raw = str(conf().get("grok_voice_low_latency_model") or "").strip()
    return raw or None


def _forced_effort() -> str:
    effort = str(conf().get("grok_voice_force_reasoning_effort") or "low").strip().lower()
    if effort == "minimal":
        effort = "low"
    return effort if effort in {"low", "medium", "xhigh"} else "low"


def _max_output_tokens() -> int:
    return _bounded_int(conf().get("grok_voice_max_output_tokens"), DEFAULT_MAX_OUTPUT_TOKENS, 16, 2000)


def _configured_channels(value: Any) -> set:
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    return set(DEFAULT_VOICE_CHANNELS)


def _config_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _safe_text(value: Any) -> str:
    return str(value or "").strip().lower()
