"""Guardrails for writes into the personal knowledge wiki."""

from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Tuple


_PRIVATE_KNOWLEDGE_PATTERNS = [
    r"bridge_[0-9a-f]{8,}",
    r"\bweixin_[A-Za-z0-9]+\b",
    r"微信桥接",
    r"桥接用户",
    r"桥接显示名",
    r"Bridge\s*ID",
    r"微信渠道",
    r"可通过微信桥接联系",
]

_EXPLICIT_SAVE_PATTERNS = (
    "保存到个人知识库",
    "保存到知识库",
    "记入知识库",
    "整理沉淀",
    "沉淀到知识库",
    "加入个人知识库",
    "写入个人知识库",
    "save to knowledge",
    "save this to knowledge",
)

_PROTOCOL_ANALYSIS_PATTERNS = (
    "ucie",
    "pcie",
    "cxl",
    "amba",
    "axi",
    "mbinit",
    "mbtrain",
    "phyretrain",
    "协议",
    "规范",
    "标准",
    "状态机",
    "寄存器",
    "encoding",
    "table",
    "field",
    "step",
    "时序",
    "映射",
    "原文",
    "证据包",
)

_PUBLIC_SOURCE_HINTS = (
    "http://",
    "https://",
    "> source:",
    "source:",
    "来源:",
    "公开资料",
    "联网搜索",
    "web_search",
    "article",
    "paper",
    "新闻",
)

_INSUFFICIENT_HINTS = (
    "证据不足",
    "当前证据还不充分",
    "insufficient",
    "missing_terms",
    "推测",
    "可能",
)

_CURRENT_CONTEXT: contextvars.ContextVar["KnowledgeWriteContext"] = contextvars.ContextVar(
    "knowledge_write_context",
    default=None,
)


@dataclass(frozen=True)
class KnowledgeWriteContext:
    user_message: str = ""
    task_kind: str = ""
    used_web_research: bool = False
    used_knowledge_query: bool = False
    evidence_status: str = ""
    tool_name: str = ""
    tool_arguments: Dict[str, Any] = field(default_factory=dict)


def set_knowledge_write_context(context: KnowledgeWriteContext):
    return _CURRENT_CONTEXT.set(context)


def reset_knowledge_write_context(token: Any) -> None:
    _CURRENT_CONTEXT.reset(token)


def validate_knowledge_write(path: str, content: str) -> Tuple[bool, str]:
    """Reject unsafe or unconfirmed writes into knowledge/."""
    normalized_path = str(path or "").replace("\\", "/").lower()
    if not _is_knowledge_path(normalized_path):
        return True, ""

    text = str(content or "")
    for pattern in _PRIVATE_KNOWLEDGE_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return (
                False,
                "Error: private user, relationship, or social-bridge details must be stored in "
                "memory/ or relation memory, not in knowledge/. The personal knowledge wiki is for "
                "reusable non-private knowledge.",
            )

    context = _CURRENT_CONTEXT.get()
    if _has_explicit_save_intent(context):
        if _is_evidence_insufficient(context, text):
            return False, _insufficient_error()
        return True, ""

    if _is_protocol_analysis_write(context, normalized_path, text):
        return (
            False,
            "Error: protocol/specification analysis answers are read-only by default. "
            "Only write them to knowledge/ after the user explicitly confirms the conclusion "
            "with wording such as '保存到个人知识库'.",
        )

    if _is_evidence_insufficient(context, text):
        return False, _insufficient_error()

    if _is_public_source_ingest(context, text):
        return True, ""

    return True, ""


def _is_knowledge_path(normalized_path: str) -> bool:
    if normalized_path.startswith("knowledge/"):
        return True
    parts = normalized_path.split("/")
    return "knowledge" in parts


def _has_explicit_save_intent(context: KnowledgeWriteContext) -> bool:
    if context is None:
        return False
    return _contains_any(context.user_message, _EXPLICIT_SAVE_PATTERNS)


def _is_protocol_analysis_write(context: KnowledgeWriteContext, path: str, content: str) -> bool:
    combined = " ".join(
        [
            path,
            content,
            getattr(context, "user_message", "") if context is not None else "",
            getattr(context, "task_kind", "") if context is not None else "",
        ]
    )
    if not _contains_any(combined, _PROTOCOL_ANALYSIS_PATTERNS):
        return False
    if context is None:
        return False
    return context.task_kind == "knowledge" or context.used_knowledge_query


def _is_evidence_insufficient(context: KnowledgeWriteContext, content: str) -> bool:
    status = str(getattr(context, "evidence_status", "") if context is not None else "").strip().lower()
    if status == "insufficient":
        return True
    return _contains_any(content, _INSUFFICIENT_HINTS)


def _is_public_source_ingest(context: KnowledgeWriteContext, content: str) -> bool:
    if context is not None and context.used_web_research:
        return True
    if context is not None and _contains_any(context.user_message, ("搜索", "联网", "公开资料", "链接", "文章", "文档")):
        return True
    return _contains_any(content, _PUBLIC_SOURCE_HINTS)


def _contains_any(value: Any, needles: tuple[str, ...]) -> bool:
    text = str(value or "").lower()
    return any(needle.lower() in text for needle in needles)


def _insufficient_error() -> str:
    return (
        "Error: evidence is insufficient or marked as tentative; do not write this answer to "
        "knowledge/. Ask the user to continue source verification first."
    )
