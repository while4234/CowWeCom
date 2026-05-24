"""
Integration module for scheduler with AgentBridge
"""

import os
import threading
from datetime import datetime, timedelta
from typing import Optional
from croniter import croniter
from config import conf
from common.log import logger
from common.utils import expand_path
from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType

# Global scheduler service instance
_scheduler_service = None
_task_store = None
LLM_BACKEND_AUTO_SWITCH_TASK_ID = "system_llm_backend_auto_switch"
LLM_BACKEND_AUTO_SWITCH_ACTION = "system_llm_backend_auto_switch"
REASONING_POLICY_OPTIMIZER_TASK_ID = "system_reasoning_effort_policy_optimizer"
REASONING_POLICY_OPTIMIZER_ACTION = "system_reasoning_effort_policy_optimizer"
# Module-level lock to guard idempotent initialization across threads
_init_lock = threading.Lock()


def _current_agent_bridge(fallback_agent_bridge):
    """Resolve the latest process AgentBridge after global backend/cache resets."""
    try:
        from bridge.bridge import Bridge

        return Bridge().get_agent_bridge() or fallback_agent_bridge
    except Exception as e:
        logger.debug(f"[Scheduler] Falling back to captured AgentBridge: {e}")
        return fallback_agent_bridge


def ensure_llm_backend_auto_switch_task(task_store, now: Optional[datetime] = None) -> Optional[dict]:
    """Ensure the global LLM backend check is a hidden CowChat system task."""
    if task_store is None:
        return None

    from common.llm_backend_auto_switcher import scheduler_cron_expression
    from common.llm_backend_router import get_llm_backend_config

    now = now or datetime.now()
    cfg = get_llm_backend_config()
    auto_cfg = cfg.get("auto_switch") if isinstance(cfg.get("auto_switch"), dict) else {}
    enabled = bool(auto_cfg.get("enabled", True))
    expression = scheduler_cron_expression()
    task = {
        "id": LLM_BACKEND_AUTO_SWITCH_TASK_ID,
        "name": "LLM backend daily auto switch",
        "enabled": enabled,
        "system": True,
        "hidden": True,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "schedule": {"type": "cron", "expression": expression},
        "action": {
            "type": LLM_BACKEND_AUTO_SWITCH_ACTION,
            "description": "Run the global LLM backend daily route check",
        },
        "next_run_at": _next_cron_run(expression, now).isoformat(),
    }

    existing = task_store.get_task(LLM_BACKEND_AUTO_SWITCH_TASK_ID)
    if not existing:
        task_store.add_task(task)
        logger.info("[Scheduler] Registered system task: %s", LLM_BACKEND_AUTO_SWITCH_TASK_ID)
        return task

    existing_schedule = existing.get("schedule") if isinstance(existing.get("schedule"), dict) else {}
    updates = {
        "name": task["name"],
        "enabled": enabled,
        "system": True,
        "hidden": True,
        "schedule": task["schedule"],
        "action": task["action"],
    }
    if existing_schedule.get("expression") != expression or not existing.get("next_run_at"):
        updates["next_run_at"] = task["next_run_at"]
    task_store.update_task(LLM_BACKEND_AUTO_SWITCH_TASK_ID, updates)
    return task_store.get_task(LLM_BACKEND_AUTO_SWITCH_TASK_ID)


def ensure_reasoning_effort_policy_optimizer_task(task_store, now: Optional[datetime] = None) -> Optional[dict]:
    """Ensure the reasoning-effort policy optimizer is a hidden CowChat system task."""
    if task_store is None:
        return None

    now = now or datetime.now()
    enabled = bool(conf().get("reasoning_effort_policy_auto_optimize_enabled", False))
    try:
        seconds = int(conf().get("reasoning_effort_policy_auto_optimize_check_seconds") or 300)
    except (TypeError, ValueError):
        seconds = 300
    seconds = max(60, seconds)
    task = {
        "id": REASONING_POLICY_OPTIMIZER_TASK_ID,
        "name": "Reasoning effort policy optimizer",
        "enabled": enabled,
        "system": True,
        "hidden": True,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "schedule": {"type": "interval", "seconds": seconds},
        "action": {
            "type": REASONING_POLICY_OPTIMIZER_ACTION,
            "description": "Run the hidden local reasoning-effort policy optimizer",
        },
        "next_run_at": (now + timedelta(seconds=seconds)).isoformat(),
    }

    existing = task_store.get_task(REASONING_POLICY_OPTIMIZER_TASK_ID)
    if not existing:
        task_store.add_task(task)
        logger.info("[Scheduler] Registered system task: %s", REASONING_POLICY_OPTIMIZER_TASK_ID)
        return task

    existing_schedule = existing.get("schedule") if isinstance(existing.get("schedule"), dict) else {}
    updates = {
        "name": task["name"],
        "enabled": enabled,
        "system": True,
        "hidden": True,
        "schedule": task["schedule"],
        "action": task["action"],
    }
    if existing_schedule.get("seconds") != seconds or not existing.get("next_run_at"):
        updates["next_run_at"] = task["next_run_at"]
    task_store.update_task(REASONING_POLICY_OPTIMIZER_TASK_ID, updates)
    return task_store.get_task(REASONING_POLICY_OPTIMIZER_TASK_ID)


