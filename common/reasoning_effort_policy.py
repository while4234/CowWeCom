# encoding:utf-8

"""Task-level reasoning-effort routing for Agent conversations.

The policy is local-only and intentionally conservative:
- high-confidence low-risk tasks use the configured default effort;
- development, high-risk, and uncertain tasks use the configured quality effort.

No raw prompt text, session ids, API keys, or tool arguments are persisted by
the audit log.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from common.llm_backend_router import get_current_backend, get_effective_model
from common.llm_usage_tracker import stable_metadata_hash
from common.log import logger
from common.utils import expand_path
from config import conf


VALID_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}
ROUTED_EFFORTS = {"medium", "xhigh"}
DEFAULT_OPTIMIZE_EVERY = 50
DEFAULT_LEARNING_SAMPLE_LIMIT = 120
MAX_LEARNING_MESSAGE_CHARS = 1000
CHAT_SCOPE_PRIVATE = "private"
CHAT_SCOPE_GROUP = "group"
AUDIT_FILENAME = "reasoning_effort_policy_decisions.jsonl"
PRIVATE_AUDIT_FILENAME = "reasoning_effort_policy_decisions_private.jsonl"
GROUP_AUDIT_FILENAME = "reasoning_effort_policy_decisions_group.jsonl"
OPTIMIZER_REPORT_FILENAME = "reasoning_effort_policy_optimizer_reports.jsonl"
OPTIMIZER_ATTEMPT_FILENAME = "reasoning_effort_policy_optimizer_attempts.jsonl"
OPTIMIZER_STATE_FILENAME = "reasoning_effort_policy_optimizer_state.json"
LEARNED_RULES_FILENAME = "reasoning_effort_policy_learned_rules.json"
LEARNING_BUFFER_FILENAME = "reasoning_effort_policy_learning_buffer.jsonl"

_AUDIT_FILENAMES = {
    CHAT_SCOPE_PRIVATE: PRIVATE_AUDIT_FILENAME,
    CHAT_SCOPE_GROUP: GROUP_AUDIT_FILENAME,
}

_AUDIT_LOCK = threading.Lock()
_LEARNING_BUFFER_LOCK = threading.Lock()
_OPTIMIZER_LOCK = threading.Lock()
_OPTIMIZER_RUNNING = False
_LEARNED_RULES_LOCK = threading.Lock()
_LEARNED_RULES_CACHE_PATH = ""
_LEARNED_RULES_CACHE_MTIME = -1.0
_LEARNED_RULES_CACHE: List[Dict[str, Any]] = []


@dataclass
class ReasoningEffortDecision:
    task_id: str
    selected_effort: str
    decision_source: str
    reason: str
    active_backend: str
    main_model: str
    chat_scope: str = CHAT_SCOPE_PRIVATE
    local_rule: str = ""

    def usage_metadata(self) -> Dict[str, Any]:
        return {
            "reasoning_effort_selected": self.selected_effort,
            "reasoning_effort_decision_source": self.decision_source,
            "reasoning_effort_reason": self.reason,
            "reasoning_effort_backend": self.active_backend,
            "reasoning_effort_main_model": self.main_model,
            "reasoning_effort_chat_scope": self.chat_scope,
            "reasoning_effort_local_rule": self.local_rule,
        }


def resolve_reasoning_effort_for_task(user_message: str, model_adapter: Any) -> Optional[ReasoningEffortDecision]:
    """Return the sticky effort decision for one task, or None when disabled."""
    if not _policy_enabled():
        return None

    if _admin_only() and not _is_admin_model(model_adapter):
        return None

    active_backend = get_current_backend()
    main_model = get_effective_model()
    chat_scope = _chat_scope(model_adapter)
    quality_effort = _configured_effort("reasoning_effort_policy_quality_effort", "xhigh", routed_only=True)
    default_effort = _configured_effort("reasoning_effort_policy_default_effort", "medium", routed_only=True)

    task_id = uuid.uuid4().hex[:12]
    local_effort, local_rule = classify_local_task(user_message, quality_effort, default_effort)
    if not local_effort:
        local_effort = quality_effort
        local_rule = "uncertain_default_quality"

    decision = ReasoningEffortDecision(
        task_id=task_id,
        selected_effort=local_effort,
        decision_source="local",
        reason=local_rule,
        active_backend=active_backend,
        main_model=main_model,
        chat_scope=chat_scope,
        local_rule=local_rule,
    )
    record_policy_decision(decision, model_adapter=model_adapter, user_message=user_message)
    return decision


def classify_local_task(user_message: str, quality_effort: str = "xhigh", default_effort: str = "medium") -> Tuple[str, str]:
    """Return (effort, rule) for high-confidence local decisions."""
    text = _normalize_task_text(user_message)
    if not text:
        return default_effort, "empty_or_whitespace"

    quality_rule = _match_quality_rule(text)
    if quality_rule:
        return quality_effort, quality_rule

    learned_quality_rule = _match_learned_rule(text, quality_effort)
    if learned_quality_rule:
        return quality_effort, learned_quality_rule

    medium_rule = _match_medium_rule(text)
    if medium_rule:
        return default_effort, medium_rule

    learned_medium_rule = _match_learned_rule(text, default_effort)
    if learned_medium_rule:
        return default_effort, learned_medium_rule

    return "", ""


def record_policy_decision(
    decision: ReasoningEffortDecision,
    *,
    model_adapter: Any = None,
    user_message: str = "",
) -> None:
    """Append one sanitized routing decision and maybe trigger optimizer."""
    if not bool(conf().get("reasoning_effort_policy_audit_enabled", True)):
        return

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": "decision",
        "task_id": decision.task_id,
        "active_backend": decision.active_backend,
        "main_model": decision.main_model,
        "chat_scope": _normalize_chat_scope(decision.chat_scope),
        "selected_effort": decision.selected_effort,
        "decision_source": decision.decision_source,
        "decision_status": "success",
        "reason": _safe_text(decision.reason, 160),
        "local_rule": _safe_text(decision.local_rule, 96),
        "channel_type": _safe_text(getattr(model_adapter, "channel_type", ""), 64),
        "session_hash": _hash_optional(getattr(model_adapter, "session_id", "")),
        "user_hash": _hash_optional(getattr(model_adapter, "user_id", "")),
        "message_hash": stable_metadata_hash(str(user_message or "")),
        "message_features": _message_features(user_message),
    }
    record = {key: value for key, value in record.items() if value not in ("", None)}

    try:
        path = audit_log_path(decision.chat_scope)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _AUDIT_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as exc:
        logger.debug(f"[ReasoningPolicy] Failed to record decision: {exc}")
        return

    _append_learning_sample(decision, record, user_message)
    maybe_trigger_policy_optimizer_async(model_adapter)


def record_policy_task_outcome(
    decision: Optional[ReasoningEffortDecision],
    *,
    status: str,
    turn_count: int,
    max_turns: int,
    model_adapter: Any = None,
    runtime_stats: Optional[Mapping[str, Any]] = None,
    failure_reason: str = "",
    final_response: str = "",
) -> None:
    """Append sanitized post-run outcome data for a previously routed task."""
    if decision is None or not bool(conf().get("reasoning_effort_policy_audit_enabled", True)):
        return

    response_text = str(final_response or "")
    runtime_stats = runtime_stats if isinstance(runtime_stats, Mapping) else {}
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": "task_outcome",
        "task_id": decision.task_id,
        "active_backend": decision.active_backend,
        "main_model": decision.main_model,
        "chat_scope": _normalize_chat_scope(decision.chat_scope),
        "selected_effort": decision.selected_effort,
        "decision_source": decision.decision_source,
        "local_rule": _safe_text(decision.local_rule, 96),
        "task_status": _safe_text(status, 64),
        "turn_count": max(0, int(turn_count or 0)),
        "max_turns": max(0, int(max_turns or 0)),
        "max_turns_exhausted": bool(max_turns and turn_count >= max_turns),
        "failure_reason": _safe_text(failure_reason, 180),
        "final_response_chars": len(response_text.strip()),
        "final_response_hash": stable_metadata_hash(response_text) if response_text else "",
        "tool_attempt_count": _safe_int(runtime_stats.get("tool_attempt_count")),
        "tool_attempt_success_count": _safe_int(runtime_stats.get("tool_attempt_success_count")),
        "tool_attempt_error_count": _safe_int(runtime_stats.get("tool_attempt_error_count")),
        "tool_skip_count": _safe_int(runtime_stats.get("tool_skip_count")),
        "tool_failure_class": _safe_text(runtime_stats.get("tool_failure_class"), 96),
    }
    record = {key: value for key, value in record.items() if value not in ("", None)}

    try:
        path = audit_log_path(decision.chat_scope)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _AUDIT_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as exc:
        logger.debug(f"[ReasoningPolicy] Failed to record task outcome: {exc}")
        return

    maybe_trigger_policy_optimizer_async(model_adapter)


def maybe_trigger_policy_optimizer_async(model_adapter: Any = None) -> bool:
    """Start a non-blocking optimizer pass when enough new decisions exist."""
    if not _runtime_auto_optimizer_enabled():
        return False

    due, total_count, _ = _optimizer_due_status()
    if not due:
        return False

    if not _begin_optimizer_run():
        return False

    def _worker() -> None:
        try:
            run_policy_optimizer_once(model_adapter=model_adapter, record_count=total_count, reason="threshold")
        finally:
            _end_optimizer_run()

    thread = threading.Thread(target=_worker, name="reasoning-effort-policy-optimizer", daemon=True)
    thread.start()
    return True


def run_policy_optimizer_if_due(
    *,
    model_adapter: Any,
    reason: str = "scheduler",
) -> Dict[str, Any]:
    """Run one optimizer pass synchronously when the configured threshold is due."""
    due, total_count, failure_reason = _optimizer_due_status()
    if not due:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "skipped",
            "reason": reason,
            "failure_reason": failure_reason,
            "record_count": total_count,
        }

    if not _begin_optimizer_run():
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "skipped",
            "reason": reason,
            "failure_reason": "optimizer_already_running",
            "record_count": total_count,
        }

    try:
        return run_policy_optimizer_once(model_adapter=model_adapter, record_count=total_count, reason=reason)
    finally:
        _end_optimizer_run()


def run_policy_optimizer_once(
    *,
    model_adapter: Any,
    record_count: Optional[int] = None,
    reason: str = "manual",
) -> Dict[str, Any]:
    """Analyze recent routing decisions with the current model locked to xhigh."""
    records = _read_policy_decision_records_tail(limit=200)
    learning_samples = _read_learning_samples_tail(limit=DEFAULT_LEARNING_SAMPLE_LIMIT)
    record_count = record_count if record_count is not None else _count_policy_decision_records()
    active_backend = get_current_backend()
    optimizer_model = _optimizer_model_name(model_adapter)
    started = datetime.now(timezone.utc).isoformat()
    attempt_id = uuid.uuid4().hex[:12]

    report: Dict[str, Any] = {
        "timestamp": started,
        "attempt_id": attempt_id,
        "status": "skipped",
        "reason": reason,
        "active_backend": active_backend,
        "optimizer_model": optimizer_model,
        "optimizer_reasoning_effort": "xhigh",
        "analyzed_records": len(records),
        "learning_samples_analyzed": len(learning_samples),
        "candidate_rule_count": 0,
        "applied_rule_count": 0,
        "rejected_rule_count": 0,
        "raw_learning_samples_consumed": 0,
    }
    if not records or model_adapter is None:
        missing = []
        if not records:
            missing.append("no_records")
        if model_adapter is None:
            missing.append("no_model_adapter")
        report["failure_reason"] = "_and_".join(missing) or "optimizer_prerequisite_missing"
        _append_optimizer_report(report)
        _append_optimizer_attempt(report, record_count)
        _write_optimizer_state(record_count, report["status"])
        return report

    prompt = _optimizer_prompt(records, learning_samples)
    try:
        from agent.protocol.models import LLMRequest

        request = LLMRequest(
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            temperature=0,
            max_tokens=1200,
            stream=False,
            system=(
                "You optimize a conservative local routing policy. Use xhigh reasoning. "
                "Return strict JSON only. Never quote, paraphrase, or include raw user prompts."
            ),
            model=optimizer_model,
            reasoning_effort="xhigh",
            reasoning_effort_locked=True,
        )
        response = model_adapter.call(request)
        text = _extract_response_text(response)
        if not text:
            raise RuntimeError("empty_optimizer_response")
        payload = _parse_optimizer_json(text)
        candidates = _extract_rule_candidates(payload)
        applied, rejected = _apply_optimizer_rule_candidates(
            candidates,
            records=records,
            learning_samples=learning_samples,
            attempt_id=attempt_id,
        )
        report["status"] = "success"
        report["candidate_rule_count"] = len(candidates)
        report["applied_rule_count"] = len(applied)
        report["rejected_rule_count"] = len(rejected)
        report["applied_rules"] = [_rule_report_summary(rule) for rule in applied]
        report["rejected_rules"] = rejected[:20]
        summary = _safe_optimizer_note(payload.get("summary") if isinstance(payload, Mapping) else "", learning_samples, 500)
        if summary:
            report["summary"] = summary
        if learning_samples:
            consumed_ids = [str(sample.get("task_id") or "") for sample in learning_samples if sample.get("task_id")]
            _delete_learning_samples(consumed_ids)
            report["raw_learning_samples_consumed"] = len(set(consumed_ids))
    except Exception as exc:
        report["status"] = "failed"
        report["failure_reason"] = _safe_text(str(exc), 240)

    _append_optimizer_report(report)
    _append_optimizer_attempt(report, record_count)
    _write_optimizer_state(record_count, report["status"])
    return report


def audit_log_path(chat_scope: str = CHAT_SCOPE_PRIVATE) -> str:
    scope = _normalize_chat_scope(chat_scope)
    return os.path.join(_workspace_data_dir(), _AUDIT_FILENAMES[scope])


def legacy_audit_log_path() -> str:
    return os.path.join(_workspace_data_dir(), AUDIT_FILENAME)


def audit_log_paths(*, include_legacy: bool = True) -> List[str]:
    paths = [
        audit_log_path(CHAT_SCOPE_PRIVATE),
        audit_log_path(CHAT_SCOPE_GROUP),
    ]
    if include_legacy:
        paths.append(legacy_audit_log_path())
    return paths


def optimizer_report_path() -> str:
    return os.path.join(_workspace_data_dir(), OPTIMIZER_REPORT_FILENAME)


def optimizer_attempt_path() -> str:
    return os.path.join(_workspace_data_dir(), OPTIMIZER_ATTEMPT_FILENAME)


def optimizer_state_path() -> str:
    return os.path.join(_workspace_data_dir(), OPTIMIZER_STATE_FILENAME)


def learned_rules_path() -> str:
    return os.path.join(_workspace_data_dir(), LEARNED_RULES_FILENAME)


def learning_buffer_path() -> str:
    return os.path.join(_workspace_data_dir(), LEARNING_BUFFER_FILENAME)


def _extract_response_text(response: Any) -> str:
    if not isinstance(response, Mapping):
        return ""
    if response.get("error"):
        raise RuntimeError(str(response.get("message") or response.get("error")))
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else {}
    content = message.get("content") if isinstance(message, Mapping) else ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, Mapping):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts).strip()
    return str(content or "").strip()


def _match_quality_rule(text: str) -> str:
    quality_patterns = {
        "coding": (
            r"(代码|编程|程序|函数|类|接口|脚本|开发|实现|新功能|功能开发|写.*?(代码|程序|脚本)|"
            r"python|typescript|javascript|java|sql|docker|api|backend|frontend|code|function|class|script|feature)"
        ),
        "debugging": r"(报错|错误|异常|调试|修复|排查|失败|不生效|bug|traceback|stack trace|exception|debug|fix|failing test)",
        "repo_work": r"(仓库|文件|目录|路径|git|commit|push|部署|发布|迁移|测试|单元测试|repo|file|directory|deploy|migration|unit test)",
        "quality_first": r"(深入分析|详细分析|全面分析|方案设计|架构|重构|代码审查|代码走读|code review|review|质量优先|开发方案|实现方案)",
        "high_risk": (
            r"(权限|安全|删除|移除|合规|财务|法律|医疗|账号|密码|密钥|credential|secret|"
            r"permission|security|delete|remove|legal|medical|finance)"
        ),
        "multi_step": r"(多步骤|自动优化|后台任务|定时任务|工具调用|批量|长期任务|multi-step|background job|scheduler|tool call)",
    }
    for rule, pattern in quality_patterns.items():
        if re.search(pattern, text, re.IGNORECASE):
            return rule
    return ""


def _match_medium_rule(text: str) -> str:
    if len(text) > 180:
        return ""
    medium_patterns = {
        "greeting": r"^(你好|您好|早上好|晚上好|hi|hello|hey|thanks|谢谢|好的|ok|嗯|收到)[\s!！。?.？,，]*$",
        "short_translation": r"(翻译|translate).{0,120}$",
        "short_rewrite": r"(改写|润色|polish|rewrite).{0,120}$",
        "short_summary": r"(总结|摘要|概括|summari[sz]e).{0,120}$",
        "sentence_check": r"(语病|错别字|有没有问题|看看这句话|检查这句话).{0,120}$",
        "short_writing": r"(写一条|写一封|起.*?标题|取.*?标题|拟.*?标题|短信|文案|标题|祝福语|邮件).{0,120}$",
        "daily_expression_advice": r"(怎么回复|怎么说|如何回复|如何表达|沟通建议|安慰|鼓励|朋友圈|起名|取名).{0,120}$",
        "casual_daily_chat": r"(上班|下班|加班|放假|开工|上学|周一|周二|周三|周四|周五|周六|周日|周末|好累|累死|困|想睡|天气|下雨|降温|好热|好冷).{0,120}$",
        "simple_explain": r"^(简单)?(解释|说明).{0,120}$|是什么[？?]?$|什么意思[？?]?$",
    }
    for rule, pattern in medium_patterns.items():
        if re.search(pattern, text, re.IGNORECASE):
            return rule
    return ""


def _match_learned_rule(text: str, effort: str) -> str:
    normalized_effort = str(effort or "").strip().lower()
    if normalized_effort not in ROUTED_EFFORTS:
        return ""

    for rule in _load_learned_rules():
        if not bool(rule.get("enabled", True)):
            continue
        if str(rule.get("effort") or "").strip().lower() != normalized_effort:
            continue
        max_chars = _to_int(rule.get("max_chars")) or (120 if normalized_effort == "medium" else 1000)
        if max_chars > 0 and len(text) > max_chars:
            continue
        keywords = rule.get("keywords") if isinstance(rule.get("keywords"), list) else []
        for keyword in keywords:
            normalized_keyword = _normalize_rule_keyword(keyword)
            if normalized_keyword and normalized_keyword in text:
                name = _sanitize_rule_name(rule.get("name") or rule.get("id") or "rule")
                return f"learned_{normalized_effort}_{name}"[:96]
    return ""


def _policy_enabled() -> bool:
    return bool(conf().get("reasoning_effort_policy_enabled", False))


def _admin_only() -> bool:
    return bool(conf().get("reasoning_effort_policy_admin_only", False))


def _is_admin_model(model_adapter: Any) -> bool:
    if bool(getattr(model_adapter, "is_admin", False)):
        return True
    return str(getattr(model_adapter, "actor_role", "") or "").strip().lower() == "admin"


def _configured_effort(key: str, default: str, *, routed_only: bool) -> str:
    value = str(conf().get(key) or default or "").strip().lower()
    if value == "minimal":
        value = "low"
    if value == "max" and routed_only:
        value = "xhigh"
    allowed = ROUTED_EFFORTS if routed_only else VALID_EFFORTS
    return value if value in allowed else default


def _normalize_task_text(value: str) -> str:
    return " ".join(str(value or "").strip().split()).lower()


def _normalize_chat_scope(value: Any) -> str:
    text = str(value or "").strip().lower()
    return CHAT_SCOPE_GROUP if text in {"group", "group_chat", "room"} else CHAT_SCOPE_PRIVATE


def _chat_scope(model_adapter: Any) -> str:
    if bool(getattr(model_adapter, "is_group", False)):
        return CHAT_SCOPE_GROUP
    return _normalize_chat_scope(getattr(model_adapter, "chat_scope", CHAT_SCOPE_PRIVATE))


def _message_features(user_message: str) -> Dict[str, Any]:
    text = str(user_message or "")
    stripped = text.strip()
    lowered = stripped.lower()
    code_markers = ("```", "traceback", "exception", "def ", "class ", "function ", "git ", "python", "typescript")
    return {
        "char_count": len(stripped),
        "line_count": text.count("\n") + (1 if stripped else 0),
        "has_question_mark": "?" in stripped or "？" in stripped,
        "has_code_signal": any(marker in lowered for marker in code_markers),
        "has_url": bool(re.search(r"https?://|www\.", lowered)),
        "has_file_path_signal": bool(re.search(r"([a-zA-Z]:\\|/[^/\s]+/|\\[^\\\s]+\\)", stripped)),
        "is_short": len(stripped) <= 80,
    }


def _safe_text(value: Any, max_len: int = 120) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()[:max_len]


def _hash_optional(value: Any) -> str:
    text = str(value or "").strip()
    return stable_metadata_hash(text) if text else ""


def _workspace_data_dir() -> str:
    workspace = expand_path(conf().get("agent_workspace", "~/cow"))
    return os.path.join(workspace, "data")


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_int(value: Any) -> int:
    return max(0, _to_int(value))


def _optimizer_due_status() -> Tuple[bool, int, str]:
    total_count = _count_policy_decision_records()
    if not bool(conf().get("reasoning_effort_policy_auto_optimize_enabled", False)):
        return False, total_count, "auto_optimize_disabled"

    threshold = max(1, _to_int(conf().get("reasoning_effort_policy_auto_optimize_every_tasks")) or DEFAULT_OPTIMIZE_EVERY)
    state = _read_optimizer_state()
    last_count = _to_int(state.get("last_optimized_record_count"))
    if total_count - last_count < threshold:
        return False, total_count, "threshold_not_reached"
    return True, total_count, ""


def _runtime_auto_optimizer_enabled() -> bool:
    """Gate the old in-Agent optimizer; Codex scheduled optimization is preferred."""
    return bool(conf().get("reasoning_effort_policy_runtime_auto_optimize_enabled", False))


def _begin_optimizer_run() -> bool:
    global _OPTIMIZER_RUNNING
    with _OPTIMIZER_LOCK:
        if _OPTIMIZER_RUNNING:
            return False
        _OPTIMIZER_RUNNING = True
        return True


def _end_optimizer_run() -> None:
    global _OPTIMIZER_RUNNING
    with _OPTIMIZER_LOCK:
        _OPTIMIZER_RUNNING = False


def _optimizer_model_name(model_adapter: Any) -> str:
    for value in (
        get_effective_model(),
        getattr(model_adapter, "model", ""),
        conf().get("model"),
        "gpt-5.5",
    ):
        text = str(value or "").strip()
        if text:
            return text
    return "gpt-5.5"


def _append_learning_sample(
    decision: ReasoningEffortDecision,
    audit_record: Mapping[str, Any],
    user_message: str,
) -> None:
    if not _should_capture_learning_sample(decision, user_message):
        return

    text = str(user_message or "").strip()
    sample = {
        "timestamp": audit_record.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "task_id": decision.task_id,
        "chat_scope": _normalize_chat_scope(decision.chat_scope),
        "active_backend": decision.active_backend,
        "main_model": decision.main_model,
        "selected_effort": decision.selected_effort,
        "local_rule": decision.local_rule,
        "message_hash": audit_record.get("message_hash") or stable_metadata_hash(text),
        "message_features": audit_record.get("message_features") or _message_features(text),
        "message_text": text,
    }
    try:
        path = learning_buffer_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _LEARNING_BUFFER_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(sample, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as exc:
        logger.debug(f"[ReasoningPolicy] Failed to record learning sample: {exc}")


def _should_capture_learning_sample(decision: ReasoningEffortDecision, user_message: str) -> bool:
    if decision.local_rule != "uncertain_default_quality":
        return False
    if not _runtime_auto_optimizer_enabled():
        return False
    if not bool(conf().get("reasoning_effort_policy_auto_optimize_enabled", False)):
        return False
    if not bool(conf().get("reasoning_effort_policy_learning_buffer_enabled", True)):
        return False

    text = str(user_message or "").strip()
    if not text or len(text) > MAX_LEARNING_MESSAGE_CHARS:
        return False
    features = _message_features(text)
    if features.get("has_url") or features.get("has_file_path_signal"):
        return False
    return not _looks_sensitive_text(text)


def _read_learning_samples_tail(limit: int) -> List[Dict[str, Any]]:
    rows = _read_jsonl_tail(learning_buffer_path(), limit)
    return [item for item in rows if isinstance(item.get("message_text"), str) and item.get("task_id")]


def _delete_learning_samples(task_ids: List[str]) -> None:
    consumed = {str(task_id or "") for task_id in task_ids if task_id}
    if not consumed:
        return

    path = learning_buffer_path()
    with _LEARNING_BUFFER_LOCK:
        rows = _read_jsonl_tail(path, limit=100000)
        remaining = [item for item in rows if str(item.get("task_id") or "") not in consumed]
        try:
            if not remaining:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                return

            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                for item in remaining:
                    f.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            os.replace(tmp_path, path)
        except Exception as exc:
            logger.debug(f"[ReasoningPolicy] Failed to delete learning samples: {exc}")


def _load_learned_rules() -> List[Dict[str, Any]]:
    global _LEARNED_RULES_CACHE, _LEARNED_RULES_CACHE_MTIME, _LEARNED_RULES_CACHE_PATH
    path = learned_rules_path()
    try:
        mtime = os.path.getmtime(path)
    except FileNotFoundError:
        with _LEARNED_RULES_LOCK:
            _LEARNED_RULES_CACHE_PATH = path
            _LEARNED_RULES_CACHE_MTIME = -1.0
            _LEARNED_RULES_CACHE = []
        return []
    except Exception as exc:
        logger.debug(f"[ReasoningPolicy] Failed to stat learned rules: {exc}")
        return []

    with _LEARNED_RULES_LOCK:
        if _LEARNED_RULES_CACHE_PATH == path and _LEARNED_RULES_CACHE_MTIME == mtime:
            return [dict(rule) for rule in _LEARNED_RULES_CACHE]
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw_rules = data.get("rules") if isinstance(data, Mapping) else []
            rules = [dict(rule) for rule in raw_rules if isinstance(rule, Mapping)]
            _LEARNED_RULES_CACHE_PATH = path
            _LEARNED_RULES_CACHE_MTIME = mtime
            _LEARNED_RULES_CACHE = rules
            return [dict(rule) for rule in rules]
        except Exception as exc:
            logger.debug(f"[ReasoningPolicy] Failed to load learned rules: {exc}")
            _LEARNED_RULES_CACHE_PATH = path
            _LEARNED_RULES_CACHE_MTIME = mtime
            _LEARNED_RULES_CACHE = []
            return []


def _write_learned_rules(rules: List[Dict[str, Any]]) -> None:
    global _LEARNED_RULES_CACHE, _LEARNED_RULES_CACHE_MTIME, _LEARNED_RULES_CACHE_PATH
    path = learned_rules_path()
    doc = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "rules": rules[-100:],
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = -1.0
    with _LEARNED_RULES_LOCK:
        _LEARNED_RULES_CACHE_PATH = path
        _LEARNED_RULES_CACHE_MTIME = mtime
        _LEARNED_RULES_CACHE = [dict(rule) for rule in doc["rules"]]


def _parse_optimizer_json(text: str) -> Dict[str, Any]:
    stripped = str(text or "").strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```$", "", stripped)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("invalid_optimizer_json")
        try:
            payload = json.loads(stripped[start:end + 1])
        except json.JSONDecodeError as exc:
            raise RuntimeError("invalid_optimizer_json") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("invalid_optimizer_json")
    return payload


def _extract_rule_candidates(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for key in ("rules", "add_rules"):
        items = payload.get(key)
        if isinstance(items, list):
            candidates.extend(dict(item) for item in items if isinstance(item, Mapping))

    typed_keys = {
        "add_local_medium_rules": "medium",
        "medium_rules": "medium",
        "add_local_xhigh_rules": "xhigh",
        "xhigh_rules": "xhigh",
    }
    for key, effort in typed_keys.items():
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            candidate = dict(item)
            candidate.setdefault("effort", effort)
            candidates.append(candidate)
    return candidates


def _apply_optimizer_rule_candidates(
    candidates: List[Dict[str, Any]],
    *,
    records: List[Dict[str, Any]],
    learning_samples: List[Dict[str, Any]],
    attempt_id: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not candidates:
        return [], []

    if not bool(conf().get("reasoning_effort_policy_auto_apply_enabled", True)):
        rejected = []
        for candidate in candidates:
            rejected.append({
                "name": _safe_text(candidate.get("name") or candidate.get("id") or "unnamed", 80),
                "effort": _safe_text(candidate.get("effort"), 24),
                "reason": "auto_apply_disabled",
            })
        return [], rejected

    existing_rules = _load_learned_rules()
    signatures = {
        (str(rule.get("effort") or ""), tuple(rule.get("keywords") or []))
        for rule in existing_rules
    }
    applied: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    for candidate in candidates:
        rule, rejection = _validate_rule_candidate(
            candidate,
            records=records,
            learning_samples=learning_samples,
            attempt_id=attempt_id,
        )
        if rejection:
            rejected.append(rejection)
            continue
        if not rule:
            continue
        signature = (str(rule.get("effort") or ""), tuple(rule.get("keywords") or []))
        if signature in signatures:
            rejected.append({
                "name": rule["name"],
                "effort": rule["effort"],
                "reason": "duplicate_rule",
            })
            continue
        signatures.add(signature)
        existing_rules.append(rule)
        applied.append(rule)

    if applied:
        _write_learned_rules(existing_rules)
    return applied, rejected


def _validate_rule_candidate(
    candidate: Mapping[str, Any],
    *,
    records: List[Dict[str, Any]],
    learning_samples: List[Dict[str, Any]],
    attempt_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    effort = str(candidate.get("effort") or "").strip().lower()
    if effort not in ROUTED_EFFORTS:
        return None, _rule_rejection(candidate, "invalid_effort")

    name = _sanitize_rule_name(candidate.get("name") or candidate.get("id") or candidate.get("rule") or "learned")
    if _rule_name_looks_like_raw_sample(name, learning_samples):
        name = "learned"
    raw_keywords = candidate.get("keywords")
    if isinstance(raw_keywords, str):
        raw_keywords = [raw_keywords]
    if not isinstance(raw_keywords, list):
        return None, _rule_rejection(candidate, "missing_keywords")

    keywords: List[str] = []
    for keyword in raw_keywords:
        normalized = _normalize_rule_keyword(keyword)
        if normalized and not _unsafe_rule_keyword(normalized) and normalized not in keywords:
            keywords.append(normalized)
    if not keywords:
        return None, _rule_rejection(candidate, "invalid_keywords")

    min_support = max(1, _to_int(conf().get("reasoning_effort_policy_auto_apply_min_support")) or 2)
    support_ids = _supported_learning_task_ids(keywords, learning_samples)
    if len(support_ids) < min_support:
        return None, _rule_rejection(candidate, "insufficient_support", support_count=len(support_ids))

    if effort == "medium":
        if _support_has_failure_evidence(support_ids, records):
            return None, _rule_rejection(candidate, "medium_support_has_failure_evidence", support_count=len(support_ids))
        if not _support_has_success_evidence(support_ids, records, min_support=min_support):
            return None, _rule_rejection(
                candidate,
                "medium_support_missing_success_evidence",
                support_count=len(support_ids),
            )

    confidence = _coerce_confidence(candidate.get("confidence"))
    if confidence < 0.65:
        return None, _rule_rejection(candidate, "low_confidence", support_count=len(support_ids))

    default_max_chars = 120 if effort == "medium" else 500
    max_chars = _to_int(candidate.get("max_chars")) or default_max_chars
    if effort == "medium":
        max_chars = min(180, max(40, max_chars))
    else:
        max_chars = min(1000, max(80, max_chars))

    rule = {
        "id": _learned_rule_id(effort, name, keywords),
        "enabled": True,
        "effort": effort,
        "name": name,
        "keywords": keywords,
        "max_chars": max_chars,
        "confidence": confidence,
        "reason": _safe_optimizer_note(
            candidate.get("reason") or candidate.get("description") or "learned from supported background samples",
            learning_samples,
            240,
        ) or "learned from supported background samples",
        "support_count": len(support_ids),
        "source": "reasoning_effort_policy_optimizer",
        "source_attempt_id": attempt_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return rule, None


def _supported_learning_task_ids(keywords: List[str], learning_samples: List[Dict[str, Any]]) -> List[str]:
    supported = []
    for sample in learning_samples:
        text = _normalize_task_text(sample.get("message_text", ""))
        if not text:
            continue
        if any(keyword in text for keyword in keywords):
            task_id = str(sample.get("task_id") or sample.get("message_hash") or "")
            if task_id and task_id not in supported:
                supported.append(task_id)
    return supported


def _support_has_failure_evidence(task_ids: List[str], records: List[Dict[str, Any]]) -> bool:
    task_id_set = set(task_ids)
    for item in records:
        if str(item.get("task_id") or "") not in task_id_set:
            continue
        if item.get("event_type") != "task_outcome":
            continue
        status = str(item.get("task_status") or "").strip().lower()
        if status in {"failed", "error", "max_turns_exhausted"}:
            return True
        if bool(item.get("max_turns_exhausted")):
            return True
        if _safe_int(item.get("tool_attempt_error_count")) > 0:
            return True
    return False


def _support_has_success_evidence(
    task_ids: List[str],
    records: List[Dict[str, Any]],
    *,
    min_support: int,
) -> bool:
    task_id_set = set(task_ids)
    successful = set()
    for item in records:
        task_id = str(item.get("task_id") or "")
        if task_id not in task_id_set or task_id in successful:
            continue
        if item.get("event_type") != "task_outcome":
            continue
        status = str(item.get("task_status") or "").strip().lower()
        if status != "success":
            continue
        if bool(item.get("max_turns_exhausted")):
            continue
        if _safe_int(item.get("tool_attempt_error_count")) > 0:
            continue
        if _safe_int(item.get("turn_count")) > 1:
            continue
        successful.add(task_id)
        if len(successful) >= min_support:
            return True
    return False


def _rule_rejection(candidate: Mapping[str, Any], reason: str, *, support_count: int = 0) -> Dict[str, Any]:
    return {
        "name": _safe_text(candidate.get("name") or candidate.get("id") or "unnamed", 80),
        "effort": _safe_text(candidate.get("effort"), 24),
        "reason": reason,
        "support_count": max(0, int(support_count or 0)),
    }


def _rule_report_summary(rule: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "id": rule.get("id"),
        "effort": rule.get("effort"),
        "name": rule.get("name"),
        "max_chars": rule.get("max_chars"),
        "confidence": rule.get("confidence"),
        "support_count": rule.get("support_count"),
    }


def _safe_optimizer_note(value: Any, learning_samples: List[Dict[str, Any]], max_len: int) -> str:
    text = _safe_text(value, max_len)
    if not text:
        return ""
    lowered = text.lower()
    for sample in learning_samples:
        raw = _safe_text(sample.get("message_text"), max_len).lower()
        if raw and (raw in lowered or lowered in raw):
            return ""
    return text


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.75
    return max(0.0, min(1.0, confidence))


def _learned_rule_id(effort: str, name: str, keywords: List[str]) -> str:
    digest = stable_metadata_hash("|".join(keywords))[:8]
    return _safe_text(f"learned_{effort}_{name}_{digest}", 96)


def _sanitize_rule_name(value: Any) -> str:
    text = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "learned").strip().lower())
    text = re.sub(r"_+", "_", text).strip("_")
    return (text or "learned")[:48]


def _rule_name_looks_like_raw_sample(name: str, learning_samples: List[Dict[str, Any]]) -> bool:
    if not name or name == "learned":
        return False
    for sample in learning_samples:
        sample_name = _sanitize_rule_name(sample.get("message_text"))
        if sample_name and len(sample_name) > 12 and (name in sample_name or sample_name in name):
            return True
    return False


def _normalize_rule_keyword(value: Any) -> str:
    return _normalize_task_text(value)


def _unsafe_rule_keyword(keyword: str) -> bool:
    if len(keyword) < 2 or len(keyword) > 32:
        return True
    if re.search(r"https?://|www\.|@|[a-zA-Z]:\\|/[^/\s]+/|\\[^\\\s]+\\", keyword):
        return True
    return _looks_sensitive_text(keyword)


def _looks_sensitive_text(text: str) -> bool:
    value = str(text or "")
    if re.search(r"(?i)\b(api[_-]?key|token|secret|password|passwd|cookie|authorization|credential)\b\s*[:=]\s*\S+", value):
        return True
    if re.search(r"(?i)\bsk-[a-z0-9_-]{16,}\b", value):
        return True
    if re.search(r"(?i)\b(bearer|basic)\s+[a-z0-9._~+/=-]{16,}", value):
        return True
    return False


def _count_jsonl_records(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0
    except Exception as exc:
        logger.debug(f"[ReasoningPolicy] Failed to count records: {exc}")
        return 0


def _count_policy_decision_records() -> int:
    return sum(_count_decision_events(path) for path in audit_log_paths(include_legacy=True))


def _count_decision_events(path: str) -> int:
    count = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                if item.get("event_type") in (None, "", "decision"):
                    count += 1
    except FileNotFoundError:
        return 0
    except Exception as exc:
        logger.debug(f"[ReasoningPolicy] Failed to count decision events: {exc}")
        return 0
    return count


def _read_jsonl_tail(path: str, limit: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-max(1, limit):]
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.debug(f"[ReasoningPolicy] Failed to read records: {exc}")
        return []
    for line in lines:
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        except json.JSONDecodeError:
            continue
    return rows


def _read_policy_decision_records_tail(limit: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in audit_log_paths(include_legacy=True):
        scoped_rows = _read_jsonl_tail(path, limit)
        if path == legacy_audit_log_path():
            for item in scoped_rows:
                item.setdefault("chat_scope", CHAT_SCOPE_PRIVATE)
        rows.extend(scoped_rows)
    rows.sort(key=lambda item: str(item.get("timestamp") or ""))
    return rows[-max(1, limit):]


def _read_optimizer_state() -> Dict[str, Any]:
    try:
        with open(optimizer_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.debug(f"[ReasoningPolicy] Failed to read optimizer state: {exc}")
        return {}


def _write_optimizer_state(record_count: int, status: str) -> None:
    state = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_optimized_record_count": int(record_count or 0),
        "last_status": status,
    }
    try:
        path = optimizer_state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.debug(f"[ReasoningPolicy] Failed to write optimizer state: {exc}")


def _append_optimizer_report(report: Mapping[str, Any]) -> None:
    try:
        path = optimizer_report_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(report), ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as exc:
        logger.debug(f"[ReasoningPolicy] Failed to write optimizer report: {exc}")


def _append_optimizer_attempt(report: Mapping[str, Any], record_count: int) -> None:
    attempt = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "attempt_id": report.get("attempt_id"),
        "trigger_reason": report.get("reason"),
        "status": report.get("status"),
        "active_backend": report.get("active_backend"),
        "optimizer_model": report.get("optimizer_model"),
        "optimizer_reasoning_effort": report.get("optimizer_reasoning_effort"),
        "analyzed_records": report.get("analyzed_records"),
        "record_count": int(record_count or 0),
    }
    failure_reason = report.get("failure_reason")
    if failure_reason:
        attempt["failure_reason"] = _safe_text(failure_reason, 240)
    try:
        path = optimizer_attempt_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(attempt, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as exc:
        logger.debug(f"[ReasoningPolicy] Failed to write optimizer attempt: {exc}")


def _optimizer_prompt(records: List[Dict[str, Any]], learning_samples: List[Dict[str, Any]]) -> str:
    compact_records = []
    for item in records[-200:]:
        compact_records.append({
            "event_type": item.get("event_type") or "decision",
            "task_id": item.get("task_id"),
            "chat_scope": item.get("chat_scope"),
            "active_backend": item.get("active_backend"),
            "main_model": item.get("main_model"),
            "selected_effort": item.get("selected_effort"),
            "decision_source": item.get("decision_source"),
            "decision_status": item.get("decision_status"),
            "reason": item.get("reason"),
            "local_rule": item.get("local_rule"),
            "message_features": item.get("message_features"),
            "task_status": item.get("task_status"),
            "turn_count": item.get("turn_count"),
            "max_turns": item.get("max_turns"),
            "max_turns_exhausted": item.get("max_turns_exhausted"),
            "tool_attempt_count": item.get("tool_attempt_count"),
            "tool_attempt_error_count": item.get("tool_attempt_error_count"),
            "tool_skip_count": item.get("tool_skip_count"),
            "tool_failure_class": item.get("tool_failure_class"),
        })
    compact_samples = []
    for item in learning_samples[-DEFAULT_LEARNING_SAMPLE_LIMIT:]:
        compact_samples.append({
            "task_id": item.get("task_id"),
            "chat_scope": item.get("chat_scope"),
            "active_backend": item.get("active_backend"),
            "main_model": item.get("main_model"),
            "selected_effort": item.get("selected_effort"),
            "local_rule": item.get("local_rule"),
            "message_features": item.get("message_features"),
            "message_text": item.get("message_text"),
        })
    payload = {
        "sanitized_records": compact_records,
        "raw_learning_samples": compact_samples,
        "rule_constraints": {
            "valid_efforts": ["medium", "xhigh"],
            "medium": "Only for clearly simple, low-risk, non-code, non-debug, non-repo, short tasks.",
            "xhigh": "For local rules that should skip uncertainty and explicitly require deep reasoning.",
            "keywords": "Return short reusable keywords or phrases, never full raw prompts.",
            "support": "Prefer rules supported by at least two raw learning samples.",
        },
        "output_schema": {
            "summary": "short sanitized summary",
            "rules": [
                {
                    "effort": "medium|xhigh",
                    "name": "snake_case_rule_name",
                    "keywords": ["short keyword"],
                    "max_chars": 120,
                    "confidence": 0.8,
                    "reason": "sanitized reason without raw prompt text",
                }
            ],
        },
    }
    return (
        "Analyze local reasoning-effort routing. The runtime path must stay local-only; do not suggest "
        "per-request model classification. You may use raw_learning_samples only to infer short local "
        "keyword rules, but your response must not quote or paraphrase any raw prompt. Return strict JSON "
        "matching output_schema. If no rule is safe, return {\"summary\":\"no safe rule\",\"rules\":[]}.\n\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
