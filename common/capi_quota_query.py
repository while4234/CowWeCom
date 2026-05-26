# encoding:utf-8

"""Shared CAPI quota snapshot helpers for quota-card and monthly-card backends."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from functools import lru_cache
from typing import Any, Dict

from common.llm_backend_router import (
    BACKEND_CAPI,
    BACKEND_CAPI_MONTHLY,
    get_capi_provider_config,
    normalize_backend,
    resolve_provider_value,
)
from config import get_root


def query_capi_quota_snapshot(
    backend: str,
    *,
    include_usage: bool = True,
    timeout_seconds: float = 120.0,
) -> Dict[str, Any]:
    normalized = _normalize_capi_backend(backend)
    provider = get_capi_provider_config(normalized)
    api_key = resolve_provider_value(provider, "api_key", "api_key_env")
    if not api_key:
        label = "CAPI monthly" if normalized == BACKEND_CAPI_MONTHLY else "CAPI quota-card"
        raise RuntimeError(f"{label} API key is not configured")

    script = _capi_usage_script()
    if not os.path.isfile(script):
        raise RuntimeError("CAPI usage monitor script not found")

    env_name = "CAPI_MONTHLY_ROUTER_KEY" if normalized == BACKEND_CAPI_MONTHLY else "CAPI_QUOTA_ROUTER_KEY"
    env = dict(os.environ)
    env[env_name] = api_key

    argv = [
        sys.executable,
        script,
        "snapshot",
        "--api-key-env",
        env_name,
        "--period",
        "today",
        "--format",
        "json",
    ]
    if not include_usage:
        argv.append("--no-usage")
    if normalized == BACKEND_CAPI_MONTHLY:
        argv.extend(["--default-daily-quota", str(provider.get("default_daily_quota") or 90)])

    proc = subprocess.run(
        argv,
        cwd=get_root(),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=max(1.0, float(timeout_seconds)),
        check=False,
    )
    if proc.returncode != 0:
        text = _redact_secret((proc.stderr or proc.stdout or "").strip(), api_key)
        label = "CAPI monthly quota" if normalized == BACKEND_CAPI_MONTHLY else "CAPI quota-card quota"
        raise RuntimeError(text[:500] or f"{label} query failed with exit code {proc.returncode}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"CAPI quota query returned invalid JSON: {e}") from e
    return payload if isinstance(payload, dict) else {}


def format_capi_quota_snapshot_text(snapshot: Dict[str, Any]) -> str:
    formatter = getattr(_capi_usage_module(), "format_snapshot_text", None)
    if not callable(formatter):
        return json.dumps(snapshot, ensure_ascii=False, indent=2)
    return str(formatter(snapshot))


def _normalize_capi_backend(backend: str) -> str:
    normalized = normalize_backend(backend)
    return BACKEND_CAPI_MONTHLY if normalized == BACKEND_CAPI_MONTHLY else BACKEND_CAPI


def _capi_usage_script() -> str:
    return os.path.join(get_root(), "skills", "capi-usage-monitor", "scripts", "capi_usage.py")


@lru_cache(maxsize=1)
def _capi_usage_module():
    script = _capi_usage_script()
    spec = importlib.util.spec_from_file_location("cow_capi_usage_monitor_script", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("CAPI usage monitor script cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _redact_secret(text: str, secret: str) -> str:
    if not text or not secret:
        return text
    replacement = f"{secret[:3]}***{secret[-3:]}" if len(secret) >= 8 else "***"
    return text.replace(secret, replacement)
