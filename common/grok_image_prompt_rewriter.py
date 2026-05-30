# encoding:utf-8

"""Grok-backed prompt rewriting for Grok image and video generation."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict

from common.prompt_optimization_repository import (
    GROK_IMAGE_PROMPT_OPTIMIZATION_SKILL_NAME,
    resolve_grok_system_prompt_path,
    select_grok_prompt_fragments,
    strip_control_keywords,
)

try:
    from common.log import logger
except Exception:  # pragma: no cover - standalone import fallback
    import logging

    logger = logging.getLogger(__name__)


DEFAULT_IMAGE_SYSTEM_PROMPT = """You are the Grok image prompt optimizer for CowWeCom.

Rewrite the user's request into one complete prompt for Grok image generation.
Use the random repository fragments only to fill missing visual details. Preserve
the user's subject, style, names, numbers, requested text, and constraints.
Treat NSFW/nsfw as an internal selection control keyword, not final prompt text.
When a reference image is provided, preserve the reference face/identity and do
not add new appearance, expression, or gaze descriptors unless explicitly
requested. Do not add generic quality/style booster phrases such as soft
cinematic lighting, highly detailed, realistic skin texture, sensual atmosphere,
8k, 4k, UHD, HDR, masterpiece, or best quality.
Return only the final prompt text.
"""

DEFAULT_VIDEO_SYSTEM_PROMPT = """You are the Grok video prompt optimizer for CowWeCom.

