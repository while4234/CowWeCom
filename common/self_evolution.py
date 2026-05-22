"""
Lightweight self-evolution memory for reusable execution mistakes.

This module is intentionally side-channel only: callers should not append its
records to conversation history, emit agent events, or alter tool results.
"""

from __future__ import annotations

import json
import os
import platform
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.log import logger
from common.utils import expand_path


DATA_DIR_NAME = "cowagent-self-evolution"
ERRORS_FILE = "reusable_errors.jsonl"
ACTIVE_RULES_FILE = "active_rules.json"
RULE_ID_WINDOWS_SHELL = "windows-shell-dialect"

WINDOWS_SHELL_NEXT_ACTION = (
    "On Windows, CowAgent's bash tool runs through cmd.exe. Avoid Bash "
    "heredocs and Unix-only commands such as grep/sed/awk/head/tail; use "
    "python -c, a temporary .py script, cmd-compatible commands, or an "
    "explicit powershell -NoProfile -Command invocation, especially for "
    "QR/image parsing helper commands."
)

WINDOWS_SHELL_SEED_RULE = {
    "id": RULE_ID_WINDOWS_SHELL,
    "summary": "Windows shell dialect mistakes in CowAgent bash tool",
    "next_action": WINDOWS_SHELL_NEXT_ACTION,
    "count": 0,
    "first_seen": "seed",
    "last_seen": "seed",
}

_SECRET_PATTERNS = [
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer <redacted>"),
    (re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{8,})"), "sk-<redacted>"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|token|secret|password|authorization)"
            r"\s*[:=]\s*['\"]?[^'\"\s;&|]+"
        ),
        r"\1=<redacted>",
    ),
]

_UNIX_COMMAND_RE = re.compile(
    r"(^|[\s&|;()])(?:grep|sed|awk|head|tail|cat|touch|chmod|which)\b",
    re.IGNORECASE,
)


def get_data_dir(workspace_root: Optional[str] = None) -> Path:
    """Return the self-evolution data directory under the agent workspace."""
    root = workspace_root or _configured_workspace()
    data_dir = Path(expand_path(str(root))) / "data" / DATA_DIR_NAME
    return data_dir


def classify_windows_shell_failure(command: str, output: str = "") -> Optional[Dict[str, str]]:
    """Classify a failed Windows shell command as a reusable dialect mistake."""
    command = command or ""
    output = output or ""
    command_lower = command.lower()
    output_lower = output.lower()

    triggers: List[str] = []

    if re.search(r"\bpython(?:3|\.exe)?\s+-\s*<<", command_lower):
        triggers.append("Bash heredoc passed to python")
    if "<<" in command and re.search(r"(^|[\s&|;()])(?:python|py)\b", command_lower):
        triggers.append("Unix heredoc syntax")
    if _UNIX_COMMAND_RE.search(command):
        triggers.append("Unix-only command")
    if re.search(r"(^|[\s&|;()])ls\s+-[a-z]", command_lower):
        triggers.append("Unix ls options")
    if re.search(r"(^|[\s&|;()])rm\s+-[rf]+", command_lower):
        triggers.append("Unix rm options")
    if re.search(r"(^|[\s&|;()])/(tmp|mnt|home|usr|var)/", command_lower):
        triggers.append("Unix filesystem path")
    if "no characters are allowed after a here-string header" in output_lower:
        triggers.append("PowerShell here-string quoting")
    if "missing the terminator" in output_lower and "@" in command:
        triggers.append("PowerShell here-string quoting")
    if "not recognized as an internal or external command" in output_lower:
        triggers.append("cmd command not found")
    if "不是内部或外部命令" in output:
        triggers.append("cmd command not found")

    if not triggers:
        return None

    return {
        "id": RULE_ID_WINDOWS_SHELL,
        "summary": "Windows shell dialect mistake: " + ", ".join(sorted(set(triggers))),
        "next_action": WINDOWS_SHELL_NEXT_ACTION,
    }


