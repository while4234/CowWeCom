"""Guardrails for writes into the shared knowledge wiki."""

from __future__ import annotations

import os
import re
from typing import Tuple


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


def validate_knowledge_write(path: str, content: str) -> Tuple[bool, str]:
    """Reject obvious private bridge/user relationship records in knowledge/."""
    normalized_path = str(path or "").replace("\\", "/").lower()
    if not _is_knowledge_path(normalized_path):
        return True, ""

    text = str(content or "")
    for pattern in _PRIVATE_KNOWLEDGE_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return (
                False,
                "Error: private user, relationship, or social-bridge details must be stored in "
                "memory/ or relation memory, not in knowledge/. The shared knowledge wiki is for "
                "reusable non-private knowledge.",
            )
    return True, ""


def _is_knowledge_path(normalized_path: str) -> bool:
    if normalized_path.startswith("knowledge/"):
        return True
    parts = normalized_path.split("/")
    return "knowledge" in parts
