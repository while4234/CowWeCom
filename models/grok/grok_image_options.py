# encoding:utf-8

"""Shared option parsing for Grok image generation."""

from __future__ import annotations

import base64
import os
import re
import struct
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from common.log import logger


GROK_SPEED_MODEL = "grok-imagine-image"
GROK_QUALITY_MODEL = "grok-imagine-image-quality"
GROK_IMAGE_MODELS = {GROK_SPEED_MODEL, GROK_QUALITY_MODEL}
GROK_IMAGE_RESOLUTIONS = {"1k", "2k"}
GROK_IMAGE_MAX_REFERENCE_IMAGES = 3
GROK_IMAGE_ASPECT_RATIOS = {
    "landscape": "16:9",
    "square": "1:1",
    "portrait": "9:16",
    "16:9": "16:9",
    "1:1": "1:1",
    "9:16": "9:16",
    "4:3": "4:3",
    "3:4": "3:4",
    "3:2": "3:2",
    "2:3": "2:3",
}

_GROK_SPEED_QUALITY_HINTS = {
    "speed",
    "fast",
    "quick",
    "draft",
    "low",
    "standard",
    "快速",
    "快",
    "速度",
    "草稿",
}
_GROK_HIGH_QUALITY_HINTS = {
    "quality",
    "high quality",
    "high",
    "hd",
    "best",
    "detailed",
    "detail",
    "premium",
    "高质量",
    "高清",
    "精细",
    "细节",
    "质量",
}
_SUPPORTED_RATIO_VALUES = (
    ("16:9", 16 / 9),
    ("9:16", 9 / 16),
    ("4:3", 4 / 3),
    ("3:4", 3 / 4),
    ("3:2", 3 / 2),
    ("2:3", 2 / 3),
    ("1:1", 1.0),
)
_RATIO_PATTERN = re.compile(r"(?<!\d)(16|9|4|3|2|1)\s*[:：/]\s*(9|16|3|4|2|1)(?!\d)")
_PIXEL_PATTERN = re.compile(r"(?<!\d)([1-9]\d{2,4})\s*[xX×*]\s*([1-9]\d{2,4})(?!\d)")
_DATA_IMAGE_RE = re.compile(r"^data:image/[^;]+;base64,(.+)$", re.IGNORECASE | re.DOTALL)
_IMAGE_REF_RE = re.compile(r"\[\s*(?:图片|image)\s*:\s*([^\]]+?)\s*\]", re.IGNORECASE)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8"


@dataclass(frozen=True)
class GrokImageOptions:
    """Resolved Grok image options for provider calls."""

    model: str
    aspect_ratio: Optional[str] = None
    resolution: Optional[str] = None
    image_url: Any = None
    reference_dimensions: Optional[tuple[int, int]] = None
    inferred_from_reference: bool = False
    explicit_aspect_ratio: bool = False
    explicit_resolution: bool = False


