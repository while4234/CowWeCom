# encoding:utf-8

"""Image normalization before sending through chat channels."""

from __future__ import annotations

import os
import tempfile
import uuid

from common.log import logger


DEFAULT_IMAGE_SEND_MAX_WIDTH = 2048
DEFAULT_IMAGE_SEND_MAX_HEIGHT = 2048


def prepare_image_for_send(
    file_path: str,
    *,
    max_bytes: int,
    max_width: int = DEFAULT_IMAGE_SEND_MAX_WIDTH,
    max_height: int = DEFAULT_IMAGE_SEND_MAX_HEIGHT,
    prefix: str = "chat_image",
) -> str:
    """Return an image path that fits channel byte and dimension limits.

    The original path is returned when it already fits. Otherwise a temporary
    JPEG is created; callers own cleanup of any returned path different from
    ``file_path``.
    """
    source_path = str(file_path or "").strip()
    if not source_path or not os.path.exists(source_path):
        return ""

    max_bytes = _positive_int(max_bytes, 2 * 1024 * 1024)
    max_width = _positive_int(max_width, DEFAULT_IMAGE_SEND_MAX_WIDTH)
    max_height = _positive_int(max_height, DEFAULT_IMAGE_SEND_MAX_HEIGHT)

    try:
        from PIL import Image, ImageOps
    except ImportError:
        logger.warning("[ImageSendLimits] Pillow unavailable; image send normalization skipped")
        return source_path if os.path.getsize(source_path) <= max_bytes else ""

    try:
        with Image.open(source_path) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
            fmt = (opened.format or "").upper()
            width, height = image.size

        if (
            fmt in {"JPEG", "PNG"}
            and width <= max_width
            and height <= max_height
            and os.path.getsize(source_path) <= max_bytes
            and _extension_matches_format(source_path, fmt)
        ):
            return source_path

        image = _resize_to_bounds(image, max_width, max_height)
        image = _flatten_for_jpeg(image)
        out_path = os.path.join(tempfile.gettempdir(), f"{prefix}_{uuid.uuid4().hex[:8]}.jpg")
        if _save_jpeg_under_limit(image, out_path, max_bytes):
            return out_path
        logger.error(
            "[ImageSendLimits] Cannot fit image below %s bytes and %sx%s: %s",
            max_bytes,
            max_width,
            max_height,
            source_path,
        )
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        return ""
    except Exception as exc:
        logger.error("[ImageSendLimits] Image normalization failed for %s: %s", source_path, exc)
        return ""


def image_send_dimensions_from_config(config) -> tuple[int, int]:
    """Read global image-send dimension limits from the project config."""
    return (
        _positive_int(config.get("image_send_max_width", DEFAULT_IMAGE_SEND_MAX_WIDTH), DEFAULT_IMAGE_SEND_MAX_WIDTH),
        _positive_int(config.get("image_send_max_height", DEFAULT_IMAGE_SEND_MAX_HEIGHT), DEFAULT_IMAGE_SEND_MAX_HEIGHT),
    )


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _extension_matches_format(path: str, fmt: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if fmt == "JPEG":
        return ext in {".jpg", ".jpeg"}
    if fmt == "PNG":
        return ext == ".png"
    return False


def _resize_to_bounds(image, max_width: int, max_height: int):
    from PIL import Image

    width, height = image.size
    scale = min(max_width / max(width, 1), max_height / max(height, 1), 1.0)
    if scale >= 1.0:
        return image.copy()
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    return image.resize(new_size, getattr(Image, "Resampling", Image).LANCZOS)


def _flatten_for_jpeg(image):
    from PIL import Image

    if image.mode == "RGB":
        return image.copy()
    if image.mode in ("RGBA", "LA") or "transparency" in getattr(image, "info", {}):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _save_jpeg_under_limit(image, out_path: str, max_bytes: int) -> bool:
    candidate = image
    for quality in range(90, 25, -10):
        candidate.save(out_path, "JPEG", quality=quality, optimize=True)
        if os.path.getsize(out_path) <= max_bytes:
            return True

    while min(candidate.size) > 64:
        from PIL import Image

        current_size = os.path.getsize(out_path) if os.path.exists(out_path) else max_bytes * 2
        ratio = min(0.9, max(0.5, (max_bytes / max(current_size, 1)) ** 0.5 * 0.95))
        new_size = (max(1, int(candidate.width * ratio)), max(1, int(candidate.height * ratio)))
        if new_size == candidate.size:
            return False
        candidate = candidate.resize(new_size, getattr(Image, "Resampling", Image).LANCZOS)
        candidate.save(out_path, "JPEG", quality=70, optimize=True)
        if os.path.getsize(out_path) <= max_bytes:
            return True
    return False
