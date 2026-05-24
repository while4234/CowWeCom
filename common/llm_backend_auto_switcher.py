# encoding:utf-8

"""Daily Codex quota based backend auto-switcher."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from common.llm_backend_router import evaluate_midnight_backend_route, get_llm_backend_config, record_auto_check
from common.log import logger
from config import get_root


_STARTED = False
_LOCK = threading.Lock()


def start_llm_backend_auto_switcher() -> None:
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    thread = threading.Thread(target=_run_loop, name="llm-backend-auto-switcher", daemon=True)
    thread.start()
    logger.info("[LLMBackend] Auto-switcher started")


def run_once() -> Dict[str, Any]:
    return evaluate_midnight_backend_route(quota_payload_factory=_query_codex_quota_json, now=datetime.now())


def _run_loop() -> None:
    while True:
        try:
            time.sleep(_seconds_until_next_check())
            try:
                state = run_once()
                auto = state.get("auto", {}) if isinstance(state, dict) else {}
                logger.info(
                    "[LLMBackend] Auto-switch check: decision=%s reason=%s",
                    auto.get("last_decision", ""),
                    auto.get("last_reason", ""),
                )
            except Exception as e:
                logger.warning(f"[LLMBackend] Auto-switch check failed: {e}")
                record_auto_check(decision="error", reason=str(e)[:300], now=datetime.now())
        except Exception as e:
            logger.warning(f"[LLMBackend] Auto-switch loop error: {e}")
            time.sleep(60)


def _seconds_until_next_check(now: Optional[datetime] = None) -> float:
    now = now or datetime.now()
    check_time = str(get_llm_backend_config().get("auto_switch", {}).get("check_time") or "00:00")
    hour, minute = _parse_check_time(check_time)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def _parse_check_time(value: str):
    try:
        raw_hour, raw_minute = str(value or "00:00").split(":", 1)
        hour = max(0, min(23, int(raw_hour)))
        minute = max(0, min(59, int(raw_minute)))
        return hour, minute
    except Exception:
        return 0, 0


def _query_codex_quota_json() -> Dict[str, Any]:
    script = os.path.join(get_root(), "skills", "codex-quota-query", "scripts", "codex_quota.py")
    if not os.path.isfile(script):
        raise RuntimeError("codex quota skill script not found")
    cmd = [sys.executable, script, "snapshot", "--format", "json"]
    proc = subprocess.run(
        cmd,
        cwd=get_root(),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        text = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(text[:500] or f"quota query failed with exit code {proc.returncode}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"quota query returned invalid JSON: {e}")
