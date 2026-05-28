# encoding:utf-8

"""Hermes-derived xAI/Grok image generation provider for CowWeCom.

This module ports Hermes' ``plugins/image_gen/xai`` provider shape and the
``agent.image_gen_provider`` local-cache helpers into CowWeCom, while delegating
credentials to PR 1's Grok OAuth resolver.
"""

from __future__ import annotations

import base64
import os
import re
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse

import requests

from common.log import logger
from config import conf

from .auth import AuthError, DEFAULT_XAI_OAUTH_BASE_URL
from .media_download import new_generated_media_path, safe_download_to_file
from .xai_http import hermes_xai_user_agent, resolve_xai_http_credentials


_MODELS: Dict[str, Dict[str, Any]] = {
    "grok-imagine-image": {
        "display": "Grok Imagine Image",
        "speed": "~5-10s",
        "strengths": "Fast, high-quality",
    },
    "grok-imagine-image-quality": {
        "display": "Grok Imagine Image (Quality)",
        "speed": "~10-20s",
        "strengths": "Higher fidelity / detail; slower than the standard model.",
    },
}

DEFAULT_MODEL = "grok-imagine-image"
DEFAULT_RESOLUTION = "1k"
DEFAULT_ASPECT_RATIO = "square"

_XAI_ASPECT_RATIOS = {
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
_XAI_RESOLUTIONS = {"1k", "2k"}
_DEFAULT_TIMEOUT_SECONDS = 120.0
_DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 60.0
_MAX_IMAGE_BYTES = 25 * 1024 * 1024

_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer|basic)\s+[a-z0-9._~+/=-]+")
_TOKEN_FIELD_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|authorization_code|code|code_verifier)\b\s*[:=]\s*['\"]?[^'\"\s,;}]+"  # noqa: E501
)
_URL_RE = re.compile(r"https?://[^\s'\"<>]+")
_URL_IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}
_MAGIC_EXTENSIONS = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
)


class XaiImageGenError(RuntimeError):
    """Safe user-facing xAI image-generation error."""


class XAIImageGenProvider:
    """xAI ``grok-imagine-image`` backend adapted from Hermes."""

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

    def list_models(self):
        return [
            {
                "id": model_id,
                "display": meta.get("display", model_id),
                "speed": meta.get("speed", ""),
                "strengths": meta.get("strengths", ""),
            }
            for model_id, meta in _MODELS.items()
        ]

    def generate(
        self,
        prompt: str,
        *,
        aspect_ratio: Optional[str] = None,
        resolution: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        """Generate an image and return a local image file path."""
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            raise XaiImageGenError("Grok image prompt is empty.")

        options = {
            "model": self._resolve_model(model),
            "aspect_ratio": self._resolve_aspect_ratio(aspect_ratio),
            "resolution": self._resolve_resolution(resolution),
            "timeout": _safe_timeout(
                self._config_value("grok_image_timeout_seconds"),
                _DEFAULT_TIMEOUT_SECONDS,
            ),
            "download_timeout": _safe_timeout(
                self._config_value("grok_image_download_timeout_seconds"),
                _DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
            ),
        }
        payload = {
            "model": options["model"],
            "prompt": clean_prompt,
            "aspect_ratio": options["aspect_ratio"],
            "resolution": options["resolution"],
        }

        try:
            response = self._post_generation(payload, options, force_refresh=False)
            if response.status_code == 401:
                response = self._post_generation(payload, options, force_refresh=True)
            if response.status_code >= 400:
                raise XaiImageGenError(_safe_http_error(response))
            result = response.json()
            image_path = self._save_response_image(result, options)
            _assert_local_image(image_path)
            logger.info("[GrokImage] generated image file: %s", image_path)
            return image_path
        except AuthError as exc:
            raise XaiImageGenError(_sanitize_error_text(str(exc))) from exc
        except XaiImageGenError:
            raise
        except requests.Timeout as exc:
            raise XaiImageGenError(
                f"xAI image generation timed out ({int(options['timeout'])}s)."
            ) from exc
        except requests.ConnectionError as exc:
            raise XaiImageGenError(f"xAI image connection failed: {_sanitize_error_text(str(exc))}") from exc
        except ValueError as exc:
            raise XaiImageGenError(f"xAI image response was invalid: {_sanitize_error_text(str(exc))}") from exc
        except Exception as exc:
            raise XaiImageGenError(f"xAI image request failed: {_sanitize_error_text(str(exc))}") from exc

    def _post_generation(self, payload: Dict[str, Any], options: Dict[str, Any], *, force_refresh: bool):
        creds = resolve_xai_http_credentials(force_refresh=force_refresh)
        api_key = str(creds.get("api_key") or "").strip()
        if not api_key:
            raise AuthError(
                "Grok account is not logged in. Please complete Grok login in the Web admin page.",
                code="xai_auth_missing",
                relogin_required=True,
            )
        base_url = str(creds.get("base_url") or DEFAULT_XAI_OAUTH_BASE_URL).strip().rstrip("/")
        _ensure_oauth_xai_base_url(base_url, creds)
        return requests.post(
            f"{base_url}/images/generations",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": hermes_xai_user_agent(),
            },
            json=payload,
            timeout=options["timeout"],
        )

    def _save_response_image(self, result: Dict[str, Any], options: Dict[str, Any]) -> str:
        data = result.get("data", []) if isinstance(result, dict) else []
        if not data:
            raise XaiImageGenError("xAI returned no image data.")
        first = data[0] if isinstance(data, list) else {}
        if not isinstance(first, dict):
            raise XaiImageGenError("xAI returned malformed image data.")

        b64_data = first.get("b64_json")
        if b64_data:
            return _save_b64_image(str(b64_data), prefix=f"xai_{options['model']}")

        url = first.get("url")
        if url:
            return _save_url_image(
                str(url),
                prefix=f"xai_{options['model']}",
                timeout=options["download_timeout"],
            )

        raise XaiImageGenError("xAI response contained neither b64_json nor URL.")

    def _resolve_model(self, model: Optional[str]) -> str:
        candidate = str(
            model
            or self._config_value("grok_image_model")
            or os.environ.get("XAI_IMAGE_MODEL")
            or ""
        ).strip()
        return candidate if candidate in _MODELS else DEFAULT_MODEL

    def _resolve_resolution(self, resolution: Optional[str]) -> str:
        candidate = str(resolution or self._config_value("grok_image_resolution") or DEFAULT_RESOLUTION).strip().lower()
        return candidate if candidate in _XAI_RESOLUTIONS else DEFAULT_RESOLUTION

    def _resolve_aspect_ratio(self, aspect_ratio: Optional[str]) -> str:
        candidate = str(
            aspect_ratio
            or self._config_value("grok_image_aspect_ratio")
            or DEFAULT_ASPECT_RATIO
        ).strip().lower()
        return _XAI_ASPECT_RATIOS.get(candidate, _XAI_ASPECT_RATIOS[DEFAULT_ASPECT_RATIO])

    def _config_value(self, key: str):
        if key in self.config:
            return self.config[key]
        return conf().get(key)


