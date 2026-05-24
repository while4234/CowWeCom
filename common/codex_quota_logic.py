# encoding:utf-8

"""Pure helpers for Codex quota snapshots and auto-switch decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional


SECONDS_PER_DAY = 24 * 60 * 60


@dataclass(frozen=True)
class CodexQuotaWindow:
    limit_id: str
    limit_name: str
    period_name: str
    window_minutes: int
    used_percent: float
    remaining_percent: float
    resets_at: Optional[datetime]
    reached_type: str = ""


@dataclass(frozen=True)
class CodexQuotaDecision:
    should_switch: bool
    reason: str
    window: Optional[CodexQuotaWindow] = None
    allowed_used_percent: float = 0.0
    completed_days: int = 0


def as_record(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def number_or_none(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


def timestamp_to_datetime(value: Any) -> Optional[datetime]:
    numeric = number_or_none(value)
    if numeric is None or numeric <= 0:
        return None
    if numeric > 1_000_000_000_000:
        numeric = numeric / 1000.0
    return datetime.fromtimestamp(numeric)


def collect_snapshots(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    root = as_record(payload)
    by_limit = as_record(root.get("rateLimitsByLimitId"))
    snapshots = [as_record(item) for item in by_limit.values() if as_record(item)]
    if not snapshots:
        single = as_record(root.get("rateLimits"))
        if single:
            snapshots = [single]
    return sorted(snapshots, key=_snapshot_sort_key)


def _snapshot_sort_key(snapshot: Mapping[str, Any]) -> tuple:
    limit_id = str(snapshot.get("limitId") or snapshot.get("limit_id") or "")
    label = str(snapshot.get("limitName") or snapshot.get("limit_name") or limit_id)
    return (0 if limit_id == "codex" else 1, label)


def select_codex_quota_window(payload: Mapping[str, Any]) -> Optional[CodexQuotaWindow]:
    for snapshot in collect_snapshots(payload):
        limit_id = str(snapshot.get("limitId") or snapshot.get("limit_id") or "")
        if limit_id and limit_id != "codex":
            continue
        window = _window_from_snapshot(snapshot, "secondary")
        primary = _window_from_snapshot(snapshot, "primary")
        candidates = [item for item in (window, primary) if item is not None]
        if not candidates:
            continue
        weekly = [item for item in candidates if item.window_minutes >= 10080]
        return max(weekly or candidates, key=lambda item: item.window_minutes)
    return None


def _window_from_snapshot(snapshot: Mapping[str, Any], period_name: str) -> Optional[CodexQuotaWindow]:
    period = as_record(snapshot.get(period_name))
    if not period:
        return None
    window_minutes = number_or_none(period.get("windowDurationMins"))
    used_percent = number_or_none(period.get("usedPercent"))
    if window_minutes is None or used_percent is None:
        return None
    limit_id = str(snapshot.get("limitId") or snapshot.get("limit_id") or "codex")
    limit_name = str(snapshot.get("limitName") or snapshot.get("limit_name") or limit_id or "Codex")
    reached_type = str(snapshot.get("rateLimitReachedType") or snapshot.get("rate_limit_reached_type") or "")
    return CodexQuotaWindow(
        limit_id=limit_id,
        limit_name=limit_name,
        period_name=period_name,
        window_minutes=int(window_minutes),
        used_percent=float(used_percent),
        remaining_percent=max(0.0, 100.0 - float(used_percent)),
        resets_at=timestamp_to_datetime(period.get("resetsAt")),
        reached_type=reached_type,
    )


def decide_codex_auto_switch(
    payload: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    fair_share_days: int = 7,
    min_remaining_percent: float = 15.0,
) -> CodexQuotaDecision:
    window = select_codex_quota_window(payload)
    if window is None:
        return CodexQuotaDecision(False, "quota_window_missing")
    if window.reached_type:
        return CodexQuotaDecision(False, "rate_limit_reached", window=window)
    if window.remaining_percent < float(min_remaining_percent):
        return CodexQuotaDecision(False, "remaining_below_minimum", window=window)

    now = now or datetime.now()
    completed_days = _completed_quota_days(window, now=now)
    allowed = min(100.0, completed_days * (100.0 / max(1, int(fair_share_days or 7))))
    if window.used_percent <= allowed:
        return CodexQuotaDecision(
            True,
            "under_fair_share",
            window=window,
            allowed_used_percent=allowed,
            completed_days=completed_days,
        )
    return CodexQuotaDecision(
        False,
        "used_above_fair_share",
        window=window,
        allowed_used_percent=allowed,
        completed_days=completed_days,
    )


def _completed_quota_days(window: CodexQuotaWindow, *, now: datetime) -> int:
    if window.resets_at is None:
        return 1
    window_start = window.resets_at - timedelta(minutes=window.window_minutes)
    elapsed = max(0.0, (now - window_start).total_seconds())
    completed = int(elapsed // SECONDS_PER_DAY)
    return max(1, min(max(1, int(round(window.window_minutes / 1440.0))), completed))


def window_to_dict(window: Optional[CodexQuotaWindow]) -> Dict[str, Any]:
    if window is None:
        return {}
    return {
        "limit_id": window.limit_id,
        "limit_name": window.limit_name,
        "period_name": window.period_name,
        "window_minutes": window.window_minutes,
        "used_percent": window.used_percent,
        "remaining_percent": window.remaining_percent,
        "resets_at": window.resets_at.isoformat() if window.resets_at else "",
        "reached_type": window.reached_type,
    }


def decision_to_dict(decision: CodexQuotaDecision) -> Dict[str, Any]:
    return {
        "should_switch": decision.should_switch,
        "reason": decision.reason,
        "allowed_used_percent": decision.allowed_used_percent,
        "completed_days": decision.completed_days,
        "window": window_to_dict(decision.window),
    }
