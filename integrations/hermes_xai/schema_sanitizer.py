# encoding:utf-8

"""xAI Responses tool-schema compatibility helpers.

Hermes strips a few schema shapes that Grok rejects while leaving the original
tool definitions untouched for non-xAI providers.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Tuple


def sanitize_xai_tools(tools: Any) -> Tuple[Any, int]:
    """Return a sanitized copy of Responses tools and a removal count."""
    if not tools:
        return tools, 0
    cleaned = copy.deepcopy(tools)
    removed = _sanitize_schema_node(cleaned)
    return cleaned, removed


def _sanitize_schema_node(value: Any) -> int:
    removed = 0
    if isinstance(value, list):
        for item in value:
            removed += _sanitize_schema_node(item)
        return removed
    if not isinstance(value, dict):
        return 0

    enum_values = value.get("enum")
    if isinstance(enum_values, list) and any(
        isinstance(item, str) and "/" in item for item in enum_values
    ):
        value.pop("enum", None)
        removed += 1

    for unsupported_key in ("$schema", "$id", "examples"):
        if unsupported_key in value:
            value.pop(unsupported_key, None)
            removed += 1

    for child in list(value.values()):
        removed += _sanitize_schema_node(child)
    return removed
