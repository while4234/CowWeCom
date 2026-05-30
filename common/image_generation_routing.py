# encoding:utf-8

"""Shared routing helpers for image generation provider selection."""

from __future__ import annotations

import re
from typing import Any

from common.image_reference_routing import (
    is_image_to_image_reference_request,
    is_text_to_image_only_request,
)


GPT_IMAGE_RUNTIME = "codex_auth"
GROK_IMAGE_RUNTIME = "grok"

_AMBIGUOUS_LEGACY_PREFIXES = {"看", "找"}
_BUILTIN_IMAGE_CREATE_PREFIXES = (
    "生成一张图片",
    "生成一张图",
    "生成一个图片",
    "生成一个图",
    "生成图片",
    "生成图像",
    "生成照片",
    "生成海报",
    "生成头像",
    "生成插画",
    "生成封面",
    "生成壁纸",
    "生成图",
    "帮我生成图片",
    "帮我生成一张图",
    "帮我生图",
    "生图",
    "出图",
    "画图",
    "绘图",
    "作图",
    "制图",
    "做图",
    "做一张图",
    "做个图",
    "画一张",
    "画一个",
    "画个",
    "画只",
    "画张",
    "画幅",
    "画成",
    "画一下",
    "画",
    "draw an image",
    "draw a picture",
    "draw a photo",
    "generate an image",
    "generate a picture",
    "generate a photo",
    "create an image",
    "create a picture",
    "create a photo",
    "make an image",
    "make a picture",
)
_NON_CREATE_DRAW_PREFIXES = ("画面", "画质", "画风", "画家", "画法", "画布", "画册", "画报", "画师")
_IMAGE_GENERATION_CN_RE = re.compile(
    r"(?:"
    r"生图|出图|画图|绘图|作图|制图|做(?:一张|一个|个)?图|"
    r"生成(?:一张|一个|个)?(?:图片|图像|图|照片|海报|头像|插画|封面|壁纸)|"
    r"随机生成.{0,40}(?:图片|图像|照片|图)|"
    r"(?:^|[\s，。！？:：,、])画(?:[\s:：,，]|一张|一个|个|只|张|幅|成|一下|点|些|出)|"
    r"图生图|以图生图|修图|改图|编辑(?:这张|这幅|这个)?图|换背景|去背景|抠图|"
    r"参考(?:这张|上面|刚才|引用).{0,20}(?:生成|生图|出图|画图|绘图|做图|改图)"
    r")",
    re.IGNORECASE,
)
_IMAGE_GENERATION_EN_RE = re.compile(
    r"(?:"
    r"\b(?:draw|generate|create|make|imagine)\b.{0,24}"
    r"\b(?:image|picture|photo|poster|avatar|illustration|wallpaper)\b|"
    r"\b(?:image|picture|photo|poster|avatar|illustration|wallpaper)\b.{0,24}"
    r"\b(?:draw|generate|create|make|edit|modify|replace)\b|"
    r"\b(?:edit|modify|replace|retouch)\b.{0,24}\b(?:image|picture|photo)\b"
    r")",
    re.IGNORECASE,
)
_BUILTIN_VIDEO_CREATE_PREFIXES = (
    "生成视频",
    "生成一个视频",
    "生成一段视频",
    "视频生成",
    "做视频",
    "做一个视频",
    "做一段视频",
    "图生视频",
    "把图动起来",
    "让图动起来",
    "让这张图动起来",
    "animate this image",
    "image to video",
    "generate a video",
    "create a video",
    "make a video",
)
_VIDEO_GENERATION_CN_RE = re.compile(
    r"(?:"
    r"生成(?:一个|一段|个|段)?视频|视频生成|(?:做|制作|创建|画)(?:一个|一段|个|段)?视频|"
    r"图生视频|以图生视频|把(?:这张)?图动起来|让(?:这张)?图动起来|"
    r"(?:^|[\s，。！？:：,、])视频(?:生成|制作|创作)"
    r")",
    re.IGNORECASE,
)
_VIDEO_GENERATION_EN_RE = re.compile(
    r"(?:"
    r"\b(?:draw|generate|create|make|produce)\b.{0,24}\b(?:video|clip|short)\b|"
    r"\b(?:video|clip|short)\b.{0,24}\b(?:generate|create|make|produce)\b|"
    r"\b(?:image to video|animate this image|animate the image)\b"
    r")",
    re.IGNORECASE,
)
_MEDIA_GENERATION_STATUS_RE = re.compile(
    r"(?:刚才|刚刚|之前|失败|成功|审核|看不到|没看到|收不到|没有收到|为什么|原因|问题|报错|status|failed|failure|success|error|why)",
    re.IGNORECASE,
)

_GPT_PROVIDER = r"(?<![a-z0-9])(?:gpt|chatgpt|openai|codex)(?![a-z0-9])"
_GPT_PROVIDER_RE = re.compile(_GPT_PROVIDER, re.IGNORECASE)
_GPT_IMAGE_RE = re.compile(
    r"(?:" + _GPT_PROVIDER + r".{0,12}"
    r"(?:生图|生成(?:图片|图像|图)?|画图|绘图|作图|image|picture|photo|draw|generate|create))"
    r"|(?:(?:生图|生成(?:图片|图像|图)?|画图|绘图|作图|image|picture|photo|draw|generate|create)"
    r".{0,12}" + _GPT_PROVIDER + r")",
    re.IGNORECASE,
)
_RANDOM_PROMPT_TEXT_RE = re.compile(
    r"(?:随机|随便|任意|\brandom\b).{0,32}(?:提示词|\bprompt\b)|"
    r"(?:提示词|\bprompt\b).{0,32}(?:随机|随便|任意|\brandom\b)",
    re.IGNORECASE,
)


