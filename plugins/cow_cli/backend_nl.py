# encoding:utf-8

"""High-confidence natural-language aliases for LLM backend commands."""

from __future__ import annotations

import re
from typing import Optional, Tuple


BackendCommand = Tuple[str, str]


_PUNCTUATION_RE = re.compile(r"[\s,，。.!！?？:：;；\"'`“”‘’（）()\[\]【】<>《》]+")
_CAPI_RE = re.compile(r"\bcapi\b", re.IGNORECASE)
_CODEX_RE = re.compile(r"\bcodex\b", re.IGNORECASE)

_REQUEST_MARKERS = (
    "帮我",
    "请",
    "麻烦",
    "替我",
    "给我",
    "现在",
    "直接",
    "立即",
    "马上",
)

_EXPLAIN_OR_QUESTION_MARKERS = (
    "怎么",
    "如何",
    "怎样",
    "为什么",
    "为啥",
    "什么区别",
    "区别",
    "对比",
    "是否",
    "有啥",
    "好处",
    "优势",
    "劣势",
    "风险",
    "可行",
    "介绍",
    "解释",
    "说明",
    "教程",
    "入口",
    "方法",
    "原理",
)

_QUESTION_SUFFIXES = ("吗", "嘛", "么")

_NEGATIVE_MARKERS = (
    "不要",
    "别",
    "先别",
    "无需",
    "不用",
    "不是要",
    "不需要",
)

_STRONG_SWITCH_MARKERS = (
    "切换",
    "切到",
    "切成",
    "切回",
    "换到",
    "换成",
    "换回",
    "改成",
    "改为",
    "改用",
    "设置为",
    "设为",
    "启用",
    "转到",
)

_WEAK_SWITCH_MARKERS = ("使用", "用", "走")

_BACKEND_USAGE_CONTEXT = (
    "后端",
    "模型",
    "回复",
    "回答",
    "请求",
    "路由",
    "backend",
    "model",
    "reply",
    "route",
)

_MONTHLY_CARD_MARKERS = (
    "月卡",
    "月度",
    "包月",
    "capi_monthly",
    "capimonthly",
    "capimonth",
)

_QUOTA_MARKERS = (
    "额度",
    "余额",
    "剩余",
    "剩下",
    "用量",
    "消耗",
    "套餐",
    "到期",
    "quota",
    "usage",
    "balance",
    "remaining",
    "credit",
)

_QUOTA_REQUEST_MARKERS = (
    "查",
    "查询",
    "查看",
    "看下",
    "看一下",
    "统计",
    "多少",
    "还有",
    "剩",
    "show",
    "check",
    "current",
)

_QUOTA_CARD_MARKERS = (
    "额度卡",
    "总额度",
    "总量",
    "quotacard",
    "totalquota",
    "totalcard",
)

_CODEX_QUOTA_MARKERS = ("gpt", "chatgpt", "openai")

_CURRENT_BACKEND_QUOTA_MARKERS = (
    "\u5f53\u524d\u540e\u7aef",
    "\u73b0\u5728\u540e\u7aef",
    "\u76ee\u524d\u540e\u7aef",
    "currentbackend",
    "activebackend",
)

_CURRENT_MARKERS = (
    "\u5f53\u524d",
    "\u73b0\u5728",
    "\u76ee\u524d",
    "current",
    "active",
)

_BACKEND_COMPACT_MARKERS = (
    "\u540e\u7aef",
    "backend",
)

_LOCALIZED_QUOTA_MARKERS = (
    "\u989d\u5ea6",
    "\u4f59\u989d",
    "\u5269\u4f59",
    "\u5269\u4e0b",
    "\u4f7f\u7528\u91cf",
    "\u7528\u91cf",
    "\u6d88\u8017",
    "\u5230\u671f",
)

_SENSITIVE_WORD_RE = re.compile(r"\b(api[_ -]?key|key|token|secret)\b", re.IGNORECASE)
_SENSITIVE_COMPACT_MARKERS = (
    "apikey",
    "api_key",
    "openaiapikey",
    "openai_api_key",
    "capiapikey",
    "capikey",
    "密钥",
    "秘钥",
    "令牌",
    "口令",
    "卡密",
    "激活码",
)

_SENSITIVE_REQUEST_MARKERS = (
    "查看",
    "显示",
    "告诉",
    "给我",
    "发我",
    "导出",
    "我的",
    "当前",
    "现在",
    "配置",
    "配置的",
    "用的",
    "是什么",
    "是多少",
    "show",
    "print",
    "display",
    "what is",
    "what's",
    "current",
    "configured",
    "my ",
)

_STATUS_MARKERS = (
    "状态",
    "当前",
    "现在",
    "目前",
    "正在用",
    "用的是",
    "用的什么",
    "是什么",
    "查询",
    "查看",
)


def parse_backend_natural_command(content: str) -> Optional[BackendCommand]:
    """Return a cow_cli backend command for unambiguous natural-language input.

    The parser intentionally favors precision over recall. It only maps explicit
    backend switch/status requests and lets informational questions continue to
    the Agent.
    """

    text = str(content or "").strip()
    if not text:
        return None

    normalized = _normalize(text)
    compact = _compact(text)

    quota_command = _quota_backend_command(normalized, compact)
    if quota_command:
        return "backend", quota_command

    if _looks_like_sensitive_secret_request(normalized, compact):
        return "backend", "credential-safety"

    if _looks_like_auto_reset(normalized, compact):
        return "backend", "auto reset"

    target = _target_backend(normalized, compact)
    if _looks_like_status_request(normalized, compact, target):
        return "backend", "status"

    if target and _looks_like_switch_request(normalized, compact):
        return "backend", target

    return None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _compact(text: str) -> str:
    return _PUNCTUATION_RE.sub("", text.strip().lower())


