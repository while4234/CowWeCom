# encoding:utf-8

"""Hidden model-aware prompt enhancement for image generation."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from common.log import logger
except Exception:  # pragma: no cover - standalone script fallback
    import logging

    logger = logging.getLogger(__name__)


ENHANCED_PROMPT_MARKER = "[CowWeCom hidden image prompt enhancement]"
PROMPT_METADATA_FILE = "prompt_metadata.json"
PROMPT_HISTORY_FILE = "prompt-history.jsonl"

_MAX_TEMPLATE_CONTENT_CHARS = 1200
_MAX_HISTORY_PROMPT_CHARS = 12000
_CATEGORY_CACHE: Dict[str, list[dict[str, Any]]] = {}


_USE_CASES = {
    "portrait": {
        "categories": ("profile-avatar", "social-media-post"),
        "keywords": (
            "portrait",
            "avatar",
            "profile",
            "headshot",
            "selfie",
            "photo",
            "person",
            "people",
            "character",
            "人物",
            "人像",
            "写真",
            "头像",
            "照片",
            "自拍",
            "美女",
            "男生",
            "女生",
            "形象照",
        ),
    },
    "poster": {
        "categories": ("poster-flyer", "product-marketing", "social-media-post"),
        "keywords": ("poster", "flyer", "banner", "campaign", "海报", "活动", "宣传", "封面", "横幅"),
    },
    "infographic": {
        "categories": ("infographic-edu-visual", "app-web-design", "social-media-post"),
        "keywords": (
            "infographic",
            "diagram",
            "flowchart",
            "chart",
            "process",
            "timeline",
            "workflow",
            "流程图",
            "架构图",
            "信息图",
            "图表",
            "步骤",
            "时间线",
            "示意图",
        ),
    },
    "product": {
        "categories": ("product-marketing", "ecommerce-main-image", "social-media-post"),
        "keywords": ("product", "ecommerce", "packshot", "mockup", "商品", "产品", "电商", "主图", "包装", "广告"),
    },
    "ui": {
        "categories": ("app-web-design", "infographic-edu-visual"),
        "keywords": ("ui", "ux", "app", "web", "interface", "dashboard", "界面", "网页", "应用", "看板"),
    },
    "thumbnail": {
        "categories": ("youtube-thumbnail", "social-media-post"),
        "keywords": ("thumbnail", "youtube", "cover", "封面图", "缩略图", "视频封面"),
    },
    "comic": {
        "categories": ("comic-storyboard", "game-asset", "social-media-post"),
        "keywords": ("comic", "storyboard", "manga", "panel", "漫画", "分镜", "故事板"),
    },
    "game": {
        "categories": ("game-asset", "comic-storyboard"),
        "keywords": ("game", "sprite", "asset", "icon", "游戏", "角色", "道具", "素材", "图标"),
    },
}

_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "make",
    "create",
    "generate",
    "draw",
    "image",
    "photo",
    "picture",
    "please",
    "using",
    "use",
    "一个",
    "一张",
    "帮我",
    "生成",
    "制作",
    "画",
    "图片",
    "照片",
    "高清",
    "好看",
    "高质量",
}

_UNSAFE_TEMPLATE_PATTERNS = (
    re.compile(r"\b(NSFW|nude|naked|see-through|wardrobe malfunction)\b", re.I),
    re.compile(r"(露点|裸体|走光|色情|成人)"),
)


def redact_hidden_image_prompt_text(value: Any) -> str:
    """Remove hidden enhanced prompts from errors/log strings."""
    text = str(value or "")
    if ENHANCED_PROMPT_MARKER not in text:
        return text
    prefix = text.split(ENHANCED_PROMPT_MARKER, 1)[0].rstrip(" \t\r\n:-")
    omitted = "[hidden enhanced image prompt omitted]"
    return f"{prefix}: {omitted}" if prefix else omitted


def enhance_image_prompt(
    prompt: str,
    *,
    target: str,
    model: str = "",
    runtime: str = "",
    image_url: Any = None,
    quality: str | None = None,
    size: str | None = None,
    aspect_ratio: str | None = None,
    library_dir: str | None = None,
    enabled: Any = None,
) -> Dict[str, Any]:
    """Return hidden enhanced prompt metadata.

    The caller should send ``metadata["enhanced_prompt"]`` to the image model
    and keep the metadata out of normal user-facing status messages.
    """
    original = str(prompt or "").strip()
    if not original:
        return _disabled_metadata(original, target, "empty")
    if ENHANCED_PROMPT_MARKER in original:
        return _disabled_metadata(original, target, "already_enhanced")
    if not _is_enabled(enabled):
        return _disabled_metadata(original, target, "disabled")

    target_name = _normalize_target(target)
    if target_name == "grok":
        try:
            from common.grok_image_prompt_rewriter import rewrite_grok_image_prompt

            return rewrite_grok_image_prompt(
                original,
                model=model,
                runtime=runtime,
                image_url=image_url,
                quality=quality,
                size=size,
                aspect_ratio=aspect_ratio,
                enabled=True,
            )
        except Exception as exc:
            logger.warning("[ImagePromptEnhancer] Grok prompt rewrite failed; using original prompt: %s", exc)
            return _disabled_metadata(original, target, "grok_rewrite_failed")

    library = _resolve_library_dir(library_dir)
    manifest = _load_manifest(library)
    use_case = _detect_use_case(original, target=target_name)
    templates = _find_templates(library, manifest, original, use_case, target=target_name)
    enhanced = _build_enhanced_prompt(
        original,
        target=target_name,
        model=model,
        runtime=runtime,
        use_case=use_case,
        templates=templates,
        image_url=image_url,
        quality=quality,
        size=size,
        aspect_ratio=aspect_ratio,
        manifest=manifest,
    )
    return {
        "version": "youmind-full-library-v1",
        "enhanced": True,
        "target": target_name,
        "model": model or "",
        "runtime": runtime or "",
        "use_case": use_case,
        "original_prompt": original,
        "enhanced_prompt": enhanced,
        "library": {
            "name": "YouMind-OpenLab/nano-banana-pro-prompts-recommend-skill",
            "updatedAt": str(manifest.get("updatedAt") or ""),
            "totalPrompts": int(manifest.get("totalPrompts") or 0),
            "path": str(library) if library else "",
        },
        "templates": [_template_metadata(item) for item in templates],
        "created_at": time.time(),
    }


def write_prompt_metadata(output_dir: str | None, metadata: Dict[str, Any]) -> None:
    if not output_dir or not metadata or not metadata.get("enhanced"):
        return
    try:
        path = Path(output_dir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        target = path / PROMPT_METADATA_FILE
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(_history_safe_metadata(metadata), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, target)
    except Exception as exc:
        logger.debug("[ImagePromptEnhancer] failed to write prompt metadata: %s", exc)


def read_prompt_metadata(output_dir: str | None) -> Optional[Dict[str, Any]]:
    if not output_dir:
        return None
    path = Path(output_dir).expanduser().resolve() / PROMPT_METADATA_FILE
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.debug("[ImagePromptEnhancer] failed to read prompt metadata %s: %s", path, exc)
        return None


def record_prompt_history(
    *,
    workspace_root: str,
    memory_user_id: str,
    session_id: str = "",
    job_id: str = "",
    output_path: str = "",
    metadata: Optional[Dict[str, Any]],
) -> Optional[str]:
    if not workspace_root or not memory_user_id or not metadata or not metadata.get("enhanced_prompt"):
        return None
    try:
        history_dir = (
            Path(workspace_root).expanduser().resolve()
            / "users"
            / str(memory_user_id)
            / "files"
            / "image-generation"
        )
        history_dir.mkdir(parents=True, exist_ok=True)
        record = _history_safe_metadata(metadata)
        record.update(
            {
                "session_id": str(session_id or ""),
                "job_id": str(job_id or ""),
                "output_path": str(output_path or ""),
                "recorded_at": time.time(),
            }
        )
        history_path = history_dir / PROMPT_HISTORY_FILE
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return str(history_path)
    except Exception as exc:
        logger.warning("[ImagePromptEnhancer] failed to record prompt history: %s", exc)
        return None


def load_prompt_history(
    *,
    workspace_root: str,
    memory_user_id: str,
    session_id: str = "",
    job_id: str = "",
    limit: int = 1,
) -> List[Dict[str, Any]]:
    if not workspace_root or not memory_user_id:
        return []
    history_path = (
        Path(workspace_root).expanduser().resolve()
        / "users"
        / str(memory_user_id)
        / "files"
        / "image-generation"
        / PROMPT_HISTORY_FILE
    )
    if not history_path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    session_text = str(session_id or "")
    job_text = str(job_id or "")
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if job_text and str(record.get("job_id") or "") != job_text:
            continue
        if session_text and str(record.get("session_id") or "") not in {"", session_text}:
            continue
        records.append(record)
        if len(records) >= max(int(limit or 1), 1):
            break
    return records


def _is_enabled(enabled: Any = None) -> bool:
    if enabled is not None and _is_disabled_flag(enabled):
        return False
    for key in (
        "SKILL_IMAGE_GENERATION_PROMPT_ENHANCEMENT_ENABLED",
        "IMAGE_PROMPT_ENHANCEMENT_ENABLED",
    ):
        value = os.environ.get(key)
        if value is not None:
            return not _is_disabled_flag(value)
    try:
        from config import conf

        configured = conf().get("image_prompt_enhancement_enabled", True)
        if _is_disabled_flag(configured):
            return False
        if enabled is None:
            return bool(configured)
    except Exception:
        pass
    return True


def _is_disabled_flag(value: Any) -> bool:
    return str(value).strip().lower() in {"0", "false", "no", "off", "disabled"}


def _disabled_metadata(prompt: str, target: str, reason: str) -> Dict[str, Any]:
    return {
        "version": "youmind-full-library-v1",
        "enhanced": False,
        "disabled_reason": reason,
        "target": _normalize_target(target),
        "original_prompt": prompt,
        "enhanced_prompt": prompt,
        "templates": [],
        "library": {},
        "created_at": time.time(),
    }


def _resolve_library_dir(configured: str | None = None) -> Optional[Path]:
    try:
        from common.prompt_optimization_repository import resolve_nano_banana_library_dir

        return resolve_nano_banana_library_dir(configured)
    except Exception as exc:
        logger.debug("[ImagePromptEnhancer] prompt optimization skill lookup failed: %s", exc)
    return None


def _load_manifest(library: Optional[Path]) -> Dict[str, Any]:
    if not library:
        return {"updatedAt": "", "totalPrompts": 0, "categories": []}
    try:
        data = json.loads((library / "manifest.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"categories": []}
    except Exception as exc:
        logger.warning("[ImagePromptEnhancer] failed to load manifest: %s", exc)
        return {"updatedAt": "", "totalPrompts": 0, "categories": []}


def _detect_use_case(prompt: str, *, target: str) -> str:
    text = prompt.lower()
    if _normalize_target(target) == "grok":
        for use_case in ("infographic", "poster", "product", "ui", "thumbnail", "comic", "game"):
            if _has_any_keyword(text, _USE_CASES[use_case]["keywords"]):
                return use_case
        return "portrait"
    best_case = "general"
    best_score = 0
    for use_case, info in _USE_CASES.items():
        score = sum(1 for keyword in info["keywords"] if keyword.lower() in text)
        if score > best_score:
            best_case = use_case
            best_score = score
    return best_case


def _find_templates(
    library: Optional[Path],
    manifest: Dict[str, Any],
    prompt: str,
    use_case: str,
    *,
    target: str,
    limit: int = 3,
) -> list[dict[str, Any]]:
    if not library:
        return []
    category_map = _category_map(manifest)
    candidate_slugs = _candidate_category_slugs(use_case, target=target)
    terms = _search_terms(prompt, use_case)
    scored: list[tuple[int, dict[str, Any]]] = []
    for slug in candidate_slugs:
        category = category_map.get(slug)
        if not category:
            continue
        for item in _load_category(library, category):
            score = _score_template(item, terms, slug=slug, use_case=use_case)
            if score > 0:
                scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, item in scored:
        item_id = str(item.get("id") or "")
        if item_id in seen or _template_looks_unsafe(item):
            continue
        selected.append(item)
        seen.add(item_id)
        if len(selected) >= limit:
            break
    return selected


def _category_map(manifest: Dict[str, Any]) -> dict[str, dict[str, Any]]:
    categories = manifest.get("categories") or []
    if not isinstance(categories, list):
        return {}
    return {str(item.get("slug") or ""): item for item in categories if isinstance(item, dict)}


def _candidate_category_slugs(use_case: str, *, target: str) -> tuple[str, ...]:
    if use_case in _USE_CASES:
        primary = _USE_CASES[use_case]["categories"]
    else:
        primary = ("social-media-post", "product-marketing", "others")
    if _normalize_target(target) == "grok" and use_case == "portrait":
        return ("profile-avatar", "social-media-post", "product-marketing", "others")
    extras = ("others",)
    return tuple(dict.fromkeys((*primary, *extras)))


def _load_category(library: Path, category: Dict[str, Any]) -> list[dict[str, Any]]:
    filename = str(category.get("file") or "").strip()
    if not filename:
        return []
    path = (library / filename).resolve()
    cache_key = str(path)
    if cache_key in _CATEGORY_CACHE:
        return _CATEGORY_CACHE[cache_key]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else []
        for item in items:
            if isinstance(item, dict):
                item["_category_slug"] = str(category.get("slug") or "")
                item["_category_title"] = str(category.get("title") or "")
        _CATEGORY_CACHE[cache_key] = [item for item in items if isinstance(item, dict)]
        return _CATEGORY_CACHE[cache_key]
    except Exception as exc:
        logger.warning("[ImagePromptEnhancer] failed to load category %s: %s", filename, exc)
        _CATEGORY_CACHE[cache_key] = []
        return []


def _score_template(item: Dict[str, Any], terms: Iterable[str], *, slug: str, use_case: str) -> int:
    haystack = " ".join(
        str(item.get(key) or "")
        for key in ("title", "description", "content")
    ).lower()
    score = 0
    for term in terms:
        term_l = term.lower()
        if len(term_l) < 2:
            continue
        if term_l in haystack:
            score += 8 if term_l in str(item.get("title") or "").lower() else 4
    if item.get("sourceMedia"):
        score += 4
    if use_case in _USE_CASES and slug in _USE_CASES[use_case]["categories"]:
        score += 10
    if item.get("needReferenceImages"):
        score -= 2
    return score


def _search_terms(prompt: str, use_case: str) -> list[str]:
    terms = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", prompt)
    terms.extend(_USE_CASES.get(use_case, {}).get("keywords", ())[:6])
    cleaned: list[str] = []
    for term in terms:
        value = term.strip().lower()
        if value and value not in _STOPWORDS and value not in cleaned:
            cleaned.append(value)
    return cleaned[:16]


def _build_enhanced_prompt(
    original: str,
    *,
    target: str,
    model: str,
    runtime: str,
    use_case: str,
    templates: list[dict[str, Any]],
    image_url: Any,
    quality: str | None,
    size: str | None,
    aspect_ratio: str | None,
    manifest: Dict[str, Any],
) -> str:
    template_block = _template_block(templates)
    context_block = _context_block(
        model=model,
        runtime=runtime,
        use_case=use_case,
        image_url=image_url,
        quality=quality,
        size=size,
        aspect_ratio=aspect_ratio,
        manifest=manifest,
    )
    if target == "grok":
        body = _grok_prompt_body(original, use_case=use_case, template_block=template_block)
    else:
        body = _gpt_prompt_body(original, use_case=use_case, template_block=template_block)
    return f"{ENHANCED_PROMPT_MARKER}\n{context_block}\n\n{body}".strip()


def _context_block(**kwargs: Any) -> str:
    lines = ["Generation context:"]
    for key in ("model", "runtime", "use_case", "quality", "size", "aspect_ratio"):
        value = kwargs.get(key)
        if value:
            lines.append(f"- {key}: {value}")
    if kwargs.get("image_url"):
        lines.append("- input_images: use provided reference images; preserve requested unchanged areas")
    manifest = kwargs.get("manifest") or {}
    if manifest.get("totalPrompts"):
        lines.append(
            f"- prompt_library: YouMind Nano Banana Pro references, {manifest.get('totalPrompts')} prompts, updated {manifest.get('updatedAt')}"
        )
    return "\n".join(lines)


def _template_block(templates: list[dict[str, Any]]) -> str:
    if not templates:
        return "No exact library template was strong enough; use the full-library category grammar as guidance."
    lines = ["Matched prompt-library templates for hidden style/structure guidance:"]
    for index, item in enumerate(templates, start=1):
        title = _compact_text(item.get("title"), 140)
        desc = _compact_text(item.get("description"), 220)
        excerpt = _compact_text(_sanitize_template_excerpt(item.get("content")), _MAX_TEMPLATE_CONTENT_CHARS)
        lines.append(
            f"{index}. [{item.get('_category_slug') or 'unknown'} #{item.get('id')}] {title}\n"
            f"   Description: {desc}\n"
            f"   Structural cues: {excerpt}"
        )
    return "\n".join(lines)


def _grok_prompt_body(original: str, *, use_case: str, template_block: str) -> str:
    if use_case == "portrait":
        return f"""Grok Imagine portrait prompt. Create a high-aesthetic, photorealistic person-focused image.

