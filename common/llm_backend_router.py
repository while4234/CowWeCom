# encoding:utf-8

"""Runtime state and routing helpers for the active LLM backend."""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from common import const
from common.codex_quota_logic import CodexQuotaDecision, decide_codex_auto_switch, decision_to_dict
from common.log import logger


BACKEND_CAPI = "capi"
BACKEND_CODEX = "codex"


DEFAULT_LLM_BACKEND_CONFIG: Dict[str, Any] = {
    "current_backend": BACKEND_CAPI,
    "state_path": "",
    "providers": {
        "codex": {
            "auth_file": "",
            "model": "gpt-5.5",
            "reasoning_effort": "xhigh",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "endpoint_path": "/responses",
            "timeout_seconds": 120,
            "retry_count": 1,
            "retry_delay_seconds": 1.0,
            "tools_enabled": True,
        },
        "capi": {
            "label": "CAPI/OpenAI-compatible",
        },
    },
    "auto_switch": {
        "enabled": True,
        "check_time": "00:00",
        "quota_window": "weekly",
        "fair_share_days": 7,
        "min_remaining_percent": 15,
        "respect_manual_override": True,
    },
}


def deep_merge(defaults: Mapping[str, Any], overrides: Any) -> Dict[str, Any]:
    merged = copy.deepcopy(dict(defaults))
    if not isinstance(overrides, Mapping):
        return merged
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def get_llm_backend_config() -> Dict[str, Any]:
    from config import conf

    return deep_merge(DEFAULT_LLM_BACKEND_CONFIG, conf().get("llm_backend", {}))


def get_codex_provider_config() -> Dict[str, Any]:
    cfg = get_llm_backend_config()
    providers = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    return deep_merge(DEFAULT_LLM_BACKEND_CONFIG["providers"]["codex"], providers.get("codex", {}))


def get_codex_model() -> str:
    return str(get_codex_provider_config().get("model") or "gpt-5.5")


def get_state_path() -> str:
    cfg = get_llm_backend_config()
    configured = str(cfg.get("state_path") or "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    from config import get_root

    return os.path.join(get_root(), "data", "llm-backend-router", "state.json")


def load_state() -> Dict[str, Any]:
    path = get_state_path()
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"[LLMBackend] Failed to read backend state {path}: {e}")
        return {}


