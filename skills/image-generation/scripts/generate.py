#!/usr/bin/env python3
"""
Unified image generation script.

Usage:
    python generate.py '<json_args>'

When runtime is "codex_broker", this script is broker-only: it calls the
configured local broker command and will not fall back to API providers.

Standalone API-provider mode still supports these model families when broker
runtime is not enabled:

    - gpt-image-2 / gpt-image-1                    鈫?OpenAI
    - nano-banana / gemini-*-image-*               鈫?Gemini
    - doubao-seedream-* / seedream-*               鈫?Seedream (Volcengine Ark)
    - qwen-image-2.0 / qwen-image-2.0-pro / etc.   鈫?Qwen (DashScope)
    - image-01 / minimax-image                     鈫?MiniMax
    - any model                                    鈫?LinkAI (universal proxy)

Dependencies: requests (stdlib: json, sys, os, base64, io, abc, uuid, pathlib, urllib)
"""

import json
import sys
import os
import base64
import io
import logging
import time
import uuid
import re
import shlex
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import urlparse
from urllib.error import URLError

try:
    import requests

    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

_CODEX_SESSION_AUTH_VALUES = {
    "codex",
    "codex_auth",
    "codex_builtin",
    "codex_session",
    "codex_login",
    "chatgpt",
    "openai_account",
}

_CODEX_AUTH_RUNTIMES = {
    "codex",
    "codex_auth",
    "codex-auth",
    "codex_session",
    "codex-session",
    "codex_login",
    "codex-login",
}

_GROK_RUNTIMES = {
    "grok",
    "xai",
    "x.ai",
    "grok_auth",
    "grok-auth",
    "xai_oauth",
    "xai-oauth",
}

_GROK_SPEED_MODEL = "grok-imagine-image"
_GROK_QUALITY_MODEL = "grok-imagine-image-quality"
_DETACHED_COWWECOM_LOG_STREAMS = []
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

_DATA_IMAGE_RE = re.compile(r"^data:image/[^;]+;base64,(.+)$", re.DOTALL)


# ---------------------------------------------------------------------------
# Size / aspect-ratio resolution
# ---------------------------------------------------------------------------

_SIZE_TABLE = {
    # (tier, ratio) -> "WxH"
    ("1K", "1:1"): "1024x1024",
    ("1K", "3:2"): "1536x1024",
    ("1K", "2:3"): "1024x1536",
    ("2K", "1:1"): "2048x2048",
    ("2K", "16:9"): "2048x1152",
    ("2K", "9:16"): "1152x2048",
    ("4K", "16:9"): "3840x2160",
    ("4K", "9:16"): "2160x3840",
}

_TIER_ORDER = ["1K", "2K", "4K"]
_RATIO_DEFAULT = {"1K": "1:1", "2K": "1:1", "4K": "16:9"}

_PIXEL_RE = re.compile(r"^\d+x\d+$")


def resolve_size(size: str | None, aspect_ratio: str | None) -> str | None:
    """Resolve (size, aspect_ratio) to a concrete 'WxH' string or None."""
    if size and _PIXEL_RE.match(size):
        return size
    if size and size.lower() == "auto":
        size = None
    if not size and not aspect_ratio:
        return None

    tier = size.upper() if size else None
    ratio = aspect_ratio

    if tier and ratio:
        key = (tier, ratio)
        if key in _SIZE_TABLE:
            return _SIZE_TABLE[key]
        # Upgrade: try higher tiers with same ratio
        start = _TIER_ORDER.index(tier) + 1 if tier in _TIER_ORDER else 0
        for t in _TIER_ORDER[start:]:
            if (t, ratio) in _SIZE_TABLE:
                return _SIZE_TABLE[(t, ratio)]
        # Cross-tier: any tier with this ratio
        for t in _TIER_ORDER:
            if (t, ratio) in _SIZE_TABLE:
                return _SIZE_TABLE[(t, ratio)]
        # Tier default
        if tier in _RATIO_DEFAULT:
            return _SIZE_TABLE.get((tier, _RATIO_DEFAULT[tier]))

    if tier and not ratio:
        default_ratio = _RATIO_DEFAULT.get(tier)
        if default_ratio:
            return _SIZE_TABLE.get((tier, default_ratio))

    if ratio and not tier:
        for t in _TIER_ORDER:
            if (t, ratio) in _SIZE_TABLE:
                return _SIZE_TABLE[(t, ratio)]

    return None


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def _load_image(source: str) -> bytes:
    """Load image from a local file path or URL."""
    if source.startswith("file://"):
        parsed = urlparse(source)
        source = parsed.path or source[7:]
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", source):
            source = source[1:]
    if os.path.isfile(source):
        with open(source, "rb") as f:
            return f.read()
    if _HAS_REQUESTS:
        resp = requests.get(source, timeout=60)
        resp.raise_for_status()
        return resp.content
    req = Request(source)
    with urlopen(req, timeout=60) as resp:
        return resp.read()


def _compress_image(data: bytes, max_bytes: int = 4 * 1024 * 1024, max_edge: int = 4096) -> bytes:
    """Compress image to fit size/dimension limits. Requires Pillow only when needed."""
    if len(data) <= max_bytes:
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(data))
            w, h = img.size
            if max(w, h) <= max_edge:
                return data
        except ImportError:
            return data
        except Exception:
            return data

    try:
        from PIL import Image
    except ImportError:
        return data

    img = Image.open(io.BytesIO(data))
    w, h = img.size

    if max(w, h) > max_edge:
        ratio = max_edge / max(w, h)
        w, h = int(w * ratio), int(h * ratio)
        img = img.resize((w, h), Image.LANCZOS)

    buf = io.BytesIO()
    fmt = img.format or "PNG"
    if fmt.upper() == "JPEG":
        quality = 85
        while True:
            buf.seek(0)
            buf.truncate()
            img.save(buf, format="JPEG", quality=quality)
            if buf.tell() <= max_bytes or quality <= 20:
                break
            quality -= 10
    else:
        img.save(buf, format=fmt)
        if buf.tell() > max_bytes:
            buf.seek(0)
            buf.truncate()
            img.save(buf, format="JPEG", quality=75)
    return buf.getvalue()