def _next_cron_run(expression: str, now: datetime) -> datetime:
    return croniter(expression, now).get_next(datetime)


def init_scheduler(agent_bridge) -> bool:
    """
    Initialize scheduler service (idempotent).

    Safe to call multiple times and from multiple threads: only the first
    successful call creates the singleton ``SchedulerService`` + background
    scanning thread. Subsequent calls return immediately.

    Args:
        agent_bridge: AgentBridge instance

    Returns:
        True if scheduler is initialized (newly created or already running)
    """
    global _scheduler_service, _task_store

    # Fast path: already initialized and running
    if _scheduler_service is not None and getattr(_scheduler_service, "running", False):
        return True

    with _init_lock:
        # Re-check under the lock to avoid races where multiple threads
        # passed the fast-path check before any of them acquired the lock.
        if _scheduler_service is not None and getattr(_scheduler_service, "running", False):
            return True

        try:
            from agent.tools.scheduler.task_store import TaskStore
            from agent.tools.scheduler.scheduler_service import SchedulerService

            # Get workspace from config
            workspace_root = expand_path(conf().get("agent_workspace", "~/cow"))
            store_path = os.path.join(workspace_root, "scheduler", "tasks.json")

            # Create task store (reuse if already created)
            if _task_store is None:
                _task_store = TaskStore(store_path)
                logger.debug(f"[Scheduler] Task store initialized: {store_path}")

            ensure_llm_backend_auto_switch_task(_task_store)
            ensure_reasoning_effort_policy_optimizer_task(_task_store)

            # Create execute callback
            def execute_task_callback(task: dict):
                """Callback to execute a scheduled task"""
                try:
                    action = task.get("action", {})
                    action_type = action.get("type")

                    if action_type == LLM_BACKEND_AUTO_SWITCH_ACTION:
                        return _execute_llm_backend_auto_switch(task)
                    if action_type == REASONING_POLICY_OPTIMIZER_ACTION:
                        current_agent_bridge = _current_agent_bridge(agent_bridge)
                        return _execute_reasoning_effort_policy_optimizer(task, current_agent_bridge)

                    current_agent_bridge = _current_agent_bridge(agent_bridge)
                    if action_type == "agent_task":
                        return _execute_agent_task(task, current_agent_bridge)
                    elif action_type == "send_message":
                        # Legacy support for old tasks
                        return _execute_send_message(task, current_agent_bridge)
                    elif action_type == "tool_call":
                        # Legacy support for old tasks
                        return _execute_tool_call(task, current_agent_bridge)
                    elif action_type == "skill_call":
                        # Legacy support for old tasks
                        return _execute_skill_call(task, current_agent_bridge)
                    else:
                        logger.warning(f"[Scheduler] Unknown action type: {action_type}")
                        return False
                except Exception as e:
                    logger.error(f"[Scheduler] Error executing task {task.get('id')}: {e}")
                    return False

            # Create scheduler service
            _scheduler_service = SchedulerService(_task_store, execute_task_callback)
            _scheduler_service.start()

            logger.debug("[Scheduler] Scheduler service initialized and started")
            return True

        except Exception as e:
            logger.error(f"[Scheduler] Failed to initialize scheduler: {e}")
            return False


def get_task_store():
    """Get the global task store instance"""
    return _task_store


def get_scheduler_service():
    """Get the global scheduler service instance"""
    return _scheduler_service


