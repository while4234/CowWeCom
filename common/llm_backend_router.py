# encoding:utf-8

"""Runtime state and routing helpers for the active LLM backend."""

from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime
from typing import Any, Callable, Dict, Mapping, Optional

from common import const
from common.codex_quota_logic import CodexQuotaDecision, decide_codex_auto_switch, decision_to_dict
from common.log import logger


BACKEND_CAPI = "capi"
BACKEND_CAPI_MONTHLY = "capi_monthly"
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
            "label": "CAPI/OpenAI-compatible quota card",
            "api_key": "",
            "api_key_env": "CAPI_API_KEY",
            "api_base": "",
            "api_base_env": "OPENAI_API_BASE",
            "wire_api": "",
            "model": "",
            "request_timeout_seconds": 120,
            "connectivity_timeout_seconds": 12,
        },
        "capi_monthly": {
            "label": "CAPI Monthly Card",
            "api_key": "",
            "api_key_env": "CAPI_MONTHLY_API_KEY",
            "api_base": "",
            "api_base_env": "OPENAI_API_BASE",
            "wire_api": "",
            "model": "",
            "request_timeout_seconds": 120,
            "connectivity_timeout_seconds": 12,
            "default_daily_quota": 90,
        },
    },
    "auto_switch": {
        "enabled": True,
        "check_time": "00:00",
        "quota_window": "weekly",
        "fair_share_days": 7,
        "min_remaining_percent": 15,
        "respect_manual_override": True,
        "prefer_capi_monthly_at_check_time": True,
        "monthly_post_task_check_enabled": True,
        "monthly_min_remaining_percent": 10,
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


def get_capi_provider_config(backend: Optional[str] = None) -> Dict[str, Any]:
    cfg = get_llm_backend_config()
    providers = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    base = deep_merge(DEFAULT_LLM_BACKEND_CONFIG["providers"]["capi"], providers.get("capi", {}))
    if normalize_backend(backend or get_current_backend()) == BACKEND_CAPI_MONTHLY:
        monthly = deep_merge(DEFAULT_LLM_BACKEND_CONFIG["providers"]["capi_monthly"], providers.get("capi_monthly", {}))
        merged = deep_merge(base, monthly)
        for inherited_key in ("api_base", "wire_api", "model"):
            if not str(monthly.get(inherited_key) or "").strip():
                merged[inherited_key] = base.get(inherited_key, "")
        return merged
    return base


def resolve_provider_value(provider: Mapping[str, Any], value_key: str, env_key: str) -> str:
    env_name = str(provider.get(env_key) or "").strip()
    if env_name:
        env_value = os.getenv(env_name)
        if env_value:
            return str(env_value)
    value = provider.get(value_key)
    return str(value) if value else ""


def _provider_timeout_seconds(
    provider: Mapping[str, Any],
    key: str,
    default: float,
    *,
    minimum: float = 1.0,
    maximum: float = 600.0,
) -> float:
    try:
        timeout = float(provider.get(key) or default)
    except (TypeError, ValueError):
        timeout = default
    return max(minimum, min(float(timeout), maximum))


def get_effective_openai_api_config(backend: Optional[str] = None) -> Dict[str, Any]:
    """Return route-aware OpenAI-compatible API settings for the active CAPI backend."""
    from config import conf

    normalized_backend = normalize_backend(backend or get_current_backend())
    provider = get_capi_provider_config(normalized_backend)
    api_key = resolve_provider_value(provider, "api_key", "api_key_env")
    api_base = resolve_provider_value(provider, "api_base", "api_base_env") or str(conf().get("open_ai_api_base") or "")
    wire_api = str(
        provider.get("wire_api")
        or conf().get("open_ai_wire_api")
        or conf().get("openai_wire_api")
        or conf().get("wire_api")
        or ""
    )
    model = str(provider.get("model") or conf().get("model") or const.GPT_41_MINI)
    return {
        "api_key": api_key,
        "api_base": api_base,
        "wire_api": wire_api,
        "model": model,
        "backend": normalized_backend,
        "request_timeout_seconds": _provider_timeout_seconds(
            provider,
            "request_timeout_seconds",
            120.0,
        ),
    }


def has_capi_monthly_credentials() -> bool:
    provider = get_capi_provider_config(BACKEND_CAPI_MONTHLY)
    return bool(resolve_provider_value(provider, "api_key", "api_key_env"))


def has_capi_credentials(backend: Optional[str] = None) -> bool:
    provider = get_capi_provider_config(backend or BACKEND_CAPI)
    return bool(resolve_provider_value(provider, "api_key", "api_key_env"))


def is_capi_backend(backend: Optional[str]) -> bool:
    if backend is None:
        return False
    return normalize_backend(backend or "") in {BACKEND_CAPI, BACKEND_CAPI_MONTHLY}


def check_capi_connectivity(backend: Optional[str] = None, *, timeout_seconds: Optional[float] = None) -> bool:
    """Probe the OpenAI-compatible endpoint used by the selected CAPI backend."""
    normalized_backend = normalize_backend(backend or get_current_backend())
    if not is_capi_backend(normalized_backend):
        return True

    from config import conf
    from models.openai.openai_http_client import OpenAIHTTPClient, OpenAIHTTPError
    from models.openai.responses_api_adapter import build_responses_payload, is_responses_wire_api

    provider = get_capi_provider_config(normalized_backend)
    routed = get_effective_openai_api_config(normalized_backend)
    model = str(routed.get("model") or "").strip()
    api_key = str(routed.get("api_key") or "").strip()
    api_base = str(routed.get("api_base") or "").strip()
    if not model or not api_key:
        logger.warning(
            "[LLMBackend] CAPI connectivity probe skipped: missing model or API key "
            "(backend=%s)",
            normalized_backend,
        )
        return False

    timeout = (
        max(1.0, min(float(timeout_seconds), 60.0))
        if timeout_seconds is not None
        else _provider_timeout_seconds(
            provider,
            "connectivity_timeout_seconds",
            12.0,
            maximum=60.0,
        )
    )
    client = OpenAIHTTPClient(proxy=conf().get("proxy") or None, timeout=timeout)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }

    try:
        if is_responses_wire_api(routed.get("wire_api")):
            responses_payload = build_responses_payload(payload, store=False)
            stream = client.responses(
                api_key=api_key,
                api_base=api_base or None,
                timeout=timeout,
                stream=True,
                **responses_payload,
            )
        else:
            stream = client.chat_completions(
                api_key=api_key,
                api_base=api_base or None,
                timeout=timeout,
                stream=True,
                **payload,
            )
        if not _capi_stream_probe_succeeded(stream, normalized_backend):
            return False
        logger.info("[LLMBackend] CAPI connectivity probe succeeded: backend=%s", normalized_backend)
        return True
    except OpenAIHTTPError as e:
        logger.warning(
            "[LLMBackend] CAPI connectivity probe failed: backend=%s status=%s message=%s",
            normalized_backend,
            e.status_code,
            e.message[:180],
        )
        return False
    except Exception as e:
        logger.warning(
            "[LLMBackend] CAPI connectivity probe failed: backend=%s error=%s",
            normalized_backend,
            str(e)[:180],
        )
        return False