def _save_image(data: bytes, output_dir: str) -> str:
    """Save image bytes to output_dir and return the path."""
    os.makedirs(output_dir, exist_ok=True)
    ext = "png"
    if data[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    elif data[:4] == b"RIFF":
        ext = "webp"
    filename = f"{uuid.uuid4().hex[:12]}.{ext}"
    path = os.path.join(output_dir, filename)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _save_result_image(source, output_dir: str) -> str:
    """Normalize a provider/broker image result to a local file path."""
    if not source:
        raise RuntimeError("empty image result")
    if isinstance(source, bytes):
        return _save_image(source, output_dir)
    if not isinstance(source, str):
        raise RuntimeError(f"unsupported image result type: {type(source).__name__}")

    match = _DATA_IMAGE_RE.match(source)
    if match:
        return _save_image(base64.b64decode(match.group(1)), output_dir)
    if os.path.isfile(source):
        return os.path.abspath(source)
    return _save_image(_load_image(source), output_dir)


def _normalize_call_args(args: dict) -> dict:
    """Accept the compact Codex image prompt shape plus legacy script args."""
    normalized = dict(args)
    if not normalized.get("prompt"):
        for key in ("input", "text"):
            value = normalized.get(key)
            if isinstance(value, str) and value.strip():
                normalized["prompt"] = value
                break

    if not normalized.get("image_url"):
        for key in ("input_images", "reference_images", "images"):
            value = normalized.get(key)
            if value:
                normalized["image_url"] = value
                break
    return normalized


def _normalized_token(value) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


_BROKER_ONLY_RUNTIMES = {
    "broker",
    "codex_broker",
    "codex-broker",
    "external_broker",
    "external-broker",
    "local_broker",
    "local-broker",
}


def _runtime_from_args_or_env(args: dict) -> str:
    return (
        str(
            args.get("runtime")
            or os.environ.get("SKILL_IMAGE_GENERATION_RUNTIME")
            or os.environ.get("IMAGE_GENERATION_RUNTIME")
            or ""
        )
        .strip()
        .lower()
    )


def _is_broker_only_runtime(runtime: str) -> bool:
    return runtime in _BROKER_ONLY_RUNTIMES


def _is_codex_auth_runtime(runtime: str) -> bool:
    return runtime in _CODEX_AUTH_RUNTIMES


def _is_grok_runtime(runtime: str) -> bool:
    return runtime in _GROK_RUNTIMES


def _requests_codex_session_auth(args: dict) -> bool:
    auth_source = _normalized_token(args.get("auth_source") or args.get("auth"))
    provider = _normalized_token(args.get("provider"))
    return (
        auth_source in _CODEX_SESSION_AUTH_VALUES
        or provider in {"codex", "codex_builtin", "image_gen"}
    )


def _codex_session_auth_error() -> str:
    return (
        "Codex session auth was requested, but this runtime is not configured "
        "for direct Codex-auth image generation. Set runtime=codex_auth or "
        "SKILL_IMAGE_GENERATION_RUNTIME=codex_auth."
    )


def _broker_command_from_env():
    command_json = (
        os.environ.get("IMAGE_GENERATION_BROKER_COMMAND_JSON")
        or os.environ.get("SKILL_IMAGE_GENERATION_BROKER_COMMAND_JSON")
        or ""
    )
    if command_json:
        try:
            parsed = json.loads(command_json)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid IMAGE_GENERATION_BROKER_COMMAND_JSON: {e}")
        if not isinstance(parsed, list) or not parsed:
            raise RuntimeError("IMAGE_GENERATION_BROKER_COMMAND_JSON must be a non-empty JSON list")
        return [str(part) for part in parsed]

    command = (
        os.environ.get("IMAGE_GENERATION_BROKER_COMMAND")
        or os.environ.get("SKILL_IMAGE_GENERATION_BROKER_COMMAND")
        or os.environ.get("CODEX_IMAGE_GEN_COMMAND")
        or ""
    ).strip()
    if not command:
        return None
    return [
        part.strip('"')
        for part in shlex.split(command, posix=(os.name != "nt"))
        if part.strip('"')
    ]


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

class ImageProvider(ABC):
    """Abstract base class for image generation providers."""

    @abstractmethod
    def generate(
        self,
        prompt: str,
        *,
        image_url: str | list | None = None,
        quality: str | None = None,
        size: str | None = None,
        aspect_ratio: str | None = None,
        output_dir: str = ".",
    ) -> list[str]:
        """Generate image(s) and return list of local file paths.

        `size` may be a tier ("1K" / "2K" / "4K" / "512") or pixels ("WxH").
        Providers that need pixel sizes should call `resolve_size(size, aspect_ratio)`.
        """
        ...


# ---------------------------------------------------------------------------
# Grok / xAI account provider
# ---------------------------------------------------------------------------

def _ensure_project_root_on_path() -> None:
    candidates = [
        os.environ.get("COWWECHAT_ROOT"),
        str(Path(__file__).resolve().parents[3]) if len(Path(__file__).resolve().parents) > 3 else "",
        os.getcwd(),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        root = Path(candidate).expanduser().resolve()
        if (root / "integrations" / "hermes_xai" / "image_gen.py").exists():
            root_str = str(root)
            if root_str not in sys.path:
                sys.path.insert(0, root_str)
            return


def _route_cowwecom_console_logs_to_stderr() -> None:
    try:
        from common.log import logger as cow_logger
    except Exception:
        return
    for handler in getattr(cow_logger, "handlers", []):
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            old_stream = getattr(handler, "stream", None)
            if old_stream is not None and old_stream not in {sys.stderr, sys.__stderr__}:
                _DETACHED_COWWECOM_LOG_STREAMS.append(old_stream)
            handler.stream = sys.__stderr__


def _resolve_grok_model(prompt: str, quality: str | None = None, model: str | None = None) -> str:
    explicit_model = (model or "").strip()
    if explicit_model in {_GROK_SPEED_MODEL, _GROK_QUALITY_MODEL}:
        return explicit_model

    quality_hint = str(quality or "").strip().lower()
    if quality_hint in _GROK_HIGH_QUALITY_HINTS:
        return _GROK_QUALITY_MODEL
    if quality_hint in _GROK_SPEED_QUALITY_HINTS:
        return _GROK_SPEED_MODEL

    haystack = str(prompt or "").lower()
    if _has_grok_quality_phrase(haystack):
        return _GROK_QUALITY_MODEL
    return explicit_model or _GROK_SPEED_MODEL


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


def _resolve_grok_resolution(size: str | None) -> str | None:
    value = str(size or "").strip().lower()
    if not value or value == "auto":
        return None
    if "2k" in value or "2048" in value:
        return "2k"
    if "1k" in value or "1024" in value:
        return "1k"
    return None


class GrokXAIProvider(ImageProvider):
    """Thin skill adapter around the Hermes-derived xAI image provider."""

    def __init__(self, model: str = ""):
        self.model = model or ""

    def generate(
        self,
        prompt: str,
        *,
        image_url: str | list | None = None,
        quality: str | None = None,
        size: str | None = None,
        aspect_ratio: str | None = None,
        output_dir: str = ".",
    ) -> list[str]:
        if image_url:
            raise RuntimeError(
                "Grok/xAI image generation currently supports text-to-image only in this skill. "
                "Use the default Codex image-generation runtime for image editing or image fusion."
            )

        _ensure_project_root_on_path()
        from integrations.hermes_xai.image_gen import XAIImageGenProvider

        _route_cowwecom_console_logs_to_stderr()
        selected_model = _resolve_grok_model(prompt, quality=quality, model=self.model)
        self.model = selected_model
        generated_path = Path(
            XAIImageGenProvider().generate(
                prompt,
                aspect_ratio=aspect_ratio,
                resolution=_resolve_grok_resolution(size),
                model=selected_model,
            )
        ).expanduser().resolve()
        if not generated_path.exists() or generated_path.stat().st_size <= 0:
            raise RuntimeError("Grok/xAI image generation returned an empty local file.")

        output = Path(output_dir).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        suffix = generated_path.suffix or ".png"
        dest = output / f"grok-image-{uuid.uuid4().hex[:8]}{suffix}"
        shutil.copyfile(generated_path, dest)
        if dest.stat().st_size <= 0:
            raise RuntimeError("Grok/xAI image generation copied an empty output file.")
        return [str(dest)]


# ---------------------------------------------------------------------------
# OpenAI-compatible provider (gpt-image-2, gpt-image-1)
# ---------------------------------------------------------------------------

class OpenAIProvider(ImageProvider):
    """Provider for OpenAI Image API (generations + edits)."""

    DEFAULT_MODEL = "gpt-image-2"
    RESPONSES_IMAGE_SIZES = {"auto", "1024x1024", "1024x1536", "1536x1024"}
    RESPONSES_IMAGE_QUALITIES = {"auto", "low", "medium", "high"}

    def __init__(self, api_key: str, api_base: str, model: str):
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model = model or self.DEFAULT_MODEL

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
        }

    @staticmethod
    def _raise_for_api_error(resp):
        """Raise with server error details instead of bare HTTP status."""
        if resp.status_code >= 400:
            try:
                body = resp.json()
                msg = body.get("error", {}).get("message") or body.get("message") or resp.text
            except Exception:
                msg = resp.text or resp.reason
            raise RuntimeError(f"API {resp.status_code}: {msg} (url: {resp.url})")

    def _post_json(self, url: str, payload: dict) -> dict:
        headers = {**self._headers(), "Content-Type": "application/json"}
        if _HAS_REQUESTS:
            resp = requests.post(url, headers=headers, json=payload, timeout=300)
            self._raise_for_api_error(resp)
            return resp.json()
        data = json.dumps(payload).encode()
        req = Request(url, data=data, headers=headers, method="POST")
        with urlopen(req, timeout=300) as r:
            return json.loads(r.read())

    def _post_sse_json_events(self, url: str, payload: dict) -> list[dict]:
        """POST a streaming Responses request and parse JSON SSE data events."""
        headers = {**self._headers(), "Content-Type": "application/json"}
        events: list[dict] = []
        if _HAS_REQUESTS:
            resp = requests.post(url, headers=headers, json=payload, timeout=300, stream=True)
            self._raise_for_api_error(resp)
            for raw_line in resp.iter_lines(decode_unicode=True):
                event = self._parse_sse_data_line(raw_line)
                if event is not None:
                    events.append(event)
            return events

        data = json.dumps(payload).encode()
        req = Request(url, data=data, headers=headers, method="POST")
        with urlopen(req, timeout=300) as r:
            for raw_line in r:
                event = self._parse_sse_data_line(raw_line)
                if event is not None:
                    events.append(event)
        return events

    @staticmethod
    def _parse_sse_data_line(line: str | bytes | None) -> dict | None:
        if not line:
            return None
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        line = line.strip()
        if not line.startswith("data:"):
            return None
        data = line[5:].strip()
        if not data or data == "[DONE]":
            return None
        parsed = json.loads(data)
        if isinstance(parsed, dict):
            return parsed
        return None

    def _post_multipart(self, url: str, fields: dict, files: list[tuple]) -> dict:
        """POST multipart/form-data using requests (or fall back to urllib)."""
        headers = self._headers()
        if _HAS_REQUESTS:
            resp = requests.post(url, headers=headers, data=fields, files=files, timeout=300)
            self._raise_for_api_error(resp)
            return resp.json()
        boundary = uuid.uuid4().hex
        body = b""
        for key, val in fields.items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{val}\r\n".encode()
        for field_name, (filename, filedata, content_type) in files:
            body += (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"{field_name}\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode() + filedata + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        req = Request(url, data=body, headers=headers, method="POST")
        with urlopen(req, timeout=300) as r:
            return json.loads(r.read())

    def generate(
        self,
        prompt: str,
        *,
        image_url=None,
        quality: str | None = None,
        size: str | None = None,
        aspect_ratio: str | None = None,
        output_dir: str = ".",
    ) -> list[str]:
        # OpenAI Images API expects pixel size like 1024x1024.
        resolved = resolve_size(size, aspect_ratio) if (size or aspect_ratio) else None
        if image_url:
            if self._use_responses_api():
                return self._edit_with_responses(
                    prompt,
                    image_url=image_url,
                    quality=quality,
                    size=resolved,
                    output_dir=output_dir,
                )
            return self._edit(prompt, image_url=image_url, quality=quality, size=resolved, output_dir=output_dir)
        return self._create(prompt, quality=quality, size=resolved, output_dir=output_dir)

    def _create(self, prompt: str, *, quality: str | None, size: str | None, output_dir: str) -> list[str]:
        if self._use_responses_api():
            return self._create_with_responses(prompt, quality=quality, size=size, output_dir=output_dir)

        url = f"{self.api_base}/images/generations"
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
        }
        if quality:
            payload["quality"] = quality
        if size:
            payload["size"] = size
        result = self._post_json(url, payload)
        return self._save_results(result, output_dir)

    def _use_responses_api(self) -> bool:
        raw = (
            os.environ.get("OPENAI_WIRE_API")
            or os.environ.get("SKILL_IMAGE_GENERATION_OPENAI_WIRE_API")
            or os.environ.get("SKILL_IMAGE_GENERATION_WIRE_API")
            or ""
        ).strip().lower()
        return raw in {"response", "responses"}

    def _responses_model(self) -> str:
        return (
            os.environ.get("SKILL_IMAGE_GENERATION_RESPONSES_MODEL")
            or os.environ.get("OPENAI_RESPONSES_MODEL")
            or os.environ.get("OPENAI_MODEL")
            or "gpt-5"
        )

    def _responses_reasoning_effort(self) -> str | None:
        raw = (
            os.environ.get("SKILL_IMAGE_GENERATION_REASONING_EFFORT")
            or os.environ.get("OPENAI_REASONING_EFFORT")
            or ""
        ).strip().lower()
        if raw == "max":
            raw = "xhigh"
        if raw in {"none", "low", "medium", "high", "xhigh"}:
            return raw
        return None

    def _create_with_responses(
        self,
        prompt: str,
        *,
        quality: str | None,
        size: str | None,
        output_dir: str,
    ) -> list[str]:
        url = f"{self.api_base}/responses"
        tool = {"type": "image_generation"}
        if size in self.RESPONSES_IMAGE_SIZES:
            tool["size"] = size
        if quality in self.RESPONSES_IMAGE_QUALITIES:
            tool["quality"] = quality

        payload = {
            "model": self._responses_model(),
            "input": [
                {
                    "role": "user",
                    "content": self._responses_input_content(prompt),
                }
            ],
            "tools": [tool],
            "tool_choice": {"type": "image_generation"},
            "stream": True,
        }
        reasoning_effort = self._responses_reasoning_effort()
        if reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}
        if os.environ.get("OPENAI_DISABLE_RESPONSE_STORAGE", "").lower() in {"1", "true", "yes"}:
            payload["store"] = False

        events = self._post_sse_json_events(url, payload)
        return self._save_responses_results({"output": events}, output_dir)

    def _edit_with_responses(
        self,
        prompt: str,
        *,
        image_url,
        quality: str | None,
        size: str | None,
        output_dir: str,
    ) -> list[str]:
        url = f"{self.api_base}/responses"
        tool = {"type": "image_generation"}
        if size in self.RESPONSES_IMAGE_SIZES:
            tool["size"] = size
        if quality in self.RESPONSES_IMAGE_QUALITIES:
            tool["quality"] = quality

        payload = {
            "model": self._responses_model(),
            "input": [
                {
                    "role": "user",
                    "content": self._responses_input_content(prompt, image_url=image_url),
                }
            ],
            "tools": [tool],
            "tool_choice": {"type": "image_generation"},
            "stream": True,
        }
        reasoning_effort = self._responses_reasoning_effort()
        if reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}
        if os.environ.get("OPENAI_DISABLE_RESPONSE_STORAGE", "").lower() in {"1", "true", "yes"}:
            payload["store"] = False

        events = self._post_sse_json_events(url, payload)
        return self._save_responses_results({"output": events}, output_dir)

    @staticmethod
    def _responses_input_content(prompt: str, image_url=None) -> list[dict]:
        content = [{"type": "input_text", "text": prompt}]
        if not image_url:
            return content

        urls = image_url if isinstance(image_url, list) else [image_url]
        for url in urls:
            content.append({
                "type": "input_image",
                "image_url": OpenAIProvider._responses_image_url(url),
            })
        return content

    @staticmethod
    def _responses_image_url(source: str) -> str:
        if _DATA_IMAGE_RE.match(source) or source.startswith(("http://", "https://")):
            return source
        data = _compress_image(_load_image(source))
        mime = _guess_mime(data)
        encoded = base64.b64encode(data).decode()
        return f"data:{mime};base64,{encoded}"

    def _edit(
        self,
        prompt: str,
        *,
        image_url,
        quality: str | None,
        size: str | None,
        output_dir: str,
    ) -> list[str]:
        urls = image_url if isinstance(image_url, list) else [image_url]
        image_data_list = [_compress_image(_load_image(u)) for u in urls]

        url = f"{self.api_base}/images/edits"

        fields = {"model": self.model, "prompt": prompt}
        if quality:
            fields["quality"] = quality
        if size:
            fields["size"] = size

        files = []
        for i, img_bytes in enumerate(image_data_list):
            ext = "png"
            if img_bytes[:3] == b"\xff\xd8\xff":
                ext = "jpg"
            field_name = "image[]" if len(image_data_list) > 1 else "image"
            files.append((field_name, (f"image_{i}.{ext}", img_bytes, f"image/{ext}")))

        result = self._post_multipart(url, fields, files)
        return self._save_results(result, output_dir)

    @staticmethod
    def _save_results(result: dict, output_dir: str) -> list[str]:
        paths = []
        for item in result.get("data", []):
            if "b64_json" in item:
                raw = base64.b64decode(item["b64_json"])
                paths.append(_save_image(raw, output_dir))
            elif "url" in item:
                raw = _load_image(item["url"])
                paths.append(_save_image(raw, output_dir))
        return paths

    @staticmethod
    def _save_responses_results(result: dict, output_dir: str) -> list[str]:
        paths: list[str] = []
        candidates = []
        if isinstance(result.get("response"), dict):
            candidates.extend(result["response"].get("output") or [])
        candidates.extend(result.get("output") or [])
        for item in candidates:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "image_generation_call":
                nested = item.get("item")
                if isinstance(nested, dict) and nested.get("type") == "image_generation_call":
                    item = nested
                else:
                    continue
            image_b64 = item.get("result") or item.get("b64_json") or item.get("partial_image_b64")
            if image_b64:
                paths.append(_save_image(base64.b64decode(image_b64), output_dir))
        if not paths:
            raise RuntimeError(f"Responses image generation returned no image: {result}")
        return paths