def _legacy_task_owner(task: dict) -> Optional[str]:
    action = task.get("action", {}) or {}
    channel_type = action.get("channel_type")
    legacy_user_id = action.get("notify_session_id")
    if channel_type and legacy_user_id:
        return f"{channel_type}:{legacy_user_id}"
    return None


def _scheduler_session_id(task: dict, receiver: str) -> str:
    return f"scheduler_{receiver}_{task['id']}"


def _apply_task_owner_context(
    task: dict,
    context: Context,
    channel_type: str,
    conversation_id: Optional[str] = None,
) -> bool:
    """Attach the task creator identity while preserving scheduler isolation."""
    owner_actor_id = task.get("owner_actor_id") or _legacy_task_owner(task)
    if not owner_actor_id:
        logger.error(f"[Scheduler] Task {task.get('id')}: missing owner identity")
        return False

    context["channel_type"] = channel_type
    context["actor_id"] = owner_actor_id
    context["actor_role"] = task.get("owner_role", "user")
    if task.get("owner_memory_user_id"):
        context["memory_user_id"] = task.get("owner_memory_user_id")
    if conversation_id:
        context["conversation_id"] = conversation_id
    return True


def _resolve_task_profile(task: dict, context: Context):
    if not task.get("owner_actor_id") and not _legacy_task_owner(task):
        return None
    try:
        from agent.user_profiles import resolve_agent_user_profile

        return resolve_agent_user_profile(context)
    except Exception as e:
        logger.error(f"[Scheduler] Task {task.get('id')}: failed to resolve owner profile: {e}")
        return None


def _guard_scheduled_tool(tool, profile):
    from agent.access_control import GuardedTool, ToolAccessPolicy

    tool_name = getattr(tool, "name", "")
    if tool_name in {'read', 'write', 'edit', 'bash', 'grep', 'find', 'ls', 'web_fetch', 'send', 'browser'}:
        merged_config = dict(getattr(tool, "config", None) or {})
        merged_config["cwd"] = profile.tool_workspace
        tool.config = merged_config
        tool.cwd = merged_config["cwd"]
    return GuardedTool(tool, ToolAccessPolicy(profile))


def _remember_delivered_output(
    agent_bridge,
    task: dict,
    channel_type: str,
    content: str,
) -> None:
    """Best-effort persistence of the message the scheduler sent to a user.

    Uses notify_session_id (the real chat session_id stored at task creation time)
    so that group chats correctly associate the output with the user's conversation.
    Falls back to receiver for backward compatibility with old tasks.

    Per-action-type behaviour:
        - agent_task / tool_call / skill_call: gated by ``scheduler_inject_to_session``
          (default True). These produce AI-generated content worth remembering.
        - send_message: additionally gated by ``scheduler_inject_send_message``
          (default False). Fixed reminder text rarely benefits follow-up Q&A and
          would just consume context tokens.
    """
    if not content:
        return
    action = task.get("action", {})
    action_type = action.get("type", "")

    # send_message defaults to NOT being injected; explicit opt-in via config.
    if action_type == "send_message":
        if not conf().get("scheduler_inject_send_message", False):
            return

    session_id = action.get("notify_session_id") or action.get("receiver")
    if not session_id:
        return
    try:
        remember = getattr(agent_bridge, "remember_scheduled_output", None)
        if remember:
            task_desc = action.get("task_description") or action.get("content", "")
            remember(session_id, str(content), channel_type=channel_type, task_description=task_desc)
    except Exception as e:
        logger.warning(
            f"[Scheduler] Failed to remember delivered output for {session_id}: {e}"
        )


def _is_weixin_channel(channel_type: str) -> bool:
    return channel_type == "weixin" or str(channel_type or "").startswith("weixin_")


def _get_running_channel(channel_type: str):
    try:
        from agent.social_bridge.service import ActiveMessageRouter

        return ActiveMessageRouter._get_running_channel(channel_type)
    except Exception as e:
        logger.debug(f"[Scheduler] Failed to resolve running channel '{channel_type}': {e}")
        return None


def _get_scheduler_channel(channel_type: str):
    channel = _get_running_channel(channel_type)
    if channel is not None:
        return channel

    from channel.channel_factory import create_channel

    return create_channel(channel_type)