User request:
{original}

{template_block}

Final visual brief:
- Portrait type: polished editorial portrait / lifestyle photo unless the user requested another people-photo style.
- Subject identity: follow the user's requested person, age, gender, role, ethnicity, wardrobe, and expression. If unspecified, keep it tasteful, natural, and believable.
- Pose and expression: clear face, natural body proportions, relaxed but intentional pose, emotionally readable expression.
- Wardrobe and styling: cohesive styling that supports the concept; avoid accidental logos or readable brand marks.
- Environment: cinematic but realistic setting, with background details that reinforce the user's idea without stealing attention from the subject.
- Lighting: flattering natural or studio lighting, soft highlights, controlled shadows, realistic skin texture.
- Camera/framing: portrait-friendly composition, 50mm-85mm editorial look, sharp focus on eyes/face, no artificial over-smoothing.
- Aesthetic constraints: premium, modern, tasteful, beautiful, coherent color palette, visually balanced.
- Avoid: watermark, random text, extra fingers, distorted anatomy, plastic skin, low-resolution artifacts, unintended logos, NSFW content."""
    return f"""Grok Imagine prompt. Follow the user's request exactly while keeping the output visually polished and coherent.

User request:
{original}

{template_block}

Final visual brief:
- Primary goal: satisfy the user request without switching subject or medium.
- Composition: simple strong focal point, balanced layout, clear hierarchy.
- Style: premium, modern, high-aesthetic, photorealistic where applicable.
- Details: add enough scene, lighting, camera, material, and mood information to make the image feel finished.
- Text: only include text explicitly requested by the user; keep it short and legible.
- Avoid: watermark, unintended logos, random extra text, distorted objects, clutter, low-resolution artifacts."""


def _gpt_prompt_body(original: str, *, use_case: str, template_block: str) -> str:
    return f"""GPT image prompt. Produce one finished image that is visually refined, useful, and faithful to the user's request.

