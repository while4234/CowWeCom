import asyncio
import types
import unittest
from unittest.mock import patch

from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from channel.discord.discord_channel import (
    DiscordChannel,
    GROK_DIRECT_GEN_IMAGE_COMMAND,
    GROK_DIRECT_GEN_VIDEO_COMMAND,
    GROK_GEN_IMAGE_COMMAND,
    GROK_GEN_VIDEO_COMMAND,
    GROK_IMAGE_OPTION_NAMES,
    GROK_MEDIA_MODE_NORMAL,
    GROK_MEDIA_MODE_REAL,
    GROK_MEDIA_REAL_PROMPT,
    GROK_VIDEO_DEFAULT_DURATION,
    GROK_VIDEO_DEFAULT_RESOLUTION,
    PROJECT_DISCORD_COMMANDS,
    _apply_grok_media_mode_prompt,
    _build_direct_grok_image_args,
    _build_enhanced_grok_image_args,
    _build_grok_video_job_args,
    _build_grok_direct_image_query,
    _build_grok_direct_video_query,
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


async def _async_noop():
    return None


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
            {"cmd": "/backend grok", "desc": "backend"},
            {"cmd": "/restart", "desc": "restart"},
            {"cmd": "/codex-app", "desc": "unrelated"},
            {"cmd": "/grok-direct image -- prompt", "desc": "grok"},
            {"cmd": "/grok-direct video -- prompt", "desc": "grok video"},
        ])

        names = {item["name"] for item in commands}
        self.assertIn("status", names)
        self.assertIn("backend", names)
        self.assertNotIn("restart", names)
        self.assertNotIn("codex-app", names)
        self.assertNotIn("grok-direct", names)
        backend_command = next(item for item in commands if item["name"] == "backend")
        self.assertEqual(backend_command["cmd"], "/backend")

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
        self.assertEqual(
            _build_grok_direct_image_query("studio portrait", "C:\\tmp\\ref.png", quality="quality"),
            "/grok-direct image --quality quality -- studio portrait\n[image: C:\\tmp\\ref.png]",
        )
        self.assertEqual(
            _build_grok_direct_video_query("city timelapse", duration="10s", resolution="720p"),
            "/grok-direct video --resolution 720p --duration 10s -- city timelapse",
        )
        self.assertEqual(
            _build_grok_direct_video_query("city timelapse"),
            "/grok-direct video --resolution 480p --duration 10s -- city timelapse",
        )

    def test_grok_media_prompt_mode_defaults_to_real_and_allows_normal(self):
        self.assertEqual(
            _apply_grok_media_mode_prompt("city timelapse"),
            f"city timelapse\n{GROK_MEDIA_REAL_PROMPT}",
        )
        self.assertEqual(
            _apply_grok_media_mode_prompt("city timelapse", GROK_MEDIA_MODE_NORMAL),
            "city timelapse",
        )
        self.assertEqual(
            _apply_grok_media_mode_prompt(f"city timelapse\n{GROK_MEDIA_REAL_PROMPT}", GROK_MEDIA_MODE_REAL),
            f"city timelapse\n{GROK_MEDIA_REAL_PROMPT}",
        )

    def test_enhanced_grok_image_args_keep_prompt_enhancement_enabled(self):
        args = _build_enhanced_grok_image_args("make it cinematic", "C:\\tmp\\ref.png", quality="quality")

        self.assertEqual(args["prompt"], "make it cinematic")
        self.assertEqual(args["runtime"], "grok")
        self.assertEqual(args["quality"], "quality")
        self.assertTrue(args["prompt_enhancement"])
        self.assertEqual(args["image_url"], "C:\\tmp\\ref.png")

    def test_discord_grok_job_args_distinguish_text_and_reference_modes(self):
        enhanced_image_args = _build_enhanced_grok_image_args("make a poster", quality="speed")
        self.assertTrue(enhanced_image_args["prompt_enhancement"])
        self.assertNotIn("image_url", enhanced_image_args)

        image_args = _build_direct_grok_image_args("make a poster", quality="speed")
        self.assertEqual(image_args["runtime"], "grok")
        self.assertFalse(image_args["prompt_enhancement"])
        self.assertNotIn("image_url", image_args)

        image_ref_args = _build_direct_grok_image_args("make it cinematic", "C:\\tmp\\ref.png", quality="quality")
        self.assertEqual(image_ref_args["image_url"], "C:\\tmp\\ref.png")
        self.assertFalse(image_ref_args["prompt_enhancement"])

        image_multi_ref_args = _build_direct_grok_image_args(
            "combine references",
            ["C:\\tmp\\ref1.png", "C:\\tmp\\ref2.png", "C:\\tmp\\ref3.png"],
            quality="quality",
        )
        self.assertEqual(
            image_multi_ref_args["image_url"],
            ["C:\\tmp\\ref1.png", "C:\\tmp\\ref2.png", "C:\\tmp\\ref3.png"],
        )

        video_args = _build_grok_video_job_args(
            "city timelapse",
            duration="10s",
            resolution="720p",
            prompt_enhancement=False,
        )
        self.assertFalse(video_args["prompt_enhancement"])
        self.assertEqual(video_args["aspect_ratio"], "16:9")
        self.assertNotIn("image_url", video_args)

        video_ref_args = _build_grok_video_job_args(
            "animate softly",
            "C:\\tmp\\ref.png",
            prompt_enhancement=False,
        )
        self.assertEqual(video_ref_args["image_url"], "C:\\tmp\\ref.png")
        self.assertNotIn("aspect_ratio", video_ref_args)
        self.assertEqual(video_ref_args["duration"], GROK_VIDEO_DEFAULT_DURATION)
        self.assertEqual(video_ref_args["resolution"], GROK_VIDEO_DEFAULT_RESOLUTION)

        video_multi_ref_args = _build_grok_video_job_args(
            "animate references",
            ["C:\\tmp\\ref1.png", "C:\\tmp\\ref2.png"],
            prompt_enhancement=False,
        )
        self.assertEqual(video_multi_ref_args["image_url"], ["C:\\tmp\\ref1.png", "C:\\tmp\\ref2.png"])
        self.assertNotIn("aspect_ratio", video_multi_ref_args)

    def test_normalize_proxy_url_adds_default_scheme(self):
        self.assertEqual(_normalize_proxy_url("127.0.0.1:7897"), "http://127.0.0.1:7897")
        self.assertEqual(_normalize_proxy_url("http://127.0.0.1:7897"), "http://127.0.0.1:7897")
        self.assertEqual(_normalize_proxy_url(""), "")

    def test_guild_sync_prunes_global_commands_before_syncing_guild(self):
        channel = _discord_channel_instance()
        channel.guild_id = "200"
        channel.discord = types.SimpleNamespace(Object=lambda id: types.SimpleNamespace(id=id))

        class FakeTree:
            def __init__(self):
                self.cleared = []
                self.synced = []

            def clear_commands(self, guild=None):
                self.cleared.append(guild)

            async def sync(self, guild=None):
                self.synced.append(guild)
                return []

        tree = FakeTree()
        channel.bot = types.SimpleNamespace(tree=tree)

        asyncio.run(channel._sync_commands())

        self.assertEqual(tree.cleared, [None])
        self.assertIsNone(tree.synced[0])
        self.assertEqual(tree.synced[1].id, 200)

    @patch("channel.discord.discord_channel._cow_cli_suggestions", return_value=[{"name": "backend", "cmd": "/backend", "desc": "backend"}])
    def test_registers_project_grok_commands_and_allowed_cli_commands(self, _suggestions):
        channel = _discord_channel_instance()
        channel.discord = types.SimpleNamespace(Interaction=object, Attachment=object)
        channel._registered_commands = []

        class FakeCommand:
            def __init__(self, name, description, callback):
                self.name = name
                self.description = description
                self.callback = callback

        class FakeGroup:
            def __init__(self, name, description):
                self.name = name
                self.description = description
                self.commands = []

            def add_command(self, command):
                self.commands.append(command)

        class FakeChoice:
            def __init__(self, name, value):
                self.name = name
                self.value = value

        class FakeAppCommands:
            Command = FakeCommand
            Group = FakeGroup
            Choice = FakeChoice

            @staticmethod
            def describe(**_kwargs):
                return lambda callback: callback

            @staticmethod
            def rename(**_kwargs):
                return lambda callback: callback

            @staticmethod
            def choices(**kwargs):
                def decorate(callback):
                    existing = dict(getattr(callback, "_choices", {}))
                    existing.update(kwargs)
                    callback._choices = existing
                    return callback

                return decorate

        class FakeTree:
            def __init__(self):
                self.commands = []

            def add_command(self, command, guild=None):
                command.guild = guild
                self.commands.append(command)

        bot = types.SimpleNamespace(tree=FakeTree())
        channel.app_commands = FakeAppCommands

        channel._register_slash_commands(bot)

        names = {command.name for command in bot.tree.commands}
        self.assertIn("backend", names)
        self.assertTrue(PROJECT_DISCORD_COMMANDS.issubset(names))
        self.assertNotIn("grok-direct", names)
        self.assertNotIn("imagine", names)
        self.assertNotIn("image-to-image", names)
        self.assertNotIn("image-to-video", names)
        self.assertIn(GROK_GEN_IMAGE_COMMAND, channel._registered_commands)

        image_command = next(command for command in bot.tree.commands if command.name == GROK_GEN_IMAGE_COMMAND)
        image_annotation_names = set(image_command.callback.__annotations__)
        self.assertTrue(set(GROK_IMAGE_OPTION_NAMES).issubset(image_annotation_names))
        self.assertNotIn("image", image_annotation_names)
        image_quality_values = {choice.value for choice in image_command.callback._choices["quality"]}
        image_mode_choices = {choice.value: choice.name for choice in image_command.callback._choices["mode"]}
        self.assertEqual(image_quality_values, {"speed", "quality"})
        self.assertEqual(set(image_mode_choices), {"normal", "real"})
        self.assertEqual(image_mode_choices["real"], "real (default)")

        video_command = next(command for command in bot.tree.commands if command.name == GROK_GEN_VIDEO_COMMAND)
        video_mode_values = {choice.value for choice in video_command.callback._choices["mode"]}
        self.assertEqual(video_mode_values, {"normal", "real"})

        direct_image_group = next(command for command in bot.tree.commands if command.name == GROK_DIRECT_GEN_IMAGE_COMMAND)
        self.assertEqual({command.name for command in direct_image_group.commands}, {"normal", "real"})
        direct_image_normal = next(command for command in direct_image_group.commands if command.name == "normal")
        direct_image_real = next(command for command in direct_image_group.commands if command.name == "real")
        self.assertIn("prompt", direct_image_normal.callback.__annotations__)
        self.assertNotIn("mode", direct_image_normal.callback.__annotations__)
        self.assertNotIn("prompt", direct_image_real.callback.__annotations__)
        self.assertIn("camera_angle", direct_image_real.callback.__annotations__)
        self.assertIn("tattoo", direct_image_real.callback.__annotations__)
        self.assertIn("prompt_2", direct_image_real.callback.__annotations__)
        self.assertEqual({choice.value for choice in direct_image_real.callback._choices["quality"]}, {"speed", "quality"})

        direct_video_group = next(command for command in bot.tree.commands if command.name == GROK_DIRECT_GEN_VIDEO_COMMAND)
        self.assertEqual({command.name for command in direct_video_group.commands}, {"normal", "real"})
        direct_video_normal = next(command for command in direct_video_group.commands if command.name == "normal")
        direct_video_real = next(command for command in direct_video_group.commands if command.name == "real")
        video_annotation_names = set(direct_video_real.callback.__annotations__)
        self.assertTrue({f"image{index}" for index in range(1, 7)}.issubset(video_annotation_names))
        self.assertEqual(len(video_annotation_names - {"interaction"}), 24)
        self.assertNotIn("image", video_annotation_names)
        self.assertNotIn("image7", video_annotation_names)
        self.assertNotIn("prompt", video_annotation_names)
        self.assertIn("tattoo", video_annotation_names)
        self.assertIn("prompt_6", video_annotation_names)
        self.assertNotIn("prompt_7", video_annotation_names)
        self.assertIn("prompt", direct_video_normal.callback.__annotations__)
        self.assertIn("image7", direct_video_normal.callback.__annotations__)
        self.assertNotIn("mode", direct_video_normal.callback.__annotations__)
        duration_choices = {choice.value: choice.name for choice in direct_video_real.callback._choices["duration"]}
        duration_values = set(duration_choices)
        resolution_values = {choice.value for choice in direct_video_real.callback._choices["resolution"]}
        self.assertEqual(duration_values, {"6s", "10s"})
        self.assertEqual(duration_choices["10s"], "10s (default)")
        self.assertEqual(resolution_values, {"480p", "720p"})

    def test_direct_video_interaction_uses_optional_image_and_selected_options(self):
        async def run():
            channel = _discord_channel_instance()
            channel.admin_user_id = "100"
            channel.guild_id = "200"
            channel.allowed_channel_ids = {"300"}
            channel.ephemeral_replies = False
            channel.loop = asyncio.get_running_loop()
            channel.discord = types.SimpleNamespace()
            captured = {}

            interaction = types.SimpleNamespace(
                id="abc",
                user=types.SimpleNamespace(id=100, display_name="Admin", name="admin"),
                channel=types.SimpleNamespace(id=300, name="bot"),
                guild=types.SimpleNamespace(id=200),
                response=types.SimpleNamespace(defer=lambda **_kwargs: _async_noop()),
            )

            async def save_image(_attachment, _bucket_id):
                return "C:\\tmp\\ref.png"

            def submit_video(prompt, image_path, duration, resolution, _context, prompt_enhancement=True):
                captured["submit"] = {
                    "prompt": prompt,
                    "image_path": image_path,
                    "duration": duration,
                    "resolution": resolution,
                    "prompt_enhancement": prompt_enhancement,
                }
                return Reply(ReplyType.TEXT, "ok")

            async def send_reply(reply, _context):
                captured["reply"] = reply.content
                return True

            channel._save_interaction_image_attachment = save_image
            channel._submit_grok_video_job = submit_video
            channel._send_reply_async = send_reply

            await channel._handle_grok_video_interaction(
                interaction,
                "animate softly",
                image_attachment=object(),
                duration="10s",
                resolution="720p",
                direct=True,
            )
            return captured

        captured = asyncio.run(run())

        self.assertEqual(captured["submit"]["prompt"], f"animate softly\n{GROK_MEDIA_REAL_PROMPT}")
        self.assertEqual(captured["submit"]["image_path"], "C:\\tmp\\ref.png")
        self.assertEqual(captured["submit"]["duration"], "10s")
        self.assertEqual(captured["submit"]["resolution"], "720p")
        self.assertFalse(captured["submit"]["prompt_enhancement"])
        self.assertEqual(captured["reply"], "ok")

    def test_direct_video_interaction_saves_ordered_reference_images(self):
        async def run():
            channel = _discord_channel_instance()
            channel.admin_user_id = "100"
            channel.guild_id = "200"
            channel.allowed_channel_ids = {"300"}
            channel.ephemeral_replies = False
            channel.loop = asyncio.get_running_loop()
            channel.discord = types.SimpleNamespace()
            captured = {"buckets": []}

            interaction = types.SimpleNamespace(
                id="abc",
                user=types.SimpleNamespace(id=100, display_name="Admin", name="admin"),
                channel=types.SimpleNamespace(id=300, name="bot"),
                guild=types.SimpleNamespace(id=200),
                response=types.SimpleNamespace(defer=lambda **_kwargs: _async_noop()),
            )

            async def save_image(_attachment, bucket_id):
                captured["buckets"].append(bucket_id)
                return f"C:\\tmp\\{bucket_id}.png"

            def submit_video(prompt, image_path, duration, resolution, _context, prompt_enhancement=True):
                captured["submit"] = {
                    "prompt": prompt,
                    "image_path": image_path,
                    "duration": duration,
                    "resolution": resolution,
                    "prompt_enhancement": prompt_enhancement,
                }
                return Reply(ReplyType.TEXT, "ok")

            async def send_reply(reply, _context):
                captured["reply"] = reply.content
                return True

            channel._save_interaction_image_attachment = save_image
            channel._submit_grok_video_job = submit_video
            channel._send_reply_async = send_reply

            await channel._handle_grok_video_interaction(
                interaction,
                "animate references",
                image_attachments=(object(), object(), object(), object()),
                duration="10s",
                resolution="720p",
                direct=True,
            )
            return captured

        captured = asyncio.run(run())

        self.assertEqual(captured["buckets"], ["abc-image1", "abc-image2", "abc-image3", "abc-image4"])
        self.assertEqual(
            captured["submit"]["image_path"],
            [
                "C:\\tmp\\abc-image1.png",
                "C:\\tmp\\abc-image2.png",
                "C:\\tmp\\abc-image3.png",
                "C:\\tmp\\abc-image4.png",
            ],
        )
        self.assertEqual(captured["submit"]["prompt"], f"animate references\n{GROK_MEDIA_REAL_PROMPT}")
        self.assertEqual(captured["submit"]["duration"], "10s")
        self.assertEqual(captured["submit"]["resolution"], "720p")
        self.assertFalse(captured["submit"]["prompt_enhancement"])
        self.assertEqual(captured["reply"], "ok")

    def test_direct_grok_image_interaction_without_image_submits_text_to_image(self):
        async def run():
            channel = _discord_channel_instance()
            channel.admin_user_id = "100"
            channel.guild_id = "200"
            channel.allowed_channel_ids = {"300"}
            channel.ephemeral_replies = False
            channel.loop = asyncio.get_running_loop()
            captured = {}

            interaction = types.SimpleNamespace(
                id="abc",
                user=types.SimpleNamespace(id=100, display_name="Admin", name="admin"),
                channel=types.SimpleNamespace(id=300, name="bot"),
                guild=types.SimpleNamespace(id=200),
                response=types.SimpleNamespace(defer=lambda **_kwargs: _async_noop()),
            )

            def submit_image(prompt, image_path, quality, _context):
                captured["submit"] = {
                    "prompt": prompt,
                    "image_path": image_path,
                    "quality": quality,
                }
                return Reply(ReplyType.TEXT, "ok")

            async def send_reply(reply, _context):
                captured["reply"] = reply.content
                return True

            channel._submit_direct_grok_image_job = submit_image
            channel._send_reply_async = send_reply

            await channel._handle_grok_image_interaction(
                interaction,
                "make it a cinematic poster",
                image_attachment=None,
                quality="speed",
                mode=GROK_MEDIA_MODE_NORMAL,
                direct=True,
            )
            return captured

        captured = asyncio.run(run())

        self.assertEqual(captured["submit"]["prompt"], "make it a cinematic poster")
        self.assertEqual(captured["submit"]["image_path"], "")
        self.assertEqual(captured["submit"]["quality"], "speed")
        self.assertEqual(captured["reply"], "ok")

    def test_enhanced_grok_image_interaction_without_image_submits_text_to_image(self):
        async def run():
            channel = _discord_channel_instance()
            channel.admin_user_id = "100"
            channel.guild_id = "200"
            channel.allowed_channel_ids = {"300"}
            channel.ephemeral_replies = False
            channel.loop = asyncio.get_running_loop()
            captured = {}

            interaction = types.SimpleNamespace(
                id="abc",
                user=types.SimpleNamespace(id=100, display_name="Admin", name="admin"),
                channel=types.SimpleNamespace(id=300, name="bot"),
                guild=types.SimpleNamespace(id=200),
                response=types.SimpleNamespace(defer=lambda **_kwargs: _async_noop()),
            )

            def submit_image(prompt, image_path, quality, _context):
                captured["submit"] = {
                    "prompt": prompt,
                    "image_path": image_path,
                    "quality": quality,
                }
                return Reply(ReplyType.TEXT, "ok")

            async def send_reply(reply, _context):
                captured["reply"] = reply.content
                return True

            channel._submit_enhanced_grok_image_job = submit_image
            channel._send_reply_async = send_reply

            await channel._handle_grok_image_interaction(
                interaction,
                "make a cinematic poster",
                image_attachment=None,
                quality="quality",
                direct=False,
            )
            return captured

        captured = asyncio.run(run())

        self.assertEqual(captured["submit"]["prompt"], f"make a cinematic poster\n{GROK_MEDIA_REAL_PROMPT}")
        self.assertEqual(captured["submit"]["image_path"], "")
        self.assertEqual(captured["submit"]["quality"], "quality")
        self.assertEqual(captured["reply"], "ok")

    def test_direct_grok_image_interaction_with_image_submits_image_to_image(self):
        async def run():
            channel = _discord_channel_instance()
            channel.admin_user_id = "100"
            channel.guild_id = "200"
            channel.allowed_channel_ids = {"300"}
            channel.ephemeral_replies = False
            channel.loop = asyncio.get_running_loop()
            captured = {}

            interaction = types.SimpleNamespace(
                id="abc",
                user=types.SimpleNamespace(id=100, display_name="Admin", name="admin"),
                channel=types.SimpleNamespace(id=300, name="bot"),
                guild=types.SimpleNamespace(id=200),
                response=types.SimpleNamespace(defer=lambda **_kwargs: _async_noop()),
            )

            async def save_image(_attachment, _bucket_id):
                return "C:\\tmp\\ref.png"

            def submit_image(prompt, image_path, quality, _context):
                captured["submit"] = {
                    "prompt": prompt,
                    "image_path": image_path,
                    "quality": quality,
                }
                return Reply(ReplyType.TEXT, "ok")

            async def send_reply(reply, _context):
                captured["reply"] = reply.content
                return True

            channel._save_interaction_image_attachment = save_image
            channel._submit_direct_grok_image_job = submit_image
            channel._send_reply_async = send_reply

            await channel._handle_grok_image_interaction(
                interaction,
                "make it cinematic",
                image_attachment=object(),
                quality="quality",
                direct=True,
            )
            return captured

        captured = asyncio.run(run())

        self.assertEqual(captured["submit"]["prompt"], f"make it cinematic\n{GROK_MEDIA_REAL_PROMPT}")
        self.assertEqual(captured["submit"]["image_path"], "C:\\tmp\\ref.png")
        self.assertEqual(captured["submit"]["quality"], "quality")
        self.assertEqual(captured["reply"], "ok")

    def test_direct_grok_image_interaction_saves_ordered_reference_images(self):
        async def run():
            channel = _discord_channel_instance()
            channel.admin_user_id = "100"
            channel.guild_id = "200"
            channel.allowed_channel_ids = {"300"}
            channel.ephemeral_replies = False
            channel.loop = asyncio.get_running_loop()
            captured = {"buckets": []}

            interaction = types.SimpleNamespace(
                id="abc",
                user=types.SimpleNamespace(id=100, display_name="Admin", name="admin"),
                channel=types.SimpleNamespace(id=300, name="bot"),
                guild=types.SimpleNamespace(id=200),
                response=types.SimpleNamespace(defer=lambda **_kwargs: _async_noop()),
            )

            async def save_image(_attachment, bucket_id):
                captured["buckets"].append(bucket_id)
                return f"C:\\tmp\\{bucket_id}.png"

            def submit_image(prompt, image_path, quality, _context):
                captured["submit"] = {
                    "prompt": prompt,
                    "image_path": image_path,
                    "quality": quality,
                }
                return Reply(ReplyType.TEXT, "ok")

            async def send_reply(reply, _context):
                captured["reply"] = reply.content
                return True

            channel._save_interaction_image_attachment = save_image
            channel._submit_direct_grok_image_job = submit_image
            channel._send_reply_async = send_reply

            await channel._handle_grok_image_interaction(
                interaction,
                "combine these references",
                image_attachments=(object(), object(), object()),
                quality="quality",
                direct=True,
            )
            return captured

        captured = asyncio.run(run())

        self.assertEqual(captured["buckets"], ["abc-image1", "abc-image2", "abc-image3"])
        self.assertEqual(
            captured["submit"]["image_path"],
            ["C:\\tmp\\abc-image1.png", "C:\\tmp\\abc-image2.png", "C:\\tmp\\abc-image3.png"],
        )
        self.assertEqual(captured["submit"]["prompt"], f"combine these references\n{GROK_MEDIA_REAL_PROMPT}")
        self.assertEqual(captured["submit"]["quality"], "quality")
        self.assertEqual(captured["reply"], "ok")

    def test_direct_grok_image_real_mode_composes_template_prompt(self):
        async def run():
            channel = _discord_channel_instance()
            channel.admin_user_id = "100"
            channel.guild_id = "200"
            channel.allowed_channel_ids = {"300"}
            channel.ephemeral_replies = False
            channel.loop = asyncio.get_running_loop()
            captured = {}

            interaction = types.SimpleNamespace(
                id="abc",
                user=types.SimpleNamespace(id=100, display_name="Admin", name="admin"),
                channel=types.SimpleNamespace(id=300, name="bot"),
                guild=types.SimpleNamespace(id=200),
                response=types.SimpleNamespace(defer=lambda **_kwargs: _async_noop()),
            )

            def submit_image(prompt, image_path, quality, _context):
                captured["submit"] = {
                    "prompt": prompt,
                    "image_path": image_path,
                    "quality": quality,
                }
                return Reply(ReplyType.TEXT, "ok")

            async def send_reply(reply, _context):
                captured["reply"] = reply.content
                return True

            channel._submit_direct_grok_image_job = submit_image
            channel._send_reply_async = send_reply

            await channel._handle_grok_image_interaction(
                interaction,
                "",
                quality="speed",
                direct=True,
                real_mode_options={
                    "selections": {
                        "camera_angle": "custom:under a desk",
                        "scene": "custom:test room",
                        "time": "custom:midnight",
                        "light_source": "custom:monitor",
                        "color_tone": "custom:cyan",
                        "nationality": "custom:Testland",
                        "action": "custom:looking toward the lens",
                        "clothing": "custom:a black jacket",
                        "lower_state": "custom:with tailored pants",
                        "tattoo": "custom:a tiny wrist tattoo",
                        "expression": "custom:calm expression",
                    },
                    "extra_prompts": {},
                },
            )
            return captured

        captured = asyncio.run(run())

        prompt = captured["submit"]["prompt"]
        self.assertIn("One imaginary 20-year-old Testland woman", prompt)
        self.assertIn("from under a desk looking up in test room at midnight", prompt)
        self.assertLess(prompt.index("a tiny wrist tattoo"), prompt.index("calm expression"))
        self.assertIn("background with test room", prompt)
        self.assertEqual(captured["submit"]["image_path"], "")
        self.assertEqual(captured["submit"]["quality"], "speed")
        self.assertEqual(captured["reply"], "ok")

    def test_direct_grok_image_real_mode_requires_extra_reference_prompt(self):
        async def run():
            channel = _discord_channel_instance()
            channel.admin_user_id = "100"
            channel.guild_id = "200"
            channel.allowed_channel_ids = {"300"}
            channel.ephemeral_replies = False
            channel.loop = asyncio.get_running_loop()
            captured = {"buckets": []}

            interaction = types.SimpleNamespace(
                id="abc",
                user=types.SimpleNamespace(id=100, display_name="Admin", name="admin"),
                channel=types.SimpleNamespace(id=300, name="bot"),
                guild=types.SimpleNamespace(id=200),
                response=types.SimpleNamespace(defer=lambda **_kwargs: _async_noop()),
            )

            async def save_image(_attachment, bucket_id):
                captured["buckets"].append(bucket_id)
                return f"C:\\tmp\\{bucket_id}.png"

            def submit_image(*_args, **_kwargs):
                captured["submitted"] = True
                return Reply(ReplyType.TEXT, "bad")

            async def send_message(text, _context):
                captured["message"] = text
                return True

            channel._save_interaction_image_attachment = save_image
            channel._submit_direct_grok_image_job = submit_image
            channel._send_discord_message = send_message

            await channel._handle_grok_image_interaction(
                interaction,
                "",
                image_attachments=(object(), object()),
                direct=True,
                real_mode_options={
                    "selections": {},
                    "extra_prompts": {},
                },
            )
            return captured

        captured = asyncio.run(run())

        self.assertEqual(captured["buckets"], ["abc-image1", "abc-image2"])
        self.assertIn("prompt_2", captured["message"])
        self.assertNotIn("submitted", captured)

    def test_direct_grok_video_interaction_without_image_submits_text_to_video(self):
        async def run():
            channel = _discord_channel_instance()
            channel.admin_user_id = "100"
            channel.guild_id = "200"
            channel.allowed_channel_ids = {"300"}
            channel.ephemeral_replies = False
            channel.loop = asyncio.get_running_loop()
            captured = {}

            interaction = types.SimpleNamespace(
                id="abc",
                user=types.SimpleNamespace(id=100, display_name="Admin", name="admin"),
                channel=types.SimpleNamespace(id=300, name="bot"),
                guild=types.SimpleNamespace(id=200),
                response=types.SimpleNamespace(defer=lambda **_kwargs: _async_noop()),
            )

            def submit_video(prompt, image_path, duration, resolution, _context, prompt_enhancement=True):
                captured["submit"] = {
                    "prompt": prompt,
                    "image_path": image_path,
                    "duration": duration,
                    "resolution": resolution,
                    "prompt_enhancement": prompt_enhancement,
                }
                return Reply(ReplyType.TEXT, "ok")

            async def send_reply(reply, _context):
                captured["reply"] = reply.content
                return True

            channel._submit_grok_video_job = submit_video
            channel._send_reply_async = send_reply

            await channel._handle_grok_video_interaction(
                interaction,
                "city skyline timelapse",
                image_attachment=None,
                direct=True,
            )
            return captured

        captured = asyncio.run(run())

        self.assertEqual(captured["submit"]["prompt"], f"city skyline timelapse\n{GROK_MEDIA_REAL_PROMPT}")
        self.assertEqual(captured["submit"]["image_path"], "")
        self.assertEqual(captured["submit"]["duration"], "10s")
        self.assertEqual(captured["submit"]["resolution"], "480p")
        self.assertFalse(captured["submit"]["prompt_enhancement"])
        self.assertEqual(captured["reply"], "ok")

    def test_direct_grok_video_real_mode_inserts_ordered_reference_prompts(self):
        async def run():
            channel = _discord_channel_instance()
            channel.admin_user_id = "100"
            channel.guild_id = "200"
            channel.allowed_channel_ids = {"300"}
            channel.ephemeral_replies = False
            channel.loop = asyncio.get_running_loop()
            captured = {"buckets": []}

            interaction = types.SimpleNamespace(
                id="abc",
                user=types.SimpleNamespace(id=100, display_name="Admin", name="admin"),
                channel=types.SimpleNamespace(id=300, name="bot"),
                guild=types.SimpleNamespace(id=200),
                response=types.SimpleNamespace(defer=lambda **_kwargs: _async_noop()),
            )

            async def save_image(_attachment, bucket_id):
                captured["buckets"].append(bucket_id)
                return f"C:\\tmp\\{bucket_id}.png"

            def submit_video(prompt, image_path, duration, resolution, _context, prompt_enhancement=True):
                captured["submit"] = {
                    "prompt": prompt,
                    "image_path": image_path,
                    "duration": duration,
                    "resolution": resolution,
                    "prompt_enhancement": prompt_enhancement,
                }
                return Reply(ReplyType.TEXT, "ok")

            async def send_reply(reply, _context):
                captured["reply"] = reply.content
                return True

            channel._save_interaction_image_attachment = save_image
            channel._submit_grok_video_job = submit_video
            channel._send_reply_async = send_reply

            await channel._handle_grok_video_interaction(
                interaction,
                "",
                image_attachments=(object(), object(), object()),
                duration="10s",
                resolution="720p",
                direct=True,
                real_mode_options={
                    "selections": {
                        "camera_angle": "custom:floor corner",
                        "scene": "custom:studio room",
                        "time": "custom:late evening",
                        "light_source": "custom:soft lamp",
                        "color_tone": "custom:warm",
                        "nationality": "custom:Unused",
                        "action": "custom:standing still",
                        "clothing": "custom:a linen shirt",
                        "lower_state": "custom:with wide-leg pants",
                        "tattoo": "custom:a shoulder tattoo",
                        "expression": "custom:focused expression",
                    },
                    "extra_prompts": {
                        2: "the background mood board",
                        3: "the lighting reference",
                    },
                },
            )
            return captured

        captured = asyncio.run(run())

        prompt = captured["submit"]["prompt"]
        self.assertIn("<IMAGE_2> is the background mood board", prompt)
        self.assertIn("<IMAGE_3> is the lighting reference", prompt)
        self.assertLess(prompt.index("a shoulder tattoo"), prompt.index("focused expression"))
        self.assertLess(prompt.index("<IMAGE_2>"), prompt.index("background with studio room"))
        self.assertEqual(
            captured["submit"]["image_path"],
            ["C:\\tmp\\abc-image1.png", "C:\\tmp\\abc-image2.png", "C:\\tmp\\abc-image3.png"],
        )
        self.assertFalse(captured["submit"]["prompt_enhancement"])
        self.assertEqual(captured["reply"], "ok")

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

    def test_message_event_forwards_plain_text_without_message_content_flag(self):
        async def run():
            channel = _discord_channel_instance()
            channel.message_content_enabled = False
            captured = {}

            class FakeBot:
                def event(self, callback):
                    captured[callback.__name__] = callback
                    return callback

            async def fake_handle_message(message):
                captured["message"] = message

            channel._handle_message = fake_handle_message
            channel._register_bot_events(FakeBot())

            message = types.SimpleNamespace(
                author=types.SimpleNamespace(bot=False),
                content="hello discord",
            )
            await captured["on_message"](message)
            return captured

        captured = asyncio.run(run())
        self.assertIn("message", captured)
        self.assertEqual(captured["message"].content, "hello discord")

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
