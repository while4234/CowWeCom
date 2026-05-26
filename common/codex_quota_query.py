# encoding:utf-8

"""Shared helper for querying the local Codex quota snapshot script."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict

from config import get_root


def query_codex_quota_json(*, timeout_seconds: float = 120.0) -> Dict[str, Any]:
    """Run the codex-quota skill and return its JSON snapshot."""
    script = os.path.join(get_root(), "skills", "codex-quota-query", "scripts", "check_codex_quota.py")
    if not os.path.isfile(script):
        raise RuntimeError("codex quota skill script not found")
    timeout_ms = max(1000, int(float(timeout_seconds) * 1000))
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    proc = subprocess.run(
        [
            sys.executable,
            script,
            "--project-dir",
            get_root(),
            "--format",
            "json",
            "--timeout-ms",
            str(timeout_ms),
        ],
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
        text = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(text[:500] or f"quota query failed with exit code {proc.returncode}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"quota query returned invalid JSON: {e}") from e
    return payload if isinstance(payload, dict) else {}


def format_codex_quota_snapshot_text(payload: Dict[str, Any]) -> str:
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    rate_limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), list) else []
    lines = [
        "GPT/Codex quota",
        f"- account: {account.get('label') or account.get('email_masked') or 'unknown'}",
        f"- plan: {account.get('plan_type') or account.get('planType') or 'unknown'}",
        f"- account type: {account.get('type') or 'unknown'}",
        f"- status: {'blocked' if summary.get('blocked') else 'available'}",
        "",
    ]
    if not rate_limits:
        lines.append("- no Codex rate limit data returned")
        return "\n".join(lines).strip()

    for limit in rate_limits:
        if not isinstance(limit, dict):
            continue
        limit_name = limit.get("limit_name") or limit.get("limitName") or "Codex"
        status = limit.get("status") or "unknown"
        reached = limit.get("reached_type") or ""
        lines.append(f"{limit_name}: {status}{f' ({reached})' if reached else ''}")
        windows = limit.get("windows") if isinstance(limit.get("windows"), list) else []
        for window in windows:
            if not isinstance(window, dict):
                continue
            used = _format_percent(window.get("used_percent"))
            remaining = _format_percent(window.get("remaining_percent"))
            reset_at = window.get("reset_at") or "unknown"
            lines.append(
                f"- {_window_text(window.get('window_minutes'))}: "
                f"used {used}, remaining about {remaining}, resets {reset_at}"
            )
        lines.append("")
    return "\n".join(lines).strip()


def _format_percent(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "unknown"
    return f"{number:.1f}".rstrip("0").rstrip(".") + "%"


def _window_text(value: Any) -> str:
    try:
        minutes = int(float(value))
    except (TypeError, ValueError):
        return "window"
    if minutes % 10080 == 0:
        return f"{(minutes // 10080) * 7} day window"
    if minutes % 1440 == 0:
        return f"{minutes // 1440} day window"
    if minutes % 60 == 0:
        return f"{minutes // 60} hour window"
    return f"{minutes} minute window"
