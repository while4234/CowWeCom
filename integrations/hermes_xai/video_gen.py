# encoding:utf-8

"""Hermes-derived xAI/Grok video generation provider for CowWeCom.

This module keeps Hermes' ``plugins/video_gen/xai`` provider shape for
``/videos/generations`` submit/poll behavior, and adds CowWeCom-specific local
MP4 download plus Grok OAuth refresh handling.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import unquote, urlparse

import requests

from common.log import logger
from config import conf

from .auth import AuthError, DEFAULT_XAI_OAUTH_BASE_URL
from .media_download import safe_download_to_file
from .xai_http import hermes_xai_user_agent, resolve_xai_http_credentials


DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-imagine-video"
DEFAULT_DURATION = 8
DEFAULT_ASPECT_RATIO = "16:9"
DEFAULT_RESOLUTION = "720p"
DEFAULT_TIMEOUT_SECONDS = 240
DEFAULT_POLL_INTERVAL_SECONDS = 5
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 120

VALID_ASPECT_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"}
VALID_RESOLUTIONS = {"480p", "720p"}
MAX_REFERENCE_IMAGES = 7
_IMAGE_ASPECT_RATIOS = (
    ("16:9", 16 / 9),
    ("9:16", 9 / 16),
    ("4:3", 4 / 3),
    ("3:4", 3 / 4),
    ("3:2", 3 / 2),
    ("2:3", 2 / 3),
    ("1:1", 1.0),
)

_MODELS: Dict[str, Dict[str, Any]] = {
    "grok-imagine-video": {
        "display": "Grok Imagine Video",
        "speed": "~60-240s",
        "strengths": "Text-to-video + image-to-video; up to 7 reference images.",
        "modalities": ["text", "image"],
    },
}

_TERMINAL_FAILURE_STATUSES = {"failed", "error", "expired", "cancelled"}
_MAX_IMAGE_BYTES = 25 * 1024 * 1024
_MAX_VIDEO_BYTES = 512 * 1024 * 1024
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer|basic)\s+[a-z0-9._~+/=-]+")
_TOKEN_FIELD_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|authorization_code|code|code_verifier)\b\s*[:=]\s*['\"]?[^'\"\s,;}]+"  # noqa: E501
)
_URL_RE = re.compile(r"https?://[^\s'\"<>]+")
_DATA_IMAGE_RE = re.compile(r"^data:image/[^;]+;base64,", re.IGNORECASE)
_MAGIC_IMAGE_MIMES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"RIFF", "image/webp"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
_VIDEO_CONTENT_TYPES = {"video/mp4", "application/octet-stream"}


class XaiVideoGenError(RuntimeError):
    """Safe user-facing xAI video-generation error."""


class XAIVideoGenProvider:
    """xAI ``grok-imagine-video`` backend adapted from Hermes."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = dict(config or {})

    @property
    def name(self) -> str:
        return "xai"

    @property
    def display_name(self) -> str:
        return "xAI (Grok)"

    def is_available(self) -> bool:
        try:
            creds = resolve_xai_http_credentials()
            return bool(creds.get("api_key"))
        except Exception:
            return False

    def list_models(self) -> List[Dict[str, Any]]:
        return [{"id": model_id, **meta} for model_id, meta in _MODELS.items()]

    def capabilities(self) -> Dict[str, Any]:
        return {
            "modalities": ["text", "image"],
            "aspect_ratios": sorted(VALID_ASPECT_RATIOS),
            "resolutions": sorted(VALID_RESOLUTIONS),
            "max_duration": 15,
            "min_duration": 1,
            "supports_audio": False,
            "supports_negative_prompt": False,
            "max_reference_images": MAX_REFERENCE_IMAGES,
        }

    def generate(
        self,
        prompt: str,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        duration: Optional[int] = None,
        aspect_ratio: Optional[str] = None,
        resolution: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        poll_interval_seconds: Optional[int] = None,
    ) -> str:
        """Generate a video and return a local MP4 file path."""
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            raise XaiVideoGenError("Grok video prompt is empty.")

        if isinstance(image_url, (list, tuple)) and not reference_image_urls:
            image_values = [str(item or "").strip() for item in image_url if str(item or "").strip()]
            if len(image_values) == 1:
                image_url = image_values[0]
            elif image_values:
                reference_image_urls = image_values
                image_url = None
        refs, ref_dimensions = _normalize_reference_images(reference_image_urls)
        image_url_norm = None
        image_dimensions = None
        if image_url:
            image_url_norm, image_dimensions = _normalize_media_reference(image_url)
        if image_url_norm and refs:
            raise XaiVideoGenError("image_url and reference_image_urls cannot be combined on xAI.")
        if refs and len(refs) > MAX_REFERENCE_IMAGES:
            raise XaiVideoGenError(f"xAI video supports at most {MAX_REFERENCE_IMAGES} reference images.")
        has_reference_images = bool(image_url_norm or refs)

        options = {
            "model": self._resolve_model(model),
            "duration": _clamp_duration(duration, has_reference_images=has_reference_images),
            "aspect_ratio": self._resolve_aspect_ratio(
                aspect_ratio,
                has_reference_images=has_reference_images,
                reference_dimensions=image_dimensions or _first_dimensions(ref_dimensions),
            ),
            "resolution": self._resolve_resolution(resolution),
            "timeout": _bounded_float(
                timeout_seconds
                if timeout_seconds is not None
                else self._config_value("grok_video_timeout_seconds"),
                DEFAULT_TIMEOUT_SECONDS,
                minimum=1.0,
                maximum=900.0,
            ),
            "poll_interval": _bounded_float(
                poll_interval_seconds
                if poll_interval_seconds is not None
                else self._config_value("grok_video_poll_interval_seconds"),
                DEFAULT_POLL_INTERVAL_SECONDS,
                minimum=0.01,
                maximum=60.0,
            ),
            "download_timeout": _bounded_float(
                self._config_value("grok_video_download_timeout_seconds"),
                DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
                minimum=1.0,
                maximum=600.0,
            ),
        }
        payload: Dict[str, Any] = {
            "model": options["model"],
            "prompt": clean_prompt,
            "duration": options["duration"],
            "aspect_ratio": options["aspect_ratio"],
            "resolution": options["resolution"],
        }
        if image_url_norm:
            payload["image"] = {"url": image_url_norm}
        if refs:
            payload["reference_images"] = refs
        logger.info(
            "[GrokVideo] submitting request: has_image=%s reference_count=%s model=%s",
            bool(image_url_norm),
            len(refs or []),
            options["model"],
        )

        try:
            request_id = self._submit(payload)
            body = self._poll(
                request_id,
                timeout_seconds=float(options["timeout"]),
                poll_interval=float(options["poll_interval"]),
            )
            video_url = _extract_video_url(body)
            video_path = _save_url_video(
                video_url,
                prefix=f"xai_{options['model']}",
                timeout=float(options["download_timeout"]),
            )
            _assert_local_video(video_path)
            logger.info("[GrokVideo] generated video file: %s", video_path)
            return video_path
        except AuthError as exc:
            raise XaiVideoGenError(_sanitize_error_text(str(exc))) from exc
        except XaiVideoGenError:
            raise
        except requests.Timeout as exc:
            raise XaiVideoGenError(f"xAI video request timed out: {_sanitize_error_text(str(exc))}") from exc
        except requests.ConnectionError as exc:
            raise XaiVideoGenError(f"xAI video connection failed: {_sanitize_error_text(str(exc))}") from exc
        except ValueError as exc:
            raise XaiVideoGenError(f"xAI video response was invalid: {_sanitize_error_text(str(exc))}") from exc
        except Exception as exc:
            raise XaiVideoGenError(f"xAI video request failed: {_sanitize_error_text(str(exc))}") from exc

    def _submit(self, payload: Dict[str, Any]) -> str:
        response = self._post_generation(payload, force_refresh=False)
        if response.status_code == 401:
            response = self._post_generation(payload, force_refresh=True)
        if response.status_code >= 400:
            raise XaiVideoGenError(_safe_http_error(response, "generation submit"))
        body = response.json()
        request_id = body.get("request_id") if isinstance(body, dict) else None
        if not request_id:
            raise XaiVideoGenError("xAI video response did not include request_id.")
        return str(request_id)

    def _poll(self, request_id: str, *, timeout_seconds: float, poll_interval: float) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last_status = "queued"
        while time.monotonic() < deadline:
            response = self._get_status(request_id, force_refresh=False)
            if response.status_code == 401:
                response = self._get_status(request_id, force_refresh=True)
            if response.status_code >= 400:
                raise XaiVideoGenError(_safe_http_error(response, "generation poll"))

            body = response.json()
            if not isinstance(body, dict):
                raise XaiVideoGenError("xAI video poll response was malformed.")
            last_status = str(body.get("status") or "").lower()
            if last_status == "done":
                return body
            if last_status in _TERMINAL_FAILURE_STATUSES:
                message = (
                    ((body.get("error") or {}) if isinstance(body.get("error"), dict) else {}).get("message")
                    or body.get("message")
                    or f"xAI video generation ended with status '{last_status}'."
                )
                raise XaiVideoGenError(_sanitize_error_text(message))
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
        raise XaiVideoGenError(f"Timed out waiting for video generation after {int(timeout_seconds)}s.")

    def _post_generation(self, payload: Dict[str, Any], *, force_refresh: bool):
        creds = self._resolve_credentials(force_refresh=force_refresh)
        return requests.post(
            f"{creds['base_url']}/videos/generations",
            headers={
                **_xai_headers(creds["api_key"]),
                "x-idempotency-key": str(uuid.uuid4()),
            },
            json=payload,
            timeout=60,
        )

    def _get_status(self, request_id: str, *, force_refresh: bool):
        creds = self._resolve_credentials(force_refresh=force_refresh)
        return requests.get(
            f"{creds['base_url']}/videos/{request_id}",
            headers=_xai_headers(creds["api_key"]),
            timeout=30,
        )

    def _resolve_credentials(self, *, force_refresh: bool) -> Dict[str, str]:
        creds = resolve_xai_http_credentials(force_refresh=force_refresh)
        api_key = str(creds.get("api_key") or "").strip()
        if not api_key:
            raise AuthError(
                "Grok account is not logged in. Please complete Grok login in the Web admin page.",
                code="xai_auth_missing",
                relogin_required=True,
            )
        base_url = str(creds.get("base_url") or DEFAULT_XAI_OAUTH_BASE_URL or DEFAULT_XAI_BASE_URL).strip().rstrip("/")
        _ensure_oauth_xai_base_url(base_url, creds)
        return {"api_key": api_key, "base_url": base_url or DEFAULT_XAI_BASE_URL}

    def _resolve_model(self, model: Optional[str]) -> str:
        candidate = str(model or self._config_value("grok_video_model") or os.environ.get("XAI_VIDEO_MODEL") or "").strip()
        return candidate if candidate in _MODELS else DEFAULT_MODEL

    def _resolve_resolution(self, resolution: Optional[str]) -> str:
        candidate = str(resolution or self._config_value("grok_video_resolution") or DEFAULT_RESOLUTION).strip().lower()
        return candidate if candidate in VALID_RESOLUTIONS else DEFAULT_RESOLUTION

    def _resolve_aspect_ratio(
        self,
        aspect_ratio: Optional[str],
        *,
        has_reference_images: bool = False,
        reference_dimensions: Optional[tuple[int, int]] = None,
    ) -> str:
        explicit = str(aspect_ratio or "").strip()
        if has_reference_images and not explicit:
            inferred = _infer_aspect_ratio(reference_dimensions)
            if inferred:
                return inferred
        candidate = str(explicit or self._config_value("grok_video_aspect_ratio") or DEFAULT_ASPECT_RATIO).strip()
        if candidate in VALID_ASPECT_RATIOS:
            return candidate
        inferred = _infer_aspect_ratio(reference_dimensions) if has_reference_images else None
        return inferred or DEFAULT_ASPECT_RATIO

    def _config_value(self, key: str):
        if key in self.config:
            return self.config[key]
        return conf().get(key)


