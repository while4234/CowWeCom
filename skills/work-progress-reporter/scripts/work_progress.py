#!/usr/bin/env python3
"""个人工作进度与周报状态助手。"""

from __future__ import annotations

import argparse
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_TIMEZONE = "Asia/Shanghai"
STATE_VERSION = 1
DAILY_TASK_KEY = "daily_checkin"
FRIDAY_TASK_KEY = "friday_report"
MONDAY_TASK_KEY = "monday_plan_fallback"
WEEKEND_TASK_PREFIX = "weekend_overtime_"
PRIVACY_NOTICE = "为保护个人工作进度隐私，请私聊我启用或汇报工作进度。"


class WorkProgressError(ValueError):
    """用户输入或状态不满足约束。"""


@dataclass(frozen=True)
class UserScope:
    workspace: Path
    memory_user_id: str

    @property
    def user_dir(self) -> Path:
        return self.workspace / "memory" / "users" / self.memory_user_id / "work-progress"

    @property
    def state_path(self) -> Path:
        return self.user_dir / "state.json"

    @property
    def reports_dir(self) -> Path:
        return self.user_dir / "reports"


def today_local(value: Optional[str] = None) -> date:
    if value:
        return date.fromisoformat(value)
    return date.today()


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def privacy_notice(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "ok": True,
        "private_only": True,
        "message": PRIVACY_NOTICE,
    }


def week_key(day: date) -> str:
    iso_year, iso_week, _ = day.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def monday_of_week(day: date) -> date:
    return day - timedelta(days=day.isoweekday() - 1)


def week_bounds(day: date) -> Tuple[date, date]:
    monday = monday_of_week(day)
    return monday, monday + timedelta(days=6)


def next_monday_after(day: date) -> date:
    return monday_of_week(day) + timedelta(days=7)


def parse_week_offset_day(base_day: date, week_offset: int = 0) -> date:
    return monday_of_week(base_day) + timedelta(days=7 * week_offset)


def require_memory_user_id(memory_user_id: str) -> str:
    value = (memory_user_id or "").strip()
    if not value:
        raise WorkProgressError("缺少 memory_user_id，无法定位当前用户的私有进度目录。")
    if value in {".", ".."}:
        raise WorkProgressError("memory_user_id 不合法。")
    if any(sep in value for sep in ("/", "\\")):
        raise WorkProgressError("memory_user_id 不能包含路径分隔符。")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
        raise WorkProgressError("memory_user_id 只能包含字母、数字、下划线、点、冒号和短横线。")
    return value


def resolve_scope(workspace: str, memory_user_id: str) -> UserScope:
    root = Path(workspace).expanduser().resolve()
    safe_id = require_memory_user_id(memory_user_id)
    scope = UserScope(root, safe_id)
    user_dir = scope.user_dir.resolve()
    allowed_root = (root / "memory" / "users" / safe_id).resolve()
    if user_dir != allowed_root / "work-progress":
        raise WorkProgressError("工作进度目录解析异常，已拒绝访问。")
    return scope


def default_state(actor_id: str = "", memory_user_id: str = "", timezone: str = DEFAULT_TIMEZONE) -> Dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "profile": {
            "actor_id": actor_id or "",
            "memory_user_id": memory_user_id or "",
            "timezone": timezone or DEFAULT_TIMEZONE,
        },
        "weeks": {},
        "scheduler": {},
        "pending_prompt": None,
        "updated_at": now_iso(),
    }


def load_state(scope: UserScope, actor_id: str = "", timezone: str = DEFAULT_TIMEZONE) -> Dict[str, Any]:
    if not scope.state_path.exists():
        return default_state(actor_id=actor_id, memory_user_id=scope.memory_user_id, timezone=timezone)
    with scope.state_path.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    if not isinstance(state, dict):
        raise WorkProgressError("状态文件格式错误。")
    state.setdefault("version", STATE_VERSION)
    state.setdefault("weeks", {})
    state.setdefault("scheduler", {})
    state.setdefault("pending_prompt", None)
    profile = state.setdefault("profile", {})
    profile["memory_user_id"] = scope.memory_user_id
    if actor_id:
        profile["actor_id"] = actor_id
    profile.setdefault("timezone", timezone or DEFAULT_TIMEZONE)
    return state