def _send_scheduler_reply(task: dict, channel_type: str, receiver: str, reply: Reply, context: Context) -> bool:
    try:
        channel = _get_scheduler_channel(channel_type)
        if not channel:
            logger.error(f"[Scheduler] Failed to create channel: {channel_type}")
            return False
        action = task.get("action", {}) or {}

        if channel_type == "web" and hasattr(channel, "request_to_session"):
            request_id = context.get("request_id")
            if request_id:
                channel.request_to_session[request_id] = receiver
                logger.debug(f"[Scheduler] Registered request_id {request_id} -> session {receiver}")

        if (
            getattr(reply, "type", ReplyType.TEXT) == ReplyType.TEXT
            and hasattr(channel, "active_send_text_result")
            and (_is_weixin_channel(channel_type) or channel_type == "wecom_bot")
        ):
            if channel_type == "wecom_bot":
                result = channel.active_send_text_result(
                    receiver,
                    str(reply.content or ""),
                    is_group=bool(context.get("isgroup", False)),
                    mention_user_ids=action.get("mention_user_ids"),
                    mention_display_names=action.get("mention_display_names"),
                )
            else:
                result = channel.active_send_text_result(receiver, str(reply.content or ""))
            if result.get("ok"):
                return True
            logger.error(
                f"[Scheduler] Task {task.get('id')}: failed to send Weixin message "
                f"to {receiver}: {result}"
            )
            return False

        sent = channel.send(reply, context)
        if sent is False:
            logger.error(
                f"[Scheduler] Task {task.get('id')}: channel send returned False "
                f"for {channel_type}:{receiver}"
            )
            return False
        return True
    except Exception as e:
        logger.error(f"[Scheduler] Failed to send message: {e}")
        import traceback

        logger.error(f"[Scheduler] Traceback: {traceback.format_exc()}")
        return False


def _execute_llm_backend_auto_switch(task: dict) -> bool:
    """Run the global LLM backend route check without involving Agent/LLM."""
    try:
        from common.llm_backend_auto_switcher import run_once

        state = run_once()
        auto = state.get("auto", {}) if isinstance(state, dict) else {}
        logger.info(
            "[Scheduler] System task %s completed: decision=%s reason=%s",
            task.get("id", LLM_BACKEND_AUTO_SWITCH_TASK_ID),
            auto.get("last_decision", ""),
            auto.get("last_reason", ""),
        )
        return True
    except Exception as e:
        logger.warning(
            "[Scheduler] System task %s failed: %s",
            task.get("id", LLM_BACKEND_AUTO_SWITCH_TASK_ID),
            str(e)[:300],
        )
        return False


def _execute_reasoning_effort_policy_optimizer(task: dict, agent_bridge) -> bool:
    """Run the hidden policy optimizer through the current CowChat model adapter."""
    try:
        from bridge.agent_bridge import AgentLLMModel
        from common.reasoning_effort_policy import run_policy_optimizer_if_due

        adapter = AgentLLMModel(getattr(agent_bridge, "bridge", agent_bridge))
        adapter.channel_type = "scheduler_system"
        adapter.session_id = task.get("id", REASONING_POLICY_OPTIMIZER_TASK_ID)
        adapter.user_id = "system"
        adapter.actor_role = "admin"
        adapter.is_admin = True
        adapter.is_group = False

        report = run_policy_optimizer_if_due(
            model_adapter=adapter,
            reason="cowchat_scheduler",
        )
        status = str(report.get("status") or "")
        logger.info(
            "[Scheduler] System task %s completed: status=%s applied=%s skipped_reason=%s",
            task.get("id", REASONING_POLICY_OPTIMIZER_TASK_ID),
            status,
            report.get("applied_rule_count", 0),
            report.get("failure_reason", ""),
        )
        return status in {"success", "skipped"}
    except Exception as e:
        logger.warning(
            "[Scheduler] System task %s failed: %s",
            task.get("id", REASONING_POLICY_OPTIMIZER_TASK_ID),
            str(e)[:300],
        )
        return False