def _xai_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": hermes_xai_user_agent(),
    }


def _normalize_reference_images(
    reference_image_urls: Optional[List[str]],
) -> tuple[Optional[List[Dict[str, str]]], List[tuple[int, int]]]:
    refs = []
    dimensions = []
    for url in reference_image_urls or []:
        normalized, size = _normalize_media_reference(url)
        if normalized:
            refs.append({"url": normalized})
        if size:
            dimensions.append(size)
    return refs or None, dimensions


def _normalize_media_reference(value: Any) -> tuple[str, Optional[tuple[int, int]]]:
    source = str(value or "").strip().strip("'\"")
    if not source:
        return "", None
    lowered = source.lower()
    if lowered.startswith(("http://", "https://")) or _DATA_IMAGE_RE.match(source):
        return source, None
    path = _local_path_from_reference(source)
    if not path or not os.path.isfile(path):
        return source, None
    size = os.path.getsize(path)
    if size <= 0:
        raise XaiVideoGenError(f"Reference image is empty: {path}")
    if size > _MAX_IMAGE_BYTES:
        raise XaiVideoGenError(f"Reference image exceeds {_MAX_IMAGE_BYTES // (1024 * 1024)}MB: {path}")
    with open(path, "rb") as handle:
        raw = handle.read()
    mime = _guess_image_mime(raw, path)
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}", _image_dimensions(path)


