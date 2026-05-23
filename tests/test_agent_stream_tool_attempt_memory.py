import tempfile
import unittest

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