def _execute_agent_task(task: dict, agent_bridge):
    """
    Execute an agent_task action - let Agent handle the task
    
    Args:
        task: Task dictionary
        agent_bridge: AgentBridge instance
    """
    try:
        action = task.get("action", {})
        task_description = action.get("task_description")
        receiver = action.get("receiver")
        is_group = action.get("is_group", False)
        channel_type = action.get("channel_type", "unknown")
        
        if not task_description:
            logger.error(f"[Scheduler] Task {task['id']}: No task_description specified")
            return False
        
        if not receiver:
            logger.error(f"[Scheduler] Task {task['id']}: No receiver specified")
            return False
        
        # Check for unsupported channels
        if channel_type == "dingtalk":
            logger.warning(f"[Scheduler] Task {task['id']}: DingTalk channel does not support scheduled messages (Stream mode limitation). Task will execute but message cannot be sent.")
        
        logger.info(f"[Scheduler] Task {task['id']}: Executing agent task '{task_description}'")
        
        # Create a unique session_id for this scheduled task to avoid polluting user's conversation
        # Format: scheduler_<receiver>_<task_id> to ensure isolation
        scheduler_session_id = _scheduler_session_id(task, receiver)
        
        # Create context for Agent
        context = Context(ContextType.TEXT, task_description)
        context["receiver"] = receiver
        context["isgroup"] = is_group
        context["session_id"] = scheduler_session_id
        if not _apply_task_owner_context(task, context, channel_type, scheduler_session_id):
            return False
        
        # Channel-specific setup
        if channel_type == "web":
            import uuid
            request_id = f"scheduler_{task['id']}_{uuid.uuid4().hex[:8]}"
            context["request_id"] = request_id
        elif channel_type == "feishu":
            context["receive_id_type"] = "chat_id" if is_group else "open_id"
            context["msg"] = None
        elif channel_type == "dingtalk":
            # DingTalk requires msg object, set to None for scheduled tasks
            context["msg"] = None
            if not is_group:
                sender_staff_id = action.get("dingtalk_sender_staff_id")
                if sender_staff_id:
                    context["dingtalk_sender_staff_id"] = sender_staff_id
        elif channel_type == "wecom_bot":
            context["msg"] = None

        # Use Agent to execute the task
        # Mark this as a scheduled task execution to prevent recursive task creation
        context["is_scheduled_task"] = True
        
        try:
            # Don't clear history - scheduler tasks use isolated session_id so they won't pollute user conversations
            reply = agent_bridge.agent_reply(task_description, context=context, on_event=None, clear_history=False)
            
            if reply and reply.content:
                if _send_scheduler_reply(task, channel_type, receiver, reply, context):
                    _remember_delivered_output(agent_bridge, task, channel_type, reply.content)
                    logger.info(f"[Scheduler] Task {task['id']} executed successfully, result sent to {receiver}")
                    return True
                return False
            else:
                logger.error(f"[Scheduler] Task {task['id']}: No result from agent execution")
                return False
                
        except Exception as e:
            logger.error(f"[Scheduler] Failed to execute task via Agent: {e}")
            import traceback
            logger.error(f"[Scheduler] Traceback: {traceback.format_exc()}")
            return False
            
    except Exception as e:
        logger.error(f"[Scheduler] Error in _execute_agent_task: {e}")
        import traceback
        logger.error(f"[Scheduler] Traceback: {traceback.format_exc()}")
        return False