def _capi_stream_probe_succeeded(stream: Any, backend: str) -> bool:
    for event in stream:
        if isinstance(event, dict) and event.get("error"):
            logger.warning(
                "[LLMBackend] CAPI connectivity probe failed: backend=%s status=%s message=%s",
                backend,
                event.get("status_code", ""),
                str(event.get("message") or event.get("error") or "")[:180],
            )
            return False
        return True
    logger.warning("[LLMBackend] CAPI connectivity probe failed: backend=%s empty stream", backend)
    return False


def is_capi_quota_exhausted_error(error: Any) -> bool:
    """Return True for hard quota/payment exhaustion from CAPI-style relays."""
    text = _stringify_error(error).lower()
    status_code = _extract_status_code(text)
    quota_markers = (
        "insufficient_quota",
        "quota_exceeded",
        "quota exceeded",
        "quota exhausted",
        "quota has been exhausted",
        "credit exhausted",
        "credits exhausted",
        "no credit",
        "no credits",
        "balance not enough",
        "insufficient balance",
        "payment required",
        "billing hard limit",
        "monthly quota",
        "monthly limit",
        "额度不足",
        "额度用完",
        "额度耗尽",
        "余额不足",
        "月卡额度",
        "套餐额度",
    )
    if any(marker in text for marker in quota_markers):
        return True
    return status_code == 402


