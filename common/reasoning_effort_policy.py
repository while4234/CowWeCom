# encoding:utf-8

"""Task-level reasoning-effort routing for Agent conversations.

The policy is intentionally conservative:
- local rules only decide when the task shape is obvious;
- uncertain tasks default to the configured quality effort.

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

from agent.protocol.models import LLMRequest
from common.llm_backend_router import get_current_backend, get_effective_model
from common.llm_usage_tracker import stable_metadata_hash
from common.log import logger
from common.utils import expand_path
from config import conf


VALID_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}
ROUTED_EFFORTS = {"medium", "xhigh"}
DEFAULT_OPTIMIZE_EVERY = 50
AUDIT_FILENAME = "reasoning_effort_policy_decisions.jsonl"
OPTIMIZER_REPORT_FILENAME = "reasoning_effort_policy_optimizer_reports.jsonl"
OPTIMIZER_STATE_FILENAME = "reasoning_effort_policy_optimizer_state.json"

_AUDIT_LOCK = threading.Lock()
_OPTIMIZER_LOCK = threading.Lock()
_OPTIMIZER_RUNNING = False


@dataclass
class ReasoningEffortDecision:
    task_id: str
    selected_effort: str
    decision_source: str
    reason: str
    active_backend: str
    main_model: str
    local_rule: str = ""

    def usage_metadata(self) -> Dict[str, Any]:
        return {
            "reasoning_effort_selected": self.selected_effort,
            "reasoning_effort_decision_source": self.decision_source,
            "reasoning_effort_reason": self.reason,
            "reasoning_effort_backend": self.active_backend,
            "reasoning_effort_main_model": self.main_model,
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

    medium_rule = _match_medium_rule(text)
    if medium_rule:
        return default_effort, medium_rule

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
        "task_id": decision.task_id,
        "active_backend": decision.active_backend,
        "main_model": decision.main_model,
        "selected_effort": decision.selected_effort,
        "decision_source": decision.decision_source,
        "reason": _safe_text(decision.reason, 160),
        "local_rule": _safe_text(decision.local_rule, 96),
        "channel_type": _safe_text(getattr(model_adapter, "channel_type", ""), 64),
        "session_hash": _hash_optional(getattr(model_adapter, "session_id", "")),
        "user_hash": _hash_optional(getattr(model_adapter, "user_id", "")),
        "message_hash": stable_metadata_hash(str(user_message or "")),
    }
    record = {key: value for key, value in record.items() if value not in ("", None)}

    try:
        path = audit_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _AUDIT_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as exc:
        logger.debug(f"[ReasoningPolicy] Failed to record decision: {exc}")
        return

    maybe_trigger_policy_optimizer_async(model_adapter)


def maybe_trigger_policy_optimizer_async(model_adapter: Any = None) -> bool:
    """Start a non-blocking optimizer pass when enough new decisions exist."""
    if not bool(conf().get("reasoning_effort_policy_auto_optimize_enabled", False)):
        return False

    threshold = max(1, _to_int(conf().get("reasoning_effort_policy_auto_optimize_every_tasks")) or DEFAULT_OPTIMIZE_EVERY)
    total_count = _count_jsonl_records(audit_log_path())
    state = _read_optimizer_state()
    last_count = _to_int(state.get("last_optimized_record_count"))
    if total_count - last_count < threshold:
        return False

    global _OPTIMIZER_RUNNING
    with _OPTIMIZER_LOCK:
        if _OPTIMIZER_RUNNING:
            return False
        _OPTIMIZER_RUNNING = True

    def _worker() -> None:
        global _OPTIMIZER_RUNNING
        try:
            run_policy_optimizer_once(model_adapter=model_adapter, record_count=total_count, reason="threshold")
        finally:
            with _OPTIMIZER_LOCK:
                _OPTIMIZER_RUNNING = False

    thread = threading.Thread(target=_worker, name="reasoning-effort-policy-optimizer", daemon=True)
    thread.start()
    return True


def run_policy_optimizer_once(
    *,
    model_adapter: Any,
    record_count: Optional[int] = None,
    reason: str = "manual",
) -> Dict[str, Any]:
    """Analyze recent routing decisions with same-backend gpt-5.5+xhigh."""
    records = _read_jsonl_tail(audit_log_path(), limit=200)
    record_count = record_count if record_count is not None else _count_jsonl_records(audit_log_path())
    active_backend = get_current_backend()
    started = datetime.now(timezone.utc).isoformat()

    report: Dict[str, Any] = {
        "timestamp": started,
        "status": "skipped",
        "reason": reason,
        "active_backend": active_backend,
        "optimizer_model": "gpt-5.5",
        "optimizer_reasoning_effort": "xhigh",
        "analyzed_records": len(records),
    }
    if not records or model_adapter is None:
        report["failure_reason"] = "no_records_or_model_adapter"
        _append_optimizer_report(report)
        _write_optimizer_state(record_count, report["status"])
        return report

    prompt = _optimizer_prompt(records)
    try:
        request = LLMRequest(
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            temperature=0,
            max_tokens=900,
            stream=False,
            system=(
                "You optimize a conservative routing policy. Return concise JSON-like "
                "recommendations. Do not request raw user text."
            ),
            model="gpt-5.5",
            reasoning_effort="xhigh",
        )
        response = model_adapter.call(request)
        text = _extract_response_text(response)
        if not text:
            raise RuntimeError("empty_optimizer_response")
        report["status"] = "success"
        report["recommendation"] = text[:6000]
    except Exception as exc:
        report["status"] = "failed"
        report["failure_reason"] = _safe_text(str(exc), 240)

    _append_optimizer_report(report)
    _write_optimizer_state(record_count, report["status"])
    return report


def audit_log_path() -> str:
    return os.path.join(_workspace_data_dir(), AUDIT_FILENAME)


def optimizer_report_path() -> str:
    return os.path.join(_workspace_data_dir(), OPTIMIZER_REPORT_FILENAME)


def optimizer_state_path() -> str:
    return os.path.join(_workspace_data_dir(), OPTIMIZER_STATE_FILENAME)


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
        "coding": r"(代码|编程|函数|类|接口|脚本|python|typescript|javascript|java|sql|docker|api|backend|frontend|code|function|class|script)",
        "debugging": r"(报错|错误|异常|调试|修复|bug|traceback|stack trace|exception|debug|fix|failing test)",
        "repo_work": r"(仓库|文件|目录|路径|git|commit|push|部署|发布|迁移|测试|单元测试|repo|file|directory|deploy|migration|unit test)",
        "quality_first": r"(深入分析|详细分析|方案设计|架构|重构|code review|review|质量优先|开发方案|实现方案)",
        "high_risk": r"(权限|安全|删除|移除|合规|财务|法律|医疗|credential|secret|permission|security|delete|remove|legal|medical|finance)",
        "multi_step": r"(多步骤|自动优化|后台任务|定时任务|工具调用|multi-step|background job|scheduler|tool call)",
    }
    for rule, pattern in quality_patterns.items():
        if re.search(pattern, text, re.IGNORECASE):
            return rule
    return ""


def _match_medium_rule(text: str) -> str:
    if len(text) > 180:
        return ""
    medium_patterns = {
        "greeting": r"^(你好|您好|早上好|晚上好|hi|hello|hey|thanks|谢谢|好的|ok|嗯|收到)[\s!！。,.，]*$",
        "short_translation": r"(翻译|translate).{0,120}$",
        "short_rewrite": r"(改写|润色|polish|rewrite).{0,120}$",
        "simple_explain": r"^(简单)?(解释|说明).{0,120}$|是什么[？?]?$|什么意思[？?]?$",
    }
    for rule, pattern in medium_patterns.items():
        if re.search(pattern, text, re.IGNORECASE):
            return rule
    return ""


def _policy_enabled() -> bool:
    return bool(conf().get("reasoning_effort_policy_enabled", False))


def _admin_only() -> bool:
    return bool(conf().get("reasoning_effort_policy_admin_only", True))


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


def _count_jsonl_records(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0
    except Exception as exc:
        logger.debug(f"[ReasoningPolicy] Failed to count records: {exc}")
        return 0


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


def _optimizer_prompt(records: List[Dict[str, Any]]) -> str:
    compact_records = []
    for item in records[-200:]:
        compact_records.append({
            "selected_effort": item.get("selected_effort"),
            "decision_source": item.get("decision_source"),
            "reason": item.get("reason"),
            "local_rule": item.get("local_rule"),
        })
    return (
        "Analyze these sanitized local reasoning-effort routing decisions. Recommend only conservative "
        "local-rule changes. Protect quality-first tasks from being downgraded to medium. Return sections: "
        "add_local_xhigh_rules, add_local_medium_rules, risky_medium_rules, local_rule_coverage.\n\n"
        + json.dumps(compact_records, ensure_ascii=False, separators=(",", ":"))
    )
