# encoding:utf-8

"""Small helpers for latency telemetry logs.

The helpers intentionally avoid writing raw session or user identifiers. Logs can
still be correlated through a stable short hash when investigating slow requests.
"""

import hashlib
import time
from typing import Any, Optional


def monotonic() -> float:
    return time.perf_counter()


def elapsed(start: Any, end: Optional[float] = None) -> Optional[float]:
    if start is None:
        return None
    try:
        start_value = float(start)
    except (TypeError, ValueError):
        return None
    end_value = monotonic() if end is None else end
    return max(0.0, end_value - start_value)


def format_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return "n/a"
    return f"{seconds:.2f}s"


def hash_id(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "none"
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:12]
