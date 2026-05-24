from __future__ import annotations

import json
import re
from typing import Any, Generator, Iterable, Mapping, Optional
from uuid import uuid4

import requests


CODEX_CHATGPT_DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_RESPONSES_DEFAULT_ENDPOINT = "/responses"
DEFAULT_MAX_RESPONSE_BYTES = 5_000_000
DEFAULT_MAX_ERROR_RESPONSE_BYTES = 200_000
BLOCKED_EXTRA_HEADERS = {
    "accept",
    "authorization",
    "connection",
    "content-length",
    "content-type",
    "host",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class CodexSSEParser:
    def parse_text(self, text: str, *, request_id: str = "") -> list[dict[str, Any]]:
        return list(
            self.iter_json_events(
                [str(text or "").encode("utf-8")],
                limit=max(len(str(text or "").encode("utf-8")) + 1, 1),
                request_id=request_id,
            )
        )

    def iter_json_events(
        self,
        chunks: Iterable[bytes],
        *,
        limit: int,
        request_id: str = "",
    ) -> Generator[dict[str, Any], None, None]:
        buffer = b""
        total = 0
        for raw_chunk in chunks:
            if not raw_chunk:
                continue
            chunk = bytes(raw_chunk)
            total += len(chunk)
            if total > limit:
                raise RuntimeError(f"codex_response_too_large: response exceeded maxResponseBytes={limit}")
            buffer += chunk
            while True:
                terminator = self._find_event_terminator(buffer)
                if not terminator:
                    break
                end_pos, term_len = terminator
                event_bytes = buffer[:end_pos]
                buffer = buffer[end_pos + term_len:]
                event = self._decode_event(event_bytes, request_id=request_id)
                if event is not None:
                    yield event

        if buffer.strip():
            event = self._decode_event(buffer, request_id=request_id)
            if event is not None:
                yield event

    @staticmethod
    def _find_event_terminator(buffer: bytes) -> Optional[tuple[int, int]]:
        candidates = []
        for marker in (b"\r\n\r\n", b"\n\n", b"\r\r"):
            index = buffer.find(marker)
            if index != -1:
                candidates.append((index, len(marker)))
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[0])

    def _decode_event(self, event_bytes: bytes, *, request_id: str = "") -> Optional[dict[str, Any]]:
        try:
            event_text = event_bytes.decode("utf-8")
        except UnicodeDecodeError:
            event_text = event_bytes.decode("utf-8", errors="replace")

        data_lines = []
        for line in event_text.splitlines():
            if not line or line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if not separator:
                continue
            if value.startswith(" "):
                value = value[1:]
            if field == "data":
                data_lines.append(value)
        if not data_lines:
            return None

        raw_data = "\n".join(data_lines).strip()
        if not raw_data or raw_data == "[DONE]":
            return None
        try:
            event = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            suffix = f" requestId={request_id}" if request_id else ""
            raise RuntimeError(f"codex_response_invalid: invalid SSE JSON event{suffix}") from exc
        return event if isinstance(event, dict) else None


class CodexResponsesTransport:
    def __init__(self, *, parser: Optional[CodexSSEParser] = None, proxy: Optional[str] = None) -> None:
        self.parser = parser or CodexSSEParser()
        self.proxies = {"http": proxy, "https": proxy} if proxy else None

    def stream_responses(
        self,
        payload: dict[str, Any],
        tokens: Mapping[str, Any],
        *,
        config: Optional[Mapping[str, Any]] = None,
        request_id: str = "",
    ) -> Generator[dict[str, Any], None, None]:
        cfg = config or {}
        req_id = request_id or uuid4().hex[:12]
        timeout = _int_config(
            cfg,
            "codex_timeout_seconds",
            "codex_direct_timeout_seconds",
            "timeout_seconds",
            "request_timeout",
            default=60,
        )
        max_response_bytes = _int_config(
            cfg,
            "codex_max_response_bytes",
            "codex_direct_max_response_bytes",
            "max_response_bytes",
            default=DEFAULT_MAX_RESPONSE_BYTES,
        )
        max_error_bytes = _int_config(
            cfg,
            "codex_max_error_response_bytes",
            "codex_direct_max_error_response_bytes",
            "max_error_response_bytes",
            default=DEFAULT_MAX_ERROR_RESPONSE_BYTES,
        )
        response = None
        try:
            response = requests.post(
                _responses_url(cfg),
                headers=build_codex_headers(tokens, request_id=req_id, config=cfg),
                json=_clean_payload(payload),
                stream=True,
                timeout=timeout,
                proxies=self.proxies,
            )
            if response.status_code >= 400:
                detail = _limited_response_text(response, max_error_bytes)
                raise RuntimeError(f"{_summarize_http_error(detail, response.status_code)} requestId={req_id}")
            yield from self.parser.iter_json_events(
                response.iter_content(chunk_size=None, decode_unicode=False),
                limit=max_response_bytes,
                request_id=req_id,
            )
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(f"provider_timeout: request timed out after {timeout:g}s requestId={req_id}") from exc
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"provider_network_error: {exc} requestId={req_id}") from exc
        finally:
            if response is not None:
                response.close()


