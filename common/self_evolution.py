"""
Lightweight self-evolution memory for reusable execution mistakes.

This module stores side-channel lessons and exposes deterministic local command
policies. Callers should not append records to conversation history or emit
agent events when these policies are applied.
"""

from __future__ import annotations

import json
import os
import platform
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common.log import logger
from common.utils import expand_path


DATA_DIR_NAME = "cowagent-self-evolution"
ERRORS_FILE = "reusable_errors.jsonl"
ACTIVE_RULES_FILE = "active_rules.json"
RULE_ID_WINDOWS_SHELL = "windows-shell-dialect"
RULE_ID_WINDOWS_CMD_SET_QUOTING = "windows-cmd-env-set-quoting"
RULE_ID_WINDOWS_PYTHON_C_QUOTING = "windows-python-c-quoting"
RULE_ID_WINDOWS_NPM_CMD_SHIM = "windows-npm-cmd-shim"

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

WINDOWS_CMD_SET_NEXT_ACTION = (
    "When using cmd.exe, set environment variables before a command with "
    "quoted syntax such as set \"PYTHONUTF8=1\" && python ... so trailing "
    "spaces are not included in the value."
)

WINDOWS_PYTHON_C_NEXT_ACTION = (
    "On Windows cmd.exe, avoid multi-line or heavily quoted python -c snippets; "
    "write a temporary .py file or use a short, single-line command instead."
)

WINDOWS_NPM_CMD_SHIM_NEXT_ACTION = (
    "When Python subprocess launches npm-installed CLIs on Windows, resolve the "
    ".cmd or .exe shim explicitly, for example clawhub.cmd, npx.cmd, or "
    "openclaw.cmd."
)

_LESSONS_BY_ID = {
    RULE_ID_WINDOWS_SHELL: {
        "id": RULE_ID_WINDOWS_SHELL,
        "summary": "Windows shell dialect mistakes in CowAgent bash tool",
        "next_action": WINDOWS_SHELL_NEXT_ACTION,
    },
    RULE_ID_WINDOWS_CMD_SET_QUOTING: {
        "id": RULE_ID_WINDOWS_CMD_SET_QUOTING,
        "summary": "Windows cmd.exe environment assignment needs quoted set syntax",
        "next_action": WINDOWS_CMD_SET_NEXT_ACTION,
    },
    RULE_ID_WINDOWS_PYTHON_C_QUOTING: {
        "id": RULE_ID_WINDOWS_PYTHON_C_QUOTING,
        "summary": "Windows cmd.exe fragile python -c quoting",
        "next_action": WINDOWS_PYTHON_C_NEXT_ACTION,
    },
    RULE_ID_WINDOWS_NPM_CMD_SHIM: {
        "id": RULE_ID_WINDOWS_NPM_CMD_SHIM,
        "summary": "Windows Python subprocess needs npm CLI .cmd shims",
        "next_action": WINDOWS_NPM_CMD_SHIM_NEXT_ACTION,
    },
}


@dataclass(frozen=True)
class WindowsShellPolicyDecision:
    """Deterministic command guard result; contains no persisted history."""

    command: str
    applied_rule_ids: Tuple[str, ...] = ()
    block_reason: str = ""

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
_ACTIVE_RULES_CACHE: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]] = {}
_ACTIVE_RULES_LOCK = threading.Lock()

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

    if _has_unquoted_cmd_set_assignment(command):
        return _lesson(RULE_ID_WINDOWS_CMD_SET_QUOTING)
    if "invalid pythonutf8 environment variable value" in output_lower:
        return _lesson(RULE_ID_WINDOWS_CMD_SET_QUOTING)
    if _uses_fragile_python_c(command) and _looks_like_cmd_quoting_failure(output):
        return _lesson(RULE_ID_WINDOWS_PYTHON_C_QUOTING)
    if _looks_like_missing_npm_shim(command, output):
        return _lesson(RULE_ID_WINDOWS_NPM_CMD_SHIM)

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


def apply_windows_shell_policies(command: str) -> WindowsShellPolicyDecision:
    """
    Apply cheap deterministic Windows shell fixes before cmd.exe execution.

    This intentionally does not read active_rules.json. It is safe to run for
    every bash command because it only uses compiled regex checks in memory.
    """
    original = command or ""
    rewritten = original
    applied: List[str] = []

    rewritten, changed = _quote_cmd_set_assignments(rewritten)
    if changed:
        applied.append(RULE_ID_WINDOWS_CMD_SET_QUOTING)

    if _uses_fragile_python_c(rewritten) and ("\n" in rewritten or "\r" in rewritten):
        return WindowsShellPolicyDecision(
            command=rewritten,
            applied_rule_ids=tuple(dict.fromkeys(applied + [RULE_ID_WINDOWS_PYTHON_C_QUOTING])),
            block_reason=WINDOWS_PYTHON_C_NEXT_ACTION,
        )

    return WindowsShellPolicyDecision(
        command=rewritten,
        applied_rule_ids=tuple(dict.fromkeys(applied)),
    )


