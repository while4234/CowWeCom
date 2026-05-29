import types
import unittest
from unittest.mock import patch

from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from channel.discord.discord_channel import (
    DiscordChannel,
    _build_grok_media_shortcut_query,
    _cow_cli_suggestions,
    _discord_command_name,
    _normalize_proxy_url,
    _normalise_cow_cli_suggestions,
    _split_slash_text,
)


def _singleton_class(factory):
    for cell in factory.__closure__ or []:
        value = cell.cell_contents
        if isinstance(value, type):
            return value
    raise AssertionError("singleton class not found")


def _discord_channel_instance():
    cls = _singleton_class(DiscordChannel)
    return object.__new__(cls)


class DiscordChannelTest(unittest.TestCase):
    def test_discord_command_name_normalizes_valid_slash_command(self):
        self.assertEqual(_discord_command_name("/grok-direct image -- prompt"), "grok-direct")
        self.assertEqual(_discord_command_name("/backend status"), "backend")
        self.assertEqual(_discord_command_name("/bad command"), "bad")
        self.assertEqual(_discord_command_name("/含中文"), "")

    def test_split_slash_text_preserves_baked_args(self):
        self.assertEqual(_split_slash_text("/grok-direct image"), ("grok-direct", "image"))
        self.assertEqual(_split_slash_text("backend status"), ("backend", "status"))

    def test_cow_cli_suggestions_excludes_cli_only_commands_and_drops_example_args(self):
        commands = _normalise_cow_cli_suggestions([
            {"cmd": "/status", "desc": "status"},
            {"cmd": "/restart", "desc": "restart"},
            {"cmd": "/grok-direct image -- prompt", "desc": "grok"},
            {"cmd": "/grok-direct video -- prompt", "desc": "grok video"},
        ])

        names = {item["name"] for item in commands}
        self.assertIn("status", names)
        self.assertIn("grok-direct", names)
        self.assertNotIn("restart", names)
        grok_command = next(item for item in commands if item["name"] == "grok-direct")
        self.assertEqual(grok_command["cmd"], "/grok-direct")

    def test_cow_cli_suggestions_has_safe_fallback_when_plugin_scan_context_is_absent(self):
        commands = _cow_cli_suggestions()

        self.assertIn("status", {item["name"] for item in commands})

    def test_grok_media_shortcuts_cover_image_video_and_image_to_video(self):
        self.assertEqual(
            _build_grok_media_shortcut_query("image", "a neon city"),
            "/grok-direct image -- a neon city",
        )
        self.assertEqual(
            _build_grok_media_shortcut_query("image", "make it a poster", "C:\\tmp\\ref.png"),
            "/grok-direct image -- make it a poster\n[image: C:\\tmp\\ref.png]",
        )
        self.assertEqual(
            _build_grok_media_shortcut_query("video", "a neon city timelapse"),
            "/grok-direct video -- a neon city timelapse",
        )
        self.assertEqual(
            _build_grok_media_shortcut_query("video", "animate softly", "C:\\tmp\\ref.png"),
            "/grok-direct video -- animate softly\n[image: C:\\tmp\\ref.png]",
        )

    def test_normalize_proxy_url_adds_default_scheme(self):
        self.assertEqual(_normalize_proxy_url("127.0.0.1:7897"), "http://127.0.0.1:7897")
        self.assertEqual(_normalize_proxy_url("http://127.0.0.1:7897"), "http://127.0.0.1:7897")
        self.assertEqual(_normalize_proxy_url(""), "")

    @patch("channel.discord.discord_channel._cow_cli_suggestions", return_value=[])
    def test_registers_image_to_image_shortcut_command(self, _suggestions):
        channel = _discord_channel_instance()
        channel.discord = types.SimpleNamespace(Interaction=object, Attachment=object)
        channel._registered_commands = []

        class FakeCommand:
            def __init__(self, name, description, callback):
                self.name = name
                self.description = description
                self.callback = callback

        class FakeAppCommands:
            Command = FakeCommand

            @staticmethod
            def describe(**_kwargs):
                return lambda callback: callback

            @staticmethod
            def rename(**_kwargs):
                return lambda callback: callback

        class FakeTree:
            def __init__(self):
                self.commands = []

            def add_command(self, command):
                self.commands.append(command)

        bot = types.SimpleNamespace(tree=FakeTree())
        channel.app_commands = FakeAppCommands

        channel._register_slash_commands(bot)

        names = {command.name for command in bot.tree.commands}
        self.assertIn("image-to-image", names)
        self.assertIn("image-to-image", channel._registered_commands)

    def test_allowed_actor_requires_configured_discord_admin(self):
        channel = _discord_channel_instance()
        channel.admin_user_id = "100"
        channel.guild_id = "200"
        channel.allowed_channel_ids = {"300"}

        self.assertTrue(channel._is_allowed_actor("100", "300", "200"))
        self.assertFalse(channel._is_allowed_actor("101", "300", "200"))
        self.assertFalse(channel._is_allowed_actor("100", "301", "200"))
        self.assertFalse(channel._is_allowed_actor("100", "300", "201"))

    def test_interaction_context_uses_discord_admin_actor_and_channel(self):
        channel = _discord_channel_instance()
        user = types.SimpleNamespace(id=100, display_name="Admin", name="admin")
        text_channel = types.SimpleNamespace(id=300, name="bot")
        guild = types.SimpleNamespace(id=200)
        interaction = types.SimpleNamespace(user=user, channel=text_channel, guild=guild)

        context = channel._build_interaction_context(interaction)

        self.assertEqual(context.type, ContextType.TEXT)
        self.assertEqual(context["channel_type"], "discord")
        self.assertEqual(context["receiver"], "300")
        self.assertEqual(context["actor_id"], "discord:100")
        self.assertEqual(context["actor_role"], "admin")
        self.assertEqual(context["_discord_channel_id"], "300")
        self.assertEqual(context["_discord_guild_id"], "200")

    @patch("channel.discord.discord_channel.PluginManager")
    def test_run_cow_cli_command_invokes_loaded_plugin(self, manager_cls):
        channel = _discord_channel_instance()
        context = channel._build_interaction_context(
            types.SimpleNamespace(
                user=types.SimpleNamespace(id=100, display_name="Admin", name="admin"),
                channel=types.SimpleNamespace(id=300, name="bot"),
                guild=types.SimpleNamespace(id=200),
            )
        )

        class FakePlugin:
            def on_handle_context(self, e_context):
                e_context["reply"] = Reply(ReplyType.TEXT, "ok")

        manager_cls.return_value.plugins.get.return_value = FakePlugin

        reply = channel._run_cow_cli_command("/status", context)

        self.assertEqual(reply.type, ReplyType.TEXT)
        self.assertEqual(reply.content, "ok")

    @patch("channel.discord.discord_channel.asyncio.run_coroutine_threadsafe")
    def test_send_schedules_reply_on_discord_loop(self, run_threadsafe):
        channel = _discord_channel_instance()
        channel.bot = object()
        channel.loop = types.SimpleNamespace(is_closed=lambda: False)
        channel.discord = types.SimpleNamespace(File=lambda *args, **kwargs: object())

        channel._send_reply_async = lambda reply, context: "coroutine"
        run_threadsafe.return_value.result.return_value = True

        result = channel.send(Reply(ReplyType.TEXT, "hello"), {})

        self.assertTrue(result)
        self.assertTrue(run_threadsafe.called)


if __name__ == "__main__":
    unittest.main()