# ---------------------------------------------------------------------------
# Codex auth provider (uses logged-in Codex credentials)
# ---------------------------------------------------------------------------

class CodexAuthProvider(ImageProvider):
    """Provider that calls the Codex image backend with local Codex auth."""

    DEFAULT_MODEL = "gpt-5.5"
    DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"

    def __init__(
        self,
        model: str = "",
        *,
        auth_file: str | None = None,
        base_url: str | None = None,
    ):
        auth = self._load_auth(auth_file)
        tokens = auth.get("tokens") or {}
        access_token = str(tokens.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("Codex auth file does not contain an access token. Run `codex login` and try again.")

        self.access_token = access_token
        self.account_id = str(tokens.get("account_id") or "").strip()
        self.api_base = (base_url or os.environ.get("CODEX_IMAGE_GENERATION_BASE_URL") or self.DEFAULT_BASE_URL).rstrip("/")
        self.model = self._resolve_model(model)
        self.timeout = int(
            os.environ.get("CODEX_IMAGE_GENERATION_TIMEOUT")
            or os.environ.get("SKILL_IMAGE_GENERATION_CODEX_TIMEOUT")
            or "600"
        )

    @classmethod
    def _load_auth(cls, auth_file: str | None) -> dict:
        path = cls._auth_path(auth_file)
        try:
            with open(path, "r", encoding="utf-8") as f:
                auth = json.load(f)
        except FileNotFoundError as e:
            raise RuntimeError("Codex auth file not found. Run `codex login` and try again.") from e
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Codex auth file is not valid JSON: {e}") from e
        if not isinstance(auth, dict):
            raise RuntimeError("Codex auth file must contain a JSON object")
        return auth

    @staticmethod
    def _auth_path(auth_file: str | None) -> str:
        configured = (
            auth_file
            or os.environ.get("CODEX_AUTH_FILE")
            or os.environ.get("SKILL_IMAGE_GENERATION_CODEX_AUTH_FILE")
            or os.environ.get("SKILL_IMAGE_GENERATION_AUTH_FILE")
            or os.environ.get("CODEX_AUTH_JSON")
            or ""
        ).strip()
        if configured:
            return os.path.abspath(os.path.expanduser(configured))
        codex_home = os.environ.get("CODEX_HOME")
        root = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
        return str((root / "auth.json").resolve())

    @classmethod
    def _resolve_model(cls, requested_model: str) -> str:
        configured = (
            os.environ.get("SKILL_IMAGE_GENERATION_CODEX_MODEL")
            or os.environ.get("CODEX_IMAGE_GENERATION_MODEL")
            or os.environ.get("SKILL_IMAGE_GENERATION_RESPONSES_MODEL")
            or os.environ.get("OPENAI_RESPONSES_MODEL")
            or os.environ.get("OPENAI_MODEL")
            or ""
        ).strip()
        if configured:
            return configured

        requested = str(requested_model or "").strip()
        if requested.startswith(("gpt-", "o1-", "o3-", "o4-")) and not requested.startswith("gpt-image"):
            return requested
        return cls.DEFAULT_MODEL

    def generate(
        self,
        prompt: str,
        *,
        image_url=None,
        quality: str | None = None,
        size: str | None = None,
        aspect_ratio: str | None = None,
        output_dir: str = ".",
    ) -> list[str]:
        resolved_size = resolve_size(size, aspect_ratio) if (size or aspect_ratio) else None
        payload = self._build_payload(
            prompt,
            image_url=image_url,
            quality=quality,
            size=resolved_size,
        )
        events = self._post_sse_json_events(f"{self.api_base}/responses", payload)
        return self._save_image_events(events, output_dir)

    def _headers(self) -> dict:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "User-Agent": "codex-cli",
        }
        if self.account_id:
            headers["ChatGPT-Account-Id"] = self.account_id
        return headers

    def _build_payload(self, prompt: str, *, image_url=None, quality: str | None, size: str | None) -> dict:
        tool = {"type": "image_generation"}
        if size in OpenAIProvider.RESPONSES_IMAGE_SIZES:
            tool["size"] = size
        if quality in OpenAIProvider.RESPONSES_IMAGE_QUALITIES:
            tool["quality"] = quality

        content = [{"type": "input_text", "text": prompt}]
        for image in self._image_list(image_url):
            content.append({
                "type": "input_image",
                "image_url": OpenAIProvider._responses_image_url(image),
            })

        return {
            "model": self.model,
            "instructions": self._instructions(bool(image_url)),
            "input": [{"role": "user", "content": content}],
            "tools": [tool],
            "tool_choice": {"type": "image_generation"},
            "stream": True,
            "store": False,
        }

    @staticmethod
    def _image_list(image_url) -> list[str]:
        if not image_url:
            return []
        return [str(item) for item in (image_url if isinstance(image_url, list) else [image_url]) if str(item or "").strip()]

    @staticmethod
    def _instructions(has_input_image: bool) -> str:
        base = "Use the image_generation tool exactly once and return a short message after the image is generated."
        if has_input_image:
            return (
                base
                + " Preserve input identity, layout, and unchanged regions unless the user explicitly asks otherwise."
            )
        return base

    def _post_sse_json_events(self, url: str, payload: dict) -> list[dict]:
        headers = self._headers()
        events: list[dict] = []
        if _HAS_REQUESTS:
            resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout, stream=True)
            self._raise_for_api_error(resp)
            for raw_line in resp.iter_lines(decode_unicode=True):
                event = OpenAIProvider._parse_sse_data_line(raw_line)
                if event is not None:
                    events.append(event)
            return events

        data = json.dumps(payload).encode()
        req = Request(url, data=data, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                for raw_line in resp:
                    event = OpenAIProvider._parse_sse_data_line(raw_line)
                    if event is not None:
                        events.append(event)
        except URLError as e:
            raise RuntimeError(f"Codex image backend request failed: {e}") from e
        return events

    @staticmethod
    def _raise_for_api_error(resp) -> None:
        if resp.status_code < 400:
            return
        try:
            body = resp.json()
            msg = body.get("detail") or body.get("error", {}).get("message") or body.get("message") or resp.text
        except Exception:
            msg = resp.text or resp.reason
        raise RuntimeError(f"Codex backend API {resp.status_code}: {msg} (url: {resp.url})")

    @staticmethod
    def _save_image_events(events: list[dict], output_dir: str) -> list[str]:
        seen: set[str] = set()
        last_image_b64: str | None = None
        for image_b64 in CodexAuthProvider._iter_image_payloads(events):
            if image_b64 in seen:
                continue
            seen.add(image_b64)
            last_image_b64 = image_b64
        if not last_image_b64:
            raise RuntimeError("Codex image backend returned no image")
        return [_save_image(base64.b64decode(last_image_b64), output_dir)]

    @staticmethod
    def _iter_image_payloads(events: list[dict]):
        for event in events:
            for item in CodexAuthProvider._candidate_items(event):
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "image_generation_call" and not str(item.get("type") or "").startswith(
                    "response.image_generation_call."
                ):
                    continue
                image_b64 = item.get("result") or item.get("b64_json") or item.get("partial_image_b64")
                if image_b64:
                    yield image_b64

    @staticmethod
    def _candidate_items(event: dict) -> list[dict]:
        candidates = [event]
        item = event.get("item")
        if isinstance(item, dict):
            candidates.append(item)
        response = event.get("response")
        if isinstance(response, dict):
            output = response.get("output") or []
            if isinstance(output, list):
                candidates.extend([entry for entry in output if isinstance(entry, dict)])
        return candidates


# ---------------------------------------------------------------------------
# External broker provider
# ---------------------------------------------------------------------------

class ExternalBrokerProvider(ImageProvider):
    """Provider that delegates image generation to a local command/broker.

    The broker receives a JSON payload on stdin and must print JSON to stdout:
    {"images": [{"url": "/path/to/output.png"}]} or {"error": "..."}.
    Auth stays inside the broker process; this script never reads tokens.
    """

    DEFAULT_MODEL = "external-image-broker"

    def __init__(self, command: list[str], model: str):
        if not command:
            raise RuntimeError("external broker command is empty")
        self.command = command
        self.model = (
            model
            or os.environ.get("IMAGE_GENERATION_BROKER_MODEL")
            or os.environ.get("SKILL_IMAGE_GENERATION_BROKER_MODEL")
            or self.DEFAULT_MODEL
        )
        self.timeout = int(
            os.environ.get("IMAGE_GENERATION_BROKER_TIMEOUT")
            or os.environ.get("SKILL_IMAGE_GENERATION_BROKER_TIMEOUT")
            or "600"
        )

    def generate(
        self,
        prompt: str,
        *,
        image_url=None,
        quality: str | None = None,
        size: str | None = None,
        aspect_ratio: str | None = None,
        output_dir: str = ".",
    ) -> list[str]:
        payload = {
            "prompt": prompt,
            "image_url": image_url,
            "quality": quality,
            "size": size,
            "aspect_ratio": aspect_ratio,
            "output_dir": output_dir,
            "model": self.model,
        }
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            self.command,
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
            env=env,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"external broker exited {proc.returncode}: {detail}")

        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"external broker returned invalid JSON: {e}") from e

        if result.get("error"):
            raise RuntimeError(str(result["error"]))

        return self._extract_images(result, output_dir)

    @staticmethod
    def _extract_images(result: dict, output_dir: str) -> list[str]:
        items = result.get("images") or result.get("data") or []
        if isinstance(items, dict):
            items = [items]
        paths: list[str] = []
        for item in items:
            if isinstance(item, str):
                paths.append(_save_result_image(item, output_dir))
                continue
            if not isinstance(item, dict):
                continue
            if item.get("b64_json"):
                paths.append(_save_image(base64.b64decode(item["b64_json"]), output_dir))
            elif item.get("url"):
                paths.append(_save_result_image(item["url"], output_dir))
            elif item.get("path"):
                paths.append(_save_result_image(item["path"], output_dir))
        if not paths:
            raise RuntimeError(f"external broker returned no image: {result}")
        return paths


