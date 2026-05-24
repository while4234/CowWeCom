# encoding:utf-8

"""App-level daily Deep Dream scheduler.

The scheduler runs independently from lazy Agent initialization.  It processes
completed calendar days only, so a midnight run summarizes yesterday instead
of today's still-changing memory file.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from common.log import logger
from common.utils import expand_path
from config import conf


DreamRunner = Callable[[Path, date, Optional[str], bool], bool]

_STARTED = False
_LOCK = threading.Lock()


def start_daily_memory_dream_scheduler() -> None:
    """Start the singleton app-level scheduler thread."""
    global _STARTED
    cfg = _config()
    if not cfg.get("enabled", True):
        logger.info("[DailyDream] Scheduler disabled by config")
        return
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    thread = threading.Thread(
        target=_run_loop,
        name="daily-memory-dream-scheduler",
        daemon=True,
    )
    thread.start()
    logger.info("[DailyDream] Scheduler started")


def run_due_memory_dream_jobs(
    *,
    now: Optional[datetime] = None,
    workspace: Optional[Path] = None,
    runner: Optional[DreamRunner] = None,
) -> List[Dict[str, Any]]:
    """Run startup catch-up jobs through yesterday, bounded by config."""
    now = now or datetime.now()
    cfg = _config()
    max_days = max(1, int(cfg.get("catch_up_days", 1) or 1))
    last_day = now.date() - timedelta(days=1)
    first_day = last_day - timedelta(days=max_days - 1)
    results = []
    for offset in range((last_day - first_day).days + 1):
        target = first_day + timedelta(days=offset)
        result = run_daily_memory_dream_once(
            target_date=target,
            now=now,
            workspace=workspace,
            runner=runner,
            reason="startup_catch_up",
        )
        results.append(result)
    return results


def run_daily_memory_dream_once(
    *,
    target_date: Optional[Any] = None,
    now: Optional[datetime] = None,
    workspace: Optional[Path] = None,
    runner: Optional[DreamRunner] = None,
    force: bool = False,
    reason: str = "scheduled",
) -> Dict[str, Any]:
    """Run Deep Dream for one completed day and persist idempotency state."""
    now = now or datetime.now()
    target = _coerce_date(target_date) if target_date is not None else now.date() - timedelta(days=1)
    workspace = Path(workspace) if workspace is not None else _workspace()
    state = _read_state(workspace)

    if not force:
        completed_all, skipped_scopes = _completed_scopes_for_day(workspace, state, target)
        if completed_all:
            return {
                "decision": "skipped",
                "reason": "already_completed",
                "target_date": target.isoformat(),
                "scopes": skipped_scopes,
            }

    if _config().get("flush_active_agents", True):
        _flush_active_agents(target)

    scopes = _discover_scopes(workspace, target)
    if not scopes:
        _record_attempt(
            workspace,
            state,
            target,
            [],
            decision="skipped",
            reason="no_daily_memory",
            now=now,
        )
        return {
            "decision": "skipped",
            "reason": "no_daily_memory",
            "target_date": target.isoformat(),
            "scopes": [],
        }

    runner = runner or _run_deep_dream_for_scope
    completed = []
    failed = []
    skipped = []
    completed_by_scope = _state_completed_by_scope(state)
    for scope_key, user_id in scopes:
        if not force and completed_by_scope.get(scope_key) == target.isoformat():
            skipped.append(scope_key)
            continue
        try:
            ok = bool(runner(workspace, target, user_id, force))
        except Exception as e:
            ok = False
            logger.warning(f"[DailyDream] Scope {scope_key} failed for {target}: {e}")
        if ok:
            completed.append(scope_key)
        else:
            failed.append(scope_key)

    decision = "success" if completed and not failed else "partial" if completed else "failed"
    if skipped and not completed and not failed:
        decision = "skipped"
    _record_attempt(
        workspace,
        state,
        target,
        completed,
        decision=decision,
        reason=reason,
        now=now,
    )
    return {
        "decision": decision,
        "reason": reason,
        "target_date": target.isoformat(),
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
    }


def _run_loop() -> None:
    cfg = _config()
    if cfg.get("catch_up_on_startup", True):
        try:
            results = run_due_memory_dream_jobs()
            logger.info("[DailyDream] Startup catch-up results: %s", results)
        except Exception as e:
            logger.warning(f"[DailyDream] Startup catch-up failed: {e}")

    while True:
        try:
            wait_seconds = _seconds_until_next_run()
            logger.info("[DailyDream] Next run in %.1fh", wait_seconds / 3600)
            time.sleep(wait_seconds)
            result = run_daily_memory_dream_once(reason="scheduled")
            logger.info("[DailyDream] Scheduled run result: %s", result)
        except Exception as e:
            logger.warning(f"[DailyDream] Scheduler loop error: {e}")
            time.sleep(60)


def _run_deep_dream_for_scope(
    workspace: Path,
    target: date,
    user_id: Optional[str],
    force: bool,
) -> bool:
    from agent.memory.summarizer import MemoryFlushManager
    from bridge.agent_bridge import AgentLLMModel
    from bridge.bridge import Bridge

    flush_mgr = MemoryFlushManager(workspace_dir=workspace)
    flush_mgr.llm_model = AgentLLMModel(Bridge())
    return flush_mgr.deep_dream(
        user_id=user_id,
        lookback_days=1,
        force=force,
        end_date=target,
        diary_date=target,
    )


def _flush_active_agents(target: date) -> None:
    try:
        from bridge.bridge import Bridge

        bridge = Bridge()
        agent_bridge = getattr(bridge, "_agent_bridge", None)
        if not agent_bridge:
            return
        agents = []
        if getattr(agent_bridge, "default_agent", None):
            agents.append(agent_bridge.default_agent)
        agents.extend(getattr(agent_bridge, "agents", {}).values())

        threads = []
        for agent in agents:
            memory_manager = getattr(agent, "memory_manager", None)
            if not memory_manager:
                continue
            profile = getattr(agent, "_actor_profile", None)
            user_id = None
            if profile is not None and not getattr(profile, "is_admin", False):
                user_id = getattr(profile, "memory_user_id", None)
            with agent.messages_lock:
                messages = list(agent.messages)
            if not messages:
                continue
            if memory_manager.flush_manager.create_daily_summary(
                messages,
                user_id=user_id,
                target_date=target,
            ):
                thread = memory_manager.flush_manager._last_flush_thread
                if thread:
                    threads.append(thread)
        for thread in threads:
            thread.join(timeout=60)
    except Exception as e:
        logger.warning(f"[DailyDream] Failed to flush active agents: {e}")


def _discover_scopes(workspace: Path, target: date) -> List[Tuple[str, Optional[str]]]:
    scopes: List[Tuple[str, Optional[str]]] = []
    if _has_daily_memory(workspace, target, None):
        scopes.append(("shared", None))

    if _config().get("include_user_memories", True):
        user_ids = set(_configured_memory_user_ids())
        users_dir = workspace / "memory" / "users"
        if users_dir.is_dir():
            for child in users_dir.iterdir():
                if child.is_dir():
                    user_ids.add(child.name)
        for user_id in sorted(user_ids):
            if _has_daily_memory(workspace, target, user_id):
                scopes.append((f"user:{user_id}", user_id))
    return scopes


def _has_daily_memory(workspace: Path, target: date, user_id: Optional[str]) -> bool:
    if user_id:
        path = workspace / "memory" / "users" / user_id / f"{target.isoformat()}.md"
    else:
        path = workspace / "memory" / f"{target.isoformat()}.md"
    if not path.is_file():
        return False
    try:
        content = path.read_text(encoding="utf-8").strip()
    except Exception:
        return False
    meaningful = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return bool(meaningful)


def _completed_scopes_for_day(
    workspace: Path,
    state: Dict[str, Any],
    target: date,
) -> Tuple[bool, List[str]]:
    scopes = [scope for scope, _ in _discover_scopes(workspace, target)]
    if not scopes:
        return False, []
    completed_by_scope = _state_completed_by_scope(state)
    target_str = target.isoformat()
    completed = [scope for scope in scopes if completed_by_scope.get(scope) == target_str]
    return len(completed) == len(scopes), completed


def _state_completed_by_scope(state: Dict[str, Any]) -> Dict[str, str]:
    raw = state.get("completed_by_scope")
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    legacy = state.get("last_completed_date")
    if legacy:
        return {"shared": str(legacy)}
    return {}


def _record_attempt(
    workspace: Path,
    state: Dict[str, Any],
    target: date,
    completed_scopes: Iterable[str],
    *,
    decision: str,
    reason: str,
    now: datetime,
) -> None:
    state = dict(state)
    target_str = target.isoformat()
    completed_by_scope = _state_completed_by_scope(state)
    for scope in completed_scopes:
        completed_by_scope[scope] = target_str
    state["completed_by_scope"] = completed_by_scope
    if completed_scopes:
        state["last_completed_date"] = target_str
    state["last_attempted_date"] = target_str
    state["last_attempted_at"] = now.isoformat()
    state["last_decision"] = decision
    state["last_reason"] = reason
    _write_state(workspace, state)


def _read_state(workspace: Path) -> Dict[str, Any]:
    path = _state_path(workspace)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[DailyDream] Failed to read state {path}: {e}")
        return {}


def _write_state(workspace: Path, state: Dict[str, Any]) -> None:
    path = _state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)


def _state_path(workspace: Path) -> Path:
    configured = str(_config().get("state_path") or "").strip()
    if configured:
        return Path(expand_path(configured))
    return workspace / "memory" / ".deep_dream_scheduler_state.json"


def _workspace() -> Path:
    return Path(expand_path(conf().get("agent_workspace", "~/cow")))


def _configured_memory_user_ids() -> Iterable[str]:
    profiles = conf().get("agent_user_profiles", {})
    if not isinstance(profiles, dict):
        return []
    user_ids = []
    for profile in profiles.values():
        if isinstance(profile, dict) and profile.get("memory_user_id"):
            user_ids.append(str(profile["memory_user_id"]))
    return user_ids


def _seconds_until_next_run(now: Optional[datetime] = None) -> float:
    now = now or datetime.now()
    hour, minute = _parse_check_time(str(_config().get("check_time") or "00:00"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def _parse_check_time(value: str) -> Tuple[int, int]:
    try:
        raw_hour, raw_minute = str(value or "00:00").split(":", 1)
        hour = max(0, min(23, int(raw_hour)))
        minute = max(0, min(59, int(raw_minute)))
        return hour, minute
    except Exception:
        return 0, 0


def _coerce_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _config() -> Dict[str, Any]:
    cfg = conf().get("memory_deep_dream", {})
    return cfg if isinstance(cfg, dict) else {}
