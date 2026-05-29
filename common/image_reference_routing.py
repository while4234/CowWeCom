# encoding:utf-8

"""Shared helpers for routing image references into image generation."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse


DEFAULT_IMAGE_CREATE_AUTO_REF_WINDOW_SECONDS = 180
IMAGE_REF_RE = re.compile(r"\[\s*(?:\u56fe\u7247|image)\s*:\s*([^\]]+?)\s*\]", re.IGNORECASE)

_TEXT_TO_IMAGE_ONLY_HINTS = (
    "\u7eaf\u6587\u751f\u56fe",
    "\u6587\u751f\u56fe",
    "\u6587\u5b57\u751f\u56fe",
    "\u6587\u672c\u751f\u56fe",
    "\u4e0d\u53c2\u8003\u56fe",
    "\u4e0d\u53c2\u8003\u56fe\u7247",
    "\u4e0d\u4f7f\u7528\u56fe",
    "\u4e0d\u4f7f\u7528\u56fe\u7247",
    "\u4e0d\u7528\u56fe",
    "\u4e0d\u7528\u56fe\u7247",
    "\u4e0d\u8981\u53c2\u8003\u4e0a\u56fe",
    "\u4e0d\u8981\u53c2\u8003\u56fe",
    "\u4e0d\u8981\u53c2\u8003\u56fe\u7247",
    "\u65e0\u53c2\u8003\u56fe",
    "\u65e0\u53c2\u8003\u56fe\u7247",
    "texttoimage",
    "txt2img",
    "noimage",
    "withoutimage",
    "withoutreference",
)

_STRONG_IMAGE_EDIT_HINTS = (
    "\u56fe\u751f\u56fe",
    "\u4ee5\u56fe\u751f\u56fe",
    "\u4fee\u56fe",
    "\u6539\u56fe",
    "\u7f16\u8f91\u56fe",
    "\u6362\u80cc\u666f",
    "\u53bb\u80cc\u666f",
    "\u62a0\u56fe",
    "\u878d\u5408",
    "\u5408\u6210",
    "imagetoimage",
    "image2image",
    "img2img",
    "editthisimage",
    "editimage",
    "modifyimage",
    "retouchimage",
)

_REFERENCE_TERMS = (
    "\u53c2\u8003\u4e0a\u56fe",
    "\u53c2\u8003\u4e0a\u9762",
    "\u53c2\u8003\u8fd9\u5f20\u56fe",
    "\u53c2\u8003\u8fd9\u4e2a\u56fe",
    "\u6309\u7167\u8fd9\u5f20\u56fe",
    "\u57fa\u4e8e\u4e0a\u56fe",
    "\u57fa\u4e8e\u4e0a\u9762\u7684\u56fe",
    "\u628a\u521a\u624d\u90a3\u5f20\u56fe",
    "\u521a\u624d\u90a3\u5f20\u56fe",
    "\u4e0a\u56fe",
    "\u4e0a\u9762\u7684\u56fe",
    "\u8fd9\u5f20\u56fe",
)

_ACTION_TERMS = (
    "\u751f\u6210",
    "\u751f\u56fe",
    "\u51fa\u56fe",
    "\u753b",
    "\u7ed8",
    "\u6539",
    "\u4fee",
    "\u6362",
    "\u53bb",
    "\u62a0",
    "\u878d\u5408",
    "\u5408\u6210",
    "\u8f6c",
    "\u53d8",
    "\u505a\u6210",
)

_TRANSFORM_WITHOUT_REF_TERMS = (
    "\u6362\u6210",
    "\u6539\u6210",
    "\u53d8\u6210",
    "\u505a\u6210",
    "\u8f6c\u6210",
    "\u6362\u80cc\u666f",
    "\u53bb\u80cc\u666f",
    "\u62a0\u56fe",
)

_EN_REFERENCE_RE = re.compile(
    r"\b(?:this|that|previous|above|uploaded|reference)\s+"
    r"(?:image|picture|photo|img)\b|\b(?:reference image|image reference)\b",
    re.IGNORECASE,
)
_EN_ACTION_RE = re.compile(
    r"\b(?:generate|create|make|edit|modify|change|replace|turn|convert|retouch|remove)\b",
    re.IGNORECASE,
)
_EN_TRANSFORM_RE = re.compile(
    r"\b(?:change|turn|convert|make|edit|modify|replace)\s+"
    r"(?:it|this|that|the\s+(?:image|picture|photo))\s+"
    r"(?:into|to|as)\b",
    re.IGNORECASE,
)


def extract_image_references(text: Any) -> list[str]:
    """Return image references from ``[图片: ...]`` or ``[image: ...]`` markers."""
    refs: list[str] = []
    for match in IMAGE_REF_RE.finditer(str(text or "")):
        value = match.group(1).strip()
        if value and value not in refs:
            refs.append(value)
    return refs


def strip_image_references(text: Any) -> str:
    return IMAGE_REF_RE.sub("", str(text or "")).strip()


def has_image_reference(text: Any) -> bool:
    return bool(IMAGE_REF_RE.search(str(text or "")))


def is_text_to_image_only_request(text: Any) -> bool:
    compact = _compact_text(text)
    if not compact:
        return False
    return any(hint in compact for hint in _TEXT_TO_IMAGE_ONLY_HINTS)


def is_image_to_image_reference_request(
    text: Any,
    *,
    allow_transform_without_image_word: bool = False,
) -> bool:
    """Return True only for image-generation prompts that intend to use an image."""
    if is_text_to_image_only_request(text):
        return False

    raw_text = str(text or "")
    compact = _compact_text(raw_text)
    if not compact:
        return False

    if any(hint in compact for hint in _STRONG_IMAGE_EDIT_HINTS):
        return True

    has_reference = any(term in compact for term in _REFERENCE_TERMS) or bool(_EN_REFERENCE_RE.search(raw_text))
    has_action = any(term in compact for term in _ACTION_TERMS) or bool(_EN_ACTION_RE.search(raw_text))
    if has_reference and has_action:
        return True

    if allow_transform_without_image_word:
        if any(term in compact for term in _TRANSFORM_WITHOUT_REF_TERMS):
            return True
        if _EN_TRANSFORM_RE.search(raw_text):
            return True

    return False


def image_create_auto_ref_window_seconds(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = float(DEFAULT_IMAGE_CREATE_AUTO_REF_WINDOW_SECONDS)
    if seconds <= 0:
        return float(DEFAULT_IMAGE_CREATE_AUTO_REF_WINDOW_SECONDS)
    return seconds


def safe_image_ref_label(ref: Any) -> str:
    """Return a non-sensitive label for logs."""
    value = str(ref or "").strip()
    if not value:
        return ""
    if value.lower().startswith(("http://", "https://")):
        parsed = urlparse(value)
        return os.path.basename(parsed.path) or parsed.netloc or "<url>"
    return os.path.basename(value.replace("\\", "/")) or "<image>"


def _compact_text(text: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(text or "").lower())
