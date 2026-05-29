# encoding:utf-8

"""xAI/Grok text-to-speech integration backed by PR 1 OAuth credentials."""

from __future__ import annotations

import datetime
import os
import random
import re
from typing import Any, Dict, Iterable, Optional

import requests

from common.log import logger
from config import conf

from .auth import AuthError, DEFAULT_XAI_OAUTH_BASE_URL
from .proxy import xai_request_kwargs
from .xai_http import hermes_xai_user_agent, resolve_xai_http_credentials


DEFAULT_XAI_TTS_VOICE_ID = "eve"
DEFAULT_XAI_TTS_LANGUAGE = "zh"
DEFAULT_XAI_TTS_SAMPLE_RATE = 24000
DEFAULT_XAI_TTS_BIT_RATE = 128000
DEFAULT_XAI_TTS_CODEC = "mp3"
_XAI_TTS_TIMEOUT_SECONDS = 60

_XAI_INLINE_SPEECH_TAGS = (
    "pause",
    "long-pause",
    "hum-tune",
    "laugh",
    "chuckle",
    "giggle",
    "cry",
    "tsk",
    "tongue-click",
    "lip-smack",
    "breath",
    "inhale",
    "exhale",
    "sigh",
)
_XAI_WRAPPING_SPEECH_TAGS = (
    "soft",
    "whisper",
    "loud",
    "build-intensity",
    "decrease-intensity",
    "higher-pitch",
    "lower-pitch",
    "slow",
    "fast",
    "sing-song",
    "singing",
    "laugh-speak",
    "emphasis",
)
_XAI_SPEECH_TAG_RE = re.compile(
    r"(\[(?:" + "|".join(_XAI_INLINE_SPEECH_TAGS) + r")\]|</?(?:"
    + "|".join(_XAI_WRAPPING_SPEECH_TAGS)
    + r")>)",
    flags=re.IGNORECASE,
)
_XAI_FIRST_SENTENCE_RE = re.compile(r"^(.{12,120}?[。！？.!?…])\s+(?=\S)", flags=re.DOTALL)
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer|basic)\s+[a-z0-9._~+/=-]+")
_TOKEN_FIELD_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|authorization_code|code)\b\s*[:=]\s*['\"]?[^'\"\s,;}]+"
)


class XaiTtsError(RuntimeError):
    """Safe user-facing xAI TTS error."""


def generate_xai_tts(
    text: str,
    output_path: Optional[str] = None,
    *,
    voice_id: Optional[str] = None,
    language: Optional[str] = None,
    sample_rate: Optional[int] = None,
    bit_rate: Optional[int] = None,
    codec: Optional[str] = None,
    auto_speech_tags: Optional[bool] = None,
    timeout: Optional[float] = None,
) -> str:
    """Generate speech with xAI TTS and return the local audio path.

    Credentials are resolved through ``resolve_xai_http_credentials()`` so Grok
    TTS reuses the same OAuth/API-key path as PR 1 Grok Chat. A 401 triggers one
    forced OAuth refresh retry.
    """
    clean_text = str(text or "").strip()
    if not clean_text:
        raise XaiTtsError("xAI TTS text is empty.")

    options = _resolve_options(
        output_path=output_path,
        voice_id=voice_id,
        language=language,
        sample_rate=sample_rate,
        bit_rate=bit_rate,
        codec=codec,
        auto_speech_tags=auto_speech_tags,
        timeout=timeout,
    )
    if options["auto_speech_tags"]:
        clean_text = _apply_xai_auto_speech_tags(clean_text)

    path = output_path or _default_output_path(options["codec"])
    temp_created = output_path is None
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    try:
        response = _post_tts(clean_text, options, force_refresh=False)
        if response.status_code == 401:
            response = _post_tts(clean_text, options, force_refresh=True)
        if response.status_code >= 400:
            raise XaiTtsError(_safe_http_error(response))
        if not response.content:
            raise XaiTtsError("xAI TTS returned an empty audio response.")
        with open(path, "wb") as handle:
            handle.write(response.content)
        logger.info("[GrokTTS] generated voice file: %s bytes=%s", path, len(response.content))
        return path
    except AuthError as exc:
        _cleanup_file(path, temp_created or bool(output_path))
        raise XaiTtsError(_sanitize_error_text(str(exc))) from exc
    except XaiTtsError:
        _cleanup_file(path, temp_created or bool(output_path))
        raise
    except Exception as exc:
        _cleanup_file(path, temp_created or bool(output_path))
        raise XaiTtsError(f"xAI TTS request failed: {_sanitize_error_text(str(exc))}") from exc


def _post_tts(text: str, options: Dict[str, Any], *, force_refresh: bool) -> requests.Response:
    creds = resolve_xai_http_credentials(force_refresh=force_refresh)
    api_key = str(creds.get("api_key") or "").strip()
    if not api_key:
        raise AuthError(
            "Grok account is not logged in. Please complete Grok login in the Web admin page.",
            code="xai_auth_missing",
            relogin_required=True,
        )
    base_url = str(creds.get("base_url") or DEFAULT_XAI_OAUTH_BASE_URL).strip().rstrip("/")
    payload = _build_payload(text, options)
    return requests.post(
        f"{base_url}/tts",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": hermes_xai_user_agent(),
        },
        json=payload,
        timeout=options["timeout"],
        **xai_request_kwargs(),
    )


