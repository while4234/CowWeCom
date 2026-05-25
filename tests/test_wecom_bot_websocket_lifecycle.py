import unittest
from unittest.mock import patch

from channel.wecom_bot.wecom_bot_channel import WecomBotChannel


class FakeTimer:
    def __init__(self, timeout, callback):
        self.timeout = timeout
        self.callback = callback
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.callback()


class CloseRecordingWebSocket:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


class TestWecomBotWebSocketLifecycle(unittest.TestCase):
    def setUp(self):
        self.channel = WecomBotChannel()
        self.channel._ws = None
        self.channel._connected = False
        self.channel._subscribe_timeout_timer = None
        self.channel._stop_event.clear()

    def tearDown(self):
        self.channel._cancel_subscribe_timeout()
        self.channel._ws = None
        self.channel._connected = False
        self.channel._stop_event.clear()

    def test_subscribe_timeout_closes_unacked_current_websocket(self):
        ws = CloseRecordingWebSocket()
        self.channel._ws = ws

        with patch("channel.wecom_bot.wecom_bot_channel.threading.Timer", FakeTimer):
            self.channel._arm_subscribe_timeout(ws, timeout=0.25)
            timer = self.channel._subscribe_timeout_timer

            self.assertTrue(timer.started)
            self.assertEqual(timer.timeout, 0.25)

            timer.fire()

        self.assertEqual(ws.close_calls, 1)

    def test_subscribe_success_cancels_timeout_and_starts_heartbeat(self):
        ws = CloseRecordingWebSocket()
        self.channel._ws = ws

        with patch("channel.wecom_bot.wecom_bot_channel.threading.Timer", FakeTimer):
            self.channel._arm_subscribe_timeout(ws, timeout=0.25)
            timer = self.channel._subscribe_timeout_timer

            with patch.object(self.channel, "_start_heartbeat") as start_heartbeat, patch.object(
                self.channel, "report_startup_success"
            ):
                self.channel._handle_ws_message({"errcode": 0, "headers": {"req_id": "ack-1"}})

            self.assertTrue(self.channel._connected)
            self.assertTrue(timer.cancelled)
            self.assertIsNone(self.channel._subscribe_timeout_timer)
            start_heartbeat.assert_called_once_with()

            timer.fire()

        self.assertEqual(ws.close_calls, 0)

    def test_subscribe_timeout_ignores_stale_or_stopped_socket(self):
        old_ws = CloseRecordingWebSocket()
        new_ws = CloseRecordingWebSocket()
        self.channel._ws = new_ws

        with patch("channel.wecom_bot.wecom_bot_channel.threading.Timer", FakeTimer):
            self.channel._arm_subscribe_timeout(old_ws, timeout=0.25)
            self.channel._subscribe_timeout_timer.fire()

            self.channel._ws = old_ws
            self.channel._stop_event.set()
            self.channel._arm_subscribe_timeout(old_ws, timeout=0.25)
            self.channel._subscribe_timeout_timer.fire()

        self.assertEqual(old_ws.close_calls, 0)
        self.assertEqual(new_ws.close_calls, 0)


if __name__ == "__main__":
    unittest.main()