def save_state(state: Mapping[str, Any]) -> None:
    path = get_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(dict(state), f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def get_current_backend() -> str:
    state = load_state()
    backend = str(state.get("current_backend") or get_llm_backend_config().get("current_backend") or BACKEND_CAPI)
    return normalize_backend(backend)


def normalize_backend(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in {BACKEND_CODEX, "openai-codex", "codex-direct"}:
        return BACKEND_CODEX
    return BACKEND_CAPI


def is_codex_active() -> bool:
    return get_current_backend() == BACKEND_CODEX


def get_effective_model() -> str:
    if is_codex_active():
        return get_codex_model()
    from config import conf

    return str(conf().get("model") or const.GPT_41_MINI)


def get_effective_chat_bot_type(model_name: Optional[str] = None) -> str:
    if is_codex_active():
        return const.CODEX
    return resolve_configured_chat_bot_type(model_name)


def resolve_configured_chat_bot_type(model_name: Optional[str] = None) -> str:
    from config import conf

    if conf().get("use_linkai", False) and conf().get("linkai_api_key"):
        return const.LINKAI
    configured_bot_type = conf().get("bot_type")
    if configured_bot_type:
        if str(configured_bot_type).strip().lower() == BACKEND_CODEX:
            return const.OPENAI
        return configured_bot_type

    model_type = model_name if model_name is not None else conf().get("model") or const.GPT_41_MINI
    if not isinstance(model_type, str):
        logger.warning(
            "[LLMBackend] model_type is not a string: %s (type: %s), converting to string",
            model_type,
            type(model_type).__name__,
        )
        model_type = str(model_type)

    if model_type in ["text-davinci-003"]:
        return const.OPEN_AI
    if conf().get("use_azure_chatgpt", False):
        return const.CHATGPTONAZURE
    if model_type in ["wenxin", "wenxin-4"]:
        return const.BAIDU
    if model_type in ["xunfei"]:
        return const.XUNFEI
    if model_type in [const.QWEN, const.QWEN_TURBO, const.QWEN_PLUS, const.QWEN_MAX]:
        return const.QWEN_DASHSCOPE

    lowered = model_type.lower()
    if lowered.startswith(("qwen", "qwq", "qvq")):
        return const.QWEN_DASHSCOPE
    if lowered.startswith("gemini"):
        return const.GEMINI
    if lowered.startswith("glm"):
        return const.ZHIPU_AI
    if lowered.startswith("claude"):
        return const.CLAUDEAPI
    if model_type in [const.MOONSHOT, "moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"]:
        return const.MOONSHOT
    if lowered.startswith("kimi"):
        return const.MOONSHOT
    if lowered.startswith("doubao"):
        return const.DOUBAO
    if lowered.startswith("deepseek"):
        return const.DEEPSEEK
    if lowered == const.QIANFAN or lowered.startswith("ernie"):
        return const.QIANFAN
    if model_type in [const.MODELSCOPE]:
        return const.MODELSCOPE
    if lowered.startswith("minimax") or model_type in ["abab6.5-chat", "abab6.5"]:
        return const.MiniMax
    return const.OPENAI


def set_current_backend(backend: str, *, manual: bool = True, reason: str = "") -> Dict[str, Any]:
    normalized = normalize_backend(backend)
    state = load_state()
    state["current_backend"] = normalized
    state["current_backend_source"] = "manual" if manual else "auto"
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    if reason:
        state["last_reason"] = reason
    if manual:
        state["manual_override_active"] = True
        state["manual_changed_at"] = state["updated_at"]
        state["auto_switch_latched"] = False
    save_state(state)
    _reset_bridge_cache()
    return state


def clear_manual_override() -> Dict[str, Any]:
    state = load_state()
    state["manual_override_active"] = False
    state["auto_switch_latched"] = False
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)
    return state


def status_snapshot() -> Dict[str, Any]:
    state = load_state()
    return {
        "current_backend": get_current_backend(),
        "effective_model": get_effective_model(),
        "manual_override_active": bool(state.get("manual_override_active", False)),
        "auto_switch_latched": bool(state.get("auto_switch_latched", False)),
        "auto": state.get("auto", {}) if isinstance(state.get("auto"), dict) else {},
    }


def describe_status() -> str:
    snapshot = status_snapshot()
    auto = snapshot.get("auto") or {}
    lines = [
        "LLM backend status",
        f"- current_backend: {snapshot['current_backend']}",
        f"- effective_model: {snapshot['effective_model']}",
        f"- manual_override: {snapshot['manual_override_active']}",
        f"- auto_switch_latched: {snapshot['auto_switch_latched']}",
    ]
    if auto:
        lines.append(f"- last_checked_date: {auto.get('last_checked_date', '')}")
        lines.append(f"- last_decision: {auto.get('last_decision', '')}")
        lines.append(f"- last_reason: {auto.get('last_reason', '')}")
    return "\n".join(lines)


def record_auto_check(
    *,
    decision: str,
    reason: str,
    quota_decision: Optional[CodexQuotaDecision] = None,
    switched: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now or datetime.now()
    state = load_state()
    if not isinstance(state.get("auto"), dict):
        state["auto"] = {}
    state["auto"]["last_checked_date"] = now.date().isoformat()
    state["auto"]["last_checked_at"] = now.isoformat(timespec="seconds")
    state["auto"]["last_decision"] = decision
    state["auto"]["last_reason"] = reason
    if quota_decision is not None:
        state["auto"]["last_quota_decision"] = decision_to_dict(quota_decision)
    if switched:
        state["current_backend"] = BACKEND_CODEX
        state["current_backend_source"] = "auto"
        state["auto_switch_latched"] = True
    save_state(state)
    if switched:
        _reset_bridge_cache()
    return state


def evaluate_auto_switch(
    quota_payload: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now or datetime.now()
    cfg = get_llm_backend_config()
    auto_cfg = cfg.get("auto_switch", {}) if isinstance(cfg.get("auto_switch"), dict) else {}
    state = load_state()
    auto_state = state.get("auto") if isinstance(state.get("auto"), dict) else {}
    today = now.date().isoformat()

    if not bool(auto_cfg.get("enabled", True)):
        return record_auto_check(decision="skipped", reason="auto_disabled", now=now)
    if auto_state.get("last_checked_date") == today:
        return state
    if bool(state.get("manual_override_active")) and bool(auto_cfg.get("respect_manual_override", True)):
        return record_auto_check(decision="skipped", reason="manual_override_active", now=now)
    if bool(state.get("auto_switch_latched")):
        return record_auto_check(decision="skipped", reason="auto_switch_latched", now=now)
    if get_current_backend() == BACKEND_CODEX:
        return record_auto_check(decision="kept", reason="already_codex", now=now)

    quota_decision = decide_codex_auto_switch(
        quota_payload,
        now=now,
        fair_share_days=int(auto_cfg.get("fair_share_days", 7) or 7),
        min_remaining_percent=float(auto_cfg.get("min_remaining_percent", 15) or 15),
    )
    if quota_decision.should_switch:
        return record_auto_check(
            decision="switched_to_codex",
            reason=quota_decision.reason,
            quota_decision=quota_decision,
            switched=True,
            now=now,
        )
    return record_auto_check(
        decision="kept",
        reason=quota_decision.reason,
        quota_decision=quota_decision,
        now=now,
    )


def _reset_bridge_cache() -> None:
    try:
        from bridge.bridge import Bridge

        Bridge().reset_bot()
    except Exception as e:
        logger.debug(f"[LLMBackend] Bridge cache reset skipped: {e}")
