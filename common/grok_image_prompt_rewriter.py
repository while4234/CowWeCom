# encoding:utf-8

"""Grok-backed prompt rewriting for Grok image and video generation."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict

from common.prompt_optimization_repository import (
    resolve_grok_system_prompt_path,
    select_grok_prompt_fragments,
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
Return only the final prompt text.
"""

DEFAULT_VIDEO_SYSTEM_PROMPT = """You are the Grok video prompt optimizer for CowWeCom.

Rewrite the user's request into one complete prompt for Grok video generation.
Use the random repository fragments only to fill missing motion, camera, scene,
lighting, and continuity details. Preserve the user's subject, action, duration,
aspect ratio, reference-image intent, names, text, and constraints. Return only
the final prompt text.
"""

VERSION = "grok-model-rewrite-v2"
_MAX_PROMPT_CHARS = 12000
_DEFAULT_TIMEOUT_SECONDS = 60


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

    selection = select_grok_prompt_fragments(original)
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
    rewritten = _clean_model_prompt(_call_grok_text_model(system_prompt, user_prompt))
    if not rewritten:
        return _disabled_metadata(original, model, runtime, "empty_rewrite", media_type=media)

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
            "name": "image-prompt-optimization",
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
        lines.append("- reference_image: provided; preserve requested identity/objects/unchanged areas")
    keyword = selection.get("keyword")
    if keyword:
        lines.append(f"- matched_prompt_repository_keyword: {keyword}")
        if selection.get("selection_mode") == "priority_with_supplement":
            lines.append(
                f"- repository_selection_rule: prioritize {keyword}/{selection.get('category')} fragments; "
                "allow one non-priority supplement when available"
            )
        elif selection.get("category_forced"):
            lines.append(f"- repository_selection_rule: forced {keyword}/{selection.get('category')} category")
        else:
            lines.append(f"- repository_selection_rule: 90% from {keyword}, 10% from other repositories when available")
    fragments = selection.get("fragments") or []
    lines.extend(["", "Random repository fragments for missing details:"])
    if fragments:
        for index, fragment in enumerate(fragments, start=1):
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
