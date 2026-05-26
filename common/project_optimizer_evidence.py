# encoding:utf-8

"""Local-only evidence store for CowWeCom project optimization.

This module records enough runtime evidence to analyze cache hit rate,
reasoning-effort routing, tool loops, and repeated temporary scripts without
putting private user data in Git. Raw records are intentionally kept under the
ignored Agent workspace and can be consumed/deleted by the project optimizer
skill.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from common.log import logger
from common.utils import expand_path


EVENT_SUBDIR = "events"
RAW_SUBDIR = "raw_model_inputs"
TEMP_SCRIPT_SUBDIR = "temp_scripts"

_LOCK = threading.Lock()
_SCRIPT_EXTENSIONS = {".py", ".ps1", ".sh", ".js", ".ts", ".mjs", ".cjs", ".bat", ".cmd"}
_TEMP_PATH_PARTS = {"tmp", "temp", "sandbox", "workspace"}
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|cookie|authorization|credential|"
    r"access[_-]?token|refresh[_-]?token)",
    re.IGNORECASE,
)
_SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)\bsk-[a-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\bghp_[a-z0-9_]{20,}\b"),
    re.compile(r"(?i)\bgithub_pat_[a-z0-9_]{20,}\b"),
    re.compile(r"(?i)\b(bearer|basic)\s+[a-z0-9._~+/=-]{16,}"),
    re.compile(r"(?is)-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----.*?-----END (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
)


def record_agent_task_start(
    user_message: str,
    *,
    model_adapter: Any = None,
    reasoning_decision: Any = None,
) -> str:
    """Record a user-visible task start and raw user input when enabled."""
    event_id = _new_id()
    metadata = _identity_metadata(model_adapter)
    if reasoning_decision is not None:
        metadata.update(_reasoning_metadata(reasoning_decision))

    event = {
        "event_type": "agent_task_start",
        "event_id": event_id,
        **metadata,
        "message_hash": _hash_value(user_message),
        "message_chars": len(str(user_message or "")),
        "message_features": _text_features(user_message),
    }
    _append_event(event)
    _append_raw_record(
        {
            "event_type": "agent_task_start",
            "event_id": event_id,
            **metadata,
            "user_message": user_message,
        }
    )
    return event_id


def record_agent_task_end(
    task_event_id: str,
    *,
    model_adapter: Any = None,
    status: str = "",
    turn_count: int = 0,
    final_response: str = "",
    failure_reason: str = "",
    runtime_stats: Optional[Mapping[str, Any]] = None,
) -> None:
    """Record a sanitized task outcome."""
    event = {
        "event_type": "agent_task_end",
        "event_id": _new_id(),
        "task_event_id": _safe_text(task_event_id, 32),
        **_identity_metadata(model_adapter),
        "status": _safe_text(status, 64),
        "turn_count": max(0, int(turn_count or 0)),
        "final_response_hash": _hash_value(final_response) if final_response else "",
        "final_response_chars": len(str(final_response or "")),
        "failure_reason": _safe_text(failure_reason, 160),
    }
    if runtime_stats:
        for key in (
            "tool_attempt_count",
            "tool_attempt_success_count",
            "tool_attempt_error_count",
            "tool_skip_count",
            "tool_failure_class",
        ):
            if key in runtime_stats:
                event[key] = runtime_stats[key]
    _append_event(event)


def record_llm_request(request: Any, *, metadata: Optional[Mapping[str, Any]] = None) -> str:
    """Record the Agent-side LLMRequest just before it is handed to a provider."""
    request_id = _safe_text((metadata or {}).get("project_optimizer_request_id"), 32) or _new_id()
    payload = {
        "model": getattr(request, "model", None),
        "messages": getattr(request, "messages", None),
        "tools": getattr(request, "tools", None),
        "system": getattr(request, "system", None),
        "temperature": getattr(request, "temperature", None),
        "max_tokens": getattr(request, "max_tokens", None),
        "stream": getattr(request, "stream", None),
        "reasoning_effort": getattr(request, "reasoning_effort", None),
        "reasoning_effort_locked": getattr(request, "reasoning_effort_locked", None),
        "cache_shape_metadata": getattr(request, "cache_shape_metadata", None),
    }
    metadata = dict(metadata or {})
    metadata["project_optimizer_request_id"] = request_id

    event = {
        "event_type": "llm_request",
        "event_id": request_id,
        **_safe_metadata(metadata),
        **_payload_summary(payload),
    }
    _append_event(event)
    _append_raw_record({
        "event_type": "llm_request",
        "event_id": request_id,
        "metadata": _safe_metadata(metadata),
        "payload": payload,
    })
    return request_id


def record_provider_payload(
    *,
    wire_api: str,
    payload: Mapping[str, Any],
    metadata: Optional[Mapping[str, Any]] = None,
) -> str:
    """Record the final provider payload shape and optional raw local payload."""
    metadata = dict(metadata or {})
    request_id = _safe_text(metadata.get("project_optimizer_request_id"), 32) or _new_id()
    event = {
        "event_type": "provider_payload",
        "event_id": _new_id(),
        "request_event_id": request_id,
        "wire_api": _safe_text(wire_api, 32),
        **_safe_metadata(metadata),
        **_payload_summary(payload),
        "prompt_cache_key_hash": _hash_value(payload.get("prompt_cache_key")) if payload.get("prompt_cache_key") else "",
        "prompt_cache_retention": _safe_text(payload.get("prompt_cache_retention"), 32),
        "store": payload.get("store") if isinstance(payload.get("store"), bool) else "",
    }
    event["reasoning_effort"] = _payload_reasoning_effort(payload)
    _append_event(event)
    _append_raw_record({
        "event_type": "provider_payload",
        "event_id": event["event_id"],
        "request_event_id": request_id,
        "wire_api": wire_api,
        "metadata": _safe_metadata(metadata),
        "payload": dict(payload),
    })
    return request_id


def record_tool_event(
    *,
    tool_name: str,
    tool_call_id: str = "",
    arguments: Optional[Mapping[str, Any]] = None,
    result: Any = None,
    status: str = "",
    execution_time: float = 0.0,
    model_adapter: Any = None,
) -> None:
    """Record sanitized tool step evidence, with raw local args/results if enabled."""
    args = dict(arguments or {})
    result_text = _compact_text(result)
    event = {
        "event_type": "tool_event",
        "event_id": _new_id(),
        **_identity_metadata(model_adapter),
        "tool_name": _safe_text(tool_name, 80),
        "tool_call_id_hash": _hash_value(tool_call_id) if tool_call_id else "",
        "status": _safe_text(status, 32),
        "execution_time_ms": int(max(0.0, float(execution_time or 0.0)) * 1000),
        "argument_shape_hash": _hash_value(_argument_shape(args)) if args else "",
        "argument_chars": len(_compact_text(args)),
        "result_hash": _hash_value(result_text) if result_text else "",
        "result_chars": len(result_text),
    }
    _append_event(event)
    _append_raw_record({
        "event_type": "tool_event",
        "event_id": event["event_id"],
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "status": status,
        "execution_time": execution_time,
        "arguments": args,
        "result": result,
    })


def archive_temp_script(
    path: str,
    *,
    content: Optional[str] = None,
    cwd: str = "",
    source: str = "",
    visible_path: str = "",
) -> Optional[dict[str, Any]]:
    """Snapshot a temporary script into the local optimizer evidence archive."""
    if not _enabled() or not _config_bool("project_optimizer_preserve_temp_scripts", True):
        return None
    resolved = _resolve_path(path, cwd)
    if not _is_temp_script_candidate(resolved, visible_path=visible_path):
        return None
    data_dir = _data_dir()
    if _is_relative_to(resolved, data_dir):
        return None

    try:
        if content is None:
            if not resolved.is_file():
                return None
            max_bytes = _config_int("project_optimizer_temp_script_max_bytes", 1_000_000)
            if resolved.stat().st_size > max_bytes:
                return _append_temp_script_manifest(
                    resolved,
                    source=source,
                    visible_path=visible_path,
                    archived_path="",
                    digest="",
                    bytes_written=resolved.stat().st_size,
                    skipped_reason="too_large",
                )
            raw_bytes = resolved.read_bytes()
            content = raw_bytes.decode("utf-8", errors="replace")

        raw_bytes = str(content or "").encode("utf-8", errors="replace")
        max_bytes = _config_int("project_optimizer_temp_script_max_bytes", 1_000_000)
        if len(raw_bytes) > max_bytes:
            return _append_temp_script_manifest(
                resolved,
                source=source,
                visible_path=visible_path,
                archived_path="",
                digest="",
                bytes_written=len(raw_bytes),
                skipped_reason="too_large",
            )
        digest = hashlib.sha256(raw_bytes).hexdigest()[:16]
        safe_name = _safe_filename(resolved.name)
        date = _utc_now().date().isoformat()
        archive_dir = data_dir / TEMP_SCRIPT_SUBDIR / "files" / date
        archive_dir.mkdir(parents=True, exist_ok=True)
        archived = archive_dir / f"{digest}_{safe_name}"
        if not archived.exists():
            archived.write_bytes(raw_bytes)
        return _append_temp_script_manifest(
            resolved,
            source=source,
            visible_path=visible_path,
            archived_path=str(archived),
            digest=digest,
            bytes_written=len(raw_bytes),
        )
    except Exception as exc:
        logger.debug(f"[ProjectOptimizer] Failed to archive temp script {path}: {exc}")
        return None


def consume_raw_input_cache(*, reason: str = "optimizer_run") -> dict[str, Any]:
    """Delete raw optimizer input cache after a successful optimizer run."""
    raw_dir = _data_dir() / RAW_SUBDIR
    files = sorted(raw_dir.glob("*.jsonl")) if raw_dir.exists() else []
    records = 0
    bytes_deleted = 0
    deleted_files = []
    with _LOCK:
        for path in files:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    records += sum(1 for line in fh if line.strip())
                bytes_deleted += path.stat().st_size
                path.unlink()
                deleted_files.append(str(path))
            except FileNotFoundError:
                continue
            except Exception as exc:
                logger.debug(f"[ProjectOptimizer] Failed to delete raw cache {path}: {exc}")
    _append_event({
        "event_type": "raw_input_cache_consumed",
        "event_id": _new_id(),
        "reason": _safe_text(reason, 80),
        "deleted_file_count": len(deleted_files),
        "deleted_record_count": records,
        "deleted_bytes": bytes_deleted,
    })
    return {
        "deleted_file_count": len(deleted_files),
        "deleted_record_count": records,
        "deleted_bytes": bytes_deleted,
    }


def data_dir() -> Path:
    return _data_dir()


def event_files() -> list[Path]:
    events_dir = _data_dir() / EVENT_SUBDIR
    return sorted(events_dir.glob("*.jsonl")) if events_dir.exists() else []


def raw_files() -> list[Path]:
    raw_dir = _data_dir() / RAW_SUBDIR
    return sorted(raw_dir.glob("*.jsonl")) if raw_dir.exists() else []


def temp_script_manifest_path() -> Path:
    return _data_dir() / TEMP_SCRIPT_SUBDIR / "manifest.jsonl"


def _append_temp_script_manifest(
    path: Path,
    *,
    source: str,
    visible_path: str,
    archived_path: str,
    digest: str,
    bytes_written: int,
    skipped_reason: str = "",
) -> dict[str, Any]:
    record = {
        "timestamp": _utc_now().isoformat(),
        "event_type": "temp_script_snapshot",
        "event_id": _new_id(),
        "source": _safe_text(source, 80),
        "original_path": str(path),
        "visible_path": _safe_text(visible_path or str(path), 260),
        "basename": path.name,
        "original_path_hash": _hash_value(str(path)),
        "archived_path": archived_path,
        "content_hash": digest,
        "bytes": max(0, int(bytes_written or 0)),
        "skipped_reason": _safe_text(skipped_reason, 80),
    }
    manifest = temp_script_manifest_path()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with manifest.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    _append_event({
        "event_type": "temp_script_snapshot",
        "event_id": record["event_id"],
        "source": record["source"],
        "basename": record["basename"],
        "original_path_hash": record["original_path_hash"],
        "content_hash": record["content_hash"],
        "bytes": record["bytes"],
        "skipped_reason": record["skipped_reason"],
    })
    return record


def _append_event(record: Mapping[str, Any]) -> None:
    if not _enabled():
        return
    clean = {
        "timestamp": _utc_now().isoformat(),
        **{key: value for key, value in dict(record).items() if value not in ("", None)},
    }
    path = _data_dir() / EVENT_SUBDIR / f"{_utc_now().date().isoformat()}.jsonl"
    _append_jsonl(path, clean)


def _append_raw_record(record: Mapping[str, Any]) -> None:
    if not _enabled() or not _raw_enabled():
        return
    clean = {
        "timestamp": _utc_now().isoformat(),
        **_sanitize_raw(dict(record)),
    }
    path = _data_dir() / RAW_SUBDIR / f"{_utc_now().date().isoformat()}.jsonl"
    _append_jsonl(path, clean)


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
        max_chars = _config_int("project_optimizer_raw_max_payload_chars", 250_000)
        if len(text) > max_chars:
            clipped = dict(record)
            clipped["_truncated"] = True
            clipped["_original_chars"] = len(text)
            text = json.dumps(_truncate_to_json_safe(clipped), ensure_ascii=False, separators=(",", ":"), default=str)
        with _LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(text + "\n")
    except Exception as exc:
        logger.debug(f"[ProjectOptimizer] Failed to append evidence: {exc}")


def _payload_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    messages = payload.get("messages") or payload.get("input") or []
    tools = payload.get("tools") or []
    system = payload.get("system") or payload.get("instructions") or ""
    content_text = _compact_text(messages)
    summary = {
        "model": _safe_text(payload.get("model"), 96),
        "system_hash": _hash_value(system) if system else "",
        "system_chars": len(str(system or "")),
        "messages_hash": _hash_value(messages) if messages else "",
        "messages_chars": len(content_text),
        "message_count": len(messages) if isinstance(messages, list) else 1 if messages else 0,
        "tools_hash": _hash_value(tools) if tools else "",
        "tool_count": len(tools) if isinstance(tools, list) else 1 if tools else 0,
        "temperature": payload.get("temperature"),
        "max_tokens": payload.get("max_tokens") or payload.get("max_output_tokens") or "",
        "stream": payload.get("stream") if isinstance(payload.get("stream"), bool) else "",
    }
    metadata = payload.get("cache_shape_metadata")
    if isinstance(metadata, Mapping):
        for key in (
            "request_kind",
            "messages_prefix_hash",
            "runtime_context_chars",
            "self_evolution_context_chars",
            "retrieved_knowledge_chars",
            "tool_result_chars",
            "tool_attempt_count",
            "tool_attempt_error_count",
            "tool_skip_count",
            "reasoning_effort_selected",
            "reasoning_effort_local_rule",
        ):
            if metadata.get(key) not in ("", None):
                summary[key] = metadata.get(key)
    return summary


def _payload_reasoning_effort(payload: Mapping[str, Any]) -> str:
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, Mapping):
        return _safe_text(reasoning.get("effort"), 24)
    return _safe_text(payload.get("reasoning_effort"), 24)


def _safe_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    safe = {
        "project_optimizer_request_id": _safe_text(metadata.get("project_optimizer_request_id"), 32),
        "channel_type": _safe_text(metadata.get("channel_type"), 64),
        "wire_api": _safe_text(metadata.get("wire_api"), 32),
        "model": _safe_text(metadata.get("model"), 96),
        "user_label": _safe_text(metadata.get("user_label"), 160),
    }
    session_id = metadata.get("session_id")
    user_id = metadata.get("user_id")
    if session_id:
        safe["session_hash"] = _hash_value(session_id)
    if user_id:
        safe["user_hash"] = _hash_value(user_id)
    for key, value in metadata.items():
        if key.startswith("reasoning_effort_") or key in {"request_kind"}:
            safe[key] = _safe_text(value, 120)
    return {key: value for key, value in safe.items() if value not in ("", None)}


def _identity_metadata(model_adapter: Any) -> dict[str, Any]:
    if model_adapter is None:
        return {}
    return _safe_metadata({
        "channel_type": getattr(model_adapter, "channel_type", ""),
        "session_id": getattr(model_adapter, "session_id", ""),
        "user_id": getattr(model_adapter, "user_id", ""),
        "user_label": getattr(model_adapter, "user_label", ""),
    }) | {
        "is_group": bool(getattr(model_adapter, "is_group", False)),
        "actor_role": _safe_text(getattr(model_adapter, "actor_role", ""), 32),
    }


def _reasoning_metadata(decision: Any) -> dict[str, Any]:
    return {
        "reasoning_effort_task_id": _safe_text(getattr(decision, "task_id", ""), 32),
        "reasoning_effort_selected": _safe_text(getattr(decision, "selected_effort", ""), 24),
        "reasoning_effort_decision_source": _safe_text(getattr(decision, "decision_source", ""), 64),
        "reasoning_effort_reason": _safe_text(getattr(decision, "reason", ""), 120),
        "reasoning_effort_local_rule": _safe_text(getattr(decision, "local_rule", ""), 96),
    }


def _sanitize_raw(value: Any) -> Any:
    max_string_chars = _config_int("project_optimizer_raw_max_string_chars", 20_000)
    if isinstance(value, Mapping):
        if str(value.get("type") or "").lower() == "thinking":
            return {"type": "thinking", "thinking": "[REDACTED_REASONING]"}
        clean = {}
        for key, item in value.items():
            key_text = str(key)
            if _SECRET_KEY_RE.search(key_text):
                clean[key_text] = "[REDACTED_SECRET]"
            elif key_text in {"reasoning_content", "thinking"}:
                clean[key_text] = "[REDACTED_REASONING]"
            else:
                clean[key_text] = _sanitize_raw(item)
        return clean
    if isinstance(value, list):
        return [_sanitize_raw(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_raw(item) for item in value]
    if isinstance(value, str):
        text = value
        for pattern in _SECRET_TEXT_PATTERNS:
            text = pattern.sub("[REDACTED_SECRET]", text)
        if len(text) > max_string_chars:
            return text[:max_string_chars] + f"\n[TRUNCATED {len(text) - max_string_chars} chars]"
        return text
    return value


def _truncate_to_json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _truncate_to_json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate_to_json_safe(item) for item in value]
    if isinstance(value, str):
        return value[:2000] + ("..." if len(value) > 2000 else "")
    return value


def _argument_shape(args: Mapping[str, Any]) -> dict[str, str]:
    shape = {}
    for key, value in sorted(args.items(), key=lambda item: str(item[0])):
        if isinstance(value, Mapping):
            shape[str(key)] = "object"
        elif isinstance(value, list):
            shape[str(key)] = "array"
        else:
            shape[str(key)] = type(value).__name__
    return shape


def _text_features(text: Any) -> dict[str, Any]:
    value = str(text or "")
    return {
        "chars": len(value),
        "has_url": bool(re.search(r"https?://|www\.", value, re.IGNORECASE)),
        "has_code_signal": bool(re.search(r"```|\b(python|javascript|typescript|sql|docker|git)\b", value, re.IGNORECASE)),
        "has_file_path_signal": bool(re.search(r"[A-Za-z]:\\|/[^/\s]+/|\\[^\\\s]+\\", value)),
        "line_count": value.count("\n") + 1 if value else 0,
    }


def _compact_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return str(value or "")


def _resolve_path(path: str, cwd: str = "") -> Path:
    expanded = expand_path(path)
    if os.path.isabs(expanded):
        return Path(expanded).resolve()
    return Path(cwd or os.getcwd(), expanded).resolve()


def _is_temp_script_candidate(path: Path, *, visible_path: str = "") -> bool:
    visible = str(visible_path or "").strip()
    suffix_source = visible or str(path)
    if Path(suffix_source).suffix.lower() not in _SCRIPT_EXTENSIONS:
        return False
    if visible:
        parts = {part.lower() for part in re.split(r"[\\/]+", visible) if part}
        name = Path(visible).name.lower()
    else:
        parts = {part.lower() for part in path.parts}
        name = path.name.lower()
    if parts & _TEMP_PATH_PARTS:
        return True
    return name.startswith(("tmp", "temp", "scratch"))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _data_dir() -> Path:
    try:
        from config import conf

        configured = str(conf().get("project_optimizer_data_dir") or "").strip()
        if configured:
            return Path(expand_path(configured)).resolve()
        workspace = expand_path(conf().get("agent_workspace", "~/cow"))
    except Exception:
        workspace = expand_path("~/cow")
    return Path(workspace).resolve() / "data" / "project-optimizer"


def _enabled() -> bool:
    return _config_bool("project_optimizer_evidence_enabled", False)


def _raw_enabled() -> bool:
    return _config_bool("project_optimizer_raw_capture_enabled", False)


def _config_bool(key: str, default: bool) -> bool:
    try:
        from config import conf

        return bool(conf().get(key, default))
    except Exception:
        return default


def _config_int(key: str, default: int) -> int:
    try:
        from config import conf

        value = int(conf().get(key, default) or default)
    except Exception:
        value = default
    return max(0, value)


def _hash_value(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        text = str(value)
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _safe_text(value: Any, max_len: int = 96) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text[:max_len]


def _safe_filename(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "script")
    return text[:120] or "script"


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
