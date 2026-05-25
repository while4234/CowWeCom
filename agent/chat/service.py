"""
ChatService - Wraps the Agent stream execution to produce CHAT protocol chunks.

Translates agent events (message_update, message_end, tool_execution_end, etc.)
into the CHAT socket protocol format (content chunks with segment_id, tool_calls chunks).
"""

import time
from typing import Callable, Optional

from common.agent_task_limits import is_development_task, resolve_agent_task_budget
from common.capi_monthly_monitor import maybe_check_capi_monthly_after_task
from common.llm_backend_router import get_current_backend
from common.log import logger


class ChatService:
    """
    High-level service that runs an Agent for a given query and streams
    the results as CHAT protocol chunks via a callback.

    Usage:
        svc = ChatService(agent_bridge)
        svc.run(query, session_id, send_chunk_fn)
    """

    def __init__(self, agent_bridge):
        """
        :param agent_bridge: AgentBridge instance (manages agent lifecycle)
        """
        self.agent_bridge = agent_bridge

    def run(self, query: str, session_id: str, send_chunk_fn: Callable[[dict], None],
            channel_type: str = ""):
        """
        Run the agent for *query* and stream results back via *send_chunk_fn*.

        The method blocks until the agent finishes. After it returns the SDK
        will automatically send the final (streaming=false) message.

        :param query: user query text
        :param session_id: session identifier for agent isolation
        :param send_chunk_fn: callable(chunk_data: dict) to send a streaming chunk
        :param channel_type: source channel (e.g. "web", "feishu") for persistence
        """
        agent = self.agent_bridge.get_agent(session_id=session_id)
        if agent is None:
            raise RuntimeError("Failed to initialise agent for the session")

        # Pass context metadata to model for downstream API requests
        if hasattr(agent, 'model'):
            agent.model.channel_type = channel_type or ""
            agent.model.session_id = session_id or ""

        # State shared between the event callback and this method
        state = _StreamState()
        task_is_development = is_development_task(query)
        task_backend = get_current_backend()

        def on_event(event: dict):
            """Translate agent events into CHAT protocol chunks."""
            event_type = event.get("type")
            data = event.get("data", {})

            if event_type == "turn_start":
                try:
                    state.turn_number = max(state.turn_number, int(data.get("turn", 0) or 0))
                except (TypeError, ValueError):
                    pass

            elif event_type == "reasoning_update":
                delta = data.get("delta", "")
                if delta:
                    send_chunk_fn({
                        "chunk_type": "reasoning",
                        "delta": delta,
                        "segment_id": state.segment_id,
                    })

            elif event_type == "message_update":
                # Incremental text delta
                delta = data.get("delta", "")
                if delta:
                    send_chunk_fn({
                        "chunk_type": "content",
                        "delta": delta,
                        "segment_id": state.segment_id,
                    })

            elif event_type == "message_end":
                # A content segment finished.
                tool_calls = data.get("tool_calls", [])
                if tool_calls:
                    # After tool_calls are executed the next content will be
                    # a new segment; collect tool results until turn_end.
                    state.pending_tool_results = []

            elif event_type == "file_to_send":
                url = data.get("url") or ""
                if url:
                    fname = data.get("file_name") or "file"
                    ft = data.get("file_type") or "file"
                    if ft == "image":
                        link = f"![{fname}]({url})"
                    else:
                        link = f"[{fname}]({url})"
                    send_chunk_fn({
                        "chunk_type": "content",
                        "delta": "\n\n" + link + "\n\n",
                        "segment_id": state.segment_id,
                    })
                    # Remove url so the model won't repeat it in its reply
                    data.pop("url", None)

            elif event_type == "tool_execution_start":
                # Notify the client that a tool is about to run (with its input args)
                tool_name = data.get("tool_name", "")
                arguments = data.get("arguments", {})
                # Cache arguments keyed by tool_call_id so tool_execution_end can include them
                tool_call_id = data.get("tool_call_id", tool_name)
                state.pending_tool_arguments[tool_call_id] = arguments
                send_chunk_fn({
                    "chunk_type": "tool_start",
                    "tool": tool_name,
                    "arguments": arguments,
                })

            elif event_type == "tool_execution_end":
                tool_name = data.get("tool_name", "")
                tool_call_id = data.get("tool_call_id", tool_name)
                # Retrieve cached arguments from the matching tool_execution_start event
                arguments = state.pending_tool_arguments.pop(tool_call_id, data.get("arguments", {}))
                result = data.get("result", "")
                status = (
                    data.get("tool_display_status")
                    or data.get("tool_policy_status")
                    or data.get("status")
                    or "unknown"
                )
                execution_time = data.get("execution_time", 0)
                elapsed_str = f"{execution_time:.2f}s"

                # Serialise result to string if needed
                if not isinstance(result, str):
                    import json
                    try:
                        result = json.dumps(result, ensure_ascii=False)
                    except Exception:
                        result = str(result)

                tool_info = {
                    "name": tool_name,
                    "arguments": arguments,
                    "result": result,
                    "status": status,
                    "elapsed": elapsed_str,
                }
                if data.get("tool_attempt_skipped"):
                    tool_info["tool_attempt_skipped"] = True
                if data.get("tool_policy_status"):
                    tool_info["tool_policy_status"] = data.get("tool_policy_status")

                if state.pending_tool_results is not None:
                    state.pending_tool_results.append(tool_info)

            elif event_type == "turn_end":
                has_tool_calls = data.get("has_tool_calls", False)
                if has_tool_calls and state.pending_tool_results:
                    # Flush collected tool results as a single tool_calls chunk
                    send_chunk_fn({
                        "chunk_type": "tool_calls",
                        "tool_calls": state.pending_tool_results,
                    })
                    state.pending_tool_results = None
                    # Next content belongs to a new segment
                    state.segment_id += 1

            elif event_type == "llm_usage":
                state.llm_call_count += 1

        # Run the agent with our event callback ---------------------------
        logger.info(f"[ChatService] Starting agent run: session={session_id}, query={query[:80]}")

        from config import conf
        max_context_turns = conf().get("agent_max_context_turns", 20)

        # Get full system prompt with skills
        full_system_prompt = agent.get_full_system_prompt()

        # Create a copy of messages for this execution
        with agent.messages_lock:
            messages_copy = agent.messages.copy()
            original_length = len(agent.messages)

        from agent.protocol.agent_stream import AgentStreamExecutor
        workspace_root = conf().get("agent_workspace", "~/cow")
        tool_error_lesson_snapshot = self._collect_tool_error_lesson_snapshot(workspace_root)
        task_budget = resolve_agent_task_budget(query, conf())
        logger.info(
            "[ChatService] Using max_steps=%s task_budget_kind=%s",
            task_budget.max_steps,
            task_budget.kind,
        )

        executor = AgentStreamExecutor(
            agent=agent,
            model=agent.model,
            system_prompt=full_system_prompt,
            tools=agent.tools,
            max_turns=task_budget.max_steps,
            on_event=on_event,
            messages=messages_copy,
            max_context_turns=max_context_turns,
        )

        try:
            response = executor.run_stream(query)
        except Exception:
            # If executor cleared messages (context overflow), sync back
            if len(executor.messages) == 0:
                with agent.messages_lock:
                    agent.messages.clear()
                    logger.info("[ChatService] Cleared agent message history after executor recovery")
            raise

        # Sync executor messages back to agent (thread-safe).
        # The executor may have trimmed context, making its list shorter than
        # original_length. In that case we must replace entirely — just
        # appending would leave stale pre-trim messages in agent.messages
        # and cause the same trim to fire on every subsequent request.
        with agent.messages_lock:
            trimmed = len(executor.messages) < original_length
            if trimmed:
                # Context was trimmed: the executor appended the new user
                # query *before* trimming, so the new messages (user +
                # assistant + tools) sit at the tail of the trimmed list.
                # We cannot simply slice at original_length (it exceeds the
                # list length).  Instead, count how many messages the
                # executor added on top of the post-trim baseline.
                #
                # Timeline inside executor.run_stream:
                #   1. messages had `original_length` items
                #   2. append user query  → original_length + 1
                #   3. _trim_messages()   → some smaller number (includes the
                #      user query because it belongs to the last turn)
                #   4. LLM replies / tool calls appended
                #
                # The user query message is always the first message of the
                # last turn (it cannot be trimmed away), so we locate it to
                # find where "new" messages begin.
                new_start = original_length  # fallback
                for idx in range(len(executor.messages) - 1, -1, -1):
                    msg = executor.messages[idx]
                    if msg.get("role") == "user":
                        content = msg.get("content", [])
                        is_user_query = False
                        if isinstance(content, list):
                            has_text = any(
                                isinstance(b, dict) and b.get("type") == "text"
                                for b in content
                            )
                            has_tool_result = any(
                                isinstance(b, dict) and b.get("type") == "tool_result"
                                for b in content
                            )
                            is_user_query = has_text and not has_tool_result
                        elif isinstance(content, str):
                            is_user_query = True
                        if is_user_query:
                            new_start = idx
                            break
                new_messages = list(executor.messages[new_start:])
            else:
                new_messages = list(executor.messages[original_length:])
            agent.messages = list(executor.messages)

        # Persist new messages to SQLite so they survive restarts and
        # can be queried via the HISTORY interface.
        if new_messages:
            self._persist_messages(session_id, list(new_messages), channel_type)

        # Store executor reference for files_to_send access
        agent.stream_executor = executor

        # Execute post-process tools
        agent._execute_post_process_tools()
        self._schedule_post_task_self_evolution(
            agent,
            new_messages,
            response,
            workspace_root=workspace_root,
            task_is_development=task_is_development,
            process_turn_count=max(state.turn_number, state.llm_call_count),
            tool_error_lesson_count=self._count_tool_error_lesson_changes(
                tool_error_lesson_snapshot,
                workspace_root,
            ),
        )

        logger.info(f"[ChatService] Agent run completed: session={session_id}")
        maybe_check_capi_monthly_after_task(task_backend)



    @staticmethod
    def _persist_messages(session_id: str, new_messages: list, channel_type: str = ""):
        try:
            from config import conf
            if not conf().get("conversation_persistence", True):
                return
        except Exception:
            pass
        try:
            from agent.memory import get_conversation_store
            get_conversation_store().append_messages(
                session_id, new_messages, channel_type=channel_type
            )
        except Exception as e:
            logger.warning(
                f"[ChatService] Failed to persist messages for session={session_id}: {e}"
            )

    @staticmethod
    def _schedule_post_task_self_evolution(
        agent,
        new_messages: list,
        final_response: str,
        *,
        workspace_root: str,
        task_is_development: bool,
        process_turn_count: int,
        tool_error_lesson_count: int,
    ):
        try:
            from common.self_evolution import schedule_post_task_reflection

            schedule_post_task_reflection(
                model_adapter=getattr(agent, "model", None),
                new_messages=list(new_messages or []),
                final_response=final_response or "",
                workspace_root=workspace_root,
                task_is_development=bool(task_is_development),
                process_turn_count=int(process_turn_count or 0),
                tool_error_lesson_count=int(tool_error_lesson_count or 0),
            )
        except Exception as e:
            logger.debug(f"[SelfEvolution] Failed to schedule chat-service reflection: {e}")

    @staticmethod
    def _collect_tool_error_lesson_snapshot(workspace_root):
        try:
            from common.self_evolution import collect_tool_error_lesson_snapshot

            return collect_tool_error_lesson_snapshot(workspace_root)
        except Exception as e:
            logger.debug(f"[SelfEvolution] Failed to collect tool-error lesson snapshot: {e}")
            return None

    @staticmethod
    def _count_tool_error_lesson_changes(before_snapshot, workspace_root) -> int:
        try:
            from common.self_evolution import count_tool_error_lesson_changes

            return count_tool_error_lesson_changes(before_snapshot, workspace_root)
        except Exception as e:
            logger.debug(f"[SelfEvolution] Failed to count tool-error lesson changes: {e}")
            return 0


class _StreamState:
    """Mutable state shared between the event callback and the run method."""

    def __init__(self):
        self.segment_id: int = 0
        self.turn_number: int = 0
        self.llm_call_count: int = 0
        # None means we are not accumulating tool results right now.
        # A list means we are in the middle of a tool-execution phase.
        self.pending_tool_results: Optional[list] = None
        # Maps tool_call_id -> arguments captured from tool_execution_start,
        # so that tool_execution_end can attach the correct input args.
        self.pending_tool_arguments: dict = {}