def record_windows_shell_failure(
    command: str,
    output: str = "",
    exit_code: Optional[int] = None,
    workspace_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Record a failed Windows shell command if it matches a reusable pattern."""
    lesson = classify_windows_shell_failure(command, output)
    if not lesson:
        return None

    data_dir = get_data_dir(workspace_root)
    data_dir.mkdir(parents=True, exist_ok=True)
    now = _utc_now()

    event = {
        "id": lesson["id"],
        "summary": lesson["summary"],
        "next_action": lesson["next_action"],
        "exit_code": exit_code,
        "seen_at": now,
        "command_preview": _preview(command),
        "output_preview": _preview(output),
    }
    _append_jsonl(data_dir / ERRORS_FILE, event)

    active = _load_active_rules_map(data_dir / ACTIVE_RULES_FILE)
    existing = active.get(lesson["id"])
    if existing:
        count = int(existing.get("count", 0) or 0) + 1
        first_seen = existing.get("first_seen") or now
    else:
        count = 1
        first_seen = now

    rule = {
        "id": lesson["id"],
        "summary": lesson["summary"],
        "next_action": lesson["next_action"],
        "count": count,
        "first_seen": first_seen,
        "last_seen": now,
        "command_preview": event["command_preview"],
        "output_preview": event["output_preview"],
    }
    active[lesson["id"]] = rule
    _write_active_rules(data_dir / ACTIVE_RULES_FILE, active)
    logger.info("[SelfEvolution] Recorded reusable shell lesson: %s", lesson["id"])
    return rule


def ensure_seed_rules(workspace_root: Optional[str] = None) -> List[Dict[str, Any]]:
    """Persist default active rules that should be visible before first failure."""
    if platform.system().lower() != "windows":
        return list_active_rules(workspace_root, include_seed=False)

    data_dir = get_data_dir(workspace_root)
    data_dir.mkdir(parents=True, exist_ok=True)
    active = _load_active_rules_map(data_dir / ACTIVE_RULES_FILE)
    existing = active.get(RULE_ID_WINDOWS_SHELL)
    if not existing:
        active[RULE_ID_WINDOWS_SHELL] = dict(WINDOWS_SHELL_SEED_RULE)
        _write_active_rules(data_dir / ACTIVE_RULES_FILE, active)
    elif existing.get("first_seen") == "seed" and int(existing.get("count", 0) or 0) == 0:
        active[RULE_ID_WINDOWS_SHELL] = dict(WINDOWS_SHELL_SEED_RULE)
        _write_active_rules(data_dir / ACTIVE_RULES_FILE, active)
    return _sorted_rules(active)


def list_active_rules(
    workspace_root: Optional[str] = None,
    include_seed: bool = True,
) -> List[Dict[str, Any]]:
    """Read active compact rules for prompt injection or diagnostics."""
    data_dir = get_data_dir(workspace_root)
    active = _load_active_rules_map(data_dir / ACTIVE_RULES_FILE)
    if include_seed and platform.system().lower() == "windows":
        active.setdefault(RULE_ID_WINDOWS_SHELL, dict(WINDOWS_SHELL_SEED_RULE))
    return _sorted_rules(active)


def get_active_prompt_guidance(limit: int = 5) -> List[str]:
    """Return compact hidden guidance lines for the system prompt."""
    guidance = []
    for rule in list_active_rules(include_seed=True)[:limit]:
        next_action = str(rule.get("next_action") or "").strip()
        if next_action:
            guidance.append(next_action)
    return guidance


def _configured_workspace() -> str:
    try:
        from config import conf

        return conf().get("agent_workspace", "~/cow")
    except Exception:
        return "~/cow"


def _append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _load_active_rules_map(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[SelfEvolution] Failed to read active rules: %s", e)
        return {}

    raw_rules: Any
    if isinstance(data, dict) and isinstance(data.get("rules"), list):
        raw_rules = data["rules"]
    elif isinstance(data, list):
        raw_rules = data
    else:
        raw_rules = []

    active: Dict[str, Dict[str, Any]] = {}
    for item in raw_rules:
        if not isinstance(item, dict):
            continue
        rule_id = str(item.get("id") or "").strip()
        if rule_id:
            active[rule_id] = item
    return active


def _write_active_rules(path: Path, active: Dict[str, Dict[str, Any]]) -> None:
    payload = {
        "version": 1,
        "rules": _sorted_rules(active),
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _sorted_rules(active: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        active.values(),
        key=lambda item: (int(item.get("count", 0) or 0), str(item.get("last_seen", ""))),
        reverse=True,
    )


def _preview(text: str, limit: int = 800) -> str:
    text = _redact(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"...({len(text)} chars)"


def _redact(text: str) -> str:
    redacted = text or ""
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