def resolve_grok_image_model(
    prompt: str,
    *,
    quality: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    explicit_model = str(model or "").strip()
    if explicit_model in GROK_IMAGE_MODELS:
        return explicit_model

    quality_hint = str(quality or "").strip().lower()
    if quality_hint in _GROK_HIGH_QUALITY_HINTS:
        return GROK_QUALITY_MODEL
    if quality_hint in _GROK_SPEED_QUALITY_HINTS:
        return GROK_SPEED_MODEL

    haystack = str(prompt or "").lower()
    if _has_grok_quality_phrase(haystack):
        return GROK_QUALITY_MODEL
    return explicit_model or GROK_SPEED_MODEL


def resolve_grok_image_resolution(size: Optional[str]) -> Optional[str]:
    value = str(size or "").strip().lower()
    if not value or value == "auto":
        return None
    if "2k" in value or "2048" in value:
        return "2k"
    if "1k" in value or "1024" in value:
        return "1k"
    return None


def resolve_grok_image_aspect_ratio(value: Optional[str]) -> Optional[str]:
    candidate = str(value or "").strip().lower()
    if not candidate or candidate == "auto":
        return None
    return GROK_IMAGE_ASPECT_RATIOS.get(candidate)


def resolve_grok_image_options(
    *,
    prompt: str,
    image_url: Any = None,
    size: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    quality: Optional[str] = None,
    model: Optional[str] = None,
) -> GrokImageOptions:
    """Resolve model, reference image, ratio, and resolution in one place."""
    image_urls = normalize_image_references(image_url)
    first_image_url = image_urls[0] if image_urls else None
    prompt_text = str(prompt or "")
    prompt_pixels = _extract_pixel_dimensions(prompt_text)
    size_pixels = _extract_pixel_dimensions(str(size or ""))

    explicit_aspect = resolve_grok_image_aspect_ratio(aspect_ratio)
    explicit_resolution = resolve_grok_image_resolution(size)
    prompt_aspect = _extract_prompt_aspect_ratio(prompt_text)
    prompt_resolution = _extract_prompt_resolution(prompt_text)
    pixel_dimensions = size_pixels or prompt_pixels

    if not explicit_aspect and pixel_dimensions:
        explicit_aspect = _nearest_aspect_ratio(pixel_dimensions)
    if not explicit_resolution and pixel_dimensions:
        explicit_resolution = _resolution_for_dimensions(pixel_dimensions)

    aspect_is_explicit = bool(explicit_aspect or prompt_aspect)
    resolution_is_explicit = bool(explicit_resolution or prompt_resolution)
    resolved_aspect = explicit_aspect or prompt_aspect
    resolved_resolution = explicit_resolution or prompt_resolution

    reference_dimensions = None
    inferred_from_reference = False
    if first_image_url and not aspect_is_explicit and not resolution_is_explicit:
        reference_dimensions = read_reference_image_dimensions(first_image_url)
        if reference_dimensions:
            resolved_aspect = _nearest_aspect_ratio(reference_dimensions)
            resolved_resolution = _resolution_for_dimensions(reference_dimensions)
            inferred_from_reference = True
            logger.info(
                "[GrokImageOptions] inferred image-to-image options: "
                "width=%s height=%s aspect_ratio=%s resolution=%s",
                reference_dimensions[0],
                reference_dimensions[1],
                resolved_aspect,
                resolved_resolution,
            )
        else:
            logger.info(
                "[GrokImageOptions] reference image dimensions unavailable; "
                "falling back to configured Grok image defaults: source=%s",
                safe_reference_label(first_image_url),
            )

    return GrokImageOptions(
        model=resolve_grok_image_model(prompt_text, quality=quality, model=model),
        aspect_ratio=resolved_aspect,
        resolution=resolved_resolution,
        image_url=_reference_image_value(image_urls),
        reference_dimensions=reference_dimensions,
        inferred_from_reference=inferred_from_reference,
        explicit_aspect_ratio=aspect_is_explicit,
        explicit_resolution=resolution_is_explicit,
    )


def _reference_image_value(image_urls: list[str]) -> Any:
    if not image_urls:
        return None
    return image_urls[0] if len(image_urls) == 1 else image_urls


def normalize_image_references(image_url: Any) -> list[str]:
    if image_url is None:
        return []
    if isinstance(image_url, (list, tuple)):
        values = [str(item or "").strip() for item in image_url if str(item or "").strip()]
        if len(values) > GROK_IMAGE_MAX_REFERENCE_IMAGES:
            raise ValueError(f"Grok image-to-image supports up to {GROK_IMAGE_MAX_REFERENCE_IMAGES} reference images.")
        return values
    value = str(image_url or "").strip()
    return [value] if value else []


def normalize_single_image_reference(image_url: Any) -> Optional[str]:
    if image_url is None:
        return None
    if isinstance(image_url, (list, tuple)):
        values = [str(item or "").strip() for item in image_url if str(item or "").strip()]
        if not values:
            return None
        if len(values) > 1:
            raise ValueError("Grok image-to-image supports exactly one reference image.")
        return values[0]
    value = str(image_url or "").strip()
    return value or None


def extract_image_references(prompt: str) -> list[str]:
    refs: list[str] = []
    for match in _IMAGE_REF_RE.finditer(str(prompt or "")):
        ref = match.group(1).strip()
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def strip_image_references(prompt: str) -> str:
    return _IMAGE_REF_RE.sub("", str(prompt or "")).strip()


def looks_like_grok_image_to_image_request(prompt: str) -> bool:
    text = str(prompt or "").lower()
    if not text:
        return False
    chinese_hints = (
        "参考上图",
        "参考这张图",
        "参考图片",
        "基于上面的图",
        "基于这张图",
        "图生图",
        "以图生图",
        "修图",
        "改图",
        "编辑图",
        "换背景",
        "把这张图",
        "把图片",
        "这张图改",
        "这张图换",
    )
    if any(hint in text for hint in chinese_hints):
        return True
    return bool(
        re.search(
            r"\b(reference|based on|use this|this image|this picture|image-to-image|img2img|edit|modify|retouch|change background)\b",
            text,
        )
    )


def read_reference_image_dimensions(source: Any) -> Optional[tuple[int, int]]:
    value = str(source or "").strip().strip("'\"")
    if not value:
        return None
    data_match = _DATA_IMAGE_RE.match(value)
    if data_match:
        try:
            return _dimensions_from_bytes(base64.b64decode(data_match.group(1), validate=True))
        except Exception:
            return None
    if value.lower().startswith(("http://", "https://")):
        return None
    path = local_path_from_reference(value)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as handle:
            return _dimensions_from_bytes(handle.read())
    except OSError:
        return None


def local_path_from_reference(source: str) -> str:
    if str(source or "").lower().startswith("file://"):
        parsed = urlparse(source)
        path = unquote(parsed.path or source[7:])
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        return path
    expanded = os.path.abspath(os.path.expanduser(str(source or "")))
    return expanded if os.path.exists(expanded) else str(source or "")


def safe_reference_label(source: Any) -> str:
    if isinstance(source, (list, tuple)):
        labels = [safe_reference_label(item) for item in source if str(item or "").strip()]
        if not labels:
            return "<empty>"
        return ", ".join(labels[:GROK_IMAGE_MAX_REFERENCE_IMAGES])
    value = str(source or "").strip()
    if not value:
        return "<empty>"
    if _DATA_IMAGE_RE.match(value):
        return "data:image/*;base64,<redacted>"
    if value.lower().startswith(("http://", "https://")):
        parsed = urlparse(value)
        name = os.path.basename(parsed.path or "") or "<url>"
        return f"{parsed.scheme}://{parsed.netloc}/{name}"
    path = local_path_from_reference(value)
    return os.path.basename(path) or "<local-file>"


def _has_grok_quality_phrase(text: str) -> bool:
    chinese_phrases = ("高质量", "高清", "精细", "细节丰富", "质量优先", "质量模式")
    if any(phrase in text for phrase in chinese_phrases):
        return True
    return bool(
        re.search(
            r"\b(high[- ]?quality|quality\s+mode|best\s+quality|hd|high\s+detail|detailed|premium)\b",
            text,
        )
    )


def _extract_prompt_aspect_ratio(prompt: str) -> Optional[str]:
    text = str(prompt or "").lower()
    for match in _RATIO_PATTERN.finditer(text):
        ratio = f"{match.group(1)}:{match.group(2)}"
        if ratio in GROK_IMAGE_ASPECT_RATIOS:
            return ratio
    orientation_hints = (
        (("横图", "横版", "宽屏", "landscape"), "16:9"),
        (("竖图", "竖版", "竖屏", "portrait", "vertical"), "9:16"),
        (("方图", "方形", "正方形", "square"), "1:1"),
    )
    for hints, ratio in orientation_hints:
        if any(hint in text for hint in hints):
            return ratio
    return None


def _extract_prompt_resolution(prompt: str) -> Optional[str]:
    text = str(prompt or "").lower()
    if re.search(r"(?<!\w)2\s*k(?!\w)", text) or "2048" in text:
        return "2k"
    if re.search(r"(?<!\w)1\s*k(?!\w)", text) or "1024" in text:
        return "1k"
    return None


def _extract_pixel_dimensions(text: str) -> Optional[tuple[int, int]]:
    match = _PIXEL_PATTERN.search(str(text or ""))
    if not match:
        return None
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return width, height


def _nearest_aspect_ratio(dimensions: tuple[int, int]) -> Optional[str]:
    width, height = dimensions
    if width <= 0 or height <= 0:
        return None
    ratio = width / height
    return min(_SUPPORTED_RATIO_VALUES, key=lambda item: abs(item[1] - ratio))[0]


def _resolution_for_dimensions(dimensions: tuple[int, int]) -> str:
    width, height = dimensions
    longest_edge = max(width, height)
    area = width * height
    return "2k" if longest_edge >= 1536 or area >= 1800 * 1000 else "1k"


def _dimensions_from_bytes(raw: bytes) -> Optional[tuple[int, int]]:
    if not raw:
        return None
    png_size = _png_dimensions(raw)
    if png_size:
        return png_size
    jpeg_size = _jpeg_dimensions(raw)
    if jpeg_size:
        return jpeg_size
    return _pillow_dimensions(raw)


def _png_dimensions(raw: bytes) -> Optional[tuple[int, int]]:
    if len(raw) < 24 or not raw.startswith(_PNG_MAGIC):
        return None
    width, height = struct.unpack(">II", raw[16:24])
    if width > 0 and height > 0:
        return int(width), int(height)
    return None


def _jpeg_dimensions(raw: bytes) -> Optional[tuple[int, int]]:
    if len(raw) < 4 or not raw.startswith(_JPEG_MAGIC):
        return None
    index = 2
    while index + 9 < len(raw):
        if raw[index] != 0xFF:
            index += 1
            continue
        marker = raw[index + 1]
        index += 2
        while marker == 0xFF and index < len(raw):
            marker = raw[index]
            index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(raw):
            return None
        segment_length = int.from_bytes(raw[index : index + 2], "big")
        if segment_length < 2 or index + segment_length > len(raw):
            return None
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            height = int.from_bytes(raw[index + 3 : index + 5], "big")
            width = int.from_bytes(raw[index + 5 : index + 7], "big")
            if width > 0 and height > 0:
                return int(width), int(height)
        index += segment_length
    return None


def _pillow_dimensions(raw: bytes) -> Optional[tuple[int, int]]:
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(raw)) as image:
            width, height = image.size
        if width > 0 and height > 0:
            return int(width), int(height)
    except Exception:
        return None
    return None