Rewrite the user's request into one complete prompt for Grok video generation.
Use the random repository fragments only to fill missing motion, camera, scene,
lighting, and continuity details. Preserve the user's subject, action, duration,
aspect ratio, reference-image intent, names, text, and constraints. Treat
NSFW/nsfw as an internal selection control keyword, not final prompt text.
When a reference image is provided, preserve the reference face/identity across
frames and do not add new appearance descriptors unless explicitly requested.
Return only the final prompt text.
"""

VERSION = "grok-model-rewrite-v2"
RANDOM_PROMPT_VERSION = "grok-random-image-prompt-v1"
_MAX_PROMPT_CHARS = 12000
_DEFAULT_TIMEOUT_SECONDS = 60
REFERENCE_IMAGE_LOCK_PREFIX = "Reference image identity lock:"
RANDOM_PROMPT_MODE_IMAGE_TO_IMAGE = "image_to_image"
RANDOM_PROMPT_MODE_TEXT_TO_IMAGE = "text_to_image"
_DISALLOWED_IMAGE_PROMPT_SEGMENTS = (
    "soft cinematic lighting",
    "cinematic lighting",
    "highly detailed",
    "ultra detailed",
    "ultra-detailed",
    "realistic skin texture",
    "realistic skin textures",
    "detailed skin texture",
    "sensual atmosphere",
    "8k",
    "8k resolution",
    "4k",
    "4k resolution",
    "uhd",
    "hdr",
    "masterpiece",
    "best quality",
)
_DISALLOWED_IMAGE_TO_IMAGE_EXPRESSION_SEGMENTS = (
    "biting her lower lip",
    "biting his lower lip",
    "biting their lower lip",
    "biting lower lip",
    "seductive gaze",
    "sultry gaze",
    "alluring gaze",
    "bedroom eyes",
    "winking",
    "wink",
    "smiling",
    "smile",
    "smirk",
    "laughing",
    "open mouth",
    "parted lips",
    "tongue out",
)
_DISALLOWED_IMAGE_TO_IMAGE_APPEARANCE_SEGMENTS = (
    "black hair",
    "blonde hair",
    "blond hair",
    "brown hair",
    "red hair",
    "white hair",
    "silver hair",
    "gray hair",
    "grey hair",
    "pink hair",
    "blue hair",
    "green hair",
    "purple hair",
    "long hair",
    "short hair",
    "curly hair",
    "straight hair",
    "wavy hair",
    "ponytail",
    "bangs",
    "blue eyes",
    "green eyes",
    "brown eyes",
    "hazel eyes",
    "red eyes",
    "golden eyes",
    "pale skin",
    "fair skin",
    "tan skin",
    "tanned skin",
    "dark skin",
    "smooth skin",
    "flawless skin",
    "slim body",
    "curvy body",
    "hourglass figure",
    "athletic body",
    "muscular body",
    "petite body",
    "sharp jawline",
    "delicate facial features",
    "full lips",
)


def rewrite_grok_image_prompt(
    prompt: str,
    *,
    model: str = "",
    runtime: str = "",
    image_url: Any = None,
    quality: str | None = None,
    size: str | None = None,
    aspect_ratio: str | None = None,
    enabled: Any = True,
) -> Dict[str, Any]:
    return _rewrite_grok_media_prompt(
        prompt,
        media_type="image",
        model=model,
        runtime=runtime,
        image_url=image_url,
        quality=quality,
        size=size,
        aspect_ratio=aspect_ratio,
        enabled=enabled,
    )


def rewrite_grok_video_prompt(
    prompt: str,
    *,
    model: str = "",
    runtime: str = "",
    image_url: Any = None,
    duration: str | int | None = None,
    quality: str | None = None,
    size: str | None = None,
    aspect_ratio: str | None = None,
    enabled: Any = True,
) -> Dict[str, Any]:
    return _rewrite_grok_media_prompt(
        prompt,
        media_type="video",
        model=model,
        runtime=runtime,
        image_url=image_url,
        duration=duration,
        quality=quality,
        size=size,
        aspect_ratio=aspect_ratio,
        enabled=enabled,
    )


def _rewrite_grok_media_prompt(
    prompt: str,
    *,
    media_type: str,
    model: str = "",
    runtime: str = "",
    image_url: Any = None,
    duration: str | int | None = None,
    quality: str | None = None,
    size: str | None = None,
    aspect_ratio: str | None = None,
    enabled: Any = True,
) -> Dict[str, Any]:
    original = str(prompt or "").strip()
    media = "video" if media_type == "video" else "image"
    if not original:
        return _disabled_metadata(original, model, runtime, "empty", media_type=media)
    if not _enabled(enabled):
        return _disabled_metadata(original, model, runtime, "disabled", media_type=media)

    has_reference_image = bool(image_url)
    selection = (
        select_grok_prompt_fragments(original, reference_image=has_reference_image)
        if media == "image"
        else _empty_video_selection(original)
    )
    source_prompt = str(selection.get("cleaned_prompt") or original).strip() or original
    system_prompt = _resolve_system_prompt(media)
    user_prompt = _build_user_prompt(
        source_prompt,
        media_type=media,
        model=model,
        runtime=runtime,
        image_url=image_url,
        duration=duration,
        quality=quality,
        size=size,
        aspect_ratio=aspect_ratio,
        selection=selection,
    )
    rewritten = strip_control_keywords(_clean_model_prompt(_call_grok_text_model(system_prompt, user_prompt)))
    if media == "image":
        rewritten = _sanitize_grok_image_prompt(rewritten, preserve_reference_expression=bool(image_url))
    if not rewritten:
        return _disabled_metadata(original, model, runtime, "empty_rewrite", media_type=media)
    rewritten = apply_grok_reference_prompt_lock(
        rewritten,
        media_type=media,
        image_url=image_url,
    )

    return {
        "version": VERSION,
        "enhanced": True,
        "target": "grok",
        "media_type": media,
        "model": model or "",
        "runtime": runtime or "",
        "use_case": f"{media}_model_rewrite",
        "original_prompt": original,
        "source_prompt": source_prompt,
        "enhanced_prompt": rewritten,
        "library": {
            "name": GROK_IMAGE_PROMPT_OPTIMIZATION_SKILL_NAME if media == "image" else "grok-video-generation",
            "repositories_root": str(selection.get("repositories_root") or ""),
            "keyword": str(selection.get("keyword") or ""),
            "keyword_hit": bool(selection.get("keyword_hit")),
            "category": str(selection.get("category") or ""),
            "category_forced": bool(selection.get("category_forced")),
            "category_priority": bool(selection.get("category_priority")),
            "category_exclusive": bool(selection.get("category_exclusive")),
            "selection_mode": str(selection.get("selection_mode") or ""),
            "preferred_probability": selection.get("preferred_probability"),
            "fragment_prompt": str(selection.get("fragment_prompt") or ""),
        },
        "templates": [],
        "supplements": selection.get("fragments") or [],
        "created_at": time.time(),
    }


def build_grok_random_image_prompt(
    prompt: str,
    *,
    limit: int = 6,
    prompt_mode: str | None = None,
) -> Dict[str, Any]:
    """Build one random Grok image prompt and a Chinese display translation."""
    original = str(prompt or "").strip()
    if not original:
        return _disabled_metadata(original, "", "grok", "empty", media_type="image")
    mode = _resolve_random_prompt_mode(original, prompt_mode)
    selection = select_grok_prompt_fragments(
        original,
        limit=limit,
        reference_image=(mode == RANDOM_PROMPT_MODE_IMAGE_TO_IMAGE),
    )
    source_prompt = _clean_random_prompt_topic(original)
    if not source_prompt:
        source_prompt = "image-to-image visual concept" if mode == RANDOM_PROMPT_MODE_IMAGE_TO_IMAGE else "image concept"
    system_prompt = (
        _resolve_system_prompt("image")
        + "\n\nCreate one polished English Grok image prompt from the user request and random fragments. "
        "Return only the final English prompt text."
    )
    user_prompt = _build_user_prompt(
        source_prompt,
        media_type="image",
        model="grok-imagine-image",
        runtime="grok_random_prompt",
        image_url=None,
        duration=None,
        quality=None,
        size=None,
        aspect_ratio=None,
        selection=selection,
    )
    user_prompt += _build_random_prompt_output_contract(mode)
    english_prompt = strip_control_keywords(_clean_model_prompt(_call_grok_text_model(system_prompt, user_prompt)))
    english_prompt = _sanitize_grok_image_prompt(
        english_prompt,
        preserve_reference_expression=(mode == RANDOM_PROMPT_MODE_IMAGE_TO_IMAGE),
    )
    chinese_prompt = translate_grok_prompt_to_chinese(english_prompt) if english_prompt else ""
    return {
        "version": RANDOM_PROMPT_VERSION,
        "enhanced": True,
        "target": "grok",
        "media_type": "image",
        "runtime": "grok_random_prompt",
        "use_case": "random_image_prompt",
        "prompt_mode": mode,
        "original_prompt": original,
        "source_prompt": source_prompt,
        "enhanced_prompt": english_prompt,
        "chinese_prompt": chinese_prompt,
        "library": {
            "name": GROK_IMAGE_PROMPT_OPTIMIZATION_SKILL_NAME,
            "repositories_root": str(selection.get("repositories_root") or ""),
            "keyword": str(selection.get("keyword") or ""),
            "keyword_hit": bool(selection.get("keyword_hit")),
            "category": str(selection.get("category") or ""),
            "category_forced": bool(selection.get("category_forced")),
            "category_priority": bool(selection.get("category_priority")),
            "selection_mode": str(selection.get("selection_mode") or ""),
            "preferred_probability": selection.get("preferred_probability"),
            "fragment_prompt": str(selection.get("fragment_prompt") or ""),
        },
        "templates": [],
        "supplements": selection.get("fragments") or [],
        "created_at": time.time(),
    }


def format_grok_random_image_prompt_response(metadata: Dict[str, Any]) -> str:
    english_prompt = str((metadata or {}).get("enhanced_prompt") or "").strip()
    chinese_prompt = str((metadata or {}).get("chinese_prompt") or "").strip()
    mode = str((metadata or {}).get("prompt_mode") or RANDOM_PROMPT_MODE_IMAGE_TO_IMAGE)
    title = "随机文生图提示词" if mode == RANDOM_PROMPT_MODE_TEXT_TO_IMAGE else "随机图生图提示词"
    if not english_prompt:
        return "随机提示词生成失败：没有生成可展示的英文提示词。"
    lines = [
        f"{title}：",
        "",
        "English Prompt:",
        english_prompt,
        "",
        "中文翻译：",
        chinese_prompt or "（翻译暂不可用）",
    ]
    return "\n".join(lines).strip()


def translate_grok_prompt_to_chinese(prompt: str) -> str:
    text = str(prompt or "").strip()
    if not text:
        return ""
    system = (
        "Translate Grok image generation prompts into Chinese for display. Preserve quoted text, "
        "model names, aspect ratios, numbers, parameter-like tokens, and file paths. Return only Chinese."
    )
    user_prompt = "Translate this Grok image prompt into Chinese:\n\n" + text
    return _clean_model_prompt(_call_grok_text_model(system, user_prompt))


def _resolve_random_prompt_mode(prompt: str, prompt_mode: str | None = None) -> str:
    explicit_mode = str(prompt_mode or "").strip().lower().replace("-", "_")
    if explicit_mode in {"text_to_image", "t2i", "text2image"}:
        return RANDOM_PROMPT_MODE_TEXT_TO_IMAGE
    if explicit_mode in {"image_to_image", "i2i", "img2img", "image2image"}:
        return RANDOM_PROMPT_MODE_IMAGE_TO_IMAGE

    lowered = str(prompt or "").lower()
    compact = re.sub(r"\s+", "", lowered)
    if any(term in compact for term in ("文生图", "text-to-image", "texttoimage", "t2i", "text2image")):
        return RANDOM_PROMPT_MODE_TEXT_TO_IMAGE
    return RANDOM_PROMPT_MODE_IMAGE_TO_IMAGE


def _clean_random_prompt_topic(prompt: str) -> str:
    text = strip_control_keywords(str(prompt or ""))
    text = re.sub(r"(?i)\b(?:random|prompt|image[-\s]*to[-\s]*image|text[-\s]*to[-\s]*image|img2img|i2i|t2i)\b", " ", text)
    for token in (
        "随机",
        "随便",
        "任意",
        "给我",
        "帮我",
        "来个",
        "来一个",
        "生成",
        "写",
        "输出",
        "创建",
        "一个",
        "一条",
        "个",
        "条",
        "的",
        "提示词",
        "图生图",
        "文生图",
        "生图",
        "图片",
        "照片",
    ):
        text = text.replace(token, " ")
    return re.sub(r"[\s,，。！？:：;；、]+", " ", text).strip()


def _build_random_prompt_output_contract(prompt_mode: str) -> str:
    lines = [
        "",
        "Random prompt output contract:",
        "- Return one final English image-generation prompt only; no labels, no markdown, no Chinese in this step.",
        "- Use the random repository fragments as suggestions selected by code; do not invent unrelated repository content.",
        "- If fragments conflict with mandatory constraints, delete or neutralize the conflicting details.",
        "- Do not add generic quality/style booster phrases such as soft cinematic lighting, highly detailed, realistic skin texture, sensual atmosphere, 8k, 4k, UHD, HDR, masterpiece, or best quality.",
    ]
    if prompt_mode == RANDOM_PROMPT_MODE_TEXT_TO_IMAGE:
        lines.append(
            "- prompt_mode: text-to-image; a new subject may be described only when the user explicitly requested text-to-image."
        )
    else:
        lines.extend(
            [
                "- prompt_mode: image-to-image prompt text; assume the user will provide a reference image later.",
                "- Preserve the reference subject's identity, face, facial structure, skin tone/texture, hair, distinctive features, and body proportions.",
                "- Preserve the reference subject's original facial expression and gaze unless the user explicitly asks to change them.",
                "- Do not introduce new physical appearance descriptors such as hair color/style, eye color, ethnicity, age, body type, skin tone, facial features, expression, gaze, mouth pose, or attractiveness traits.",
                "- Hair is appearance. If any selected fragment adds hair, face, body-shape, skin, age, race, or eye details, remove that detail from the final prompt.",
                "- Expression is also identity-adjacent for image-to-image. If any selected fragment adds a smile, smirk, wink, parted lips, biting-lip pose, seductive gaze, or other expression control, remove it.",
            ]
        )
    return "\n".join(lines)


def _sanitize_grok_image_prompt(prompt: str, *, preserve_reference_expression: bool = False) -> str:
    text = str(prompt or "").strip()
    if not text:
        return ""

    sanitized = text
    removed_disallowed_segment = False
    phrases = list(_DISALLOWED_IMAGE_PROMPT_SEGMENTS)
    if preserve_reference_expression:
        phrases.extend(_DISALLOWED_IMAGE_TO_IMAGE_EXPRESSION_SEGMENTS)
        phrases.extend(_DISALLOWED_IMAGE_TO_IMAGE_APPEARANCE_SEGMENTS)
    for phrase in sorted(phrases, key=len, reverse=True):
        sanitized, replacements = re.subn(
            rf"(?i)(?<![A-Za-z0-9_-]){re.escape(phrase)}(?![A-Za-z0-9_-])",
            " ",
            sanitized,
        )
        removed_disallowed_segment = removed_disallowed_segment or replacements > 0

    sanitized = re.sub(r"\s+([,.;:])", r"\1", sanitized)
    sanitized = re.sub(r"(?:\s*,\s*){2,}", ", ", sanitized)
    sanitized = re.sub(r"^\s*[,.;:]+\s*", "", sanitized)
    if removed_disallowed_segment:
        sanitized = re.sub(r"\s*[,.;:]+\s*$", "", sanitized)
    else:
        sanitized = re.sub(r"\s*[,;:]+\s*$", "", sanitized)
    sanitized = re.sub(r"\s{2,}", " ", sanitized)
    return sanitized.strip()


def _empty_video_selection(prompt: str) -> Dict[str, Any]:
    cleaned_prompt = strip_control_keywords(prompt)
    return {
        "keyword": "",
        "keyword_hit": False,
        "category": "",
        "category_forced": False,
        "category_priority": False,
        "category_exclusive": False,
        "selection_mode": "none",
        "cleaned_prompt": cleaned_prompt,
        "preferred_probability": None,
        "fragment_prompt": "",
        "fragments": [],
        "repositories_root": "",
    }


def _resolve_system_prompt(media_type: str) -> str:
    media = "video" if media_type == "video" else "image"
    inline_key = f"GROK_{media.upper()}_PROMPT_REWRITE_SYSTEM_PROMPT"
    file_key = f"GROK_{media.upper()}_PROMPT_REWRITE_SYSTEM_PROMPT_FILE"
    inline = os.environ.get(inline_key)
    if inline and inline.strip():
        return inline.strip()

    try:
        from config import conf

        configured = str(conf().get(f"grok_{media}_prompt_rewrite_system_prompt") or "").strip()
        if configured:
            return configured
    except Exception:
        pass

    file_prompt = _read_system_prompt_file(media, file_key)
    if file_prompt:
        return file_prompt
    return (DEFAULT_VIDEO_SYSTEM_PROMPT if media == "video" else DEFAULT_IMAGE_SYSTEM_PROMPT).strip()


def _read_system_prompt_file(media_type: str, env_key: str) -> str:
    configured = os.environ.get(env_key)
    try:
        if not configured:
            from config import conf

            configured = str(conf().get(f"grok_{media_type}_prompt_rewrite_system_prompt_file") or "").strip()
    except Exception:
        configured = configured or ""

    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    prompt_skill_path = resolve_grok_system_prompt_path(media_type)
    if prompt_skill_path:
        candidates.append(prompt_skill_path)
    project_root = Path(__file__).resolve().parents[1]
    if media_type == "image":
        candidates.append(project_root / "skills" / "grok-image-generation" / "prompt_rewrite_system_prompt.txt")

    for candidate in candidates:
        try:
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except Exception as exc:
            logger.debug("[GrokPromptRewrite] failed to read prompt template %s: %s", candidate, exc)
    return ""


def _build_user_prompt(
    original: str,
    *,
    media_type: str,
    model: str,
    runtime: str,
    image_url: Any,
    duration: str | int | None,
    quality: str | None,
    size: str | None,
    aspect_ratio: str | None,
    selection: dict[str, Any],
) -> str:
    label = "video" if media_type == "video" else "image"
    lines = [
        f"Rewrite this user request into the final prompt for Grok {label} generation.",
        "",
        "User request:",
        original,
        "",
        "Generation context:",
        "- repository_fragment_selection: already completed by deterministic local code; do not select, request, or invent additional repository fragments",
    ]
    for key, value in (
        ("target_model", model),
        ("runtime", runtime),
        ("duration", duration),
        ("quality", quality),
        ("size_or_resolution", size),
        ("aspect_ratio", aspect_ratio),
    ):
        if value:
            lines.append(f"- {key}: {value}")
    if image_url:
        lines.append(
            "- reference_image: provided; preserve the exact subject identity, face, facial structure, "
            "skin texture/tone, hair, distinctive features, and unchanged areas; do not add new "
            "ethnicity, eye color, hair color, body type, age, or facial traits unless explicitly requested"
        )
    keyword = selection.get("keyword")
    if keyword:
        lines.append(f"- matched_prompt_repository_keyword: {keyword}")
        if selection.get("selection_mode") == "priority_with_supplement":
            lines.append(
                f"- repository_selection_rule: prioritize {keyword}/{selection.get('category')} fragments; "
                "allow one non-priority supplement when available"
            )
            if str(selection.get("category") or "").strip().lower() == "nsfw":
                lines.append(
                    "- control_keyword_rule: NSFW/nsfw is an internal fragment-selection signal; "
                    "do not copy the literal NSFW token into the final prompt"
                )
        elif selection.get("category_forced"):
            lines.append(f"- repository_selection_rule: forced {keyword}/{selection.get('category')} category")
        else:
            lines.append(f"- repository_selection_rule: 90% from {keyword}, 10% from other repositories when available")
    fragments = selection.get("fragments") or []
    constraints = [
        fragment
        for fragment in fragments
        if str(fragment.get("selection_role") or "").strip().lower() == "constraint"
        or fragment.get("constraint_type")
    ]
    random_fragments = [fragment for fragment in fragments if fragment not in constraints]
    if constraints:
        lines.extend(
            [
                "",
                "Stable user constraints (mandatory; random fragments must not override these):",
            ]
        )
        for index, fragment in enumerate(constraints, start=1):
            lines.append(
                f"{index}. [{fragment.get('repository')}/{fragment.get('file')}:{fragment.get('line')}] "
                f"{fragment.get('text')}"
            )
    lines.extend(["", "Random repository fragments for missing details:"])
    if random_fragments:
        for index, fragment in enumerate(random_fragments, start=1):
            role = str(fragment.get("selection_role") or "fragment")
            lines.append(
                f"{index}. ({role}) [{fragment.get('repository')}/{fragment.get('file')}:{fragment.get('line')}] "
                f"{fragment.get('text')}"
            )
    else:
        lines.append("- none selected; use the system prompt template only")
    return "\n".join(lines)


def _call_grok_text_model(system_prompt: str, user_prompt: str) -> str:
    from models.grok.grok_bot import GrokBot

    bot = GrokBot()
    response = bot.call_with_tools(
        messages=[{"role": "user", "content": user_prompt}],
        tools=None,
        stream=False,
        system=system_prompt,
        temperature=0.2,
        max_tokens=1200,
        max_output_tokens=1200,
        request_timeout=_DEFAULT_TIMEOUT_SECONDS,
        cache_shape_metadata={"request_kind": "grok_prompt_rewrite"},
    )
    return _extract_text_response(response)


def _extract_text_response(response: Any) -> str:
    if isinstance(response, str):
        return response
    if not isinstance(response, dict):
        return ""
    if response.get("error"):
        raise RuntimeError(str(response.get("message") or "prompt rewrite model call failed"))

    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            content = message.get("content")
            return _extract_content_text(content)

    for key in ("content", "text", "message", "result"):
        value = response.get(key)
        if value:
            return _extract_content_text(value)
    return ""


def _extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
        return "".join(parts)
    return str(content or "")


def _clean_model_prompt(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = _strip_code_fence(text)
    text = re.sub(
        r"(?i)^\s*(final\s+prompt|prompt|rewritten\s+prompt)\s*[:\uff1a]\s*",
        "",
        text,
    ).strip()
    if len(text) > _MAX_PROMPT_CHARS:
        text = text[:_MAX_PROMPT_CHARS].rstrip()
    return text


def apply_grok_reference_prompt_lock(
    prompt: str,
    *,
    media_type: str = "image",
    image_url: Any = None,
) -> str:
    text = str(prompt or "").strip()
    if not text or not image_url or REFERENCE_IMAGE_LOCK_PREFIX.lower() in text.lower():
        return text
    if str(media_type or "").strip().lower() == "video":
        lock = (
            f"{REFERENCE_IMAGE_LOCK_PREFIX} preserve the reference subject's exact face, facial structure, "
            "original expression, gaze direction, skin texture/tone, hair, distinctive features, and general body "
            "proportions across all frames; "
            "only change the requested motion, camera, style, clothing, objects, or environment; do not "
            "invent a new person or add new ethnicity, eye color, hair color, age, body type, expression, "
            "or facial traits."
        )
    else:
        lock = (
            f"{REFERENCE_IMAGE_LOCK_PREFIX} preserve the reference subject's exact face, facial structure, "
            "original expression, gaze direction, skin texture/tone, hair, distinctive features, and general "
            "body proportions; only change the "
            "requested style, clothing, objects, pose, or environment; do not invent a new person or add new "
            "ethnicity, eye color, hair color, age, body type, expression, or facial traits."
        )
    return f"{text}\n\n{lock}"


def _strip_code_fence(text: str) -> str:
    match = re.match(r"^```(?:text|prompt|markdown)?\s*(.*?)\s*```$", text.strip(), re.I | re.S)
    return match.group(1).strip() if match else text


def _disabled_metadata(
    prompt: str,
    model: str,
    runtime: str,
    reason: str,
    *,
    media_type: str,
) -> Dict[str, Any]:
    return {
        "version": VERSION,
        "enhanced": False,
        "disabled_reason": reason,
        "target": "grok",
        "media_type": media_type,
        "model": model or "",
        "runtime": runtime or "",
        "use_case": f"{media_type}_model_rewrite",
        "original_prompt": prompt,
        "source_prompt": prompt,
        "enhanced_prompt": prompt,
        "library": {},
        "templates": [],
        "supplements": [],
        "created_at": time.time(),
    }


def _enabled(value: Any) -> bool:
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}