def _execute_send_message(task: dict, agent_bridge):
    """
    Execute a send_message action
    
    Args:
        task: Task dictionary
        agent_bridge: AgentBridge instance
    """
    try:
        action = task.get("action", {})
        content = action.get("content", "")
        receiver = action.get("receiver")
        is_group = action.get("is_group", False)
        channel_type = action.get("channel_type", "unknown")
        
        if not receiver:
            logger.error(f"[Scheduler] Task {task['id']}: No receiver specified")
            return False
        
        # Create context for sending message
        context = Context(ContextType.TEXT, content)
        context["receiver"] = receiver
        context["isgroup"] = is_group
        context["session_id"] = action.get("notify_session_id") or receiver
        if not _apply_task_owner_context(task, context, channel_type, context["session_id"]):
            return False
        
        # Channel-specific context setup
        if channel_type == "web":
            # Web channel needs request_id
            import uuid
            request_id = f"scheduler_{task['id']}_{uuid.uuid4().hex[:8]}"
            context["request_id"] = request_id
            logger.debug(f"[Scheduler] Generated request_id for web channel: {request_id}")
        elif channel_type == "feishu":
            # Feishu channel: for scheduled tasks, send as new message (no msg_id to reply to)
            # Use chat_id for groups, open_id for private chats
            context["receive_id_type"] = "chat_id" if is_group else "open_id"
            # Keep isgroup as is, but set msg to None (no original message to reply to)
            # Feishu channel will detect this and send as new message instead of reply
            context["msg"] = None
            logger.debug(f"[Scheduler] Feishu: receive_id_type={context['receive_id_type']}, is_group={is_group}, receiver={receiver}")
        elif channel_type == "dingtalk":
            # DingTalk channel setup
            context["msg"] = None
            # 如果是单聊，需要传递 sender_staff_id
            if not is_group:
                sender_staff_id = action.get("dingtalk_sender_staff_id")
                if sender_staff_id:
                    context["dingtalk_sender_staff_id"] = sender_staff_id
                    logger.debug(f"[Scheduler] DingTalk single chat: sender_staff_id={sender_staff_id}")
                else:
                    logger.warning(f"[Scheduler] Task {task['id']}: DingTalk single chat message missing sender_staff_id")
        elif channel_type == "wecom_bot":
            context["msg"] = None
        elif channel_type == "qq":
            context["msg"] = None

        # Create reply
        reply = Reply(ReplyType.TEXT, content)

        if _send_scheduler_reply(task, channel_type, receiver, reply, context):
            _remember_delivered_output(agent_bridge, task, channel_type, content)
            logger.info(f"[Scheduler] Task {task['id']} executed: sent message to {receiver}")
            return True
        return False
            
    except Exception as e:
        logger.error(f"[Scheduler] Error in _execute_send_message: {e}")
        import traceback
        logger.error(f"[Scheduler] Traceback: {traceback.format_exc()}")
        return False


def _execute_tool_call(task: dict, agent_bridge):
    """
    Execute a tool_call action
    
    Args:
        task: Task dictionary
        agent_bridge: AgentBridge instance
    """
    try:
        action = task.get("action", {})
        # Support both old and new field names
        tool_name = action.get("call_name") or action.get("tool_name")
        tool_params = action.get("call_params") or action.get("tool_params", {})
        result_prefix = action.get("result_prefix", "")
        receiver = action.get("receiver")
        is_group = action.get("is_group", False)
        channel_type = action.get("channel_type", "unknown")
        
        if not tool_name:
            logger.error(f"[Scheduler] Task {task['id']}: No tool_name specified")
            return False
        
        if not receiver:
            logger.error(f"[Scheduler] Task {task['id']}: No receiver specified")
            return False
        
        scheduler_session = _scheduler_session_id(task, receiver)
        context = Context(ContextType.TEXT, tool_name)
        context["receiver"] = receiver
        context["isgroup"] = is_group
        context["session_id"] = scheduler_session
        if not _apply_task_owner_context(task, context, channel_type, scheduler_session):
            return False
        profile = _resolve_task_profile(task, context)
        if profile is None:
            logger.error(f"[Scheduler] Task {task['id']}: cannot execute tool without owner profile")
            return False

        # Get tool manager and create tool instance
        from agent.tools.tool_manager import ToolManager
        tool_manager = ToolManager()
        tool = tool_manager.create_tool(tool_name)
        
        if not tool:
            logger.error(f"[Scheduler] Task {task['id']}: Tool '{tool_name}' not found")
            return False
        
        guarded_tool = _guard_scheduled_tool(tool, profile)

        # Execute tool
        logger.info(f"[Scheduler] Task {task['id']}: Executing tool '{tool_name}' with params {tool_params}")
        result = guarded_tool.execute(tool_params)
        
        # Get result content
        if hasattr(result, 'result'):
            content = result.result
        else:
            content = str(result)
        
        # Add prefix if specified
        if result_prefix:
            content = f"{result_prefix}\n\n{content}"
        
        # Send result as message
        
        # Channel-specific context setup
        if channel_type == "web":
            # Web channel needs request_id
            import uuid
            request_id = f"scheduler_{task['id']}_{uuid.uuid4().hex[:8]}"
            context["request_id"] = request_id
            logger.debug(f"[Scheduler] Generated request_id for web channel: {request_id}")
        elif channel_type == "feishu":
            context["receive_id_type"] = "chat_id" if is_group else "open_id"
            context["msg"] = None
            logger.debug(f"[Scheduler] Feishu: receive_id_type={context['receive_id_type']}, is_group={is_group}, receiver={receiver}")
        elif channel_type == "wecom_bot":
            context["msg"] = None

        reply = Reply(ReplyType.TEXT, content)

        if _send_scheduler_reply(task, channel_type, receiver, reply, context):
            _remember_delivered_output(agent_bridge, task, channel_type, content)
            logger.info(f"[Scheduler] Task {task['id']} executed: sent tool result to {receiver}")
            return True
        return False

    except Exception as e:
        logger.error(f"[Scheduler] Error in _execute_tool_call: {e}")
        return False


