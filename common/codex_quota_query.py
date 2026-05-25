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
    script = os.path.join(get_root(), "skills", "codex-quota-query", "scripts", "codex_quota.py")
    if not os.path.isfile(script):
        raise RuntimeError("codex quota skill script not found")
    proc = subprocess.run(
        [sys.executable, script, "snapshot", "--format", "json"],
        cwd=get_root(),
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