def _target_backend(normalized: str, compact: str) -> Optional[str]:
    if _looks_like_capi_monthly(normalized, compact):
        return "capi_monthly"
    if _CAPI_RE.search(normalized) or "capi" in compact:
        return "capi"
    if _CODEX_RE.search(normalized) or "codex" in compact:
        return "codex"
    return None


def _looks_like_capi_monthly(normalized: str, compact: str) -> bool:
    if "capi-monthly" in normalized or "capi monthly" in normalized:
        return True
    if any(marker in compact for marker in _MONTHLY_CARD_MARKERS):
        return True
    capi_requested = _CAPI_RE.search(normalized) or "capi" in compact
    if not capi_requested:
        return False
    return "monthly" in normalized or "month card" in normalized


def _quota_backend_command(normalized: str, compact: str) -> Optional[str]:
    has_quota_context = any(
        marker in compact or marker in normalized
        for marker in (*_QUOTA_MARKERS, *_LOCALIZED_QUOTA_MARKERS)
    )
    if not has_quota_context:
        return None

    capi_requested = _CAPI_RE.search(normalized) or "capi" in compact
    codex_requested = (
        _CODEX_RE.search(normalized)
        or "codex" in compact
        or any(marker in compact or marker in normalized for marker in _CODEX_QUOTA_MARKERS)
    )
    monthly_requested = _looks_like_capi_monthly(normalized, compact)
    current_backend_requested = _looks_like_current_backend_quota(normalized, compact)
    quota_card_requested = any(marker in compact or marker in normalized for marker in _QUOTA_CARD_MARKERS)
    if not (capi_requested or codex_requested or monthly_requested or current_backend_requested or quota_card_requested):
        return None

    has_quota_request = any(marker in compact or marker in normalized for marker in _QUOTA_REQUEST_MARKERS)
    if _looks_like_informational_request(normalized, compact) and not has_quota_request:
        return None

    if monthly_requested:
        return "quota-capi-monthly"
    if capi_requested or quota_card_requested:
        return "quota-capi"
    if codex_requested:
        return "quota"
    if current_backend_requested:
        return "quota-current"
    return None


def _looks_like_current_backend_quota(normalized: str, compact: str) -> bool:
    if any(marker in compact for marker in _CURRENT_BACKEND_QUOTA_MARKERS):
        return True
    has_backend_context = any(marker in compact or marker in normalized for marker in _BACKEND_COMPACT_MARKERS)
    has_current_context = any(marker in compact or marker in normalized for marker in _CURRENT_MARKERS)
    return bool(has_backend_context and has_current_context)


def _looks_like_sensitive_secret_request(normalized: str, compact: str) -> bool:
    has_sensitive_word = (
        _SENSITIVE_WORD_RE.search(normalized)
        or any(marker in compact or marker in normalized for marker in _SENSITIVE_COMPACT_MARKERS)
    )
    if not has_sensitive_word:
        return False

    has_sensitive_action = any(
        marker in compact or marker in normalized
        for marker in _SENSITIVE_REQUEST_MARKERS
    )
    if has_sensitive_action:
        return True

    has_backend_context = (
        _CAPI_RE.search(normalized)
        or _CODEX_RE.search(normalized)
        or "openai" in compact
        or "backend" in normalized
        or "后端" in compact
    )
    return bool(has_backend_context and _looks_like_question(normalized, compact))


def _has_request_marker(compact: str) -> bool:
    return any(marker in compact for marker in _REQUEST_MARKERS)


def _has_negative_marker(compact: str) -> bool:
    return any(marker in compact for marker in _NEGATIVE_MARKERS)


def _looks_like_question(normalized: str, compact: str) -> bool:
    if "?" in normalized or "？" in normalized:
        return True
    if compact.startswith(("怎么", "如何", "怎样", "为什么", "为啥")):
        return True
    return compact.endswith(_QUESTION_SUFFIXES)


def _looks_like_informational_request(normalized: str, compact: str) -> bool:
    if _looks_like_question(normalized, compact) and not _has_request_marker(compact):
        return True
    return any(marker in compact for marker in _EXPLAIN_OR_QUESTION_MARKERS) and not _has_request_marker(compact)


def _looks_like_status_request(normalized: str, compact: str, target: Optional[str]) -> bool:
    has_backend_context = "后端" in compact or "backend" in normalized
    if has_backend_context and any(marker in compact for marker in _STATUS_MARKERS):
        return True
    if "backend status" in normalized or "backend show" in normalized:
        return True
    if target and any(marker in compact for marker in ("当前", "现在", "目前")) and _looks_like_question(normalized, compact):
        return True
    return False


def _looks_like_auto_reset(normalized: str, compact: str) -> bool:
    if _looks_like_informational_request(normalized, compact):
        return False
    has_backend_context = "后端" in compact or "backend" in normalized
    has_auto_context = "自动" in compact or "auto" in normalized
    has_reset_action = "重置" in compact or "恢复" in compact or "reset" in normalized
    return has_backend_context and has_auto_context and has_reset_action


def _looks_like_switch_request(normalized: str, compact: str) -> bool:
    if _has_negative_marker(compact):
        return False
    if _looks_like_informational_request(normalized, compact):
        return False
    if any(marker in compact for marker in _STRONG_SWITCH_MARKERS):
        return True
    if any(marker in compact for marker in _WEAK_SWITCH_MARKERS):
        return any(marker in compact or marker in normalized for marker in _BACKEND_USAGE_CONTEXT)
    return bool(re.search(r"\b(switch|change|set|use|route)\b", normalized))