def is_capi_runtime_fallback_error(error: Any) -> bool:
    """Return True for CAPI failures that should be replayed on a fallback backend."""
    text = _stringify_error(error).lower()
    status_code = _extract_status_code(text)
    if is_capi_quota_exhausted_error(error):
        return True
    if status_code in {0, 408, 429, 500, 502, 503, 504, 512}:
        return True
    if status_code is not None and 400 <= status_code < 500:
        return False
    transient_markers = (
        "connection error",
        "connecterror",
        "provider_network_error",
        "connection aborted",
        "connection refused",
        "connection reset",
        "connectionreseterror",
        "ssleoferror",
        "unexpected_eof",
        "max retries exceeded",
        "name resolution",
        "nameresolutionerror",
        "temporary failure in name resolution",
        "getaddrinfo",
        "remote disconnected",
        "remotedisconnected",
        "remote end closed",
        "remote host",
        "request timed out",
        "read timeout",
        "readtimeout",
        "timeout",
        "timed out",
        "stream interrupted",
        "stream error",
        "chunkedencodingerror",
        "chunked encoding",
        "internal server error",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
        "too many pending requests",
        "concurrency limit exceeded",
        "rate_limit_error",
        "rate limit",
        "overloaded",
        "unavailable",
        "temporarily unavailable",
        "server busy",
    )
    return any(marker in text for marker in transient_markers)