def _image_dimensions(path: str) -> Optional[tuple[int, int]]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
        if width > 0 and height > 0:
            return int(width), int(height)
    except Exception as exc:
        logger.debug("[GrokVideo] failed to read reference image dimensions for %s: %s", path, exc)
    return None


def _first_dimensions(values: List[tuple[int, int]]) -> Optional[tuple[int, int]]:
    return values[0] if values else None


def _infer_aspect_ratio(dimensions: Optional[tuple[int, int]]) -> Optional[str]:
    if not dimensions:
        return None
    width, height = dimensions
    if width <= 0 or height <= 0:
        return None
    ratio = width / height
    return min(_IMAGE_ASPECT_RATIOS, key=lambda item: abs(item[1] - ratio))[0]


def _local_path_from_reference(source: str) -> str:
    if source.lower().startswith("file://"):
        parsed = urlparse(source)
        path = unquote(parsed.path or source[7:])
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        return path
    return os.path.abspath(os.path.expanduser(source)) if os.path.exists(source) else source


def _guess_image_mime(raw: bytes, path: str) -> str:
    for magic, mime in _MAGIC_IMAGE_MIMES:
        if raw.startswith(magic):
            return mime
    mime = mimetypes.guess_type(path)[0]
    return mime if mime and mime.startswith("image/") else "image/png"