def _build_payload(text: str, options: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "text": text,
        "voice_id": options["voice_id"],
        "language": options["language"],
    }
    if _should_send_output_format(options):
        output_format: Dict[str, Any] = {"codec": options["codec"]}
        if options["sample_rate"]:
            output_format["sample_rate"] = options["sample_rate"]
        if options["codec"] == "mp3" and options["bit_rate"]:
            output_format["bit_rate"] = options["bit_rate"]
        payload["output_format"] = output_format
    return payload


def _should_send_output_format(options: Dict[str, Any]) -> bool:
    return (
        options["codec"] != DEFAULT_XAI_TTS_CODEC
        or options["sample_rate"] != DEFAULT_XAI_TTS_SAMPLE_RATE
        or options["bit_rate"] != DEFAULT_XAI_TTS_BIT_RATE
        or bool(conf().get("grok_tts_force_output_format", False))
    )


def _resolve_options(
    *,
    output_path: Optional[str],
    voice_id: Optional[str],
    language: Optional[str],
    sample_rate: Optional[int],
    bit_rate: Optional[int],
    codec: Optional[str],
    auto_speech_tags: Optional[bool],
    timeout: Optional[float],
) -> Dict[str, Any]:
    resolved_codec = _normalize_codec(
        codec
        or conf().get("grok_tts_codec")
        or _codec_from_path(output_path)
        or DEFAULT_XAI_TTS_CODEC
    )
    return {
        "voice_id": str(voice_id or conf().get("grok_tts_voice_id") or DEFAULT_XAI_TTS_VOICE_ID).strip()
        or DEFAULT_XAI_TTS_VOICE_ID,
        "language": str(language or conf().get("grok_tts_language") or DEFAULT_XAI_TTS_LANGUAGE).strip()
        or DEFAULT_XAI_TTS_LANGUAGE,
        "sample_rate": _safe_positive_int(
            sample_rate if sample_rate is not None else conf().get("grok_tts_sample_rate"),
            DEFAULT_XAI_TTS_SAMPLE_RATE,
        ),
        "bit_rate": _safe_positive_int(
            bit_rate if bit_rate is not None else conf().get("grok_tts_bit_rate"),
            DEFAULT_XAI_TTS_BIT_RATE,
        ),
        "codec": resolved_codec,
        "auto_speech_tags": _config_bool(
            auto_speech_tags if auto_speech_tags is not None else conf().get("grok_tts_auto_speech_tags"),
            False,
        ),
        "timeout": _safe_timeout(timeout if timeout is not None else conf().get("request_timeout")),
    }


def _apply_xai_auto_speech_tags(text: str) -> str:
    clean = text.strip()
    if not clean or _XAI_SPEECH_TAG_RE.search(clean):
        return text
    clean = re.sub(r"\n\s*\n+", " [pause] ", clean)
    clean = re.sub(r"\s*\n\s*", " ", clean)
    if not _XAI_SPEECH_TAG_RE.search(clean):
        clean = _XAI_FIRST_SENTENCE_RE.sub(r"\1 [pause] ", clean, count=1)
    clean = re.sub(r"\s{2,}", " ", clean).strip()
    return clean


def _default_output_path(codec: str) -> str:
    os.makedirs("tmp", exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    suffix = _normalize_codec(codec)
    return os.path.join("tmp", f"grok-tts-{timestamp}-{random.randint(0, 1000)}.{suffix}")


def _codec_from_path(path: Optional[str]) -> str:
    if not path:
        return ""
    ext = os.path.splitext(str(path))[1].lstrip(".").lower()
    return ext if ext in {"mp3", "wav"} else ""


def _normalize_codec(value: Any) -> str:
    codec = str(value or DEFAULT_XAI_TTS_CODEC).strip().lower()
    if codec not in {"mp3", "wav"}:
        return DEFAULT_XAI_TTS_CODEC
    return codec


def _safe_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _safe_timeout(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = _XAI_TTS_TIMEOUT_SECONDS
    return max(5.0, min(parsed, 600.0))


def _config_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "disabled"}:
        return False
    return default


def _safe_http_error(response: requests.Response) -> str:
    body = ""
    try:
        body = response.text[:500]
    except Exception:
        body = ""
    message = f"xAI TTS failed (HTTP {response.status_code})."
    if body:
        message += f" {_sanitize_error_text(body)}"
    return message


def _sanitize_error_text(value: Any, extra_secrets: Optional[Iterable[str]] = None) -> str:
    text = str(value or "")
    text = _AUTH_HEADER_RE.sub(r"\1<redacted>", text)
    text = _TOKEN_FIELD_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    for secret in extra_secrets or []:
        secret_text = str(secret or "").strip()
        if secret_text:
            text = text.replace(secret_text, "<redacted>")
    return text[:800]


def _cleanup_file(path: str, should_cleanup: bool) -> None:
    if not should_cleanup:
        return
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
