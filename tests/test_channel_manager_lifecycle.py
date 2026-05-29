import threading
import unittest
from unittest.mock import patch

import app


class FakeChannel:
    def __init__(self, name):
        self.name = name
        self.cloud_mode = False
        self.startup_calls = 0
        self.stop_calls = 0

    def startup(self):
        self.startup_calls += 1

    def stop(self):
        self.stop_calls += 1


class StuckThread:
    def is_alive(self):
        return True

    def join(self, timeout=None):
        return None


class ChannelManagerLifecycleTest(unittest.TestCase):
    def test_start_creates_independent_channel_instances(self):
        manager = app.ChannelManager()
        created = []

        def create_channel(name):
            channel = FakeChannel(name)
            created.append(channel)
            return channel

        with patch.object(app.channel_factory, "create_channel", side_effect=create_channel), \
                patch.object(app.time, "sleep", return_value=None):
            manager.start(["discord", "wecom_bot"], first_start=False)

        self.assertEqual([channel.name for channel in created], ["discord", "wecom_bot"])
        self.assertIs(manager.get_channel("discord"), created[0])
        self.assertIs(manager.get_channel("wecom_bot"), created[1])
        self.assertIsNot(manager.get_channel("discord"), manager.get_channel("wecom_bot"))

    def test_start_ignores_duplicate_channel_names(self):
        manager = app.ChannelManager()
        created = []

        def create_channel(name):
            channel = FakeChannel(name)
            created.append(channel)
            return channel

        with patch.object(app.channel_factory, "create_channel", side_effect=create_channel), \
                patch.object(app.time, "sleep", return_value=None):
            manager.start(["discord", "wecom_bot", "wecom_bot"], first_start=False)

        self.assertEqual([channel.name for channel in created], ["discord", "wecom_bot"])
        self.assertIs(manager.get_channel("wecom_bot"), created[1])

    def test_start_skips_already_running_channel_name(self):
        manager = app.ChannelManager()
        running_wecom = FakeChannel("wecom_bot")
        manager._channels = {"wecom_bot": running_wecom}

        with patch.object(app.channel_factory, "create_channel") as create_channel, \
                patch.object(app.time, "sleep", return_value=None):
            manager.start(["wecom_bot"], first_start=False)

        create_channel.assert_not_called()
        self.assertIs(manager.get_channel("wecom_bot"), running_wecom)

    def test_restart_only_stops_target_channel(self):
        manager = app.ChannelManager()
        discord = FakeChannel("discord")
        wecom = FakeChannel("wecom_bot")
        manager._channels = {"discord": discord, "wecom_bot": wecom}

        with patch.object(app.channel_factory, "create_channel", return_value=FakeChannel("wecom_bot")), \
                patch.object(app, "_clear_singleton_cache"), \
                patch.object(app.time, "sleep", return_value=None):
            manager.restart("wecom_bot")

        self.assertEqual(discord.stop_calls, 0)
        self.assertEqual(wecom.stop_calls, 1)
        self.assertIs(manager.get_channel("discord"), discord)

    def test_concurrent_same_channel_restarts_are_coalesced(self):
        manager = app.ChannelManager()
        old_wecom = FakeChannel("wecom_bot")
        manager._channels = {"wecom_bot": old_wecom}
        sleep_entered = threading.Event()
        release_sleep = threading.Event()

        def blocking_sleep(_seconds):
            sleep_entered.set()
            release_sleep.wait(timeout=2)

        with patch.object(app.channel_factory, "create_channel", return_value=FakeChannel("wecom_bot")) as create_channel, \
                patch.object(app, "_clear_singleton_cache") as clear_cache, \
                patch.object(app.time, "sleep", side_effect=blocking_sleep):
            first = threading.Thread(target=manager.restart, args=("wecom_bot",))
            first.start()
            self.assertTrue(sleep_entered.wait(timeout=2))

            duplicates = [
                threading.Thread(target=manager.restart, args=("wecom_bot",))
                for _ in range(3)
            ]
            for thread in duplicates:
                thread.start()
            for thread in duplicates:
                thread.join(timeout=2)

            release_sleep.set()
            first.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertEqual(old_wecom.stop_calls, 1)
        self.assertEqual(create_channel.call_count, 1)
        self.assertEqual(clear_cache.call_count, 1)

    def test_restart_aborts_when_old_thread_survives_stop(self):
        manager = app.ChannelManager()
        old_wecom = FakeChannel("wecom_bot")
        manager._channels = {"wecom_bot": old_wecom}
        manager._threads = {"wecom_bot": StuckThread()}

        with patch.object(app.channel_factory, "create_channel") as create_channel, \
                patch.object(app, "_clear_singleton_cache") as clear_cache, \
                patch.object(app.time, "sleep", return_value=None):
            manager.restart("wecom_bot")

        self.assertEqual(old_wecom.stop_calls, 1)
        create_channel.assert_not_called()
        clear_cache.assert_not_called()
        self.assertNotIn("wecom_bot", manager._restarting_channels)


if __name__ == "__main__":
    unittest.main()