def build_codex_headers(tokens: Mapping[str, Any], *, request_id: str, config: Mapping[str, Any]) -> dict[str, str]:
    token = str(tokens.get("access_token", "") or "").strip()
    headers = {
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "User-Agent": _text_config(
            config,
            "codex_user_agent",
            "codex_direct_user_agent",
            "user_agent",
            default="codex_cli/0.126.0-alpha.8",
        ),
        "Authorization": f"Bearer {token}",
        "originator": _text_config(
            config,
            "codex_originator",
            "codex_direct_originator",
            "originator",
            default="codex_vscode",
        ),
        "x-client-request-id": request_id,
    }
    account_id = str(tokens.get("account_id", "") or "").strip()
    if account_id:
        headers["chatgpt-account-id"] = account_id
    extra = {}
    if isinstance(config, Mapping):
        extra = config.get("codex_extra_headers") or config.get("codex_direct_extra_headers", {})
    if isinstance(extra, Mapping):
        for key, value in extra.items():
            header_name = str(key or "").strip()
            if not header_name or header_name.casefold() in BLOCKED_EXTRA_HEADERS:
                continue
            if isinstance(value, str):
                headers[header_name] = value
    return headers


def _responses_url(config: Mapping[str, Any]) -> str:
    return f"{_base_url(config)}{_endpoint_path(config)}"


def _base_url(config: Mapping[str, Any]) -> str:
    return _text_config(
        config,
        "codex_base_url",
        "codex_direct_base_url",
        "base_url",
        "baseUrl",
        default=CODEX_CHATGPT_DEFAULT_BASE_URL,
    ).rstrip("/")


def _endpoint_path(config: Mapping[str, Any]) -> str:
    raw = _text_config(
        config,
        "codex_endpoint_path",
        "codex_direct_endpoint_path",
        "codex_endpoint_path",
        "endpoint_path",
        "endpointPath",
        default=CODEX_RESPONSES_DEFAULT_ENDPOINT,
    )
    path = raw if raw.startswith("/") else f"/{raw}"
    return path.rstrip("/") or CODEX_RESPONSES_DEFAULT_ENDPOINT


def _clean_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in dict(payload or {}).items() if value is not None}


def _limited_response_text(response: requests.Response, limit: int) -> str:
    try:
        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536, decode_unicode=False):
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                chunks.append(chunk[: max(0, limit - (total - len(chunk)))])
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="ignore")
    except Exception:
        return str(getattr(response, "text", "") or "")[:limit]


def _summarize_http_error(detail: str, status_code: int) -> str:
    title_match = re.search(r"<title>(.*?)</title>", detail or "", flags=re.IGNORECASE | re.DOTALL)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip()[:200] if title_match else ""
    if title:
        return f"provider_http_error: status={status_code} title={title}"
    compact_detail = re.sub(r"\s+", " ", str(detail or "")).strip()[:300]
    if compact_detail:
        return f"provider_http_error: status={status_code} detail={compact_detail}"
    return f"provider_http_error: status={status_code}"


def _text_config(config: Mapping[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = str(config.get(key, "") or "").strip()
        if value:
            return value
    return default


def _int_config(config: Mapping[str, Any], *keys: str, default: int) -> int:
    for key in keys:
        if key not in config:
            continue
        try:
            value = int(config.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return default