# ---------------------------------------------------------------------------
# LinkAI provider (uses unified /v1/images/generations)
# ---------------------------------------------------------------------------

class LinkAIProvider(ImageProvider):
    """Provider for LinkAI unified image generation API."""

    DEFAULT_MODEL = "gpt-image-2"

    def __init__(self, api_key: str, api_base: str, model: str):
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model = model or self.DEFAULT_MODEL

    def generate(
        self,
        prompt: str,
        *,
        image_url=None,
        quality: str | None = None,
        size: str | None = None,
        aspect_ratio: str | None = None,
        output_dir: str = ".",
    ) -> list[str]:
        url = f"{self.api_base}/v1/images/generations"
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
        }
        if quality:
            payload["quality"] = quality
        # LinkAI accepts both pixel sizes (1024x1024) and tier shorthand (1K/2K/4K).
        # Pass through whatever the caller gave us; also forward aspect_ratio.
        if size:
            payload["size"] = size
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio
        if image_url:
            urls = image_url if isinstance(image_url, list) else [image_url]
            resolved = []
            for u in urls:
                if os.path.isfile(u):
                    data = _load_image(u)
                    ext = u.rsplit(".", 1)[-1].lower() if "." in u else "png"
                    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")
                    resolved.append(f"data:{mime};base64,{base64.b64encode(data).decode()}")
                else:
                    resolved.append(u)
            payload["image_url"] = resolved if len(resolved) > 1 else resolved[0]

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        if _HAS_REQUESTS:
            resp = requests.post(url, headers=headers, json=payload, timeout=300)
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                    msg = body.get("error", {}).get("message") or body.get("message") or resp.text
                except Exception:
                    msg = resp.text or resp.reason
                raise RuntimeError(f"API {resp.status_code}: {msg}")
            result = resp.json()
        else:
            data = json.dumps(payload).encode()
            req = Request(url, data=data, headers=headers, method="POST")
            with urlopen(req, timeout=300) as r:
                result = json.loads(r.read())

        if "error" in result:
            raise RuntimeError(result["error"].get("message", str(result["error"])))

        paths = []
        for item in result.get("data", []):
            if "url" in item:
                raw = _load_image(item["url"])
                paths.append(_save_image(raw, output_dir))
            elif "b64_json" in item:
                raw = base64.b64decode(item["b64_json"])
                paths.append(_save_image(raw, output_dir))
        return paths