def record_windows_shell_policy_application(
    rule_id: str,
    command: str,
    action: str,
    workspace_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Record a deterministic command policy application as a compact lesson."""
    lesson = _lesson(rule_id)
    if not lesson:
        return None

    data_dir = get_data_dir(workspace_root)
    data_dir.mkdir(parents=True, exist_ok=True)
    now = _utc_now()

    event = {
        "id": lesson["id"],
        "summary": lesson["summary"],
        "next_action": lesson["next_action"],
        "event": "policy_applied",
        "action": _preview(action, limit=200),
        "seen_at": now,
        "command_preview": _preview(command),
    }
    _append_jsonl(data_dir / ERRORS_FILE, event)
    rule = _upsert_active_rule(data_dir / ACTIVE_RULES_FILE, lesson, event, now)
    logger.info("[SelfEvolution] Applied reusable shell policy: %s", lesson["id"])
    return rule


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
    rule = _upsert_active_rule(data_dir / ACTIVE_RULES_FILE, lesson, event, now)
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


def get_active_prompt_guidance(limit: int = 8) -> List[str]:
    """Return stable compact guidance lines for request-scoped model context."""
    guidance: List[str] = []
    seen = set()

    own_rules = sorted(
        list_active_rules(include_seed=True),
        key=lambda item: str(item.get("id") or ""),
    )
    for rule in own_rules:
        next_action = str(rule.get("next_action") or "").strip()
        if next_action and next_action not in seen:
            seen.add(next_action)
            guidance.append(next_action)
        if len(guidance) >= limit:
            return guidance

    try:
        from common.tool_attempt_memory import get_active_prompt_guidance as get_tool_prompt_guidance

        tool_guidance = get_tool_prompt_guidance(limit=max(0, limit - len(guidance)))
    except Exception as e:
        logger.debug("[SelfEvolution] Tool-attempt prompt guidance skipped: %s", e)
        tool_guidance = []

    for item in tool_guidance:
        line = str(item or "").strip()
        if line and line not in seen:
            seen.add(line)
            guidance.append(line)
        if len(guidance) >= limit:
            break
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
    cache_key = str(path)
    try:
        mtime = path.stat().st_mtime
        cached = _ACTIVE_RULES_CACHE.get(cache_key)
        if cached and cached[0] == mtime:
            return dict(cached[1])
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
    try:
        _ACTIVE_RULES_CACHE[cache_key] = (mtime, dict(active))
    except Exception:
        pass
    return active


def _write_active_rules(path: Path, active: Dict[str, Dict[str, Any]]) -> None:
    payload = {
        "version": 1,
        "rules": _sorted_rules(active),
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
    try:
        _ACTIVE_RULES_CACHE[str(path)] = (path.stat().st_mtime, dict(active))
    except Exception:
        pass


def _upsert_active_rule(
    path: Path,
    lesson: Dict[str, str],
    event: Dict[str, Any],
    now: str,
) -> Dict[str, Any]:
    with _ACTIVE_RULES_LOCK:
        active = _load_active_rules_map(path)
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
        }
        for key in ("command_preview", "output_preview", "action"):
            if event.get(key):
                rule[key] = event[key]
        active[lesson["id"]] = rule
        _write_active_rules(path, active)
        return rule


def _sorted_rules(active: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        active.values(),
        key=lambda item: (int(item.get("count", 0) or 0), str(item.get("last_seen", ""))),
        reverse=True,
    )


_CMD_SET_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>^|(?:&&|\|\|)\s*)set\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)="
    r"(?P<value>[^&|\r\n]*?)"
    r"(?P<sep>\s*(?:&&|\|\|))",
    re.IGNORECASE,
)


def _quote_cmd_set_assignments(command: str) -> Tuple[str, bool]:
    changed = False

    def replace(match: re.Match[str]) -> str:
        nonlocal changed
        prefix = match.group("prefix")
        name = match.group("name")
        value = match.group("value").strip()
        sep = match.group("sep").strip()
        changed = True
        return f'{prefix}set "{name}={value}" {sep}'

    return _CMD_SET_ASSIGNMENT_RE.sub(replace, command or ""), changed


def _has_unquoted_cmd_set_assignment(command: str) -> bool:
    return bool(_CMD_SET_ASSIGNMENT_RE.search(command or ""))


def _uses_fragile_python_c(command: str) -> bool:
    return bool(re.search(r"(^|[\s&|;()])(?:python|python3|py)(?:\.exe)?\s+-c\s+", command or "", re.IGNORECASE))


def _looks_like_cmd_quoting_failure(output: str) -> bool:
    lower = (output or "").lower()
    markers = (
        'file "<string>"',
        "syntaxerror",
        "indentationerror",
        "was unexpected at this time",
        "the syntax of the command is incorrect",
        "unexpected token",
    )
    return any(marker in lower for marker in markers)


def _looks_like_missing_npm_shim(command: str, output: str) -> bool:
    combined = f"{command}\n{output}".lower()
    if not any(name in combined for name in ("clawhub", "openclaw", "npx")):
        return False
    markers = (
        "[winerror 2]",
        "the system cannot find the file specified",
        "no such file or directory",
        "not recognized as an internal or external command",
    )
    return any(marker in combined for marker in markers)


def _lesson(rule_id: str) -> Optional[Dict[str, str]]:
    lesson = _LESSONS_BY_ID.get(rule_id)
    return dict(lesson) if lesson else None


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