def select_capi_runtime_fallback_backend(
    previous_backend: str,
    error: Any,
    *,
    quota_payload: Optional[Mapping[str, Any]] = None,
    quota_payload_factory: Optional[Callable[[], Mapping[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> str:
    """Pick the next backend for an in-flight CAPI Agent replay."""
    normalized = normalize_backend(previous_backend)
    if normalized == BACKEND_CAPI_MONTHLY and is_capi_quota_exhausted_error(error):
        payload = quota_payload
        if payload is None and quota_payload_factory is not None:
            try:
                payload = quota_payload_factory()
            except Exception as e:
                logger.warning(
                    "[LLMBackend] Codex quota query failed during runtime monthly fallback: %s",
                    str(e)[:300],
                )
                payload = None

        if payload is not None:
            cfg = get_llm_backend_config()
            auto_cfg = cfg.get("auto_switch", {}) if isinstance(cfg.get("auto_switch"), dict) else {}
            quota_decision = decide_codex_auto_switch(
                payload,
                now=now,
                fair_share_days=int(auto_cfg.get("fair_share_days", 7) or 7),
                min_remaining_percent=float(auto_cfg.get("min_remaining_percent", 15) or 15),
            )
            logger.info(
                "[LLMBackend] Runtime monthly fallback Codex quota decision: "
                "should_switch=%s reason=%s",
                quota_decision.should_switch,
                quota_decision.reason,
            )
            if quota_decision.should_switch:
                return BACKEND_CODEX

        if has_capi_credentials(BACKEND_CAPI):
            return BACKEND_CAPI
    return BACKEND_CODEX


def _stringify_error(error: Any) -> str:
    if isinstance(error, Mapping):
        return json.dumps(error, ensure_ascii=False, default=str)
    return str(error or "")


def _extract_status_code(text: str) -> Optional[int]:
    for pattern in (
        r"status(?:[_\s]+code)?\s*[:=]\s*([0-9]{1,3})",
        r"status:\s*([0-9]{1,3})",
        r"http\s+([0-9]{1,3})",
    ):
        match = re.search(pattern, text)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
    return None


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
    if raw in {BACKEND_CAPI_MONTHLY, "capi-monthly", "capi_month", "capi-month", "monthly", "month"}:
        return BACKEND_CAPI_MONTHLY
    return BACKEND_CAPI


def is_codex_active() -> bool:
    return get_current_backend() == BACKEND_CODEX


def is_capi_monthly_active() -> bool:
    return get_current_backend() == BACKEND_CAPI_MONTHLY


def get_effective_model() -> str:
    if is_codex_active():
        return get_codex_model()
    capi_model = get_capi_provider_config().get("model")
    if capi_model:
        return str(capi_model)
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
    else:
        state["manual_override_active"] = False
        state["auto_switch_latched"] = normalized == BACKEND_CODEX
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
        "monthly_card": state.get("monthly_card", {}) if isinstance(state.get("monthly_card"), dict) else {},
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
    monthly = snapshot.get("monthly_card") or {}
    if monthly:
        lines.append(f"- monthly_remaining: {monthly.get('remaining', '')}/{monthly.get('total', '')}")
        lines.append(f"- monthly_last_action: {monthly.get('last_action', '')}")
    return "\n".join(lines)


def record_auto_check(
    *,
    decision: str,
    reason: str,
    quota_decision: Optional[CodexQuotaDecision] = None,
    switched: bool = False,
    switched_backend: Optional[str] = None,
    clear_manual_override: bool = False,
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
    target_backend = normalize_backend(switched_backend) if switched_backend else (BACKEND_CODEX if switched else "")
    if target_backend:
        state["current_backend"] = target_backend
        state["current_backend_source"] = "auto"
        state["manual_override_active"] = False
        state["auto_switch_latched"] = target_backend == BACKEND_CODEX
    elif clear_manual_override:
        state["current_backend_source"] = "auto"
        state["manual_override_active"] = False
        state["auto_switch_latched"] = False
    save_state(state)
    if target_backend or clear_manual_override:
        _reset_bridge_cache()
    return state


def record_monthly_quota_check(
    snapshot: Mapping[str, Any],
    *,
    action: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now or datetime.now()
    quota = snapshot.get("quota") if isinstance(snapshot.get("quota"), dict) else {}
    total = _number_or_zero(quota.get("total"))
    remaining = _number_or_zero(quota.get("remaining"))
    remaining_percent = (remaining / total * 100.0) if total > 0 else 0.0
    state = load_state()
    state["monthly_card"] = {
        "last_checked_at": now.isoformat(timespec="seconds"),
        "mode": quota.get("mode") or ("total" if quota.get("total_mode") else "daily"),
        "total": total,
        "used": _number_or_zero(quota.get("used")),
        "remaining": remaining,
        "remaining_percent": round(remaining_percent, 2),
        "progress": _number_or_zero(quota.get("progress")),
        "expire_at": quota.get("expire_at"),
        "last_action": action,
    }
    state["updated_at"] = now.isoformat(timespec="seconds")
    save_state(state)
    return state


def evaluate_midnight_backend_route(
    quota_payload: Optional[Mapping[str, Any]] = None,
    *,
    quota_payload_factory: Optional[Callable[[], Mapping[str, Any]]] = None,
    capi_connectivity_checker: Optional[Callable[[str], bool]] = None,
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

    checker = capi_connectivity_checker or check_capi_connectivity
    if bool(auto_cfg.get("prefer_capi_monthly_at_check_time", True)) and has_capi_monthly_credentials():
        if not _run_capi_connectivity_check(BACKEND_CAPI_MONTHLY, checker):
            return _record_capi_connectivity_fallback(BACKEND_CAPI_MONTHLY, now=now)
        return record_auto_check(
            decision="switched_to_capi_monthly",
            reason="daily_monthly_card_reset",
            switched_backend=BACKEND_CAPI_MONTHLY,
            now=now,
        )

    if not _run_capi_connectivity_check(BACKEND_CAPI, checker):
        return _record_capi_connectivity_fallback(BACKEND_CAPI, now=now)

    payload = quota_payload
    if payload is None and quota_payload_factory is not None:
        payload = quota_payload_factory()
    return evaluate_auto_switch(
        payload or {},
        ignore_manual_override=True,
        clear_manual_override_on_check=True,
        now=now,
    )


def _run_capi_connectivity_check(backend: str, checker: Callable[[str], bool]) -> bool:
    try:
        return bool(checker(backend))
    except Exception as e:
        logger.warning(
            "[LLMBackend] CAPI connectivity checker raised: backend=%s error=%s",
            normalize_backend(backend),
            str(e)[:180],
        )
        return False


def _record_capi_connectivity_fallback(backend: str, *, now: datetime) -> Dict[str, Any]:
    normalized = normalize_backend(backend)
    return record_auto_check(
        decision="switched_to_codex",
        reason=f"capi_connectivity_failed:{normalized}",
        switched_backend=BACKEND_CODEX,
        now=now,
    )


def select_backend_after_monthly_quota_low(
    quota_payload: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now or datetime.now()
    cfg = get_llm_backend_config()
    auto_cfg = cfg.get("auto_switch", {}) if isinstance(cfg.get("auto_switch"), dict) else {}
    quota_decision = decide_codex_auto_switch(
        quota_payload,
        now=now,
        fair_share_days=int(auto_cfg.get("fair_share_days", 7) or 7),
        min_remaining_percent=float(auto_cfg.get("min_remaining_percent", 15) or 15),
    )
    target = BACKEND_CODEX if quota_decision.should_switch else BACKEND_CAPI
    return record_auto_check(
        decision="monthly_low_switched_to_{}".format(target),
        reason=quota_decision.reason,
        quota_decision=quota_decision,
        switched_backend=target,
        now=now,
    )


def evaluate_auto_switch(
    quota_payload: Mapping[str, Any],
    *,
    ignore_manual_override: bool = False,
    clear_manual_override_on_check: bool = False,
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
    if (
        bool(state.get("manual_override_active"))
        and bool(auto_cfg.get("respect_manual_override", True))
        and not ignore_manual_override
    ):
        return record_auto_check(decision="skipped", reason="manual_override_active", now=now)
    if bool(state.get("auto_switch_latched")):
        return record_auto_check(
            decision="skipped",
            reason="auto_switch_latched",
            clear_manual_override=clear_manual_override_on_check,
            now=now,
        )
    if get_current_backend() == BACKEND_CODEX:
        return record_auto_check(
            decision="kept",
            reason="already_codex",
            clear_manual_override=clear_manual_override_on_check,
            now=now,
        )

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
        clear_manual_override=clear_manual_override_on_check,
        now=now,
    )


def _number_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _reset_bridge_cache() -> None:
    try:
        from bridge.bridge import Bridge

        Bridge().reset_bot()
    except Exception as e:
        logger.debug(f"[LLMBackend] Bridge cache reset skipped: {e}")
    _reset_cloud_chat_service()


def _reset_cloud_chat_service() -> None:
    """Drop cloud ChatService so existing cloud sessions bind the current Bridge."""
    try:
        import sys

        cloud_module = sys.modules.get("common.cloud_client")
        client = getattr(cloud_module, "chat_client", None) if cloud_module else None
        if client is not None and hasattr(client, "_chat_service"):
            client._chat_service = None
    except Exception as e:
        logger.debug(f"[LLMBackend] Cloud chat service reset skipped: {e}")
