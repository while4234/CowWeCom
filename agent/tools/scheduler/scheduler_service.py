"""
Background scheduler service for executing scheduled tasks
"""

import re
import time
import threading
from datetime import datetime, timedelta
from typing import Callable, Optional
from croniter import croniter
from common.log import logger


class SchedulerService:
    """
    Background service that executes scheduled tasks
    """
    
    def __init__(
        self,
        task_store,
        execute_callback: Callable,
        notify_callback: Optional[Callable] = None,
        overdue_grace_seconds: int = 300,
    ):
        """
        Initialize scheduler service
        
        Args:
            task_store: TaskStore instance
            execute_callback: Function to call when executing a task
            notify_callback: Function to call when sending scheduler notices
            overdue_grace_seconds: How long a task may be late before it is
                treated as missed instead of executed automatically
        """
        self.task_store = task_store
        self.execute_callback = execute_callback
        self.notify_callback = notify_callback or execute_callback
        self.overdue_grace_seconds = max(0, int(overdue_grace_seconds))
        self.running = False
        self.thread = None
        self._lock = threading.Lock()
    
    def start(self):
        """Start the scheduler service"""
        with self._lock:
            if self.running:
                logger.warning("[Scheduler] Service already running")
                return
            
            self.running = True
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()
            logger.debug("[Scheduler] Service started")
    
    def stop(self):
        """Stop the scheduler service"""
        with self._lock:
            if not self.running:
                return
            
            self.running = False
            if self.thread:
                self.thread.join(timeout=5)
            logger.info("[Scheduler] Service stopped")
    
    def _run_loop(self):
        """Main scheduler loop"""
        logger.debug("[Scheduler] Scheduler loop started")
        
        while self.running:
            try:
                self._check_and_execute_tasks()
            except Exception as e:
                logger.error(f"[Scheduler] Error in scheduler loop: {e}")

            time.sleep(30)
    
    def _check_and_execute_tasks(self, now: Optional[datetime] = None):
        """Check for due tasks and execute them"""
        now = now or datetime.now()
        tasks = self.task_store.list_tasks(enabled_only=True)
        
        for task in tasks:
            try:
                # Check if task is due
                if self._is_task_due(task, now):
                    logger.info(f"[Scheduler] Executing task: {task['id']} - {task['name']}")
                    success = self._execute_task(task)
                    self._finalize_task_attempt(task, now, success)
            except Exception as e:
                logger.error(f"[Scheduler] Error processing task {task.get('id')}: {e}")
                self._handle_invalid_task(task, now, str(e))

    def run_task_now(self, task_id: str) -> bool:
        """Execute a stored task immediately and update it like a scheduled run."""
        task = self.task_store.get_task(task_id)
        if not task:
            raise ValueError(f"Task '{task_id}' not found")
        now = datetime.now()
        logger.info(f"[Scheduler] Manually executing task: {task['id']} - {task.get('name')}")
        success = self._execute_task(task)
        self._finalize_task_attempt(task, now, success)
        return success
    
    def _is_task_due(self, task: dict, now: datetime) -> bool:
        """
        Check if a task is due to run
        
        Args:
            task: Task dictionary
            now: Current datetime
            
        Returns:
            True if task should run now
        """
        next_run_str = task.get("next_run_at")
        if not next_run_str:
            # Calculate initial next_run_at
            next_run = self._calculate_next_run(task, now)
            if next_run:
                self.task_store.update_task(task['id'], {
                    "next_run_at": next_run.isoformat()
                })
                return False
            self._handle_invalid_task(
                task,
                now,
                "missing next_run_at and schedule cannot calculate a future run",
            )
            return False
        
        try:
            next_run = datetime.fromisoformat(next_run_str)
            
            # Check if task is overdue (e.g., service restart)
            if next_run < now:
                time_diff = (now - next_run).total_seconds()
                
                if time_diff > self.overdue_grace_seconds:
                    self._handle_missed_task(task, next_run, now, time_diff)
                    return False
            
            return now >= next_run
        except Exception as e:
            self._handle_invalid_task(task, now, f"invalid next_run_at: {e}")
            return False

    def _finalize_task_attempt(self, task: dict, now: datetime, success: bool) -> None:
        """Advance, remove, or keep a task after an execution attempt."""
        next_run = self._calculate_next_run(task, now)
        if next_run:
            updates = {
                "next_run_at": next_run.isoformat(),
                "last_attempt_at": now.isoformat(),
            }
            if success:
                updates.update({
                    "last_run_at": now.isoformat(),
                    "last_error": None,
                    "last_error_at": None,
                })
            self.task_store.update_task(task["id"], updates)
            return

        if success:
            self.task_store.delete_task(task["id"])
            logger.info(f"[Scheduler] One-time task completed and removed: {task['id']}")
            return

        self.task_store.update_task(task["id"], {
            "enabled": False,
            "last_attempt_at": now.isoformat(),
        })
        logger.warning(f"[Scheduler] One-time task failed and was disabled: {task['id']}")

    def _handle_invalid_task(self, task: dict, now: datetime, reason: str) -> None:
        """Disable and notify for tasks that cannot be safely scheduled."""
        task_id = task.get("id")
        if not task_id:
            return
        safe_reason = self._safe_notice_reason(reason)
        logger.error(f"[Scheduler] Task {task_id} became invalid: {safe_reason}")
        self.task_store.update_task(task_id, {
            "enabled": False,
            "last_error": safe_reason,
            "last_error_at": now.isoformat(),
        })
        content = (
            f"定时任务已暂停：{task.get('name', task_id)}\n"
            f"原因：{safe_reason}\n"
            "这个任务无法继续安全调度，所以没有被静默跳过。你可以回复让我重新创建/补跑，或确认跳过。"
        )
        self._send_notice(task, "invalid", content)
    
    def _calculate_next_run(self, task: dict, from_time: datetime) -> Optional[datetime]:
        """
        Calculate next run time for a task
        
        Args:
            task: Task dictionary
            from_time: Calculate from this time
            
        Returns:
            Next run datetime or None for one-time tasks
        """
        schedule = task.get("schedule", {})
        schedule_type = schedule.get("type")
        
        if schedule_type == "cron":
            # Cron expression
            expression = schedule.get("expression")
            if not expression:
                return None
            
            try:
                cron = croniter(expression, from_time)
                return cron.get_next(datetime)
            except Exception as e:
                logger.error(f"[Scheduler] Invalid cron expression '{expression}': {e}")
                return None
        
        elif schedule_type == "interval":
            # Interval in seconds
            seconds = schedule.get("seconds", 0)
            if seconds <= 0:
                return None
            return from_time + timedelta(seconds=seconds)
        
        elif schedule_type == "once":
            # One-time task at specific time
            run_at_str = schedule.get("run_at")
            if not run_at_str:
                return None
            
            try:
                run_at = datetime.fromisoformat(run_at_str)
                # Only return if in the future
                if run_at > from_time:
                    return run_at
            except Exception:
                pass
            return None
        
        return None
    
    def _handle_missed_task(
        self,
        task: dict,
        missed_run: datetime,
        now: datetime,
        overdue_seconds: float,
    ) -> None:
        """Record and notify when a due task is too late to auto-run."""
        task_id = task.get("id", "")
        logger.warning(
            f"[Scheduler] Task {task_id} is overdue by {int(overdue_seconds)}s, "
            "not auto-running; notifying receiver"
        )

        schedule = task.get("schedule", {}) or {}
        next_run = None
        updates = {
            "last_error": self._safe_notice_reason(
                f"missed scheduled run at {missed_run.isoformat()} "
                f"after being overdue by {int(overdue_seconds)}s"
            ),
            "last_error_at": now.isoformat(),
            "last_missed_run_at": missed_run.isoformat(),
        }

        if schedule.get("type") == "once":
            updates["enabled"] = False
            logger.info(f"[Scheduler] One-time task {task_id} missed and was disabled")
        else:
            next_run = self._calculate_next_run(task, now)
            if next_run:
                updates["next_run_at"] = next_run.isoformat()
                logger.info(f"[Scheduler] Rescheduled task {task_id} to {next_run}")

        self.task_store.update_task(task_id, updates)
        self._notify_task_missed(task, missed_run, now, overdue_seconds, next_run)

    def _execute_task(self, task: dict) -> bool:
        """
        Execute a task
        
        Args:
            task: Task dictionary

        Returns:
            True when the execution and delivery path reported success.
        """
        try:
            # Call the execute callback
            result = self.execute_callback(task)
            if result is False:
                reason = "scheduled task execution returned failure"
                self._mark_task_failure(task, reason)
                self._notify_task_failure(task, reason)
                return False
            return True
        except Exception as e:
            logger.error(f"[Scheduler] Error executing task {task['id']}: {e}")
            self._mark_task_failure(task, str(e))
            self._notify_task_failure(task, str(e))
            return False

    def _mark_task_failure(self, task: dict, reason: str) -> None:
        self.task_store.update_task(task["id"], {
            "last_error": self._safe_notice_reason(reason),
            "last_error_at": datetime.now().isoformat(),
        })

    def _notify_task_missed(
        self,
        task: dict,
        missed_run: datetime,
        now: datetime,
        overdue_seconds: float,
        next_run: Optional[datetime],
    ) -> None:
        minutes = max(1, int(overdue_seconds // 60))
        next_run_text = next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else "无"
        content = (
            f"定时任务未执行：{task.get('name', task.get('id', '未知任务'))}\n"
            f"原计划时间：{missed_run.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"发现时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"原因：调度器恢复时已超时约 {minutes} 分钟，本次没有自动补跑。\n"
            f"下一次计划：{next_run_text}\n"
            "你可以回复让我现在补跑，也可以直接跳过这次。"
        )
        self._send_notice(task, "missed", content)

    def _notify_task_failure(self, task: dict, reason: str) -> None:
        safe_reason = self._safe_notice_reason(reason)
        content = (
            f"定时任务执行失败：{task.get('name', task.get('id', '未知任务'))}\n"
            f"失败原因：{safe_reason}\n"
            "任务没有被静默丢弃。你可以回复让我重新执行，也可以决定跳过这次。"
        )
        self._send_notice(task, "failed", content)

    @staticmethod
    def _safe_notice_reason(reason: str) -> str:
        text = re.sub(r"\s+", " ", str(reason or "")).strip()
        text = re.sub(
            r"(?i)\b(token|cookie|secret|password|api[_-]?key)\b\s*[:=]\s*\S+",
            r"\1=<redacted>",
            text,
        )
        text = re.sub(r"[A-Za-z]:\\Users\\[^\\\s]+", r"<user-home>", text)
        if len(text) > 300:
            return text[:297].rstrip() + "..."
        return text or "unknown error"

    def _send_notice(self, task: dict, notice_type: str, content: str) -> None:
        try:
            notice_task = self._build_notice_task(task, notice_type, content)
            self.notify_callback(notice_task)
        except Exception as e:
            logger.error(
                f"[Scheduler] Failed to send {notice_type} notice for "
                f"task {task.get('id')}: {e}"
            )

    @staticmethod
    def _build_notice_task(task: dict, notice_type: str, content: str) -> dict:
        action = dict(task.get("action", {}) or {})
        notice_action = {
            "type": "send_message",
            "content": content,
            "receiver": action.get("receiver"),
            "receiver_name": action.get("receiver_name"),
            "is_group": action.get("is_group", False),
            "channel_type": action.get("channel_type", "unknown"),
            "notify_session_id": action.get("notify_session_id"),
        }
        return {
            "id": f"{task.get('id', 'unknown')}_{notice_type}_notice",
            "name": f"{task.get('name', task.get('id', 'unknown'))} {notice_type} notice",
            "enabled": True,
            "owner_actor_id": task.get("owner_actor_id"),
            "owner_role": task.get("owner_role", "user"),
            "owner_memory_user_id": task.get("owner_memory_user_id"),
            "owner_conversation_id": task.get("owner_conversation_id"),
            "schedule": {"type": "once"},
            "action": notice_action,
        }