def _execute_skill_call(task: dict, agent_bridge):
    """
    Execute a skill_call action by asking Agent to run the skill
    
    Args:
        task: Task dictionary
        agent_bridge: AgentBridge instance
    """
    try:
        action = task.get("action", {})
        # Support both old and new field names
        skill_name = action.get("call_name") or action.get("skill_name")
        skill_params = action.get("call_params") or action.get("skill_params", {})
        result_prefix = action.get("result_prefix", "")
        receiver = action.get("receiver")
        is_group = action.get("is_group", action.get("isgroup", False))
        channel_type = action.get("channel_type", "unknown")
        
        if not skill_name:
            logger.error(f"[Scheduler] Task {task['id']}: No skill_name specified")
            return False
        
        if not receiver:
            logger.error(f"[Scheduler] Task {task['id']}: No receiver specified")
            return False
        
        logger.info(f"[Scheduler] Task {task['id']}: Executing skill '{skill_name}' with params {skill_params}")
        
        # Create a unique session_id for this scheduled task to avoid polluting user's conversation
        # Format: scheduler_<receiver>_<task_id> to ensure isolation
        scheduler_session_id = _scheduler_session_id(task, receiver)
        
        # Build a natural language query for the Agent to execute the skill
        # Format: "Use skill-name to do something with params"
        param_str = ", ".join([f"{k}={v}" for k, v in skill_params.items()])
        query = f"Use {skill_name} skill"
        if param_str:
            query += f" with {param_str}"
        
        # Create context for Agent
        context = Context(ContextType.TEXT, query)
        context["receiver"] = receiver
        context["isgroup"] = is_group
        context["session_id"] = scheduler_session_id
        if not _apply_task_owner_context(task, context, channel_type, scheduler_session_id):
            return False
        
        # Channel-specific setup
        if channel_type == "web":
            import uuid
            request_id = f"scheduler_{task['id']}_{uuid.uuid4().hex[:8]}"
            context["request_id"] = request_id
        elif channel_type == "feishu":
            context["receive_id_type"] = "chat_id" if is_group else "open_id"
            context["msg"] = None
        elif channel_type == "wecom_bot":
            context["msg"] = None

        # Use Agent to execute the skill
        try:
            # Don't clear history - scheduler tasks use isolated session_id so they won't pollute user conversations
            reply = agent_bridge.agent_reply(query, context=context, on_event=None, clear_history=False)
            
            if reply and reply.content:
                content = reply.content
                
                # Add prefix if specified
                if result_prefix:
                    content = f"{result_prefix}\n\n{content}"
                
                if _send_scheduler_reply(
                    task,
                    channel_type,
                    receiver,
                    Reply(ReplyType.TEXT, content),
                    context,
                ):
                    _remember_delivered_output(agent_bridge, task, channel_type, content)
                    logger.info(f"[Scheduler] Task {task['id']} executed: skill result sent to {receiver}")
                    return True
                return False
            else:
                logger.error(f"[Scheduler] Task {task['id']}: No result from skill execution")
                return False
                
        except Exception as e:
            logger.error(f"[Scheduler] Failed to execute skill via Agent: {e}")
            import traceback
            logger.error(f"[Scheduler] Traceback: {traceback.format_exc()}")
            return False
            
    except Exception as e:
        logger.error(f"[Scheduler] Error in _execute_skill_call: {e}")
        import traceback
        logger.error(f"[Scheduler] Traceback: {traceback.format_exc()}")
        return False


def attach_scheduler_to_tool(tool, context: Context = None):
    """
    Attach scheduler components to a SchedulerTool instance
    
    Args:
        tool: SchedulerTool instance
        context: Current context (optional)
    """
    tool = getattr(tool, "inner", tool)

    if _task_store:
        tool.task_store = _task_store
    
    if context:
        tool.current_context = context
        
        channel_type = context.get("channel_type") or conf().get("channel_type", "unknown")
        if not tool.config:
            tool.config = {}
        tool.config["channel_type"] = channel_type
