# encoding:utf-8

"""Safe memory for repeated tool attempts.

This module stores only hashes, counts, classes, and short operational labels.
It must never persist raw tool arguments, tool outputs, prompts, credentials,
or full local paths.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from common.llm_usage_tracker import stable_metadata_hash
from common.log import logger
from common.utils import expand_path


DATA_DIR_NAME = "tool-attempt-memory"
ATTEMPTS_FILE = "attempts.jsonl"
ACTIVE_RULES_FILE = "active_rules.json"

FAILURE_NON_RETRYABLE_ARGS = "non_retryable_args"
FAILURE_SHELL_DIALECT = "shell_dialect"
FAILURE_TRANSIENT = "transient"
FAILURE_UNKNOWN = "unknown_failure"

PERSISTENT_SKIP_FAILURES = {FAILURE_NON_RETRYABLE_ARGS, FAILURE_SHELL_DIALECT}
READONLY_REUSABLE_TOOLS = {"read", "ls", "knowledge_query"}
MUTATING_TOOLS = {"write", "edit", "bash", "send", "browser", "env_config", "scheduler"}


@dataclass
class ToolSkipDecision:
    should_skip: bool
    reason: str = ""
    failure_class: str = ""
    count: int = 0


class ToolAttemptMemory:
    """Record and reuse high-confidence tool-attempt lessons."""

    def __init__(self, workspace_root: Optional[str] = None):
        self.data_dir = get_data_dir(workspace_root)

    def should_skip(self, tool_name: str, args: Dict[str, Any]) -> ToolSkipDecision:
        """Return whether an attempt should be skipped before execution."""
        return self.should_skip_with_rules(tool_name, args, _load_active_rules(self.data_dir))

    def should_skip_with_rules(
        self,
        tool_name: str,
        args: Dict[str, Any],
        rules: Dict[str, Dict[str, Any]],
    ) -> ToolSkipDecision:
        """Return whether an attempt should be skipped using a stable rule snapshot."""
        key = _attempt_key(tool_name, args)
        rule = (rules or {}).get(key)
        if not rule:
            return ToolSkipDecision(False)

        failure_class = str(rule.get("failure_class") or "")
        count = _to_int(rule.get("count"))
        if failure_class not in PERSISTENT_SKIP_FAILURES or count < 3:
            return ToolSkipDecision(False)
        if failure_class == FAILURE_NON_RETRYABLE_ARGS and _path_now_exists_for_retry(tool_name, args):
            return ToolSkipDecision(False)

        last_seen = _parse_time(rule.get("last_seen"))
        if not last_seen or datetime.now(timezone.utc) - last_seen > timedelta(days=7):
            return ToolSkipDecision(False)

        next_action = str(rule.get("next_action") or "").strip()
        reason = (
            f"Known repeated non-retryable tool attempt skipped "
            f"(class={failure_class}, count={count})."
        )
        if next_action:
            reason += f" {next_action}"
        return ToolSkipDecision(True, reason=reason, failure_class=failure_class, count=count)

    def load_rules_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Load active rules for one user request."""
        return _load_active_rules(self.data_dir)

    def record_attempt(
        self,
        tool_name: str,
        args: Dict[str, Any],
        result_status: str,
        result: Any,
        *,
        skipped: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Record one attempt using safe metadata only."""
        tool_name = str(tool_name or "")
        args = args if isinstance(args, dict) else {}
        result_text = _result_preview_source(result)
        failure_class = classify_tool_failure(tool_name, args, result_status, result_text)
        result_hash = stable_metadata_hash(result_text) if result_text else ""
        now = _utc_now()

        record = {
            "tool_name": tool_name,
            "args_hash": stable_metadata_hash(args),
            "args_shape_hash": stable_metadata_hash(_shape(args)),
            "failure_class": failure_class,
            "result_status": str(result_status or ""),
            "result_hash": result_hash,
            "result_chars": len(result_text),
            "skipped": bool(skipped),
            "seen_at": now,
        }

        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            _append_jsonl(self.data_dir / ATTEMPTS_FILE, record)
            if failure_class in PERSISTENT_SKIP_FAILURES:
                self._upsert_active_rule(record, now)
        except Exception as exc:
            logger.debug("[ToolAttemptMemory] Failed to record attempt: %s", exc)
            return None
        return record

    def _upsert_active_rule(self, record: Dict[str, Any], now: str) -> None:
        rules = _load_active_rules(self.data_dir)
        key = _record_key(record)
        existing = rules.get(key)
        count = _to_int(existing.get("count")) + 1 if existing else 1
        first_seen = (existing or {}).get("first_seen") or now
        rules[key] = {
            "key": key,
            "tool_name": record.get("tool_name"),
            "args_hash": record.get("args_hash"),
            "args_shape_hash": record.get("args_shape_hash"),
            "failure_class": record.get("failure_class"),
            "count": count,
            "first_seen": first_seen,
            "last_seen": now,
            "next_action": _next_action(record.get("failure_class")),
        }
        _write_active_rules(self.data_dir / ACTIVE_RULES_FILE, rules)


def get_data_dir(workspace_root: Optional[str] = None) -> Path:
    root = workspace_root or _configured_workspace()
    return Path(expand_path(str(root))) / "data" / DATA_DIR_NAME


def classify_tool_failure(
    tool_name: str,
    args: Dict[str, Any],
    result_status: str,
    result_text: str,
) -> str:
    status = str(result_status or "").lower()
    if status in ("success", "ok"):
        return ""

    text = str(result_text or "")
    lower = text.lower()
    tool_name = str(tool_name or "")

    if tool_name == "bash":
        try:
            from common.self_evolution import classify_windows_shell_failure

            command = str((args or {}).get("command") or "")
            if classify_windows_shell_failure(command, text):
                return FAILURE_SHELL_DIALECT
        except Exception:
            pass

    if f"class={FAILURE_NON_RETRYABLE_ARGS}" in lower:
        return FAILURE_NON_RETRYABLE_ARGS
    if f"class={FAILURE_SHELL_DIALECT}" in lower:
        return FAILURE_SHELL_DIALECT

    transient_markers = (
        "connection error",
        "timed out",
        "timeout",
        "ssl",
        "eof occurred",
        "http 5",
        "temporarily unavailable",
        "rate limit",
    )
    if any(marker in lower for marker in transient_markers):
        return FAILURE_TRANSIENT

    non_retryable_markers = (
        "unsupported",
        "unknown action",
        "required",
        "offset",
        "beyond end",
        "file not found",
        "path not found",
        "access denied",
        "permission denied",
        "outside workspace",
        "not readable",
        "not a directory",
        "invalid url",
        "invalid json",
    )
    if any(marker in lower for marker in non_retryable_markers):
        return FAILURE_NON_RETRYABLE_ARGS
    return FAILURE_UNKNOWN


def is_readonly_reusable_tool(tool_name: str) -> bool:
    return str(tool_name or "") in READONLY_REUSABLE_TOOLS


def is_mutating_tool(tool_name: str) -> bool:
    return str(tool_name or "") in MUTATING_TOOLS


def _attempt_key(tool_name: str, args: Dict[str, Any]) -> str:
    return f"{tool_name}:{stable_metadata_hash(args)}"


def _record_key(record: Dict[str, Any]) -> str:
    return f"{record.get('tool_name')}:{record.get('args_hash')}"


def _result_preview_source(result: Any) -> str:
    if isinstance(result, (dict, list)):
        try:
            return json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
        except TypeError:
            return str(result)
    return str(result or "")


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _shape(value[key]) for key in sorted(value.keys(), key=lambda item: str(item))}
    if isinstance(value, list):
        return [_shape(item) for item in value[:3]]
    if isinstance(value, tuple):
        return tuple(_shape(item) for item in value[:3])
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, int):
        return "<int>"
    if isinstance(value, float):
        return "<float>"
    if value is None:
        return "<none>"
    return "<str>"


def _next_action(failure_class: Any) -> str:
    if failure_class == FAILURE_SHELL_DIALECT:
        return "Use a Windows-compatible command or PowerShell syntax instead."
    if failure_class == FAILURE_NON_RETRYABLE_ARGS:
        return "Change the tool arguments or choose a different tool before retrying."
    return "Try a different approach."


def _path_now_exists_for_retry(tool_name: str, args: Dict[str, Any]) -> bool:
    if str(tool_name or "") not in {"read", "ls"} or not isinstance(args, dict):
        return False
    value = args.get("path") or args.get("cwd")
    if not value:
        return False
    try:
        return Path(expand_path(str(value))).exists()
    except Exception:
        return False


def _configured_workspace() -> str:
    try:
        from config import conf

        return conf().get("agent_workspace", "~/cow")
    except Exception:
        return "~/cow"


def _load_active_rules(data_dir: Path) -> Dict[str, Dict[str, Any]]:
    path = data_dir / ACTIVE_RULES_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rules = data.get("rules") if isinstance(data, dict) else []
    if not isinstance(rules, list):
        return {}
    result = {}
    for item in rules:
        if isinstance(item, dict) and item.get("key"):
            result[str(item["key"])] = item
    return result


def _write_active_rules(path: Path, rules: Dict[str, Dict[str, Any]]) -> None:
    payload = {
        "version": 1,
        "rules": sorted(
            rules.values(),
            key=lambda item: (_to_int(item.get("count")), str(item.get("last_seen") or "")),
            reverse=True,
        ),
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
