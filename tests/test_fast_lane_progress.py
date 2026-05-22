import threading
import unittest
from unittest.mock import patch

from bridge.agent_event_handler import AgentEventHandler
from bridge.context import Context, ContextType
from channel.chat_channel import ChatChannel
from common.agent_task_runtime import SessionRuntime, TaskPolicy


class ImmediatePool:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        return fn(*args, **kwargs)


def make_text_context(content):
    return Context(
        ContextType.TEXT,
        content,
        {
            "session_id": "wechat-session",
            "receiver": "wechat-session",
            "channel_type": "weixin",
        },
    )


def make_channel_without_thread():
    channel = ChatChannel.__new__(ChatChannel)
    channel.futures = {}
    channel.sessions = {}
    channel.lock = threading.Lock()
    return channel


class TestFastLaneProgress(unittest.TestCase):
    def test_progress_queries_are_classified_as_control(self):
        channel = make_channel_without_thread()
        runtime = SessionRuntime()
        runtime.start_task("long task", max_turns=15)

        for text in ("/状态", "/q", "/q 进展", "/q进展", "/q 到哪了", "/q status"):
            policy, payload = channel._classify_fast_lane(make_text_context(text), runtime)
            self.assertEqual(policy, TaskPolicy.CONTROL_PROGRESS)
            self.assertFalse(payload.get("include_eta_note", False))

        policy, payload = channel._classify_fast_lane(make_text_context("/q 还要多久"), runtime)
        self.assertEqual(policy, TaskPolicy.CONTROL_PROGRESS)
        self.assertTrue(payload["include_eta_note"])

    def test_q_without_running_task_returns_help(self):
        channel = make_channel_without_thread()
        policy, payload = channel._classify_fast_lane(make_text_context("/q"), SessionRuntime())

        self.assertEqual(policy, TaskPolicy.QUICK_REPLY)
        self.assertTrue(payload["help"])

    def test_quick_reply_tool_intent_is_refused(self):
        channel = make_channel_without_thread()
        policy, payload = channel._classify_fast_lane(
            make_text_context("/q 查看项目文件"),
            SessionRuntime(),
        )

        self.assertEqual(policy, TaskPolicy.QUICK_REPLY)
        self.assertTrue(payload["refuse"])

    def test_quick_reply_plain_question_is_quick(self):
        channel = make_channel_without_thread()
        policy, payload = channel._classify_fast_lane(
            make_text_context("/q 只回复 pong"),
            SessionRuntime(),
        )

        self.assertEqual(policy, TaskPolicy.QUICK_REPLY)
        self.assertEqual(payload["query"], "只回复 pong")

    def test_progress_command_does_not_enter_normal_queue(self):
        channel = make_channel_without_thread()
        sent = []
        channel._handle_control_progress = lambda context, runtime, include_eta_note=False: sent.append(
            runtime.status_text(include_eta_note=include_eta_note)
        )
        pool = ImmediatePool()

        with patch("channel.chat_channel.control_pool", pool):
            channel.produce(make_text_context("/q 进展"))

        runtime = channel.sessions["wechat-session"]
        self.assertEqual(runtime.queue.qsize(), 0)
        self.assertEqual(len(pool.calls), 1)
        self.assertIn("当前没有运行中的任务", sent[0])

    def test_agent_events_update_progress_snapshot(self):
        runtime = SessionRuntime()
        runtime.start_task("analyze latency", max_turns=15)
        context = make_text_context("analyze latency")
        context["_session_runtime"] = runtime
        handler = AgentEventHandler(context=context)

        handler.handle_event({"type": "turn_start", "data": {"turn": 3}})
        handler.handle_event({"type": "message_update", "data": {"delta": r"读取 C:\secret\file.txt token=abc"}})
        handler.handle_event({"type": "tool_execution_start", "data": {"tool_name": "read_file"}})
        handler.handle_event({"type": "tool_execution_end", "data": {"tool_name": "read_file", "status": "success"}})
        handler.handle_event({"type": "llm_usage", "data": {"usage": {"prompt_tokens": 100, "cached_tokens": 80}}})

        progress = runtime.progress
        self.assertEqual(progress.turn, 3)
        self.assertEqual(progress.last_tool_name, "read_file")
        self.assertEqual(progress.last_tool_status, "success")
        self.assertEqual(progress.tool_call_count, 1)
        self.assertEqual(progress.llm_call_count, 1)
        self.assertNotIn("C:\\secret", progress.last_visible_preview)
        self.assertNotIn("abc", progress.last_visible_preview)

    def test_cancel_marks_progress_and_clears_pending(self):
        runtime = SessionRuntime()
        token = runtime.start_task("long task")
        runtime.queue.put(make_text_context("queued"))

        self.assertTrue(runtime.cancel_running())
        cleared = runtime.clear_pending()

        self.assertTrue(token.is_cancelled())
        self.assertEqual(cleared, 1)
        self.assertEqual(runtime.progress.phase, "cancel_requested")
        self.assertIn("取消", runtime.status_text())


if __name__ == "__main__":
    unittest.main()
