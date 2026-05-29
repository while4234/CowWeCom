# encoding:utf-8

"""Shared routing helpers for image generation provider selection."""

from __future__ import annotations

import re
from typing import Any


GPT_IMAGE_RUNTIME = "codex_auth"
GROK_IMAGE_RUNTIME = "grok"

_GPT_PROVIDER = r"(?<![a-z0-9])(?:gpt|chatgpt|openai|codex)(?![a-z0-9])"
_GPT_PROVIDER_RE = re.compile(_GPT_PROVIDER, re.IGNORECASE)
_GPT_IMAGE_RE = re.compile(
    r"(?:" + _GPT_PROVIDER + r".{0,12}"
    r"(?:生图|生成(?:图片|图像|图)?|画图|绘图|作图|image|picture|photo|draw|generate|create))"
    r"|(?:(?:生图|生成(?:图片|图像|图)?|画图|绘图|作图|image|picture|photo|draw|generate|create)"
    r".{0,12}" + _GPT_PROVIDER + r")",
    re.IGNORECASE,
)


def explicit_gpt_image_requested(prompt: Any) -> bool:
    """Return True only when the prompt explicitly asks GPT/OpenAI/Codex to make the image."""
    text = " ".join(str(prompt or "").strip().split())
    if not text or not _GPT_PROVIDER_RE.search(text):
        return False
    return bool(_GPT_IMAGE_RE.search(text))


def default_image_runtime_for_profile(
    prompt: Any,
    profile: Any,
    *,
    configured_runtime: str = GPT_IMAGE_RUNTIME,
) -> str:
    if explicit_gpt_image_requested(prompt):
        return GPT_IMAGE_RUNTIME

    try:
        from common.llm_backend_router import BACKEND_GROK, get_current_backend_for_profile

        if get_current_backend_for_profile(profile) == BACKEND_GROK:
            return GROK_IMAGE_RUNTIME
    except Exception:
        pass

    return configured_runtime or GPT_IMAGE_RUNTIME
