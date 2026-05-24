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
import queue
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common.log import logger
from common.utils import expand_path


DATA_DIR_NAME = "cowagent-self-evolution"
ERRORS_FILE = "reusable_errors.jsonl"
ACTIVE_RULES_FILE = "active_rules.json"
REFLECTIONS_FILE = "post_task_reflections.jsonl"
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
    (re.compile(r"(?i)\b[A-Z]:\\(?:[^\\/:*?\"<>|\s]+\\)*[^\\/:*?\"<>|\s]*"), "<path>"),
    (re.compile(r"(?i)(?:/Users|/home|/mnt|/tmp)/[^\s'\";]+"), "<path>"),
]
_ACTIVE_RULES_CACHE: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]] = {}
_ACTIVE_RULES_LOCK = threading.Lock()
_REFLECTION_QUEUE: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=10)
_REFLECTION_WORKER_LOCK = threading.Lock()
_REFLECTION_WORKER_STARTED = False

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


def record_reusable_learning(
    rule_id: str,
    summary: str,
    next_action: str,
    details: str = "",
    workspace_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Record a manual reusable workflow lesson as an active compact rule."""
    normalized_id = _normalize_rule_id(rule_id or summary)
    summary = _preview(summary, limit=240)
    next_action = _preview(next_action, limit=600)
    details = _preview(details, limit=800)
    if not normalized_id or not summary or not next_action:
        return None

    data_dir = get_data_dir(workspace_root)
    data_dir.mkdir(parents=True, exist_ok=True)
    now = _utc_now()

    lesson = {
        "id": normalized_id,
        "summary": summary,
        "next_action": next_action,
    }
    event = {
        **lesson,
        "event": "manual_learning",
        "seen_at": now,
    }
    if details:
        event["details_preview"] = details

    _append_jsonl(data_dir / ERRORS_FILE, event)
    rule = _upsert_active_rule(data_dir / ACTIVE_RULES_FILE, lesson, event, now)
    logger.info("[SelfEvolution] Recorded reusable manual lesson: %s", normalized_id)
    return rule


def extract_intermediate_process_texts(
    messages: Optional[List[Dict[str, Any]]] = None,
    final_response: str = "",
    *,
    extra_texts: Optional[List[str]] = None,
    max_items: int = 12,
    max_chars: int = 6000,
) -> List[str]:
    """Extract assistant progress text that preceded tool calls in this run."""
    texts: List[str] = []
    final_normalized = _normalize_text_for_compare(final_response)

    for message in messages or []:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        text, has_tool_use = _assistant_text_and_tool_use(message)
        if not text or not has_tool_use:
            continue
        if final_normalized and _normalize_text_for_compare(text) == final_normalized:
            continue
        texts.append(text)

    for text in extra_texts or []:
        clean = str(text or "").strip()
        if clean:
            texts.append(clean)

    return _dedupe_and_bound_texts(texts, max_items=max_items, max_chars=max_chars)


def schedule_post_task_reflection(
    *,
    model_adapter: Any = None,
    new_messages: Optional[List[Dict[str, Any]]] = None,
    final_response: str = "",
    intermediate_texts: Optional[List[str]] = None,
    workspace_root: Optional[str] = None,
) -> bool:
    """Queue post-task self-evolution reflection without blocking the reply path."""
    if not _config_bool("cowagent_self_evolution_post_task_enabled", True):
        return False

    max_items = _config_int("cowagent_self_evolution_post_task_max_texts", 12, minimum=1, maximum=30)
    max_chars = _config_int("cowagent_self_evolution_post_task_max_chars", 6000, minimum=500, maximum=20000)
    texts = extract_intermediate_process_texts(
        new_messages,
        final_response,
        extra_texts=intermediate_texts,
        max_items=max_items,
        max_chars=max_chars,
    )
    payload = {
        "model_adapter": model_adapter,
        "intermediate_texts": texts,
        "workspace_root": workspace_root,
        "reason": "post_task",
    }
    try:
        _reflection_queue().put_nowait(payload)
    except queue.Full:
        logger.debug("[SelfEvolution] Post-task reflection queue is full; skipping this run")
        return False
    _ensure_reflection_worker()
    return True


def run_post_task_reflection_once(
    *,
    model_adapter: Any = None,
    intermediate_texts: Optional[List[str]] = None,
    workspace_root: Optional[str] = None,
    reason: str = "manual",
) -> Dict[str, Any]:
    """
    Refresh tool-attempt lessons, then mine assistant process text for lessons.

    The input is already reduced to assistant progress statements; tool args,
    tool outputs, final answers, and user prompts are intentionally excluded.
    """
    started = _utc_now()
    texts = _dedupe_and_bound_texts(
        intermediate_texts or [],
        max_items=_config_int("cowagent_self_evolution_post_task_max_texts", 12, minimum=1, maximum=30),
        max_chars=_config_int("cowagent_self_evolution_post_task_max_chars", 6000, minimum=500, maximum=20000),
    )
    report: Dict[str, Any] = {
        "timestamp": started,
        "event": "post_task_reflection",
        "reason": _preview(reason, limit=80),
        "status": "skipped",
        "process_text_count": len(texts),
        "process_text_hash": _hash_texts(texts),
    }

    tool_rule_count = 0
    try:
        ensure_seed_rules(workspace_root)
        from common.tool_attempt_memory import list_active_rules as list_tool_attempt_rules

        tool_rule_count = len(list_tool_attempt_rules(workspace_root))
    except Exception as exc:
        report["tool_error_refresh_status"] = "failed"
        report["tool_error_refresh_error"] = _preview(str(exc), limit=180)
    else:
        report["tool_error_refresh_status"] = "ok"
    report["tool_attempt_rule_count"] = tool_rule_count

    if not texts:
        _append_reflection_report(workspace_root, report)
        return report

    try:
        existing_ids = {str(rule.get("id") or "") for rule in list_active_rules(workspace_root, include_seed=True)}
        lessons = _infer_process_lessons(texts)
        lessons.extend(_ask_model_for_process_lessons(model_adapter, texts, workspace_root))
        lessons = _normalize_reflection_lessons(lessons)

        recorded: List[Dict[str, Any]] = []
        for lesson in lessons:
            rule = record_reusable_learning(
                lesson["id"],
                lesson["summary"],
                lesson["next_action"],
                details=lesson.get("details", ""),
                workspace_root=workspace_root,
            )
            if not rule:
                continue
            recorded.append({
                "id": rule.get("id"),
                "count": rule.get("count"),
                "new": str(rule.get("id") or "") not in existing_ids,
            })
            existing_ids.add(str(rule.get("id") or ""))

        if recorded:
            report["status"] = "success"
            report["recorded_lessons"] = recorded
            report["guidance_after"] = get_active_prompt_guidance(limit=8, workspace_root=workspace_root)
        else:
            report["status"] = "skipped"
            report["skip_reason"] = "no_reusable_process_lessons"
    except Exception as exc:
        report["status"] = "failed"
        report["failure_reason"] = _preview(str(exc), limit=240)

    _append_reflection_report(workspace_root, report)
    return report


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


def get_active_prompt_guidance(limit: int = 8, workspace_root: Optional[str] = None) -> List[str]:
    """Return stable compact guidance lines for request-scoped model context."""
    guidance: List[str] = []
    seen = set()

    own_rules = sorted(
        list_active_rules(workspace_root, include_seed=True),
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

        tool_guidance = get_tool_prompt_guidance(limit=max(0, limit - len(guidance)), workspace_root=workspace_root)
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


def _reflection_queue() -> "queue.Queue[Dict[str, Any]]":
    global _REFLECTION_QUEUE
    configured_size = _config_int("cowagent_self_evolution_post_task_queue_size", 10, minimum=1, maximum=100)
    if _REFLECTION_QUEUE.maxsize == configured_size:
        return _REFLECTION_QUEUE
    with _REFLECTION_WORKER_LOCK:
        if _REFLECTION_QUEUE.maxsize != configured_size and _REFLECTION_QUEUE.empty():
            _REFLECTION_QUEUE = queue.Queue(maxsize=configured_size)
    return _REFLECTION_QUEUE


def _ensure_reflection_worker() -> None:
    global _REFLECTION_WORKER_STARTED
    with _REFLECTION_WORKER_LOCK:
        if _REFLECTION_WORKER_STARTED:
            return
        thread = threading.Thread(
            target=_reflection_worker_loop,
            name="cowagent-self-evolution-reflection",
            daemon=True,
        )
        _REFLECTION_WORKER_STARTED = True
        thread.start()


def _reflection_worker_loop() -> None:
    global _REFLECTION_WORKER_STARTED
    try:
        while True:
            try:
                payload = _reflection_queue().get(timeout=3)
            except queue.Empty:
                with _REFLECTION_WORKER_LOCK:
                    if _reflection_queue().empty():
                        _REFLECTION_WORKER_STARTED = False
                        return
                continue
            try:
                run_post_task_reflection_once(**payload)
            except Exception as exc:
                logger.debug("[SelfEvolution] Post-task reflection failed: %s", exc)
            finally:
                try:
                    _reflection_queue().task_done()
                except Exception:
                    pass
    finally:
        with _REFLECTION_WORKER_LOCK:
            if _reflection_queue().empty():
                _REFLECTION_WORKER_STARTED = False


def _assistant_text_and_tool_use(message: Dict[str, Any]) -> Tuple[str, bool]:
    content = message.get("content")
    parts: List[str] = []
    has_tool_use = False
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use":
                has_tool_use = True
            elif block_type == "text":
                parts.append(str(block.get("text") or ""))
    text = "\n".join(part.strip() for part in parts if str(part or "").strip()).strip()
    return text, has_tool_use


def _dedupe_and_bound_texts(texts: List[str], *, max_items: int, max_chars: int) -> List[str]:
    bounded: List[str] = []
    seen = set()
    remaining = max(0, int(max_chars or 0))
    for item in texts:
        if len(bounded) >= max_items or remaining <= 0:
            break
        text = _preview(str(item or ""), limit=min(1000, remaining)).strip()
        if not text:
            continue
        key = _normalize_text_for_compare(text)
        if not key or key in seen:
            continue
        seen.add(key)
        bounded.append(text)
        remaining -= len(text)
    return bounded


def _hash_texts(texts: List[str]) -> str:
    if not texts:
        return ""
    return sha256(json.dumps(texts, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _infer_process_lessons(texts: List[str]) -> List[Dict[str, str]]:
    joined = "\n".join(texts)
    lower = joined.lower()
    lessons: List[Dict[str, str]] = []

    if "clawhub" in lower and (
        "inspect --file" in lower
        or ("install" in lower and ("no files" in lower or "staging" in lower))
    ):
        lessons.append({
            "id": "clawhub-inspect-file-staging",
            "summary": "ClawHub install may not stage files in temporary directories",
            "next_action": (
                "When localizing ClawHub skills, verify install output and fall back "
                "to inspect --file into a staging directory before scanning."
            ),
            "details": "Detected from assistant process text after a task, not from a tool error.",
        })

    if "powershell" in lower and "execution policy" in lower:
        lessons.append({
            "id": "powershell-execution-policy-scripts",
            "summary": "PowerShell script execution policy can block local CLI shims",
            "next_action": (
                "If PowerShell blocks a trusted local CLI shim during skill staging, use "
                "the .cmd shim or an explicit cmd.exe invocation and keep the safety scan."
            ),
            "details": "Detected from assistant process text after a task.",
        })

    if "skill" in lower and "windows" in lower and "bash" in lower:
        lessons.append({
            "id": "community-skill-windows-localization",
            "summary": "Community skills with Bash scripts need Windows localization",
            "next_action": (
                "When installing community skills for CowWechat on Windows, inspect "
                "Bash-only scripts and provide Python or cmd/PowerShell-compatible "
                "entrypoints before enabling the runtime copy."
            ),
            "details": "Detected from assistant process text after a task.",
        })

    if "skill" in lower and ("security gate" in lower or "safety scan" in lower or "safe scan" in lower):
        lessons.append({
            "id": "community-skill-security-gate",
            "summary": "Community skill installs need staging and safety review",
            "next_action": (
                "Stage community skill files in a temporary directory, run the skill "
                "security scanner, and manually inspect scripts before syncing repo "
                "and runtime copies."
            ),
            "details": "Detected from assistant process text after a task.",
        })

    return lessons


def _ask_model_for_process_lessons(
    model_adapter: Any,
    texts: List[str],
    workspace_root: Optional[str],
) -> List[Dict[str, str]]:
    if model_adapter is None or not hasattr(model_adapter, "call"):
        return []
    try:
        from agent.protocol.models import LLMRequest
    except Exception:
        return []

    prompt = _build_reflection_prompt(texts, workspace_root)
    request = LLMRequest(
        messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        temperature=0,
        max_tokens=900,
        stream=False,
        system=(
            "You mine reusable execution lessons from assistant progress text. "
            "Return strict JSON only. Never include secrets, raw paths, user text, "
            "tool outputs, or final-answer content."
        ),
    )
    try:
        response = model_adapter.call(request)
        text = _extract_model_response_text(response)
        return _parse_reflection_lessons(text)
    except Exception as exc:
        logger.debug("[SelfEvolution] Model post-task reflection skipped: %s", exc)
        return []


def _build_reflection_prompt(texts: List[str], workspace_root: Optional[str]) -> str:
    existing_guidance = get_active_prompt_guidance(limit=8, workspace_root=workspace_root)
    payload = {
        "assistant_process_text": texts,
        "existing_guidance": existing_guidance,
    }
    return (
        "Analyze the assistant process/progress statements from one completed task.\n"
        "These statements are not the final answer. Identify only durable, reusable "
        "operational lessons that are not already covered by existing_guidance. "
        "Ignore ordinary status narration, plans, and task-specific details. "
        "Prefer zero lessons unless a future similar task should behave differently.\n\n"
        "Return strict JSON with this schema:\n"
        "{\"lessons\":[{\"id\":\"kebab-case-id\",\"summary\":\"short\","
        "\"next_action\":\"specific future behavior\",\"details\":\"optional safe context\"}]}\n"
        "Use at most 3 lessons. Do not include raw local paths, credentials, tokens, "
        "tool outputs, private user content, or the final answer.\n\n"
        f"Input JSON:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _extract_model_response_text(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    if response.get("error"):
        raise RuntimeError(str(response.get("message") or response.get("error")))
    choices = response.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
    content = message.get("content") if message else ""
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or item.get("content") or "")
            if isinstance(item, dict) else str(item)
            for item in content
        ).strip()
    return str(content or "").strip()


def _parse_reflection_lessons(text: str) -> List[Dict[str, str]]:
    raw = str(text or "").strip()
    if not raw:
        return []
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if match:
        raw = match.group(1)
    elif "{" in raw and "}" in raw:
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("[SelfEvolution] Reflection response was not JSON: %s", raw[:200])
        return []
    lessons = payload.get("lessons") if isinstance(payload, dict) else payload
    if not isinstance(lessons, list):
        return []
    return [lesson for lesson in lessons if isinstance(lesson, dict)]


def _normalize_reflection_lessons(lessons: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    normalized: List[Dict[str, str]] = []
    seen = set()
    for item in lessons:
        rule_id = _normalize_rule_id(str(item.get("id") or item.get("summary") or ""))
        summary = _preview(str(item.get("summary") or ""), limit=180)
        next_action = _preview(str(item.get("next_action") or item.get("next") or ""), limit=500)
        details = _preview(str(item.get("details") or ""), limit=500)
        if not rule_id or not summary or not next_action:
            continue
        if rule_id in seen:
            continue
        seen.add(rule_id)
        normalized.append({
            "id": rule_id,
            "summary": summary,
            "next_action": next_action,
            "details": details,
        })
        if len(normalized) >= 5:
            break
    return normalized


def _append_reflection_report(workspace_root: Optional[str], report: Dict[str, Any]) -> None:
    try:
        data_dir = get_data_dir(workspace_root)
        data_dir.mkdir(parents=True, exist_ok=True)
        _append_jsonl(data_dir / REFLECTIONS_FILE, report)
    except Exception as exc:
        logger.debug("[SelfEvolution] Failed to write reflection report: %s", exc)


def _config_bool(key: str, default: bool) -> bool:
    try:
        from config import conf

        return bool(conf().get(key, default))
    except Exception:
        return default


def _config_int(key: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        from config import conf

        value = int(conf().get(key, default) or default)
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _normalize_text_for_compare(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


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
        for key in ("command_preview", "output_preview", "action", "details_preview"):
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


def _normalize_rule_id(value: str) -> str:
    raw = str(value or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    if not normalized and raw:
        normalized = "manual-learning-" + sha256(raw.encode("utf-8")).hexdigest()[:12]
    return normalized[:96]


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