User request:
{original}

{template_block}

Final prompt structure:
- Use case: {use_case}
- Asset type: infer from the user request; if unclear, create the most useful image for the request.
- Primary request: implement the user's idea directly, preserving any requested language, numbers, labels, relationships, and layout.
- Scene/backdrop: choose a backdrop that clarifies the subject and improves aesthetic quality.
- Subject: make the main subject instantly recognizable; for people, keep natural anatomy and flattering but realistic details.
- Style/medium: select the best medium for the request: photorealistic photo, product render, poster design, infographic, flowchart, UI mockup, comic, or illustration.
- Composition/framing: use clear hierarchy, intentional spacing, professional alignment, and an output-ready crop.
- Lighting/mood/color: use coherent palette, readable contrast, and mood aligned with the purpose.
- Text handling: include only text explicitly requested by the user; keep text short, correctly spelled, and placed in a clean layout. For diagrams/flowcharts, keep labels concise and legible.
- Reference image handling: when input images are provided, preserve identity, key objects, and unchanged areas unless the user asks to alter them.
- Constraints: no watermark, no unintended logos, no random extra text, no distorted anatomy, no incoherent chart labels, no cluttered layout."""


def _template_metadata(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": item.get("id"),
        "category_slug": item.get("_category_slug") or "",
        "category_title": item.get("_category_title") or "",
        "title": item.get("title") or "",
        "description": item.get("description") or "",
        "needReferenceImages": bool(item.get("needReferenceImages")),
        "sourceMedia": (item.get("sourceMedia") or [])[:2],
    }


def _history_safe_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "version",
        "enhanced",
        "target",
        "model",
        "runtime",
        "use_case",
        "original_prompt",
        "enhanced_prompt",
        "library",
        "templates",
        "created_at",
    }
    record = {key: metadata.get(key) for key in allowed if key in metadata}
    for key in ("original_prompt", "enhanced_prompt"):
        if isinstance(record.get(key), str) and len(record[key]) > _MAX_HISTORY_PROMPT_CHARS:
            record[key] = record[key][:_MAX_HISTORY_PROMPT_CHARS] + "\n...[truncated]"
    return record


def _normalize_target(target: str) -> str:
    text = str(target or "").strip().lower()
    if text in {"grok", "xai", "x.ai"} or "grok" in text:
        return "grok"
    return "gpt"


def _has_any_keyword(text: str, keywords: Iterable[str]) -> bool:
    return any(keyword.lower() in text for keyword in keywords)


def _template_looks_unsafe(item: Dict[str, Any]) -> bool:
    content = " ".join(str(item.get(key) or "") for key in ("title", "description", "content"))
    return any(pattern.search(content) for pattern in _UNSAFE_TEMPLATE_PATTERNS)


def _sanitize_template_excerpt(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"(?im)^\s*[-*]?\s*(negative prompts?|avoid)\s*:?.*$", "Avoid unsafe or incoherent visual artifacts.", text)
    text = re.sub(r"\b(NSFW|see-through fabrics?|wardrobe malfunctions?|nude|naked)\b", "unsafe content", text, flags=re.I)
    text = re.sub(r"(露点|裸体|走光|色情|成人)", "不安全内容", text)
    return text


def _compact_text(value: Any, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."