def save_state(scope: UserScope, state: Dict[str, Any]) -> None:
    scope.user_dir.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_iso()
    tmp_path = scope.state_path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    tmp_path.replace(scope.state_path)


def ensure_week(state: Dict[str, Any], day: date) -> Dict[str, Any]:
    key = week_key(day)
    monday, sunday = week_bounds(day)
    weeks = state.setdefault("weeks", {})
    week = weeks.setdefault(
        key,
        {
            "week_key": key,
            "start_date": monday.isoformat(),
            "end_date": sunday.isoformat(),
            "tasks": [],
            "checkins": [],
            "weekend_overtime_days": [],
            "next_week_plan_collected": False,
            "next_week_plan_text": "",
            "reports": [],
            "created_at": now_iso(),
            "updated_at": now_iso(),
        },
    )
    week.setdefault("tasks", [])
    week.setdefault("checkins", [])
    week.setdefault("weekend_overtime_days", [])
    week.setdefault("next_week_plan_collected", False)
    week.setdefault("next_week_plan_text", "")
    week.setdefault("reports", [])
    return week


def parse_json_list(raw: str, field_name: str) -> List[Dict[str, Any]]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkProgressError(f"{field_name} 不是合法 JSON。") from exc
    if not isinstance(value, list):
        raise WorkProgressError(f"{field_name} 必须是数组。")
    result = []
    for item in value:
        if isinstance(item, str):
            result.append({"title": item})
        elif isinstance(item, dict):
            result.append(dict(item))
        else:
            raise WorkProgressError(f"{field_name} 中只能包含字符串或对象。")
    return result