def _clamp_duration(duration: Optional[int], has_reference_images: bool) -> int:
    try:
        if isinstance(duration, str):
            match = re.search(r"\d+", duration)
            value = int(match.group(0)) if match else DEFAULT_DURATION
        else:
            value = int(duration) if duration is not None else DEFAULT_DURATION
    except (TypeError, ValueError):
        value = DEFAULT_DURATION
    if value < 1:
        value = 1
    if value > 15:
        value = 15
    if has_reference_images and value > 10:
        value = 10
    return value


def _extract_video_url(body: Dict[str, Any]) -> str:
    video = body.get("video") if isinstance(body, dict) else None
    if isinstance(video, dict):
        url = video.get("url")
        if url:
            return str(url)
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("url"):
                return str(item["url"])
    raise XaiVideoGenError("xAI video generation completed without a video URL.")


def _save_url_video(url: str, *, prefix: str, timeout: float, max_bytes: int = _MAX_VIDEO_BYTES) -> str:
    return safe_download_to_file(
        url,
        prefix=prefix,
        suffix=".mp4",
        allowed_content_types=_VIDEO_CONTENT_TYPES,
        max_bytes=max_bytes,
        timeout=timeout,
    )


def _assert_local_video(path: str) -> None:
    if not path or path.startswith(("http://", "https://")):
        raise XaiVideoGenError("xAI video was not saved to a local file.")
    if not os.path.exists(path):
        raise XaiVideoGenError("xAI video file was not created.")
    if os.path.getsize(path) <= 0:
        raise XaiVideoGenError("xAI video file is empty.")


def _ensure_oauth_xai_base_url(base_url: str, creds: Dict[str, Any]) -> None:
    auth_mode = str(creds.get("auth_mode") or "").lower()
    provider = str(creds.get("provider") or "").lower()
    if "oauth" not in auth_mode and "oauth" not in provider:
        return
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "x.ai" or host.endswith(".x.ai")):
        raise XaiVideoGenError("Refusing to send Grok OAuth credentials to a non-xAI endpoint.")


def _bounded_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _safe_http_error(response: requests.Response, action: str) -> str:
    body = ""
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            body = str(error.get("message") or "")
        if not body:
            body = str(payload)[:500]
    except Exception:
        try:
            body = response.text[:500]
        except Exception:
            body = ""
    message = f"xAI video {action} failed (HTTP {response.status_code})."
    if body:
        message += f" {_sanitize_error_text(body)}"
    return message


def _sanitize_error_text(value: Any, extra_secrets: Optional[Iterable[str]] = None) -> str:
    text = str(value or "")
    text = _AUTH_HEADER_RE.sub(r"\1<redacted>", text)
    text = _TOKEN_FIELD_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = _URL_RE.sub("<redacted-url>", text)
    for secret in extra_secrets or []:
        secret_text = str(secret or "").strip()
        if secret_text:
            text = text.replace(secret_text, "<redacted>")
    return text[:800]
