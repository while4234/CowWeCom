# encoding:utf-8

"""Safe local storage for temporary Grok/xAI media downloads."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import tempfile
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests


MAX_REDIRECTS = 5
CHUNK_SIZE = 1024 * 1024
GENERATED_MEDIA_SUBDIR = os.path.join("tmp", "grok_media")

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CONTENT_TYPE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "application/mp4": ".mp4",
    "application/octet-stream": ".bin",
}
_SAFE_SUFFIX_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


class MediaDownloadError(RuntimeError):
    """Safe user-facing media download error."""


def validate_public_https_url(url: str) -> None:
    """Validate that *url* is an HTTPS URL resolving only to public IPs."""
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme.lower() != "https":
        raise MediaDownloadError("Media URL must use https.")
    if not parsed.hostname:
        raise MediaDownloadError("Media URL must include a hostname.")

    hostname = parsed.hostname.strip().strip(".")
    if _is_localhost_name(hostname):
        raise MediaDownloadError("Media URL host is not public.")

    for address in _resolve_host_addresses(hostname, parsed.port or 443):
        _assert_public_ip(address)


def safe_download_to_file(
    url: str,
    *,
    prefix: str,
    suffix: Optional[str] = None,
    allowed_content_types: Iterable[str],
    max_bytes: int,
    timeout: float,
) -> str:
    """Download a public HTTPS media URL into ``tmp/grok_media`` safely."""
    allowed = _normalize_allowed_content_types(allowed_content_types)
    if not allowed:
        raise MediaDownloadError("No media content types are allowed.")

    current_url = str(url or "").strip()
    for _ in range(MAX_REDIRECTS + 1):
        validate_public_https_url(current_url)
        response = _get_without_redirects(current_url, timeout)
        status_code = int(getattr(response, "status_code", 200) or 200)
        if status_code in _REDIRECT_STATUSES:
            location = (getattr(response, "headers", {}) or {}).get("Location")
            if not location:
                raise MediaDownloadError("Media download redirect was missing a target.")
            current_url = urljoin(current_url, str(location))
            continue
        if status_code >= 400:
            _raise_for_status(response)

        content_type = _response_content_type(response)
        if content_type not in allowed:
            raise MediaDownloadError("Media download content type is not allowed.")

        declared_size = _declared_content_length(response)
        if declared_size is not None and declared_size > max_bytes:
            raise MediaDownloadError("Media download exceeds the configured size cap.")

        output_suffix = _resolve_output_suffix(suffix, current_url, content_type)
        path = new_generated_media_path(prefix, output_suffix)
        try:
            bytes_read = _stream_response_to_path(response, path, max_bytes)
            if bytes_read <= 0:
                raise MediaDownloadError("Media download was empty.")
            _validate_downloaded_file(path, content_type, output_suffix)
            return path
        except Exception:
            remove_file_quietly(path)
            raise

    raise MediaDownloadError("Media download followed too many redirects.")


def generated_media_dir() -> str:
    path = os.path.abspath(os.path.join(os.getcwd(), GENERATED_MEDIA_SUBDIR))
    os.makedirs(path, exist_ok=True)
    return path


def new_generated_media_path(prefix: str, suffix: str) -> str:
    safe_prefix = _SAFE_SUFFIX_RE.sub("_", str(prefix or "grok_media")).strip("._") or "grok_media"
    safe_suffix = _safe_suffix(suffix)
    fd, path = tempfile.mkstemp(prefix=f"{safe_prefix}_", suffix=safe_suffix, dir=generated_media_dir())
    os.close(fd)
    return path


def cleanup_generated_reply_media(reply) -> None:
    if not bool(getattr(reply, "cleanup_after_send", False)):
        return
    paths = []
    content = getattr(reply, "content", None)
    if isinstance(content, str):
        paths.append(content)
    extra = getattr(reply, "generated_media_paths", None) or getattr(reply, "generated_media_path", None)
    if isinstance(extra, str):
        paths.append(extra)
    elif isinstance(extra, (list, tuple, set)):
        paths.extend(str(item) for item in extra)
    for path in paths:
        cleanup_generated_media_path(path)


def cleanup_generated_media_path(path: str) -> bool:
    local_path = _strip_file_scheme(path)
    if not local_path or not _is_inside_generated_media_dir(local_path):
        return False
    return remove_file_quietly(local_path)


def remove_file_quietly(path: str) -> bool:
    try:
        if path and os.path.exists(path):
            os.remove(path)
            return True
    except OSError:
        return False
    return False


def is_generated_media_path(path: str) -> bool:
    return _is_inside_generated_media_dir(_strip_file_scheme(path))


def _get_without_redirects(url: str, timeout: float):
    try:
        return requests.get(
            url,
            timeout=(float(timeout), float(timeout)),
            stream=True,
            allow_redirects=False,
        )
    except requests.Timeout as exc:
        raise MediaDownloadError("Media download timed out.") from exc
    except requests.RequestException as exc:
        raise MediaDownloadError("Media download request failed.") from exc


def _raise_for_status(response) -> None:
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise MediaDownloadError("Media download returned an HTTP error.") from exc
    raise MediaDownloadError("Media download returned an HTTP error.")


def _stream_response_to_path(response, path: str, max_bytes: int) -> int:
    bytes_read = 0
    with open(path, "wb") as handle:
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            if not chunk:
                continue
            bytes_read += len(chunk)
            if bytes_read > max_bytes:
                raise MediaDownloadError("Media download exceeds the configured size cap.")
            handle.write(chunk)
    return bytes_read


def _validate_downloaded_file(path: str, content_type: str, suffix: str) -> None:
    if content_type == "application/octet-stream" and suffix.lower() == ".mp4":
        with open(path, "rb") as handle:
            head = handle.read(16)
        if not _looks_like_mp4(head):
            raise MediaDownloadError("Downloaded octet-stream media is not an MP4 file.")


def _looks_like_mp4(head: bytes) -> bool:
    return len(head) >= 8 and head[4:8] == b"ftyp"


def _response_content_type(response) -> str:
    value = (getattr(response, "headers", {}) or {}).get("Content-Type", "")
    return str(value or "").split(";", 1)[0].strip().lower()


def _declared_content_length(response) -> Optional[int]:
    value = (getattr(response, "headers", {}) or {}).get("Content-Length")
    if value in (None, ""):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _normalize_allowed_content_types(values: Iterable[str]) -> set:
    return {str(item or "").split(";", 1)[0].strip().lower() for item in values if str(item or "").strip()}


def _resolve_output_suffix(suffix: Optional[str], url: str, content_type: str) -> str:
    if suffix:
        return _safe_suffix(suffix)
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4"}:
        return ext
    return _CONTENT_TYPE_SUFFIXES.get(content_type, ".bin")


def _safe_suffix(suffix: str) -> str:
    text = str(suffix or "").strip().lower()
    if not text:
        return ".bin"
    if not text.startswith("."):
        text = "." + text
    text = _SAFE_SUFFIX_RE.sub("_", text)
    return text if len(text) <= 16 else ".bin"


def _resolve_host_addresses(hostname: str, port: int) -> set:
    try:
        infos = socket.getaddrinfo(hostname.encode("idna").decode("ascii"), port, type=socket.SOCK_STREAM)
    except Exception as exc:
        raise MediaDownloadError("Media URL hostname could not be resolved.") from exc
    addresses = set()
    for info in infos:
        sockaddr = info[4]
        if sockaddr:
            addresses.add(str(sockaddr[0]))
    if not addresses:
        raise MediaDownloadError("Media URL hostname could not be resolved.")
    return addresses


def _assert_public_ip(address: str) -> None:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError as exc:
        raise MediaDownloadError("Media URL resolved to an invalid address.") from exc
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        raise MediaDownloadError("Media URL host is not public.")


def _is_localhost_name(hostname: str) -> bool:
    value = str(hostname or "").strip().lower()
    return value == "localhost" or value.endswith(".localhost")


def _strip_file_scheme(path: str) -> str:
    text = str(path or "").strip()
    return text[7:] if text.lower().startswith("file://") else text


def _is_inside_generated_media_dir(path: str) -> bool:
    try:
        candidate = os.path.abspath(path)
        root = generated_media_dir()
        return os.path.commonpath([candidate, root]) == root
    except Exception:
        return False