def normalize_percent(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        stripped = value.strip().rstrip("%")
        if not stripped:
            return default
        value = stripped
    try:
        percent = int(round(float(value)))
    except (TypeError, ValueError) as exc:
        raise WorkProgressError(f"进度百分比不合法：{value}") from exc
    if percent < 0 or percent > 100:
        raise WorkProgressError("进度百分比必须在 0 到 100 之间。")
    return percent


def task_slug(title: str) -> str:
    normalized = re.sub(r"\s+", "_", title.strip().lower())
    normalized = re.sub(r"[^a-z0-9_\-\u4e00-\u9fff]+", "", normalized)
    return normalized[:24] or "task"


def next_task_id(tasks: Iterable[Dict[str, Any]], title: str) -> str:
    prefix = task_slug(title)
    existing = {str(task.get("id", "")) for task in tasks}
    if prefix not in existing:
        return prefix
    index = 2
    while f"{prefix}_{index}" in existing:
        index += 1
    return f"{prefix}_{index}"


def normalize_task(raw: Dict[str, Any], existing_tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    title = str(raw.get("title") or raw.get("name") or "").strip()
    if not title:
        raise WorkProgressError("任务标题不能为空。")
    current = normalize_percent(raw.get("current_percent"), 0)
    target = normalize_percent(raw.get("target_percent"), current)
    task = {
        "id": str(raw.get("id") or next_task_id(existing_tasks, title)),
        "title": title,
        "target_percent": target,
        "current_percent": current,
        "status": str(raw.get("status") or "进行中"),
        "notes": str(raw.get("notes") or "").strip(),
        "created_at": raw.get("created_at") or now_iso(),
        "updated_at": now_iso(),
    }
    return task


def upsert_task(tasks: List[Dict[str, Any]], raw_task: Dict[str, Any]) -> Dict[str, Any]:
    title = str(raw_task.get("title") or raw_task.get("name") or "").strip()
    if not title:
        raise WorkProgressError("任务标题不能为空。")
    raw_id = str(raw_task.get("id") or "").strip()
    for task in tasks:
        if raw_id and task.get("id") == raw_id:
            update_existing_task(task, raw_task)
            return task
        if task.get("title") == title:
            update_existing_task(task, raw_task)
            return task
    task = normalize_task(raw_task, tasks)
    tasks.append(task)
    return task


def update_existing_task(task: Dict[str, Any], raw_task: Dict[str, Any]) -> None:
    if raw_task.get("title") or raw_task.get("name"):
        task["title"] = str(raw_task.get("title") or raw_task.get("name")).strip()
    if "target_percent" in raw_task:
        task["target_percent"] = normalize_percent(raw_task.get("target_percent"), task.get("target_percent"))
    if "current_percent" in raw_task:
        task["current_percent"] = normalize_percent(raw_task.get("current_percent"), task.get("current_percent"))
    if raw_task.get("status"):
        task["status"] = str(raw_task.get("status")).strip()
    if raw_task.get("notes"):
        note = str(raw_task.get("notes")).strip()
        task["notes"] = merge_text(task.get("notes", ""), note)
    task["updated_at"] = now_iso()


def merge_text(existing: str, addition: str) -> str:
    existing = str(existing or "").strip()
    addition = str(addition or "").strip()
    if not addition:
        return existing
    if not existing:
        return addition
    if addition in existing:
        return existing
    return f"{existing}\n{addition}"


def normalize_weekend_days(raw: str) -> List[str]:
    if not raw:
        return []
    mapping = {
        "saturday": "saturday",
        "sat": "saturday",
        "周六": "saturday",
        "星期六": "saturday",
        "sunday": "sunday",
        "sun": "sunday",
        "周日": "sunday",
        "周天": "sunday",
        "星期日": "sunday",
        "星期天": "sunday",
    }
    result: List[str] = []
    for item in re.split(r"[,，\s]+", raw.strip()):
        if not item:
            continue
        day = mapping.get(item.lower(), mapping.get(item))
        if not day:
            raise WorkProgressError(f"无法识别的周末加班日期：{item}")
        if day not in result:
            result.append(day)
    return result


def set_pending_prompt(state: Dict[str, Any], prompt_type: str, target_date: date) -> None:
    state["pending_prompt"] = {
        "type": prompt_type,
        "target_date": target_date.isoformat(),
        "created_at": now_iso(),
    }


def init_user(args: argparse.Namespace) -> Dict[str, Any]:
    scope = resolve_scope(args.workspace, args.memory_user_id)
    state = load_state(scope, actor_id=args.actor_id, timezone=args.timezone)
    current_week = ensure_week(state, today_local(args.date))
    save_state(scope, state)
    return {
        "ok": True,
        "message": "已初始化当前用户的工作进度空间。",
        "state_path": str(scope.state_path),
        "has_current_week_plan": bool(current_week.get("tasks")),
    }


def set_week_plan(args: argparse.Namespace) -> Dict[str, Any]:
    scope = resolve_scope(args.workspace, args.memory_user_id)
    state = load_state(scope, actor_id=args.actor_id, timezone=args.timezone)
    base_day = parse_week_offset_day(today_local(args.date), args.week_offset)
    week = ensure_week(state, base_day)
    tasks = parse_json_list(args.tasks_json, "tasks-json")
    if not tasks:
        raise WorkProgressError("请至少提供一个本周任务。")
    week["tasks"] = []
    for raw_task in tasks:
        week["tasks"].append(normalize_task(raw_task, week["tasks"]))
    week["weekend_overtime_days"] = normalize_weekend_days(args.weekend_days)
    week["plan_text"] = args.plan_text or ""
    week["updated_at"] = now_iso()
    if args.week_offset == 1:
        current_week = ensure_week(state, today_local(args.date))
        current_week["next_week_plan_collected"] = True
        current_week["next_week_plan_text"] = args.plan_text or summarize_tasks(week["tasks"])
        current_week["updated_at"] = now_iso()
    set_pending_prompt(state, "daily_checkin", today_local(args.date))
    save_state(scope, state)
    return {
        "ok": True,
        "message": "已记录工作计划。",
        "week_key": week["week_key"],
        "tasks": deepcopy(week["tasks"]),
        "weekend_overtime_days": week["weekend_overtime_days"],
        "risk_hints": risk_hints(week),
    }


def record_checkin(args: argparse.Namespace) -> Dict[str, Any]:
    scope = resolve_scope(args.workspace, args.memory_user_id)
    state = load_state(scope, actor_id=args.actor_id, timezone=args.timezone)
    checkin_day = today_local(args.date)
    if args.next_week:
        target_week_day = next_monday_after(checkin_day)
    else:
        target_week_day = checkin_day
    week = ensure_week(state, target_week_day)
    task_updates = parse_json_list(args.task_updates_json, "task-updates-json")
    new_tasks = parse_json_list(args.new_tasks_json, "new-tasks-json")
    for update in task_updates:
        upsert_task(week["tasks"], update)
    added_tasks = []
    for raw_task in new_tasks:
        added_tasks.append(deepcopy(upsert_task(week["tasks"], raw_task)))
    checkin = {
        "date": checkin_day.isoformat(),
        "kind": args.kind,
        "progress_text": args.progress_text or "",
        "learnings": args.learnings or "",
        "blockers": args.blockers or "",
        "new_tasks": added_tasks,
        "recorded_at": now_iso(),
    }
    week["checkins"].append(checkin)
    week["updated_at"] = now_iso()
    state["pending_prompt"] = None
    save_state(scope, state)
    return {
        "ok": True,
        "message": "已记录本次工作进度。",
        "week_key": week["week_key"],
        "checkin": checkin,
        "tasks": deepcopy(week["tasks"]),
        "risk_hints": risk_hints(week),
    }


def add_task(args: argparse.Namespace) -> Dict[str, Any]:
    scope = resolve_scope(args.workspace, args.memory_user_id)
    state = load_state(scope, actor_id=args.actor_id, timezone=args.timezone)
    week = ensure_week(state, today_local(args.date))
    raw_task = {
        "title": args.title,
        "current_percent": args.current_percent,
        "target_percent": args.target_percent,
        "notes": args.notes,
    }
    task = upsert_task(week["tasks"], raw_task)
    week["updated_at"] = now_iso()
    save_state(scope, state)
    return {
        "ok": True,
        "message": "已新增或更新任务。",
        "week_key": week["week_key"],
        "task": deepcopy(task),
        "risk_hints": risk_hints(week),
    }


def get_status(args: argparse.Namespace) -> Dict[str, Any]:
    scope = resolve_scope(args.workspace, args.memory_user_id)
    state = load_state(scope, actor_id=args.actor_id, timezone=args.timezone)
    week = ensure_week(state, today_local(args.date))
    return {
        "ok": True,
        "week_key": week["week_key"],
        "tasks": deepcopy(week["tasks"]),
        "checkin_count": len(week["checkins"]),
        "weekend_overtime_days": week.get("weekend_overtime_days", []),
        "risk_hints": risk_hints(week),
        "pending_prompt": state.get("pending_prompt"),
    }


def generate_report(args: argparse.Namespace) -> Dict[str, Any]:
    scope = resolve_scope(args.workspace, args.memory_user_id)
    state = load_state(scope, actor_id=args.actor_id, timezone=args.timezone)
    report_day = today_local(args.date)
    week = ensure_week(state, report_day)
    markdown = render_report(week)
    scope.reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = scope.reports_dir / f"{week['week_key']}.md"
    report_path.write_text(markdown, encoding="utf-8")
    record = {
        "week_key": week["week_key"],
        "path": str(report_path),
        "generated_at": now_iso(),
    }
    week["reports"].append(record)
    week["updated_at"] = now_iso()
    save_state(scope, state)
    return {
        "ok": True,
        "message": "已生成本周周报。",
        "week_key": week["week_key"],
        "report_path": str(report_path),
        "report_markdown": markdown,
        "risk_hints": risk_hints(week),
    }


def save_scheduler_task(args: argparse.Namespace) -> Dict[str, Any]:
    scope = resolve_scope(args.workspace, args.memory_user_id)
    state = load_state(scope, actor_id=args.actor_id, timezone=args.timezone)
    scheduler = state.setdefault("scheduler", {})
    scheduler[args.task_key] = {
        "task_id": args.task_id,
        "name": args.name or args.task_key,
        "run_at": args.run_at or "",
        "updated_at": now_iso(),
    }
    save_state(scope, state)
    return {"ok": True, "message": "已保存提醒任务编号。", "scheduler": deepcopy(scheduler)}


def remove_scheduler_task(args: argparse.Namespace) -> Dict[str, Any]:
    scope = resolve_scope(args.workspace, args.memory_user_id)
    state = load_state(scope, actor_id=args.actor_id, timezone=args.timezone)
    scheduler = state.setdefault("scheduler", {})
    removed = scheduler.pop(args.task_key, None)
    save_state(scope, state)
    return {
        "ok": True,
        "message": "已移除本地提醒任务记录。" if removed else "本地没有这个提醒任务记录。",
        "removed": removed,
        "scheduler": deepcopy(scheduler),
    }


def schedule_plan(args: argparse.Namespace) -> Dict[str, Any]:
    scope = resolve_scope(args.workspace, args.memory_user_id)
    state = load_state(scope, actor_id=args.actor_id, timezone=args.timezone)
    plan_day = today_local(args.date)
    week = ensure_week(state, plan_day)
    scheduler = state.setdefault("scheduler", {})
    actions = [
        recurring_action(
            scheduler,
            DAILY_TASK_KEY,
            "工作进度日报提醒",
            "0 10 * * 2-5",
            daily_ai_task(),
        ),
        recurring_action(
            scheduler,
            FRIDAY_TASK_KEY,
            "工作进度周报提醒",
            "0 16 * * 5",
            friday_ai_task(),
        ),
    ]
    if not week.get("tasks"):
        monday = monday_of_week(plan_day)
        fallback_day = monday if plan_day <= monday else next_monday_after(plan_day)
        actions.extend(once_actions(scheduler, MONDAY_TASK_KEY, "工作计划兜底提醒", fallback_day, time(10, 0), monday_ai_task()))
    elif scheduler.get(MONDAY_TASK_KEY):
        actions.append(delete_action(MONDAY_TASK_KEY, scheduler[MONDAY_TASK_KEY]))

    for day_name in ("saturday", "sunday"):
        task_key = f"{WEEKEND_TASK_PREFIX}{day_name}"
        if day_name in week.get("weekend_overtime_days", []):
            offset = 5 if day_name == "saturday" else 6
            run_day = monday_of_week(plan_day) + timedelta(days=offset)
            if run_day >= plan_day:
                actions.extend(once_actions(scheduler, task_key, "周末加班进度提醒", run_day, time(17, 0), weekend_ai_task()))
        elif scheduler.get(task_key):
            actions.append(delete_action(task_key, scheduler[task_key]))

    save_state(scope, state)
    return {
        "ok": True,
        "message": "已生成调度计划，请按 scheduler_actions 调用 scheduler 工具。",
        "week_key": week["week_key"],
        "scheduler_actions": actions,
    }


def recurring_action(scheduler: Dict[str, Any], key: str, name: str, cron: str, ai_task: str) -> Dict[str, Any]:
    existing = scheduler.get(key)
    if existing:
        return {"op": "keep", "task_key": key, "task_id": existing.get("task_id"), "name": name}
    return {
        "op": "create",
        "task_key": key,
        "name": name,
        "schedule_type": "cron",
        "schedule_value": cron,
        "ai_task": ai_task,
    }


def once_actions(
    scheduler: Dict[str, Any],
    key: str,
    name: str,
    run_day: date,
    run_time: time,
    ai_task: str,
) -> List[Dict[str, Any]]:
    existing = scheduler.get(key)
    run_at = datetime.combine(run_day, run_time).isoformat()
    if existing and existing.get("run_at") == run_at:
        return [{"op": "keep", "task_key": key, "task_id": existing.get("task_id"), "name": name, "run_at": run_at}]
    create = {
        "op": "create",
        "task_key": key,
        "name": name,
        "schedule_type": "once",
        "schedule_value": run_at,
        "run_at": run_at,
        "ai_task": ai_task,
    }
    if existing:
        return [delete_action(key, existing), create]
    return [create]


def delete_action(key: str, existing: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "op": "delete",
        "task_key": key,
        "task_id": existing.get("task_id"),
        "name": existing.get("name", key),
    }


def daily_ai_task() -> str:
    return (
        "请使用 work-progress-reporter skill。当前是工作进度日报提醒。"
        "请只面向当前私聊用户，询问TA昨天的工作进度如何、当前进度百分比是否有变化、"
        "有什么收获或阻塞，并显式询问是否有新增任务。不要记录任何信息，等用户回复后再调用脚本保存。"
    )


def friday_ai_task() -> str:
    return (
        "请使用 work-progress-reporter skill。当前是周五周报提醒。"
        "请询问TA今天的工作进度、当前进度百分比、收获、阻塞和新增任务；"
        "用户回复后调用脚本记录，再生成本周中文 Markdown 周报，并询问下周计划和目标进度。"
    )


def monday_ai_task() -> str:
    return (
        "请使用 work-progress-reporter skill。当前是周一计划兜底提醒。"
        "若当前用户本周还没有计划，请询问本周任务、当前进度、本周目标进度和周末是否加班；"
        "若已有计划，只做简短确认，不重复打扰。"
    )


def weekend_ai_task() -> str:
    return (
        "请使用 work-progress-reporter skill。当前是周末加班进度提醒。"
        "仅当用户此前确认今天加班时，询问今天的工作内容、进度变化、收获或阻塞；记录时把内容归入下一周。"
    )


def summarize_tasks(tasks: List[Dict[str, Any]]) -> str:
    return "；".join(str(task.get("title", "")).strip() for task in tasks if task.get("title"))


def risk_hints(week: Dict[str, Any]) -> List[str]:
    hints = []
    for task in week.get("tasks", []):
        current = task.get("current_percent")
        target = task.get("target_percent")
        title = task.get("title", "未命名任务")
        if current is None or target is None:
            hints.append(f"{title} 缺少百分比，建议补充当前进度和目标进度。")
        elif int(current) < int(target):
            hints.append(f"{title} 当前 {current}% ，低于本周目标 {target}% 。")
    return hints


def render_report(week: Dict[str, Any]) -> str:
    title = f"# {week['week_key']} 工作周报"
    lines = [
        title,
        "",
        f"周期：{week.get('start_date')} 至 {week.get('end_date')}",
        "",
        "## 本周工作内容",
    ]
    tasks = week.get("tasks", [])
    if tasks:
        for task in tasks:
            lines.append(f"- {task.get('title', '未命名任务')}")
    else:
        lines.append("- 本周尚未记录任务。")

    lines.extend(["", "## 进度情况"])
    if tasks:
        for task in tasks:
            current = task.get("current_percent")
            target = task.get("target_percent")
            status = task.get("status") or "进行中"
            percent_text = "未记录百分比" if current is None else f"当前 {current}%"
            target_text = "未设置目标" if target is None else f"目标 {target}%"
            lines.append(f"- {task.get('title', '未命名任务')}：{percent_text}，{target_text}，状态：{status}")
    else:
        lines.append("- 暂无进度记录。")

    temporary_tasks = collect_new_tasks(week)
    lines.extend(["", "## 新增/临时任务"])
    if temporary_tasks:
        for task in temporary_tasks:
            lines.append(f"- {task.get('title', '未命名任务')}")
    else:
        lines.append("- 本周未记录新增任务。")

    lines.extend(["", "## 风险与未达项"])
    hints = risk_hints(week)
    if hints:
        lines.extend(f"- {hint}" for hint in hints)
    else:
        lines.append("- 暂未发现低于目标的任务。")

    learnings = collect_texts(week, "learnings")
    lines.extend(["", "## 本周收获"])
    if learnings:
        lines.extend(f"- {item}" for item in learnings)
    else:
        lines.append("- 本周尚未记录明确收获。")

    blockers = collect_texts(week, "blockers")
    if blockers:
        lines.extend(["", "## 阻塞与支持需求"])
        lines.extend(f"- {item}" for item in blockers)

    lines.extend(["", "## 下周计划与目标进度"])
    next_plan = str(week.get("next_week_plan_text") or "").strip()
    if next_plan:
        lines.append(next_plan)
    else:
        lines.append("请补充下周计划、各任务目标进度，以及下周末是否预计加班。")
    return "\n".join(lines).strip() + "\n"


def collect_new_tasks(week: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    seen = set()
    for checkin in week.get("checkins", []):
        for task in checkin.get("new_tasks", []):
            key = task.get("id") or task.get("title")
            if key in seen:
                continue
            seen.add(key)
            result.append(task)
    return result


def collect_texts(week: Dict[str, Any], field_name: str) -> List[str]:
    result = []
    seen = set()
    for checkin in week.get("checkins", []):
        raw = str(checkin.get(field_name) or "").strip()
        if not raw:
            continue
        for item in re.split(r"[\n；;]+", raw):
            text = item.strip()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="个人工作进度与周报状态助手")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--workspace", required=True, help="CowAgent agent_workspace")
        subparser.add_argument("--memory-user-id", required=True, help="当前用户 memory_user_id")
        subparser.add_argument("--actor-id", default="", help="当前用户 actor_id")
        subparser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="用户时区")
        subparser.add_argument("--date", default="", help="用于测试或补录的日期 YYYY-MM-DD")

    privacy_parser = subparsers.add_parser("privacy-notice")
    privacy_parser.set_defaults(func=privacy_notice)

    init_parser = subparsers.add_parser("init-user")
    add_common(init_parser)
    init_parser.set_defaults(func=init_user)

    plan_parser = subparsers.add_parser("set-week-plan")
    add_common(plan_parser)
    plan_parser.add_argument("--tasks-json", required=True)
    plan_parser.add_argument("--weekend-days", default="")
    plan_parser.add_argument("--plan-text", default="")
    plan_parser.add_argument("--week-offset", type=int, default=0)
    plan_parser.set_defaults(func=set_week_plan)

    checkin_parser = subparsers.add_parser("record-checkin")
    add_common(checkin_parser)
    checkin_parser.add_argument("--kind", default="daily")
    checkin_parser.add_argument("--progress-text", default="")
    checkin_parser.add_argument("--learnings", default="")
    checkin_parser.add_argument("--blockers", default="")
    checkin_parser.add_argument("--task-updates-json", default="[]")
    checkin_parser.add_argument("--new-tasks-json", default="[]")
    checkin_parser.add_argument("--next-week", action="store_true", help="将本次记录归入下一周")
    checkin_parser.set_defaults(func=record_checkin)

    add_task_parser = subparsers.add_parser("add-task")
    add_common(add_task_parser)
    add_task_parser.add_argument("--title", required=True)
    add_task_parser.add_argument("--current-percent", type=int, default=0)
    add_task_parser.add_argument("--target-percent", type=int, default=0)
    add_task_parser.add_argument("--notes", default="")
    add_task_parser.set_defaults(func=add_task)

    report_parser = subparsers.add_parser("generate-report")
    add_common(report_parser)
    report_parser.set_defaults(func=generate_report)

    status_parser = subparsers.add_parser("get-status")
    add_common(status_parser)
    status_parser.set_defaults(func=get_status)

    schedule_parser = subparsers.add_parser("schedule-plan")
    add_common(schedule_parser)
    schedule_parser.set_defaults(func=schedule_plan)

    scheduler_parser = subparsers.add_parser("save-scheduler-task")
    add_common(scheduler_parser)
    scheduler_parser.add_argument("--task-key", required=True)
    scheduler_parser.add_argument("--task-id", required=True)
    scheduler_parser.add_argument("--name", default="")
    scheduler_parser.add_argument("--run-at", default="")
    scheduler_parser.set_defaults(func=save_scheduler_task)

    remove_scheduler_parser = subparsers.add_parser("remove-scheduler-task")
    add_common(remove_scheduler_parser)
    remove_scheduler_parser.add_argument("--task-key", required=True)
    remove_scheduler_parser.set_defaults(func=remove_scheduler_task)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except WorkProgressError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
