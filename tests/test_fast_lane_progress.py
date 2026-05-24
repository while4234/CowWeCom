import threading
import unittest
from unittest.mock import patch

from bridge.agent_event_handler import AgentEventHandler
from bridge.bridge import Bridge
from bridge.context import Context, ContextType
from bridge.reply import ReplyType
from channel.chat_channel import ChatChannel
from common.agent_task_runtime import SessionRuntime, TaskPolicy


class ImmediatePool:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        return fn(*args, **kwargs)


class FakeChannel:
    def __init__(self):
        self.sent = []

    def _send(self, reply, context):
        self.sent.append(reply.content)


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

    def test_silence_notice_is_thresholded_while_task_runs(self):
        runtime = SessionRuntime()
        runtime.start_task("long task")
        runtime.last_visible_output_at = 100.0

        with patch("common.agent_task_runtime.monotonic", side_effect=[144.0, 145.0, 200.0, 265.0]):
            self.assertIsNone(runtime.claim_silence_notice(first_notice_seconds=45.0, repeat_notice_seconds=120.0))
            first = runtime.claim_silence_notice(first_notice_seconds=45.0, repeat_notice_seconds=120.0)
            self.assertIsNotNone(first)
            self.assertIn("还在处理", first)
            self.assertIsNone(runtime.claim_silence_notice(first_notice_seconds=45.0, repeat_notice_seconds=120.0))
            repeat = runtime.claim_silence_notice(first_notice_seconds=45.0, repeat_notice_seconds=120.0)
            self.assertIsNotNone(repeat)

    def test_visible_output_resets_silence_notice_timer(self):
        runtime = SessionRuntime()
        runtime.start_task("long task")
        runtime.last_visible_output_at = 100.0

        with patch("common.agent_task_runtime.monotonic", side_effect=[145.0, 160.0, 204.0, 205.0]):
            self.assertIsNotNone(runtime.claim_silence_notice(first_notice_seconds=45.0, repeat_notice_seconds=120.0))
            runtime.mark_visible_output("message_update")
            self.assertIsNone(runtime.claim_silence_notice(first_notice_seconds=45.0, repeat_notice_seconds=120.0))
            self.assertIsNotNone(runtime.claim_silence_notice(first_notice_seconds=45.0, repeat_notice_seconds=120.0))

    def test_silence_notice_is_suppressed_after_finish_or_cancel(self):
        finished_runtime = SessionRuntime()
        finished_runtime.start_task("long task")
        finished_runtime.finish_task("done")
        self.assertIsNone(finished_runtime.claim_silence_notice(first_notice_seconds=0.0))

        cancelled_runtime = SessionRuntime()
        cancelled_runtime.start_task("long task")
        self.assertTrue(cancelled_runtime.cancel_running())
        self.assertIsNone(cancelled_runtime.claim_silence_notice(first_notice_seconds=0.0))

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

    def test_status_text_sanitizes_summary_preview_and_error(self):
        runtime = SessionRuntime()
        runtime.start_task(r"read C:\secret\plan.txt token=summary-secret")
        runtime.update_progress("message_update", {"delta": r"opened /home/user/private/file token=visible-secret"})
        runtime.update_progress("error", {"error": r"failed with api_key=error-secret in C:\secret\trace.log"})

        text = runtime.status_text()

        self.assertNotIn("C:\\secret", text)
        self.assertNotIn("/home/user/private", text)
        self.assertNotIn("summary-secret", text)
        self.assertNotIn("visible-secret", text)
        self.assertNotIn("error-secret", text)

        failure_text = runtime.failure_notice_text("error")
        self.assertNotIn("C:\\secret", failure_text)
        self.assertNotIn("error-secret", failure_text)

    def test_turn_end_at_max_steps_sends_pre_summary_notice(self):
        runtime = SessionRuntime()
        runtime.start_task("long task", max_turns=2)
        fake_channel = FakeChannel()
        context = make_text_context("long task")
        context["channel"] = fake_channel
        context["_session_runtime"] = runtime
        handler = AgentEventHandler(context=context)

        handler.handle_event({"type": "turn_start", "data": {"turn": 2}})
        handler.handle_event({"type": "turn_end", "data": {"turn": 2, "has_tool_calls": True}})

        self.assertEqual(len(fake_channel.sent), 1)
        self.assertIn("单次尝试", fake_channel.sent[0])

    def test_non_stream_message_update_does_not_reset_visible_output_until_send(self):
        runtime = SessionRuntime()
        runtime.start_task("long task")
        runtime.last_visible_output_at = 100.0
        fake_channel = FakeChannel()
        context = make_text_context("long task")
        context["channel"] = fake_channel
        context["_session_runtime"] = runtime
        handler = AgentEventHandler(context=context)

        with patch("common.agent_task_runtime.monotonic", side_effect=[150.0]):
            handler.handle_event({"type": "message_update", "data": {"delta": "working"}})

        self.assertEqual(runtime.last_visible_output_at, 100.0)

        with patch("common.agent_task_runtime.monotonic", side_effect=[151.0, 151.0]):
            handler.handle_event({"type": "message_end", "data": {"tool_calls": [{"name": "read"}]}})

        self.assertEqual(runtime.last_visible_output_at, 151.0)
        self.assertEqual(fake_channel.sent, ["working"])

    def test_agent_bridge_error_falls_back_to_friendly_reply(self):
        bridge = Bridge()

        def raise_agent_error():
            raise RuntimeError(r"agent crashed with token=bridge-secret at C:\secret\bridge.log")

        with patch.object(bridge, "get_agent_bridge", side_effect=raise_agent_error):
            reply = bridge.fetch_agent_reply("hello", make_text_context("hello"))

        self.assertEqual(reply.type, ReplyType.ERROR)
        self.assertIn("这轮处理没有稳定完成", reply.content)
        self.assertNotIn("Agent error", reply.content)
        self.assertNotIn("bridge-secret", reply.content)
        self.assertNotIn("C:\\secret", reply.content)

    def test_cancel_marks_progress_without_clearing_pending(self):
        runtime = SessionRuntime()
        token = runtime.start_task("long task")
        runtime.queue.put(make_text_context("queued"))

        self.assertTrue(runtime.cancel_running())

        self.assertTrue(token.is_cancelled())
        self.assertEqual(runtime.queue.qsize(), 1)
        self.assertEqual(runtime.progress.phase, "cancel_requested")
        self.assertIn("取消", runtime.status_text())

    def test_cancel_control_preserves_pending_messages(self):
        channel = make_channel_without_thread()
        sent = []
        channel._send_plain_text = lambda context, text, track_visible=True: sent.append(text)
        runtime = SessionRuntime()
        token = runtime.start_task("long task")
        runtime.queue.put(make_text_context("queued"))
        channel.sessions["wechat-session"] = runtime

        channel._handle_control_cancel(make_text_context("/取消"), runtime)

        self.assertTrue(token.is_cancelled())
        self.assertEqual(runtime.queue.qsize(), 1)
        self.assertIn("队列中还有 1 条消息", sent[0])
        self.assertNotIn("已清空排队消息", sent[0])

    def test_skip_control_clears_pending_messages(self):
        channel = make_channel_without_thread()
        sent = []
        channel._send_plain_text = lambda context, text, track_visible=True: sent.append(text)
        runtime = SessionRuntime()
        runtime.queue.put(make_text_context("queued"))

        channel._handle_control_skip(make_text_context("/跳过"), runtime)

        self.assertEqual(runtime.queue.qsize(), 0)
        self.assertIn("已清空排队消息 1 条", sent[0])


if __name__ == "__main__":
    unittest.main()