# ---------------------------------------------------------------------------
# Gemini provider (Nano Banana family 鈥?gemini-*-image-*)
# ---------------------------------------------------------------------------

# Friendly aliases 鈫?real Gemini model id
_GEMINI_MODEL_ALIASES = {
    "nano-banana": "gemini-2.5-flash-image",
    "nano-banana-2": "gemini-3.1-flash-image-preview",
    "nano-banana-pro": "gemini-3-pro-image-preview",
}


class GeminiProvider(ImageProvider):
    """Provider for Google Gemini native image generation (Nano Banana family)."""

    DEFAULT_MODEL = "gemini-3.1-flash-image-preview"  # nano-banana-2

    def __init__(self, api_key: str, api_base: str, model: str):
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model = _GEMINI_MODEL_ALIASES.get(model, model or self.DEFAULT_MODEL)

    def generate(
        self,
        prompt: str,
        *,
        image_url=None,
        quality: str | None = None,  # not used; Gemini has no `quality` param
        size: str | None = None,
        aspect_ratio: str | None = None,
        output_dir: str = ".",
    ) -> list[str]:
        # Build request parts: prompt text + optional inline images
        parts: list[dict] = [{"text": prompt}]
        if image_url:
            urls = image_url if isinstance(image_url, list) else [image_url]
            for u in urls:
                data = _compress_image(_load_image(u))
                mime = _guess_mime(data)
                parts.append({
                    "inline_data": {
                        "mime_type": mime,
                        "data": base64.b64encode(data).decode(),
                    }
                })

        payload: dict = {
            "contents": [{"parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }

        # Gemini natively supports aspectRatio + imageSize tiers (512/1K/2K/4K).
        _GEMINI_VALID_TIERS = {"512", "1K", "2K", "4K"}
        _GEMINI_TIER_FALLBACK = {"3K": "2K"}
        image_config: dict = {}
        if size:
            if "x" in size.lower():
                tier = _pixels_to_tier(size)
            else:
                tier = size.upper()
            tier = _GEMINI_TIER_FALLBACK.get(tier, tier)
            if tier in _GEMINI_VALID_TIERS:
                image_config["imageSize"] = tier
        if aspect_ratio:
            image_config["aspectRatio"] = aspect_ratio
        elif size and "x" in size.lower():
            ratio = _pixels_to_ratio(size)
            if ratio:
                image_config["aspectRatio"] = ratio
        if image_config:
            payload["generationConfig"]["imageConfig"] = image_config

        url = f"{self.api_base}/v1beta/models/{self.model}:generateContent"
        headers = {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }

        if _HAS_REQUESTS:
            resp = requests.post(url, headers=headers, json=payload, timeout=300)
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                    msg = body.get("error", {}).get("message") or resp.text
                except Exception:
                    msg = resp.text or resp.reason
                raise RuntimeError(f"API {resp.status_code}: {msg}")
            result = resp.json()
        else:
            data = json.dumps(payload).encode()
            req = Request(url, data=data, headers=headers, method="POST")
            with urlopen(req, timeout=300) as r:
                result = json.loads(r.read())

        return self._extract_images(result, output_dir)

    @staticmethod
    def _extract_images(result: dict, output_dir: str) -> list[str]:
        paths: list[str] = []
        for cand in result.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                if part.get("thought"):
                    continue  # skip thinking-stage interim images
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    raw = base64.b64decode(inline["data"])
                    paths.append(_save_image(raw, output_dir))
        if not paths:
            # Surface the model's text reply (often a refusal explanation)
            for cand in result.get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    if part.get("text"):
                        raise RuntimeError(f"Gemini returned no image: {part['text'][:200]}")
            raise RuntimeError("Gemini returned no image (empty response)")
        return paths


def _guess_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF":
        return "image/webp"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/png"


def _pixels_to_tier(pixel_str: str) -> str:
    """Map 'WxH' to nearest Gemini tier (512 / 1K / 2K / 4K)."""
    try:
        w, h = (int(x) for x in pixel_str.lower().split("x"))
        long_edge = max(w, h)
    except Exception:
        return "1K"
    if long_edge <= 768:
        return "512"
    if long_edge <= 1536:
        return "1K"
    if long_edge <= 3072:
        return "2K"
    return "4K"


def _pixels_to_ratio(pixel_str: str) -> str | None:
    """Map 'WxH' to a Gemini-supported aspect ratio string when possible."""
    try:
        w, h = (int(x) for x in pixel_str.lower().split("x"))
    except Exception:
        return None
    # Reduce to a small ratio
    from math import gcd
    g = gcd(w, h)
    rw, rh = w // g, h // g
    candidate = f"{rw}:{rh}"
    supported = {"1:1", "1:4", "1:8", "2:3", "3:2", "3:4", "4:1", "4:3",
                 "4:5", "5:4", "8:1", "9:16", "16:9", "21:9"}
    return candidate if candidate in supported else None


# ---------------------------------------------------------------------------
# Seedream provider (Volcengine Ark, OpenAI-compatible /images/generations)
# ---------------------------------------------------------------------------

# Friendly aliases 鈫?real Seedream model id (Ark Model IDs).
_SEEDREAM_MODEL_ALIASES = {
    "seedream": "doubao-seedream-5-0-260128",
    "seedream-lite": "doubao-seedream-5-0-260128",
    "seedream-5.0": "doubao-seedream-5-0-260128",
    "seedream-5.0-lite": "doubao-seedream-5-0-260128",
    "seedream-5-0-lite": "doubao-seedream-5-0-260128",
    "doubao-seedream-5-0": "doubao-seedream-5-0-260128",
    "doubao-seedream-5-0-lite": "doubao-seedream-5-0-260128",
    "seedream-4.5": "doubao-seedream-4-5-251128",
    "seedream-4-5": "doubao-seedream-4-5-251128",
    "doubao-seedream-4-5": "doubao-seedream-4-5-251128",
}

# Seedream supports either a coarse tier ("2K"/"3K"/"4K") or explicit "WxH".
# We pass the user's tier through as-is when valid; otherwise translate ratio
# hints into the recommended pixel sizes from the Ark docs.
# Valid size tiers for Seedream (5.0 lite: 2K/3K, 4.5: 2K/4K).
# Unsupported tiers are mapped to the nearest valid one.
_SEEDREAM_VALID_TIERS = {"2K", "3K", "4K"}
_SEEDREAM_TIER_FALLBACK = {"512": "2K", "1K": "2K"}
_SEEDREAM_SIZE_TABLE = {
    # (tier, ratio) -> "WxH" recommended pixel sizes (Seedream 5.0 lite + 4.5 share most)
    ("2K", "1:1"): "2048x2048",
    ("2K", "3:4"): "1728x2304",
    ("2K", "4:3"): "2304x1728",
    ("2K", "16:9"): "2848x1600",
    ("2K", "9:16"): "1600x2848",
    ("2K", "3:2"): "2496x1664",
    ("2K", "2:3"): "1664x2496",
    ("2K", "21:9"): "3136x1344",
    ("3K", "1:1"): "3072x3072",
    ("3K", "3:4"): "2592x3456",
    ("3K", "4:3"): "3456x2592",
    ("3K", "16:9"): "4096x2304",
    ("3K", "9:16"): "2304x4096",
    ("3K", "2:3"): "2496x3744",
    ("3K", "3:2"): "3744x2496",
    ("3K", "21:9"): "4704x2016",
    ("4K", "1:1"): "4096x4096",
    ("4K", "3:4"): "3520x4704",
    ("4K", "4:3"): "4704x3520",
    ("4K", "16:9"): "5504x3040",
    ("4K", "9:16"): "3040x5504",
    ("4K", "2:3"): "3328x4992",
    ("4K", "3:2"): "4992x3328",
    ("4K", "21:9"): "6240x2656",
}


class SeedreamProvider(ImageProvider):
    """Provider for Volcengine Ark Seedream image generation API.

    The endpoint is OpenAI-compatible (POST {base}/images/generations) but
    accepts an extra `image` field (string or list) for image-to-image and
    multi-image fusion, plus `sequential_image_generation` / `watermark` flags.
    Reference docs accept both `2K` shorthand and explicit `WxH` for `size`.
    """

    DEFAULT_MODEL = "doubao-seedream-5-0-260128"  # seedream 5.0 lite

    def __init__(self, api_key: str, api_base: str, model: str):
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model = _SEEDREAM_MODEL_ALIASES.get((model or "").lower(), model or self.DEFAULT_MODEL)

    def generate(
        self,
        prompt: str,
        *,
        image_url=None,
        quality: str | None = None,  # not honoured by Seedream
        size: str | None = None,
        aspect_ratio: str | None = None,
        output_dir: str = ".",
    ) -> list[str]:
        url = f"{self.api_base}/images/generations"

        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "response_format": "url",
            "watermark": False,
        }

        # Default to 2K (Seedream 5.0 lite minimum tier), unless caller picks one.
        seedream_size = self._resolve_seedream_size(size, aspect_ratio)
        if seedream_size:
            payload["size"] = seedream_size

        # Image-to-image / multi-image fusion (up to 14 reference images).
        if image_url:
            urls = image_url if isinstance(image_url, list) else [image_url]
            prepared: list[str] = []
            for u in urls[:14]:
                if os.path.isfile(u):
                    data = _compress_image(_load_image(u))
                    mime = _guess_mime(data)
                    prepared.append(f"data:{mime};base64,{base64.b64encode(data).decode()}")
                else:
                    prepared.append(u)
            payload["image"] = prepared if len(prepared) > 1 else prepared[0]

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        if _HAS_REQUESTS:
            resp = requests.post(url, headers=headers, json=payload, timeout=300)
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                    err = body.get("error") or {}
                    msg = err.get("message") or body.get("message") or resp.text
                except Exception:
                    msg = resp.text or resp.reason
                raise RuntimeError(f"API {resp.status_code}: {msg}")
            result = resp.json()
        else:
            data = json.dumps(payload).encode()
            req = Request(url, data=data, headers=headers, method="POST")
            with urlopen(req, timeout=300) as r:
                result = json.loads(r.read())

        if result.get("error"):
            err = result["error"]
            raise RuntimeError(f"Seedream {err.get('code')}: {err.get('message')}")

        paths: list[str] = []
        for item in result.get("data") or []:
            u = item.get("url")
            b64 = item.get("b64_json")
            if u:
                paths.append(_save_image(_load_image(u), output_dir))
            elif b64:
                paths.append(_save_image(base64.b64decode(b64), output_dir))
        if not paths:
            raise RuntimeError(f"Seedream returned no image: {result}")
        return paths

    @staticmethod
    def _resolve_seedream_size(size: str | None, aspect_ratio: str | None) -> str | None:
        if not size and not aspect_ratio:
            return "2K"
        # Explicit pixel values: pass through (normalise separator)
        if size and "x" in size.lower() and "*" not in size:
            return size.lower()
        if size and "*" in size:
            return size.replace("*", "x")
        tier = (size or "2K").upper()
        # Map unsupported tiers (512, 1K) to the nearest valid one
        tier = _SEEDREAM_TIER_FALLBACK.get(tier, tier)
        if tier not in _SEEDREAM_VALID_TIERS:
            tier = "2K"
        ratio = aspect_ratio or "1:1"
        if (tier, ratio) in _SEEDREAM_SIZE_TABLE:
            return _SEEDREAM_SIZE_TABLE[(tier, ratio)]
        return tier


# ---------------------------------------------------------------------------
# Qwen provider (DashScope multimodal-generation: qwen-image-* family)
# ---------------------------------------------------------------------------

# Friendly aliases 鈫?real Qwen model id
_QWEN_MODEL_ALIASES = {
    "qwen": "qwen-image-2.0-pro",
    "qwen-image": "qwen-image-2.0-pro",
    "qwen-image-pro": "qwen-image-2.0-pro",
}

# Qwen pixel-size table (closest match by tier+ratio).
# qwen-image-2.0(*) supports any WxH between 512*512 and 2048*2048.
_QWEN_SIZE_TABLE = {
    # (tier, ratio) -> "W*H"
    ("1K", "1:1"): "1024*1024",
    ("1K", "16:9"): "1280*720",
    ("1K", "9:16"): "720*1280",
    ("1K", "4:3"): "1184*888",
    ("1K", "3:4"): "888*1184",
    ("1K", "3:2"): "1248*832",
    ("1K", "2:3"): "832*1248",
    ("2K", "1:1"): "2048*2048",
    ("2K", "16:9"): "2688*1536",  # exceeds 2048 cap 鈫?clamped at runtime if needed
    ("2K", "9:16"): "1536*2688",
    ("2K", "4:3"): "2368*1728",
    ("2K", "3:4"): "1728*2368",
}


class QwenProvider(ImageProvider):
    """Provider for Alibaba DashScope Qwen image API (qwen-image-2.0[-pro])."""

    DEFAULT_MODEL = "qwen-image-2.0"

    def __init__(self, api_key: str, api_base: str, model: str):
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model = _QWEN_MODEL_ALIASES.get((model or "").lower(), model or self.DEFAULT_MODEL)

    def generate(
        self,
        prompt: str,
        *,
        image_url=None,
        quality: str | None = None,  # not supported by Qwen image API
        size: str | None = None,
        aspect_ratio: str | None = None,
        output_dir: str = ".",
    ) -> list[str]:
        url = f"{self.api_base}/api/v1/services/aigc/multimodal-generation/generation"

        # Build content array: 0..3 images then a single text part.
        content: list[dict] = []
        if image_url:
            urls = image_url if isinstance(image_url, list) else [image_url]
            for u in urls[:3]:  # API caps at 3 reference images
                if os.path.isfile(u):
                    data = _compress_image(_load_image(u))
                    mime = _guess_mime(data)
                    image_field = f"data:{mime};base64,{base64.b64encode(data).decode()}"
                else:
                    image_field = u
                content.append({"image": image_field})
        content.append({"text": prompt})

        payload: dict = {
            "model": self.model,
            "input": {"messages": [{"role": "user", "content": content}]},
        }

        # Map (size, aspect_ratio) 鈫?Qwen "W*H"
        qwen_size = self._resolve_qwen_size(size, aspect_ratio)
        if qwen_size:
            payload["parameters"] = {"size": qwen_size}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        if _HAS_REQUESTS:
            resp = requests.post(url, headers=headers, json=payload, timeout=300)
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                    msg = body.get("message") or body.get("error", {}).get("message") or resp.text
                except Exception:
                    msg = resp.text or resp.reason
                raise RuntimeError(f"API {resp.status_code}: {msg}")
            result = resp.json()
        else:
            data = json.dumps(payload).encode()
            req = Request(url, data=data, headers=headers, method="POST")
            with urlopen(req, timeout=300) as r:
                result = json.loads(r.read())

        # Business-level errors arrive on HTTP 200 with a `code` field.
        if result.get("code"):
            raise RuntimeError(f"Qwen {result.get('code')}: {result.get('message')}")

        paths: list[str] = []
        choices = (result.get("output") or {}).get("choices") or []
        for ch in choices:
            for part in ((ch.get("message") or {}).get("content") or []):
                u = part.get("image")
                if u:
                    paths.append(_save_image(_load_image(u), output_dir))
        if not paths:
            raise RuntimeError(f"Qwen returned no image: {result}")
        return paths

    @staticmethod
    def _resolve_qwen_size(size: str | None, aspect_ratio: str | None) -> str | None:
        if not size and not aspect_ratio:
            return None
        if size and "x" in size.lower() and "*" not in size:
            return size.lower().replace("x", "*")
        if size and "*" in size:
            return size
        tier = (size or "1K").upper()
        # Qwen supports 1K and 2K; clamp others
        _QWEN_TIER_MAP = {"512": "1K", "3K": "2K", "4K": "2K"}
        tier = _QWEN_TIER_MAP.get(tier, tier)
        if tier not in ("1K", "2K"):
            tier = "1K"
        ratio = aspect_ratio or "1:1"
        if (tier, ratio) in _QWEN_SIZE_TABLE:
            return _QWEN_SIZE_TABLE[(tier, ratio)]
        return _QWEN_SIZE_TABLE.get((tier, "1:1"))


# ---------------------------------------------------------------------------
# MiniMax provider (image-01 family)
# ---------------------------------------------------------------------------

# Friendly aliases 鈫?real MiniMax model id
_MINIMAX_MODEL_ALIASES = {
    "minimax": "image-01",
    "minimax-image": "image-01",
    "minimax-image-01": "image-01",
}

_MINIMAX_SUPPORTED_RATIOS = {"1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9"}


class MinimaxProvider(ImageProvider):
    """Provider for MiniMax image generation API (image-01)."""

    DEFAULT_MODEL = "image-01"

    def __init__(self, api_key: str, api_base: str, model: str):
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model = _MINIMAX_MODEL_ALIASES.get((model or "").lower(), model or self.DEFAULT_MODEL)

    def generate(
        self,
        prompt: str,
        *,
        image_url=None,
        quality: str | None = None,  # not supported by MiniMax
        size: str | None = None,
        aspect_ratio: str | None = None,
        output_dir: str = ".",
    ) -> list[str]:
        url = f"{self.api_base}/v1/image_generation"
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "response_format": "base64",
        }

        # MiniMax accepts aspect_ratio directly; derive from pixels if needed.
        ratio = aspect_ratio
        if not ratio and size and "x" in size.lower():
            ratio = _pixels_to_ratio(size)
        if ratio and ratio in _MINIMAX_SUPPORTED_RATIOS:
            payload["aspect_ratio"] = ratio

        # Image-to-image uses subject_reference; accept URL or local file (鈫?base64).
        if image_url:
            urls = image_url if isinstance(image_url, list) else [image_url]
            refs = []
            for u in urls:
                if os.path.isfile(u):
                    data = _compress_image(_load_image(u))
                    mime = _guess_mime(data)
                    image_file = f"data:{mime};base64,{base64.b64encode(data).decode()}"
                else:
                    image_file = u
                refs.append({"type": "character", "image_file": image_file})
            payload["subject_reference"] = refs

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        if _HAS_REQUESTS:
            resp = requests.post(url, headers=headers, json=payload, timeout=300)
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                    msg = body.get("base_resp", {}).get("status_msg") or body.get("error", {}).get("message") or resp.text
                except Exception:
                    msg = resp.text or resp.reason
                raise RuntimeError(f"API {resp.status_code}: {msg}")
            result = resp.json()
        else:
            data = json.dumps(payload).encode()
            req = Request(url, data=data, headers=headers, method="POST")
            with urlopen(req, timeout=300) as r:
                result = json.loads(r.read())

        # MiniMax returns business errors inside base_resp even on HTTP 200.
        base_resp = result.get("base_resp") or {}
        if base_resp.get("status_code") not in (None, 0):
            raise RuntimeError(f"MiniMax {base_resp.get('status_code')}: {base_resp.get('status_msg')}")

        data_obj = result.get("data") or {}
        b64_list = data_obj.get("image_base64") or []
        urls_list = data_obj.get("image_urls") or []

        paths: list[str] = []
        for b64 in b64_list:
            paths.append(_save_image(base64.b64decode(b64), output_dir))
        for u in urls_list:
            paths.append(_save_image(_load_image(u), output_dir))
        if not paths:
            raise RuntimeError(f"MiniMax returned no image: {result}")
        return paths


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

# Model-prefix 鈫?preferred provider label.
# When the requested model matches a prefix, that provider is promoted to the
# front of the queue. All other configured providers still run as fallbacks.
_MODEL_PREFERRED_PROVIDER: list[tuple[tuple[str, ...], str]] = [
    (("grok-imagine", "grok", "xai"), "GrokXAI"),
    (("codex", "codex-auth", "codex_auth"), "CodexAuth"),
    (("broker", "external-broker", "local-broker"), "Broker"),
    (("gpt-image",), "OpenAI"),
    (("nano-banana", "gemini-"), "Gemini"),
    (("seedream", "doubao-seedream"), "Seedream"),
    (("qwen-image", "qwen"), "Qwen"),
    (("minimax", "image-01"), "MiniMax"),
]

# Default global priority when the model has no preferred provider.
_DEFAULT_PROVIDER_ORDER = ["CodexAuth", "Broker", "OpenAI", "Gemini", "Seedream", "Qwen", "MiniMax", "LinkAI"]


def _preferred_provider(model: str) -> str | None:
    m = (model or "").lower()
    for prefixes, label in _MODEL_PREFERRED_PROVIDER:
        if m.startswith(prefixes):
            return label
    return None


def _build_providers(
    model: str,
    *,
    runtime: str = "",
    broker_only: bool = False,
) -> list[tuple[str, ImageProvider]]:
    """Build an ordered list of (label, provider) to try.

    Behaviour:
      1. All providers with a configured API key are added in the global
         priority order: OpenAI 鈫?Gemini 鈫?Seedream 鈫?Qwen 鈫?MiniMax 鈫?LinkAI.
      2. If `model` natively belongs to one of the providers AND that provider
         is configured, it is promoted to the front so it gets the first
         attempt with the right model id.
      3. If the preferred provider is NOT configured (no API key), the model
         id would 100% fail on every other backend, so we drop the explicit
         model and fall back to automatic routing 鈥?every provider then uses
         its own DEFAULT_MODEL.
    """
    broker_command = _broker_command_from_env()
    if _is_grok_runtime(runtime):
        grok_model = model if str(model or "").strip() in {_GROK_SPEED_MODEL, _GROK_QUALITY_MODEL} else ""
        return [("GrokXAI", GrokXAIProvider(model=grok_model))]
    if _is_codex_auth_runtime(runtime):
        return [("CodexAuth", CodexAuthProvider(model=model))]
    if broker_only:
        if not broker_command:
            return []
        return [("Broker", ExternalBrokerProvider(command=broker_command, model=model))]

    keys = {
        "OpenAI": os.environ.get("OPENAI_API_KEY", ""),
        "Gemini": os.environ.get("GEMINI_API_KEY", ""),
        "Seedream": os.environ.get("ARK_API_KEY", ""),
        "Qwen": os.environ.get("DASHSCOPE_API_KEY", ""),
        "MiniMax": os.environ.get("MINIMAX_API_KEY", ""),
        "LinkAI": os.environ.get("LINKAI_API_KEY", ""),
    }
    bases = {
        "OpenAI": os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1"),
        "Gemini": os.environ.get("GEMINI_API_BASE", "https://generativelanguage.googleapis.com"),
        "Seedream": os.environ.get("ARK_API_BASE", "https://ark.cn-beijing.volces.com/api/v3"),
        "Qwen": os.environ.get("DASHSCOPE_API_BASE", "https://dashscope.aliyuncs.com"),
        "MiniMax": os.environ.get("MINIMAX_API_BASE", "https://api.minimaxi.com"),
        "LinkAI": os.environ.get("LINKAI_API_BASE", "https://api.link-ai.tech"),
    }

    pref = _preferred_provider(model)

    # If a specific model is requested and its native provider has no key,
    # other backends won't recognise the id 鈫?reset to auto routing.
    if pref == "Broker" and not broker_command:
        model = ""
        pref = None
    elif pref and pref != "Broker" and not keys.get(pref):
        model = ""
        pref = None

    factories = {
        "OpenAI": OpenAIProvider,
        "Gemini": GeminiProvider,
        "Seedream": SeedreamProvider,
        "Qwen": QwenProvider,
        "MiniMax": MinimaxProvider,
        "LinkAI": LinkAIProvider,
    }
    available: dict[str, ImageProvider] = {}
    if os.environ.get("SKILL_IMAGE_GENERATION_ENABLE_CODEX_AUTH_FALLBACK", "").lower() in {"1", "true", "yes"}:
        try:
            available["CodexAuth"] = CodexAuthProvider(model=model)
        except Exception:
            pass
    if broker_command:
        available["Broker"] = ExternalBrokerProvider(command=broker_command, model=model)
    for label, key in keys.items():
        if key:
            available[label] = factories[label](api_key=key, api_base=bases[label], model=model)

    # When a specific model is pinned, only try its native provider 鈥?other
    # backends won't recognise the model id so retrying them is pointless.
    if pref and pref in available:
        return [(pref, available[pref])]

    # Auto routing: try every configured provider in priority order.
    ordered: list[str] = []
    for label in _DEFAULT_PROVIDER_ORDER:
        if label in available:
            ordered.append(label)
    return [(label, available[label]) for label in ordered]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: python generate.py '<json_args>'"}))
        sys.exit(1)

    try:
        raw = sys.argv[1]
        raw = raw.replace('\u201c', '"').replace('\u201d', '"').replace('\u2018', "'").replace('\u2019', "'")
        args = _normalize_call_args(json.loads(raw))
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON: {e}"}))
        sys.exit(1)

    prompt = args.get("prompt")
    if not prompt:
        print(json.dumps({"error": "Missing required parameter: prompt"}))
        sys.exit(1)

    runtime = _runtime_from_args_or_env(args)
    if _requests_codex_session_auth(args) and not runtime:
        runtime = "codex_auth"
    if _requests_codex_session_auth(args) and not _is_codex_auth_runtime(runtime):
        print(json.dumps({"error": _codex_session_auth_error()}, ensure_ascii=False))
        sys.exit(1)

    # Model resolution priority:
    #   1. Explicit `model` in the call args (agent / user override)
    #   2. SKILL_IMAGE_GENERATION_MODEL env var (synced from
    #      config["skill"]["image-generation"]["model"] at startup)
    #   3. None 鈫?fall back to automatic provider routing (try every
    #      provider with a configured API key in global priority order)
    model = args.get("model") or os.environ.get("SKILL_IMAGE_GENERATION_MODEL") or ""
    quality = args.get("quality")
    size = args.get("size")
    aspect_ratio = args.get("aspect_ratio")
    image_url = args.get("image_url")
    broker_only = _is_broker_only_runtime(runtime)
    allow_fallback = _truthy(args.get("fallback")) or _truthy(
        os.environ.get("SKILL_IMAGE_GENERATION_FALLBACK")
        or os.environ.get("IMAGE_GENERATION_FALLBACK")
    )

    output_dir = str(args.get("output_dir") or os.environ.get("IMAGE_OUTPUT_DIR", os.path.join(os.getcwd(), "images")))

    try:
        providers = _build_providers(model, runtime=runtime, broker_only=broker_only)
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)
    if not providers:
        target = f"model '{model}'" if model else "image generation"
        if broker_only:
            print(json.dumps({
                "error": (
                    "Codex broker runtime is enabled for image generation, but no "
                    "broker command is configured. Start the Codex image broker "
                    "and set IMAGE_GENERATION_BROKER_COMMAND_JSON, "
                    "SKILL_IMAGE_GENERATION_BROKER_COMMAND_JSON, "
                    "IMAGE_GENERATION_BROKER_COMMAND, or CODEX_IMAGE_GEN_COMMAND. "
                    "CowWechat will not use API providers in codex_broker runtime."
                )
            }, ensure_ascii=False))
            sys.exit(1)
        if _is_codex_auth_runtime(runtime):
            print(json.dumps({
                "error": (
                    "Codex auth runtime is enabled for image generation, but "
                    "the local Codex auth file could not be loaded. Run `codex login` "
                    "or set CODEX_AUTH_FILE to the Codex auth JSON file, then try again."
                )
            }, ensure_ascii=False))
            sys.exit(1)
        print(json.dumps({
            "error": (
                f"No API key configured for {target}. "
                "Set IMAGE_GENERATION_BROKER_COMMAND_JSON, "
                "IMAGE_GENERATION_BROKER_COMMAND, CODEX_IMAGE_GEN_COMMAND, "
                "or at least one of OPENAI_API_KEY / GEMINI_API_KEY / "
                "ARK_API_KEY / DASHSCOPE_API_KEY / MINIMAX_API_KEY / "
                "LINKAI_API_KEY via the env_config tool, then try again."
            )
        }, ensure_ascii=False))
        sys.exit(1)
    if not allow_fallback and len(providers) > 1:
        providers = providers[:1]

    errors = []
    for label, provider in providers:
        try:
            attempt_model = getattr(provider, "model", model) or "auto"
            print(f"[image-generation] Trying {label} (model={attempt_model})...", file=sys.stderr)
            t0 = time.time()
            paths = provider.generate(
                prompt,
                image_url=image_url,
                quality=quality,
                size=size,
                aspect_ratio=aspect_ratio,
                output_dir=output_dir,
            )
            elapsed = time.time() - t0
            # Resolved model id (after alias expansion) actually sent to the API
            actual_model = getattr(provider, "model", model)
            print(
                f"[image-generation] OK {label} succeeded in {elapsed:.1f}s "
                f"(model={actual_model})",
                file=sys.stderr,
            )
            result = {
                "model": actual_model,
                "images": [{"url": p} for p in paths],
            }
            print(json.dumps(result, ensure_ascii=False))
            return
        except Exception as e:
            elapsed = time.time() - t0
            print(f"[image-generation] FAILED {label} in {elapsed:.1f}s: {e}", file=sys.stderr)
            errors.append(f"{label}: {e}")
            if not allow_fallback:
                break

    hint = " | ".join(errors)
    prefix = "Provider failed" if not allow_fallback else "All providers failed"
    if broker_only:
        print(json.dumps({
            "error": f"Codex image broker failed: {hint}. "
                     "CowWechat did not try API providers. Verify that the "
                     "Codex image broker is running in the logged-in Codex "
                     "environment and that its command is configured correctly."
        }, ensure_ascii=True))
    elif _is_grok_runtime(runtime):
        print(json.dumps({
            "error": f"Grok/xAI image generation failed: {hint}. "
                     "Confirm that the Grok account is logged in through CowWeCom "
                     "OAuth and retry. CowWechat did not fall back to Codex because "
                     "Grok/xAI was explicitly requested."
        }, ensure_ascii=True))
    else:
        print(json.dumps({
            "error": f"{prefix}: {hint}. "
                     "This is likely an API key or base URL configuration issue. "
                     "Do NOT retry with the same parameters. "
                     "Ask the user to verify their API key / base URL "
                     "(or external broker command: IMAGE_GENERATION_BROKER_COMMAND_JSON, "
                     "IMAGE_GENERATION_BROKER_COMMAND, CODEX_IMAGE_GEN_COMMAND; "
                     "API keys: OPENAI_API_KEY, GEMINI_API_KEY, ARK_API_KEY, "
                     "DASHSCOPE_API_KEY, MINIMAX_API_KEY, or LINKAI_API_KEY) "
                     "via env_config."
        }, ensure_ascii=True))
    sys.exit(1)


if __name__ == "__main__":
    main()
