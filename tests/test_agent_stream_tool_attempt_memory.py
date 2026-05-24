import tempfile
import unittest
from unittest.mock import patch

from agent.protocol.agent_stream import AgentStreamExecutor
from agent.protocol.models import LLMModel
from agent.tools.base_tool import BaseTool, ToolResult
from common.tool_attempt_memory import ToolAttemptMemory


class FakeAgent:
    memory_manager = None
    skill_manager = None
    max_context_tokens = None

    def _estimate_message_tokens(self, msg):
        return len(str(msg))

    def _get_model_context_window(self):
        return 100000


class ReadTool(BaseTool):
    name = "read"
    description = "read"
    params = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
    }

    def __init__(self, result="full read result"):
        super().__init__()
        self.calls = 0
        self.result = result

    def execute(self, params):
        self.calls += 1
        return ToolResult.success(self.result)


class SchedulerTool(BaseTool):
    name = "scheduler"
    description = "scheduler"
    params = {
        "type": "object",
        "properties": {"action": {"type": "string"}, "task_id": {"type": "string"}},
    }

    def __init__(self):
        super().__init__()
        self.calls = 0

    def execute(self, params):
        self.calls += 1
        return ToolResult.success("scheduled")


class GuardedBashTool(BaseTool):
    name = "bash"
    description = "bash"
    params = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
    }

    def __init__(self):
        super().__init__()
        self.calls = 0

    def execute(self, params):
        self.calls += 1
        return ToolResult.skipped(
            {
                "output": "Use a temporary .py file instead.",
                "exit_code": 0,
                "details": {"self_evolution_guard": True},
            },
            {
                "tool_attempt_skipped": True,
                "tool_failure_class": "shell_dialect",
                "skip_reason": "self_evolution_guard",
            },
        )