def _save_b64_image(b64_data: str, *, prefix: str) -> str:
    if "," in b64_data and b64_data.lstrip().lower().startswith("data:"):
        b64_data = b64_data.split(",", 1)[1]
    raw = base64.b64decode(b64_data)
    if not raw:
        raise ValueError("Decoded image is empty.")
    extension = _extension_from_magic(raw) or ".png"
    path = _new_image_path(prefix, extension)
    with open(path, "wb") as handle:
        handle.write(raw)
    return path


def _save_url_image(url: str, *, prefix: str, timeout: float, max_bytes: int = _MAX_IMAGE_BYTES) -> str:
    return safe_download_to_file(
        url,
        prefix=prefix,
        suffix=None,
        allowed_content_types=_URL_IMAGE_CONTENT_TYPES,
        max_bytes=max_bytes,
        timeout=timeout,
    )


def _new_image_path(prefix: str, extension: str) -> str:
    safe_prefix = re.sub(r"[^a-zA-Z0-9_.-]+", "_", prefix or "xai_image").strip("._") or "xai_image"
    return new_generated_media_path(safe_prefix, extension)


def _extension_from_magic(raw: bytes) -> str:
    for magic, extension in _MAGIC_EXTENSIONS:
        if raw.startswith(magic):
            return extension
    return ""


def _assert_local_image(path: str) -> None:
    if not path or path.startswith(("http://", "https://")):
        raise XaiImageGenError("xAI image was not saved to a local file.")
    if not os.path.exists(path):
        raise XaiImageGenError("xAI image file was not created.")
    if os.path.getsize(path) <= 0:
        raise XaiImageGenError("xAI image file is empty.")


def _ensure_oauth_xai_base_url(base_url: str, creds: Dict[str, Any]) -> None:
    auth_mode = str(creds.get("auth_mode") or "").lower()
    provider = str(creds.get("provider") or "").lower()
    if "oauth" not in auth_mode and "oauth" not in provider:
        return
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "x.ai" or host.endswith(".x.ai")):
        raise XaiImageGenError("Refusing to send Grok OAuth credentials to a non-xAI endpoint.")


def _safe_timeout(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(5.0, min(parsed, 600.0))


def _safe_http_error(response: requests.Response) -> str:
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
    message = f"xAI image generation failed (HTTP {response.status_code})."
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