def explicit_image_generation_requested(prompt: Any) -> bool:
    """Return True only when the user explicitly asks to create or edit an image."""
    text = " ".join(str(prompt or "").strip().split())
    if not text:
        return False
    if random_image_prompt_text_requested(text):
        return False
    if is_image_to_image_reference_request(text):
        return True
    return bool(_IMAGE_GENERATION_CN_RE.search(text) or _IMAGE_GENERATION_EN_RE.search(text))


def explicit_video_generation_requested(prompt: Any) -> bool:
    """Return True only when the user explicitly asks to create a video."""
    text = " ".join(str(prompt or "").strip().split())
    if not text:
        return False
    if random_image_prompt_text_requested(text):
        return False
    return bool(_VIDEO_GENERATION_CN_RE.search(text) or _VIDEO_GENERATION_EN_RE.search(text))


def random_image_prompt_text_requested(prompt: Any) -> bool:
    """Return True when the user asks to receive prompt text instead of generating media."""
    text = " ".join(str(prompt or "").strip().split())
    if not text:
        return False
    return bool(_RANDOM_PROMPT_TEXT_RE.search(text))


def looks_like_media_generation_status_question(prompt: Any) -> bool:
    """Return True for diagnostic questions about generation, not new jobs."""
    text = " ".join(str(prompt or "").strip().split())
    if not text:
        return False
    has_media_intent_words = bool(
        _IMAGE_GENERATION_CN_RE.search(text)
        or _IMAGE_GENERATION_EN_RE.search(text)
        or _VIDEO_GENERATION_CN_RE.search(text)
        or _VIDEO_GENERATION_EN_RE.search(text)
    )
    return has_media_intent_words and bool(_MEDIA_GENERATION_STATUS_RE.search(text))


def match_image_create_prefix(content: Any, configured_prefixes: Any = None) -> str | None:
    """Match only explicit image-generation prefixes.

    Older configs often include broad prefixes such as "看" or "找"; those are
    natural image-question words, so they must not trigger generation by
    themselves.
    """
    text = str(content or "").lstrip()
    if not text:
        return None

    lowered = text.lower()
    for prefix in sorted(_BUILTIN_IMAGE_CREATE_PREFIXES, key=len, reverse=True):
        prefix_lower = prefix.lower()
        if lowered.startswith(prefix_lower) and _explicit_prefix_allowed(text, prefix):
            return text[: len(prefix)]

    for prefix in _iter_prefixes(configured_prefixes):
        if not prefix or prefix in _AMBIGUOUS_LEGACY_PREFIXES:
            continue
        if text.startswith(prefix) and _explicit_prefix_allowed(text, prefix):
            return prefix
    return None


def match_video_create_prefix(content: Any, configured_prefixes: Any = None) -> str | None:
    """Match explicit video-generation prefixes and avoid plain video QA."""
    text = str(content or "").lstrip()
    if not text:
        return None

    lowered = text.lower()
    for prefix in sorted(_BUILTIN_VIDEO_CREATE_PREFIXES, key=len, reverse=True):
        prefix_lower = prefix.lower()
        if lowered.startswith(prefix_lower) and explicit_video_generation_requested(text):
            return text[: len(prefix)]

    for prefix in _iter_prefixes(configured_prefixes):
        if prefix and text.startswith(prefix) and explicit_video_generation_requested(text):
            return prefix
    return None


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


def active_backend_is_grok_for_context(context: Any) -> bool:
    """Return whether this chat actor's personal backend is Grok.

    The helper resolves the actor profile lazily and fails closed so ordinary
    users never inherit Grok-only behavior from a missing context.
    """
    if context is None:
        return False
    try:
        from agent.user_profiles import apply_profile_to_context, resolve_agent_user_profile
        from common.llm_backend_router import BACKEND_GROK, get_current_backend_for_profile

        profile = None
        try:
            profile = context.get("_actor_profile")
        except Exception:
            profile = getattr(context, "_actor_profile", None)
        if profile is None:
            profile = resolve_agent_user_profile(context)
            apply_profile_to_context(context, profile)
            try:
                context["_actor_profile"] = profile
            except Exception:
                pass
        return get_current_backend_for_profile(profile) == BACKEND_GROK
    except Exception:
        return False


def _iter_prefixes(configured_prefixes: Any) -> list[str]:
    if configured_prefixes is None:
        return []
    if isinstance(configured_prefixes, str):
        return [configured_prefixes.strip()]
    try:
        return [str(prefix or "").strip() for prefix in configured_prefixes]
    except TypeError:
        return [str(configured_prefixes or "").strip()]


def _explicit_prefix_allowed(text: str, prefix: str) -> bool:
    stripped_prefix = str(prefix or "").strip()
    if not stripped_prefix:
        return False
    if stripped_prefix in _AMBIGUOUS_LEGACY_PREFIXES:
        return False
    if stripped_prefix == "画":
        return _single_draw_prefix_allowed(text)
    return explicit_image_generation_requested(stripped_prefix) or explicit_image_generation_requested(text)


def _single_draw_prefix_allowed(text: str) -> bool:
    if not text.startswith("画"):
        return False
    if any(text.startswith(prefix) for prefix in _NON_CREATE_DRAW_PREFIXES):
        return False
    if len(text) == 1:
        return False
    return True