class RepeatedReadModel(LLMModel):
    def __init__(self):
        super().__init__(model="test-model")
        self.calls = 0

    def call_stream(self, request):
        self.calls += 1
        if self.calls in (1, 2):
            yield {
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": f"call_{self.calls}",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": '{"path":"doc.md"}',
                            },
                        }]
                    }
                }]
            }
            yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
            return

        yield {"choices": [{"delta": {"content": "done"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}


class OneReadThenDoneModel(LLMModel):
    def __init__(self):
        super().__init__(model="test-model")
        self.calls = 0

    def call_stream(self, request):
        self.calls += 1
        if self.calls == 1:
            yield {
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_skip",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": '{"path":"missing.md"}',
                            },
                        }]
                    }
                }]
            }
            yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
            return

        yield {"choices": [{"delta": {"content": "done"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}


class OneSchedulerThenDoneModel(LLMModel):
    def __init__(self):
        super().__init__(model="test-model")
        self.calls = 0

    def call_stream(self, request):
        self.calls += 1
        if self.calls == 1:
            yield {
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_scheduler",
                            "type": "function",
                            "function": {
                                "name": "scheduler",
                                "arguments": '{"action":"teleport","task_id":"new-id"}',
                            },
                        }]
                    }
                }]
            }
            yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
            return

        yield {"choices": [{"delta": {"content": "done"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}


class OneBashThenDoneModel(LLMModel):
    def __init__(self):
        super().__init__(model="test-model")
        self.calls = 0

    def call_stream(self, request):
        self.calls += 1
        if self.calls == 1:
            yield {
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_bash",
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "arguments": '{"command":"python -c \\"print(1)\\\\nprint(2)\\""}',
                            },
                        }]
                    }
                }]
            }
            yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
            return

        yield {"choices": [{"delta": {"content": "done"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}


class TestAgentStreamToolAttemptMemory(unittest.TestCase):
    def test_reuses_readonly_tool_result_only_within_same_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tool = ReadTool(result="source text")
            executor = AgentStreamExecutor(
                agent=FakeAgent(),
                model=RepeatedReadModel(),
                system_prompt="system",
                tools=[tool],
                messages=[],
            )
            executor.tool_attempt_memory = ToolAttemptMemory(tmpdir)

            response = executor.run_stream("read twice")

            self.assertEqual(response, "done")
            self.assertEqual(tool.calls, 1)
            self.assertEqual(executor._tool_duplicate_success_count, 1)
            tool_result_text = "\n".join(
                block.get("content", "")
                for message in executor.messages
                for block in (message.get("content") or [])
                if isinstance(block, dict) and block.get("type") == "tool_result"
            )
            self.assertIn("Repeated read-only tool result omitted", tool_result_text)

    def test_persisted_non_retryable_failure_short_circuits_before_execute(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = ToolAttemptMemory(tmpdir)
            args = {"path": "missing.md"}
            for _ in range(3):
                memory.record_attempt("read", args, "error", "File not found")

            tool = ReadTool()
            executor = AgentStreamExecutor(
                agent=FakeAgent(),
                model=OneReadThenDoneModel(),
                system_prompt="system",
                tools=[tool],
                messages=[],
            )
            executor.tool_attempt_memory = memory

            response = executor.run_stream("read missing")

            self.assertEqual(response, "done")
            self.assertEqual(tool.calls, 0)
            self.assertEqual(executor._tool_memory_rule_hits, 1)
            self.assertEqual(executor._tool_skip_count, 1)

    def test_persisted_policy_shape_short_circuits_before_execute(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = ToolAttemptMemory(tmpdir)
            for index in range(3):
                memory.record_attempt(
                    "scheduler",
                    {"action": "teleport", "task_id": f"old-{index}"},
                    "error",
                    "Unknown action: teleport",
                )

            tool = SchedulerTool()
            executor = AgentStreamExecutor(
                agent=FakeAgent(),
                model=OneSchedulerThenDoneModel(),
                system_prompt="system",
                tools=[tool],
                messages=[],
            )
            executor.tool_attempt_memory = memory

            response = executor.run_stream("bad scheduler action")

            self.assertEqual(response, "done")
            self.assertEqual(tool.calls, 0)
            self.assertEqual(executor._tool_memory_rule_hits, 1)

    def test_policy_skip_turn_refunds_max_turn_budget(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = ToolAttemptMemory(tmpdir)
            for index in range(3):
                memory.record_attempt(
                    "scheduler",
                    {"action": "teleport", "task_id": f"old-{index}"},
                    "error",
                    "Unknown action: teleport",
                )

            tool = SchedulerTool()
            executor = AgentStreamExecutor(
                agent=FakeAgent(),
                model=OneSchedulerThenDoneModel(),
                system_prompt="system",
                tools=[tool],
                messages=[],
                max_turns=1,
            )
            executor.tool_attempt_memory = memory

            response = executor.run_stream("bad scheduler action")

            self.assertEqual(response, "done")
            self.assertEqual(tool.calls, 0)
            self.assertEqual(executor._tool_policy_skip_turn_credit, 1)

    def test_self_evolution_guard_is_reported_as_guarded_skip(self):
        events = []
        tool = GuardedBashTool()
        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=OneBashThenDoneModel(),
            system_prompt="system",
            tools=[tool],
            messages=[],
            max_turns=1,
            on_event=events.append,
        )

        response = executor.run_stream("run fragile command")

        self.assertEqual(response, "done")
        self.assertEqual(tool.calls, 1)
        self.assertEqual(executor._tool_policy_skip_turn_credit, 1)
        self.assertEqual(executor._tool_skip_count, 1)
        self.assertEqual(executor._tool_attempt_error_count, 0)
        end_events = [event for event in events if event["type"] == "tool_execution_end"]
        self.assertEqual(end_events[-1]["data"]["status"], "skipped")
        self.assertEqual(end_events[-1]["data"]["tool_policy_status"], "guarded")
        self.assertTrue(end_events[-1]["data"]["tool_attempt_skipped"])

    def test_self_evolution_guidance_is_request_context_not_system_prompt(self):
        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=RepeatedReadModel(),
            system_prompt="system",
            tools=[],
            messages=[],
        )

        with patch(
            "common.self_evolution.get_active_prompt_guidance",
            return_value=["Use set \"PYTHONUTF8=1\" before Python commands."],
        ):
            context = executor._build_self_evolution_context_text()

        self.assertIn("Mandatory execution policy for this request", context)
        self.assertIn('set "PYTHONUTF8=1"', context)

    def test_request_context_tracks_self_evolution_guidance_separately(self):
        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=RepeatedReadModel(),
            system_prompt="system",
            tools=[],
            messages=[],
        )

        with patch("common.self_evolution.get_active_prompt_guidance", return_value=["Use supported actions."]):
            context = executor._build_request_context_text("schedule it")

        self.assertIn("Use supported actions.", context)
        self.assertGreater(executor._request_self_evolution_context_chars, 0)
        self.assertTrue(executor._request_self_evolution_context_hash)
        self.assertEqual(executor.system_prompt, "system")

    def test_stable_self_evolution_context_precedes_volatile_runtime_context(self):
        agent = FakeAgent()
        agent.runtime_info = {
            "_get_current_time": lambda: {
                "time": "10:00",
                "weekday": "Sunday",
                "timezone": "UTC",
            }
        }
        executor = AgentStreamExecutor(
            agent=agent,
            model=RepeatedReadModel(),
            system_prompt="system",
            tools=[],
            messages=[],
        )

        with patch("common.self_evolution.get_active_prompt_guidance", return_value=["Use supported actions."]):
            context = executor._build_request_context_text("schedule it")

        self.assertLess(
            context.index("Use supported actions."),
            context.index("current time is 10:00"),
        )

    def test_prepare_messages_injects_guidance_into_request_copy_only(self):
        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=RepeatedReadModel(),
            system_prompt="system",
            tools=[],
            messages=[],
        )
        executor.messages = [{"role": "user", "content": [{"type": "text", "text": "schedule it"}]}]

        with patch("common.self_evolution.get_active_prompt_guidance", return_value=["Use supported actions."]):
            executor._request_runtime_context = executor._build_request_context_text("schedule it")
            prepared = executor._prepare_messages()
            metadata = executor._build_cache_shape_metadata(prepared, [], [])

        self.assertIn("Use supported actions.", prepared[0]["content"][0]["text"])
        self.assertEqual(executor.messages[0]["content"][0]["text"], "schedule it")
        self.assertGreater(metadata["self_evolution_context_chars"], 0)
        self.assertTrue(metadata["self_evolution_context_hash"])

    def test_compacts_old_tool_results_in_request_copy_only(self):
        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=RepeatedReadModel(),
            system_prompt="system",
            tools=[],
            messages=[],
        )
        old_result = "a" * 80
        recent_result = "b" * 80
        executor.messages = [
            {"role": "user", "content": [{"type": "text", "text": "old question"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "old", "content": old_result}]},
            {"role": "assistant", "content": [{"type": "text", "text": "summary"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "recent", "content": recent_result}]},
            {"role": "user", "content": [{"type": "text", "text": "current question"}]},
        ]
        executor._request_tool_result_compaction_settings = lambda: (100, 1, 10)

        prepared = executor._prepare_messages()

        self.assertEqual(executor.messages[1]["content"][0]["content"], old_result)
        self.assertIn("Tool result compacted for cache efficiency", prepared[1]["content"][0]["content"])
        self.assertEqual(prepared[3]["content"][0]["content"], recent_result)
        self.assertEqual(executor._request_tool_result_compacted_count, 1)


if __name__ == "__main__":
    unittest.main()
