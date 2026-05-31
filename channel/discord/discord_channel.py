import asyncio
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional, Tuple

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel
from channel.discord.discord_message import DiscordMessage, discord_attachment_cache_dir
from common.image_generation_routing import (
    explicit_image_generation_requested,
    explicit_video_generation_requested,
    match_image_create_prefix,
    match_video_create_prefix,
)
from common.grok_real_mode_prompt_assets import (
    ASSET_FIELDS as GROK_REAL_MODE_ASSET_FIELDS,
    EXTRA_PROMPT_FIELDS as GROK_REAL_MODE_EXTRA_PROMPT_FIELDS,
    GrokRealModePromptError,
    compose_real_mode_prompt,
    material_choices,
)
from common.log import logger
from common.singleton import singleton
from config import conf
from plugins import Event, EventContext
from plugins.plugin_manager import PluginManager


GROK_GEN_IMAGE_COMMAND = "grok-gen-image"
GROK_GEN_VIDEO_COMMAND = "grok-gen-video"
GROK_DIRECT_GEN_IMAGE_COMMAND = "grok-direct-gen-image"
GROK_DIRECT_GEN_VIDEO_COMMAND = "grok-direct-gen-video"
PROJECT_DISCORD_COMMANDS = {
    GROK_GEN_IMAGE_COMMAND,
    GROK_GEN_VIDEO_COMMAND,
    GROK_DIRECT_GEN_IMAGE_COMMAND,
    GROK_DIRECT_GEN_VIDEO_COMMAND,
}
DISCORD_COWCLI_COMMANDS = {
    "help",
    "version",
    "status",
    "logs",
    "tokens",
    "updates",
    "ledger",
    "backend",
    "voice",
    "context",
    "skill",
    "install-browser",
    "memory",
    "knowledge",
    "config",
}
CHAT_COMMAND_FALLBACKS = {
    "help": "Show available CowCli commands",
    "version": "Show version",
    "status": "Show runtime status",
    "logs": "Show recent logs",
    "tokens": "Show local token usage",
    "updates": "Show project updates",
    "ledger": "Query local ledger",
    "backend": "View or switch model backend",
    "voice": "View or switch voice mode",
    "context": "Manage chat context",
    "skill": "Manage or query skills",
    "install-browser": "Install browser automation dependencies",
    "memory": "Manage memory",
    "knowledge": "Manage knowledge base",
    "config": "View or update configuration",
}
CLI_ONLY_COMMANDS = {"start", "stop", "restart"}
DISCORD_COMMAND_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
GROK_IMAGE_QUALITY_CHOICES = (
    ("speed (default)", "speed"),
    ("quality", "quality"),
)
GROK_IMAGE_MAX_REFERENCE_IMAGES = 3
GROK_VIDEO_MAX_REFERENCE_IMAGES = 7
GROK_IMAGE_OPTION_NAMES = tuple(f"image{index}" for index in range(1, GROK_IMAGE_MAX_REFERENCE_IMAGES + 1))
GROK_VIDEO_OPTION_NAMES = tuple(f"image{index}" for index in range(1, GROK_VIDEO_MAX_REFERENCE_IMAGES + 1))
GROK_VIDEO_DEFAULT_DURATION = "10s"
GROK_VIDEO_DEFAULT_RESOLUTION = "480p"
GROK_VIDEO_DURATION_CHOICES = (
    ("10s (default)", "10s"),
    ("6s", "6s"),
)
GROK_VIDEO_RESOLUTION_CHOICES = (
    ("480p (default)", "480p"),
    ("720p", "720p"),
)
GROK_MEDIA_MODE_NORMAL = "normal"
GROK_MEDIA_MODE_REAL = "real"
GROK_MEDIA_DEFAULT_MODE = GROK_MEDIA_MODE_REAL
GROK_MEDIA_REAL_PROMPT = (
    "Raw hidden iPhone 6s photo in dimly lit, bedroom at late night, "
    "faint screen glow + warm ambient light, extremely grainy blurry image, "
    "heavy motion blur, low exposure shadows, shaky amateur feel"
)
GROK_MEDIA_MODE_CHOICES = (
    ("real (default)", GROK_MEDIA_MODE_REAL),
    ("normal", GROK_MEDIA_MODE_NORMAL),
)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="discord-cowcli")


def _as_str_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _is_truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _normalize_proxy_url(value: Any) -> str:
    proxy_url = str(value or "").strip()
    if not proxy_url:
        return ""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", proxy_url):
        return proxy_url
    return f"http://{proxy_url}"


def _discord_proxy_url() -> str:
    for value in (
        conf().get("discord_proxy"),
        os.environ.get("DISCORD_PROXY"),
        conf().get("proxy"),
        os.environ.get("HTTPS_PROXY"),
        os.environ.get("HTTP_PROXY"),
        os.environ.get("ALL_PROXY"),
    ):
        proxy_url = _normalize_proxy_url(value)
        if proxy_url:
            return proxy_url
    return ""


def _truncate_discord_text(text: str, limit: int = 1900) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n...[truncated]"


def _discord_command_name(command_text: str) -> str:
    name = command_text.strip().split(None, 1)[0].lstrip("/").lower()
    name = name.replace("_", "-")
    return name if DISCORD_COMMAND_NAME_RE.match(name) else ""


def _cow_cli_suggestions() -> List[Dict[str, str]]:
    try:
        from plugins.cow_cli.cow_cli import slash_command_suggestions

        suggestions = slash_command_suggestions(is_admin=True)
    except Exception as e:
        logger.warning("[Discord] CowCli suggestions unavailable, using fallback commands: %s", e)
        suggestions = [{"cmd": f"/{name}", "desc": desc} for name, desc in CHAT_COMMAND_FALLBACKS.items()]
    return _normalise_cow_cli_suggestions(suggestions)


def _normalise_cow_cli_suggestions(suggestions: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    commands = []
    seen = set()
    for item in suggestions:
        command_text = str(item.get("cmd") or "").strip()
        name = _discord_command_name(command_text)
        if (
            not name
            or name in CLI_ONLY_COMMANDS
            or name not in DISCORD_COWCLI_COMMANDS
            or name in seen
        ):
            continue
        seen.add(name)
        commands.append({
            "name": name,
            "cmd": f"/{name}",
            "desc": str(item.get("desc") or CHAT_COMMAND_FALLBACKS.get(name) or "CowCli command")[:100],
        })
    return commands


def _split_slash_text(command_text: str) -> Tuple[str, str]:
    text = str(command_text or "").strip()
    if text.startswith("/"):
        text = text[1:]
    parts = text.split(None, 1)
    if not parts:
        return "", ""
    return parts[0].lower(), parts[1] if len(parts) > 1 else ""


def _normalize_grok_media_mode(mode: str) -> str:
    return GROK_MEDIA_MODE_NORMAL if str(mode or "").strip().lower() == GROK_MEDIA_MODE_NORMAL else GROK_MEDIA_MODE_REAL


def _apply_grok_media_mode_prompt(prompt: str, mode: str = GROK_MEDIA_DEFAULT_MODE) -> str:
    prompt = str(prompt or "").strip()
    if _normalize_grok_media_mode(mode) != GROK_MEDIA_MODE_REAL:
        return prompt
    if GROK_MEDIA_REAL_PROMPT.lower() in prompt.lower():
        return prompt
    return "\n".join(part for part in (prompt, GROK_MEDIA_REAL_PROMPT) if part)


def _build_grok_media_shortcut_query(
    kind: str,
    prompt: str,
    image_path: Any = "",
    *,
    quality: str = "",
    duration: str = "",
    resolution: str = "",
) -> str:
    prompt = str(prompt or "").strip()
    if image_path:
        prompt = (prompt + "\n" if prompt else "") + f"[image: {image_path}]"
    options = _grok_media_option_text(kind, quality=quality, duration=duration, resolution=resolution)
    options = f" {options}" if options else ""
    return f"/grok-direct {kind}{options} -- {prompt}".strip()


def _build_grok_direct_image_query(prompt: str, image_path: str = "", *, quality: str = "speed") -> str:
    return _build_grok_media_shortcut_query("image", prompt, image_path, quality=quality or "speed")


def _build_grok_direct_video_query(
    prompt: str,
    image_path: str = "",
    *,
    duration: str = GROK_VIDEO_DEFAULT_DURATION,
    resolution: str = GROK_VIDEO_DEFAULT_RESOLUTION,
) -> str:
    return _build_grok_media_shortcut_query(
        "video",
        prompt,
        image_path,
        duration=duration or GROK_VIDEO_DEFAULT_DURATION,
        resolution=resolution or GROK_VIDEO_DEFAULT_RESOLUTION,
    )


def _build_enhanced_grok_image_args(prompt: str, image_path: str = "", *, quality: str = "speed") -> Dict[str, Any]:
    return _build_grok_image_job_args(prompt, image_path, quality=quality, prompt_enhancement=True)


def _build_direct_grok_image_args(prompt: str, image_path: str = "", *, quality: str = "speed") -> Dict[str, Any]:
    return _build_grok_image_job_args(prompt, image_path, quality=quality, prompt_enhancement=False)


def _build_grok_image_job_args(
    prompt: str,
    image_path: Any = "",
    *,
    quality: str = "speed",
    prompt_enhancement: bool = True,
) -> Dict[str, Any]:
    args: Dict[str, Any] = {
        "prompt": str(prompt or "").strip(),
        "runtime": "grok",
        "quality": quality or "speed",
        "prompt_enhancement": bool(prompt_enhancement),
    }
    if image_path:
        args["image_url"] = image_path
    return args


def _build_grok_video_job_args(
    prompt: str,
    image_path: Any = "",
    *,
    duration: str = GROK_VIDEO_DEFAULT_DURATION,
    resolution: str = GROK_VIDEO_DEFAULT_RESOLUTION,
    prompt_enhancement: bool = True,
) -> Dict[str, Any]:
    args: Dict[str, Any] = {
        "prompt": str(prompt or "").strip(),
        "duration": duration or GROK_VIDEO_DEFAULT_DURATION,
        "resolution": resolution or GROK_VIDEO_DEFAULT_RESOLUTION,
        "prompt_enhancement": bool(prompt_enhancement),
    }
    if image_path:
        args["image_url"] = image_path
    else:
        args["aspect_ratio"] = "16:9"
    return args


def _reference_image_value(image_paths: List[str]) -> Any:
    if not image_paths:
        return ""
    return image_paths[0] if len(image_paths) == 1 else image_paths


def _attachment_tuple(*attachments) -> Tuple[Any, ...]:
    compact: List[Any] = []
    for attachment in attachments:
        if attachment is None:
            continue
        if isinstance(attachment, (list, tuple)):
            compact.extend(item for item in attachment if item is not None)
        else:
            compact.append(attachment)
    return tuple(compact)


def _grok_real_mode_selections(**values: Any) -> Dict[str, str]:
    return {field: str(values.get(field) or "").strip() for field in GROK_REAL_MODE_ASSET_FIELDS}


def _grok_real_mode_extra_prompts(**values: Any) -> Dict[int, str]:
    extra: Dict[int, str] = {}
    for field in GROK_REAL_MODE_EXTRA_PROMPT_FIELDS:
        try:
            index = int(field.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            continue
        extra[index] = str(values.get(field) or "").strip()
    return extra


def _grok_media_option_text(kind: str, *, quality: str = "", duration: str = "", resolution: str = "") -> str:
    parts: List[str] = []
    if kind == "image" and quality:
        parts.extend(["--quality", quality])
    if kind == "video":
        if resolution:
            parts.extend(["--resolution", resolution])
        if duration:
            parts.extend(["--duration", duration])
    return " ".join(parts)


@singleton
class DiscordChannel(ChatChannel):
    channel_type = "discord"
    NOT_SUPPORT_REPLYTYPE = [ReplyType.VOICE, ReplyType.CARD, ReplyType.MINIAPP]

    def __init__(self):
        super().__init__()
        self.discord = None
        self.commands = None
        self.app_commands = None
        self.bot = None
        self.loop = None
        self.admin_user_id = ""
        self.guild_id = ""
        self.allowed_channel_ids = set()
        self.ephemeral_replies = False
        self.message_content_enabled = False
        self._registered_commands = []
        self._stopping = False

    def startup(self):
        try:
            import discord
            from discord import app_commands
            from discord.ext import commands
        except Exception as e:
            self.report_startup_error(str(e))
            raise RuntimeError("discord.py is required for Discord channel; install requirements.txt") from e

        self.discord = discord
        self.commands = commands
        self.app_commands = app_commands
        token = str(conf().get("discord_bot_token") or os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
        if not token:
            self.report_startup_error("discord_bot_token is required")
            raise ValueError("discord_bot_token is required")
        self.admin_user_id = str(conf().get("discord_admin_user_id") or "").strip()
        if not self.admin_user_id:
            self.report_startup_error("discord_admin_user_id is required")
            raise ValueError("discord_admin_user_id is required")
        self.guild_id = str(conf().get("discord_guild_id") or "").strip()
        self.allowed_channel_ids = set(_as_str_list(conf().get("discord_allowed_channel_ids", [])))
        self.ephemeral_replies = _is_truthy(conf().get("discord_ephemeral_replies", False))
        self.message_content_enabled = True

        intents = discord.Intents.default()
        intents.message_content = True
        intents.messages = True
        proxy_url = _discord_proxy_url()
        bot_options = {"command_prefix": "!", "intents": intents}
        if proxy_url:
            bot_options["proxy"] = proxy_url
            logger.info("[Discord] using configured proxy for Discord gateway")
        bot = commands.Bot(**bot_options)
        self.bot = bot
        self.loop = asyncio.new_event_loop()
        self._register_bot_events(bot)
        self._register_slash_commands(bot)
        asyncio.set_event_loop(self.loop)
        try:
            self.report_startup_success()
            logger.info("[Discord] starting bot")
            self.loop.run_until_complete(bot.start(token))
        finally:
            if not self.loop.is_closed():
                pending = asyncio.all_tasks(self.loop)
                for task in pending:
                    task.cancel()
                self.loop.run_until_complete(self.loop.shutdown_asyncgens())
                self.loop.close()

    def stop(self):
        self._stopping = True
        if not self.bot or not self.loop or self.loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(self.bot.close(), self.loop)
        try:
            future.result(timeout=5)
        except Exception as e:
            logger.warning("[Discord] bot close failed: %s", e)

    def _register_bot_events(self, bot):
        @bot.event
        async def on_ready():
            try:
                if conf().get("discord_command_sync_on_startup", True):
                    await self._sync_commands()
                logger.info("[Discord] logged in as %s", getattr(bot.user, "id", "unknown"))
            except Exception as e:
                logger.warning("[Discord] command sync failed: %s", e)

        @bot.event
        async def on_message(message):
            if getattr(message.author, "bot", False):
                return
            await self._handle_message(message)

    def _register_slash_commands(self, bot):
        for item in _cow_cli_suggestions():
            command_name = item["name"]
            command_text = item["cmd"]
            description = item["desc"] or "CowCli command"

            def make_callback(bound_command_text: str):
                async def callback(interaction, args: str = ""):
                    await self._handle_slash_interaction(interaction, bound_command_text, args)

                return callback

            callback = make_callback(command_text)

            callback.__name__ = f"discord_{command_name.replace('-', '_')}"
            callback.__annotations__ = {"interaction": self.discord.Interaction, "args": str}
            command = self.app_commands.Command(
                name=command_name,
                description=description[:100],
                callback=self.app_commands.describe(args="Arguments after the command")(
                    self.app_commands.rename(args="args")(callback)
                ),
            )
            self._add_slash_command(bot, command)

        self._register_grok_generation_commands(bot)

    def _register_grok_generation_commands(self, bot):
        async def grok_gen_image_callback(
            interaction,
            prompt: str,
            image1=None,
            image2=None,
            image3=None,
            quality: str = "speed",
            mode: str = GROK_MEDIA_DEFAULT_MODE,
        ):
            await self._handle_grok_image_interaction(
                interaction,
                prompt,
                image_attachments=_attachment_tuple(image1, image2, image3),
                quality=quality,
                mode=mode,
                direct=False,
            )

        grok_gen_image_callback.__name__ = "discord_grok_gen_image"
        grok_gen_image_callback.__annotations__ = {
            "interaction": self.discord.Interaction,
            "prompt": str,
            "image1": Optional[self.discord.Attachment],
            "image2": Optional[self.discord.Attachment],
            "image3": Optional[self.discord.Attachment],
            "quality": str,
            "mode": str,
        }
        self._add_slash_command(
            bot,
            self.app_commands.Command(
                name=GROK_GEN_IMAGE_COMMAND,
                description="Grok image generation with prompt enhancement",
                callback=self._with_grok_choices(
                    self.app_commands.describe(
                        prompt="Image prompt",
                        image1="Reference image 1",
                        image2="Reference image 2",
                        image3="Reference image 3",
                        quality="Image quality",
                        mode="Prompt mode",
                    )(grok_gen_image_callback),
                    quality=GROK_IMAGE_QUALITY_CHOICES,
                    mode=GROK_MEDIA_MODE_CHOICES,
                ),
            ),
        )

        self._add_slash_command(bot, self._build_grok_direct_image_group())

        async def grok_gen_video_callback(
            interaction,
            prompt: str,
            image1=None,
            image2=None,
            image3=None,
            image4=None,
            image5=None,
            image6=None,
            image7=None,
            duration: str = GROK_VIDEO_DEFAULT_DURATION,
            resolution: str = GROK_VIDEO_DEFAULT_RESOLUTION,
            mode: str = GROK_MEDIA_DEFAULT_MODE,
        ):
            await self._handle_grok_video_interaction(
                interaction,
                prompt,
                image_attachments=_attachment_tuple(image1, image2, image3, image4, image5, image6, image7),
                duration=duration,
                resolution=resolution,
                mode=mode,
                direct=False,
            )

        grok_gen_video_callback.__name__ = "discord_grok_gen_video"
        grok_gen_video_callback.__annotations__ = {
            "interaction": self.discord.Interaction,
            "prompt": str,
            "image1": Optional[self.discord.Attachment],
            "image2": Optional[self.discord.Attachment],
            "image3": Optional[self.discord.Attachment],
            "image4": Optional[self.discord.Attachment],
            "image5": Optional[self.discord.Attachment],
            "image6": Optional[self.discord.Attachment],
            "image7": Optional[self.discord.Attachment],
            "duration": str,
            "resolution": str,
            "mode": str,
        }
        self._add_slash_command(
            bot,
            self.app_commands.Command(
                name=GROK_GEN_VIDEO_COMMAND,
                description="Grok video generation",
                callback=self._with_grok_choices(
                    self.app_commands.describe(
                        prompt="Video prompt",
                        image1="Reference image 1",
                        image2="Reference image 2",
                        image3="Reference image 3",
                        image4="Reference image 4",
                        image5="Reference image 5",
                        image6="Reference image 6",
                        image7="Reference image 7",
                        duration="Video duration",
                        resolution="Video resolution",
                        mode="Prompt mode",
                    )(grok_gen_video_callback),
                    duration=GROK_VIDEO_DURATION_CHOICES,
                    resolution=GROK_VIDEO_RESOLUTION_CHOICES,
                    mode=GROK_MEDIA_MODE_CHOICES,
                ),
            ),
        )

        self._add_slash_command(bot, self._build_grok_direct_video_group())

    def _build_grok_direct_image_group(self):
        group = self.app_commands.Group(
            name=GROK_DIRECT_GEN_IMAGE_COMMAND,
            description="Grok direct image generation without prompt enhancement",
        )

        async def normal_callback(
            interaction,
            prompt: str,
            image1=None,
            image2=None,
            image3=None,
            quality: str = "speed",
        ):
            await self._handle_grok_image_interaction(
                interaction,
                prompt,
                image_attachments=_attachment_tuple(image1, image2, image3),
                quality=quality,
                mode=GROK_MEDIA_MODE_NORMAL,
                direct=True,
            )

        normal_callback.__name__ = "discord_grok_direct_gen_image_normal"
        normal_callback.__annotations__ = {
            "interaction": self.discord.Interaction,
            "prompt": str,
            "image1": Optional[self.discord.Attachment],
            "image2": Optional[self.discord.Attachment],
            "image3": Optional[self.discord.Attachment],
            "quality": str,
        }
        group.add_command(
            self.app_commands.Command(
                name=GROK_MEDIA_MODE_NORMAL,
                description="Submit the prompt exactly as written",
                callback=self._with_grok_choices(
                    self.app_commands.describe(
                        prompt="Image prompt",
                        image1="Reference image 1",
                        image2="Reference image 2",
                        image3="Reference image 3",
                        quality="Image quality",
                    )(normal_callback),
                    quality=GROK_IMAGE_QUALITY_CHOICES,
                ),
            )
        )

        async def real_callback(
            interaction,
            image1=None,
            image2=None,
            image3=None,
            quality: str = "speed",
            camera_angle: str = "",
            scene: str = "",
            time: str = "",
            light_source: str = "",
            color_tone: str = "",
            nationality: str = "",
            action: str = "",
            clothing: str = "",
            lower_state: str = "",
            tattoo: str = "",
            expression: str = "",
            prompt_2: str = "",
            prompt_3: str = "",
        ):
            await self._handle_grok_image_interaction(
                interaction,
                "",
                image_attachments=_attachment_tuple(image1, image2, image3),
                quality=quality,
                mode=GROK_MEDIA_MODE_REAL,
                direct=True,
                real_mode_options={
                    "selections": _grok_real_mode_selections(
                        camera_angle=camera_angle,
                        scene=scene,
                        time=time,
                        light_source=light_source,
                        color_tone=color_tone,
                        nationality=nationality,
                        action=action,
                        clothing=clothing,
                        lower_state=lower_state,
                        tattoo=tattoo,
                        expression=expression,
                    ),
                    "extra_prompts": _grok_real_mode_extra_prompts(prompt_2=prompt_2, prompt_3=prompt_3),
                },
            )

        real_callback.__name__ = "discord_grok_direct_gen_image_real"
        real_callback.__annotations__ = {
            "interaction": self.discord.Interaction,
            "image1": Optional[self.discord.Attachment],
            "image2": Optional[self.discord.Attachment],
            "image3": Optional[self.discord.Attachment],
            "quality": str,
            "camera_angle": str,
            "scene": str,
            "time": str,
            "light_source": str,
            "color_tone": str,
            "nationality": str,
            "action": str,
            "clothing": str,
            "lower_state": str,
            "tattoo": str,
            "expression": str,
            "prompt_2": str,
            "prompt_3": str,
        }
        real_callback = self._with_grok_real_mode_autocomplete(real_callback)
        group.add_command(
            self.app_commands.Command(
                name=GROK_MEDIA_MODE_REAL,
                description="Compose a real-mode prompt from local materials",
                callback=self._with_grok_choices(
                    self.app_commands.describe(
                        image1="Reference image 1",
                        image2="Reference image 2",
                        image3="Reference image 3",
                        quality="Image quality",
                        camera_angle="Material key or custom:<prompt>",
                        scene="Material key or custom:<prompt>",
                        time="Material key or custom:<prompt>",
                        light_source="Material key or custom:<prompt>",
                        color_tone="Material key or custom:<prompt>",
                        nationality="Material key or custom:<prompt>",
                        action="Material key or custom:<prompt>",
                        clothing="Material key or custom:<prompt>",
                        lower_state="Material key or custom:<prompt>",
                        tattoo="Tattoo key, random, or custom:<prompt>; blank means no tattoo",
                        expression="Material key or custom:<prompt>",
                        prompt_2="Role prompt for reference image 2",
                        prompt_3="Role prompt for reference image 3",
                    )(real_callback),
                    quality=GROK_IMAGE_QUALITY_CHOICES,
                ),
            )
        )
        return group

    def _build_grok_direct_video_group(self):
        group = self.app_commands.Group(
            name=GROK_DIRECT_GEN_VIDEO_COMMAND,
            description="Grok direct video generation",
        )

        async def normal_callback(
            interaction,
            prompt: str,
            image1=None,
            image2=None,
            image3=None,
            image4=None,
            image5=None,
            image6=None,
            image7=None,
            duration: str = GROK_VIDEO_DEFAULT_DURATION,
            resolution: str = GROK_VIDEO_DEFAULT_RESOLUTION,
        ):
            await self._handle_grok_video_interaction(
                interaction,
                prompt,
                image_attachments=_attachment_tuple(image1, image2, image3, image4, image5, image6, image7),
                duration=duration,
                resolution=resolution,
                mode=GROK_MEDIA_MODE_NORMAL,
                direct=True,
            )

        normal_callback.__name__ = "discord_grok_direct_gen_video_normal"
        normal_callback.__annotations__ = {
            "interaction": self.discord.Interaction,
            "prompt": str,
            "image1": Optional[self.discord.Attachment],
            "image2": Optional[self.discord.Attachment],
            "image3": Optional[self.discord.Attachment],
            "image4": Optional[self.discord.Attachment],
            "image5": Optional[self.discord.Attachment],
            "image6": Optional[self.discord.Attachment],
            "image7": Optional[self.discord.Attachment],
            "duration": str,
            "resolution": str,
        }
        group.add_command(
            self.app_commands.Command(
                name=GROK_MEDIA_MODE_NORMAL,
                description="Submit the prompt exactly as written",
                callback=self._with_grok_choices(
                    self.app_commands.describe(
                        prompt="Video prompt",
                        image1="Reference image 1",
                        image2="Reference image 2",
                        image3="Reference image 3",
                        image4="Reference image 4",
                        image5="Reference image 5",
                        image6="Reference image 6",
                        image7="Reference image 7",
                        duration="Video duration",
                        resolution="Video resolution",
                    )(normal_callback),
                    duration=GROK_VIDEO_DURATION_CHOICES,
                    resolution=GROK_VIDEO_RESOLUTION_CHOICES,
                ),
            )
        )

        async def real_callback(
            interaction,
            image1=None,
            image2=None,
            image3=None,
            image4=None,
            image5=None,
            image6=None,
            duration: str = GROK_VIDEO_DEFAULT_DURATION,
            resolution: str = GROK_VIDEO_DEFAULT_RESOLUTION,
            camera_angle: str = "",
            scene: str = "",
            time: str = "",
            light_source: str = "",
            color_tone: str = "",
            nationality: str = "",
            action: str = "",
            clothing: str = "",
            lower_state: str = "",
            tattoo: str = "",
            expression: str = "",
            prompt_2: str = "",
            prompt_3: str = "",
            prompt_4: str = "",
            prompt_5: str = "",
            prompt_6: str = "",
        ):
            await self._handle_grok_video_interaction(
                interaction,
                "",
                image_attachments=_attachment_tuple(image1, image2, image3, image4, image5, image6),
                duration=duration,
                resolution=resolution,
                mode=GROK_MEDIA_MODE_REAL,
                direct=True,
                real_mode_options={
                    "selections": _grok_real_mode_selections(
                        camera_angle=camera_angle,
                        scene=scene,
                        time=time,
                        light_source=light_source,
                        color_tone=color_tone,
                        nationality=nationality,
                        action=action,
                        clothing=clothing,
                        lower_state=lower_state,
                        tattoo=tattoo,
                        expression=expression,
                    ),
                    "extra_prompts": _grok_real_mode_extra_prompts(
                        prompt_2=prompt_2,
                        prompt_3=prompt_3,
                        prompt_4=prompt_4,
                        prompt_5=prompt_5,
                        prompt_6=prompt_6,
                    ),
                },
            )

        real_callback.__name__ = "discord_grok_direct_gen_video_real"
        real_callback.__annotations__ = {
            "interaction": self.discord.Interaction,
            "image1": Optional[self.discord.Attachment],
            "image2": Optional[self.discord.Attachment],
            "image3": Optional[self.discord.Attachment],
            "image4": Optional[self.discord.Attachment],
            "image5": Optional[self.discord.Attachment],
            "image6": Optional[self.discord.Attachment],
            "duration": str,
            "resolution": str,
            "camera_angle": str,
            "scene": str,
            "time": str,
            "light_source": str,
            "color_tone": str,
            "nationality": str,
            "action": str,
            "clothing": str,
            "lower_state": str,
            "tattoo": str,
            "expression": str,
            "prompt_2": str,
            "prompt_3": str,
            "prompt_4": str,
            "prompt_5": str,
            "prompt_6": str,
        }
        real_callback = self._with_grok_real_mode_autocomplete(real_callback)
        group.add_command(
            self.app_commands.Command(
                name=GROK_MEDIA_MODE_REAL,
                description="Compose a real-mode prompt from local materials",
                callback=self._with_grok_choices(
                    self.app_commands.describe(
                        image1="Reference image 1",
                        image2="Reference image 2",
                        image3="Reference image 3",
                        image4="Reference image 4",
                        image5="Reference image 5",
                        image6="Reference image 6",
                        duration="Video duration",
                        resolution="Video resolution",
                        camera_angle="Material key or custom:<prompt>",
                        scene="Material key or custom:<prompt>",
                        time="Material key or custom:<prompt>",
                        light_source="Material key or custom:<prompt>",
                        color_tone="Material key or custom:<prompt>",
                        nationality="Material key or custom:<prompt>",
                        action="Material key or custom:<prompt>",
                        clothing="Material key or custom:<prompt>",
                        lower_state="Material key or custom:<prompt>",
                        tattoo="Tattoo key, random, or custom:<prompt>; blank means no tattoo",
                        expression="Material key or custom:<prompt>",
                        prompt_2="Role prompt for reference image 2",
                        prompt_3="Role prompt for reference image 3",
                        prompt_4="Role prompt for reference image 4",
                        prompt_5="Role prompt for reference image 5",
                        prompt_6="Role prompt for reference image 6",
                    )(real_callback),
                    duration=GROK_VIDEO_DURATION_CHOICES,
                    resolution=GROK_VIDEO_RESOLUTION_CHOICES,
                ),
            )
        )
        return group

    def _add_slash_command(self, bot, command):
        guild = self._configured_command_guild()
        if guild is None:
            bot.tree.add_command(command)
        else:
            bot.tree.add_command(command, guild=guild)
        self._registered_commands.append(command.name)

    def _configured_command_guild(self):
        guild_id = str(getattr(self, "guild_id", "") or "").strip()
        if not guild_id:
            return None
        return self.discord.Object(id=int(guild_id))

    def _with_grok_choices(self, callback, **choices):
        decorated = callback
        for option_name, values in choices.items():
            decorated = self.app_commands.choices(
                **{option_name: [self.app_commands.Choice(name=label, value=value) for label, value in values]}
            )(decorated)
        return decorated

    def _with_grok_real_mode_autocomplete(self, callback):
        if not hasattr(self.app_commands, "autocomplete"):
            return callback
        decorated = callback
        for field in GROK_REAL_MODE_ASSET_FIELDS:
            async def autocomplete(_interaction, current: str, *, field_name=field):
                return [
                    self.app_commands.Choice(name=key, value=key)
                    for key in material_choices(field_name, current, limit=25)
                ]

            decorated = self.app_commands.autocomplete(**{field: autocomplete})(decorated)
        return decorated

    async def _sync_commands(self):
        guild = self._configured_command_guild()
        if guild is not None:
            if _is_truthy(conf().get("discord_prune_global_commands_on_startup", True)):
                self.bot.tree.clear_commands(guild=None)
                global_synced = await self.bot.tree.sync()
                logger.info("[Discord] pruned global slash command(s); global command count=%d", len(global_synced))
            synced = await self.bot.tree.sync(guild=guild)
        else:
            synced = await self.bot.tree.sync()
        logger.info("[Discord] synced %d slash command(s)", len(synced))

    async def _handle_slash_interaction(self, interaction, command_text: str, args: str = ""):
        if not self._is_allowed_interaction(interaction):
            await interaction.response.send_message("Discord channel is restricted to the configured administrator/channel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=self.ephemeral_replies, thinking=True)
        context = self._build_interaction_context(interaction)
        command, baked_args = _split_slash_text(command_text)
        final_args = " ".join(part for part in (baked_args, str(args or "").strip()) if part).strip()
        query = f"/{command}" + (f" {final_args}" if final_args else "")
        reply = await self.loop.run_in_executor(_executor, self._run_cow_cli_command, query, context)
        await self._send_reply_async(reply, context)

    async def _handle_grok_image_interaction(
        self,
        interaction,
        prompt: str,
        *,
        image_attachment=None,
        image_attachments=None,
        quality: str = "speed",
        mode: str = GROK_MEDIA_DEFAULT_MODE,
        direct: bool = False,
        real_mode_options: Optional[Dict[str, Any]] = None,
    ):
        if not self._is_allowed_interaction(interaction):
            await interaction.response.send_message("Discord channel is restricted to the configured administrator/channel.", ephemeral=True)
            return
        if real_mode_options is None:
            prompt = _apply_grok_media_mode_prompt(prompt, mode)
        else:
            prompt = str(prompt or "").strip()

        await interaction.response.defer(ephemeral=self.ephemeral_replies, thinking=True)
        context = self._build_interaction_context(interaction)
        image_paths = await self._save_optional_interaction_images(
            interaction,
            image_attachments if image_attachments is not None else _attachment_tuple(image_attachment),
            context,
            max_images=GROK_IMAGE_MAX_REFERENCE_IMAGES,
        )
        if image_paths is None:
            return
        if real_mode_options is not None:
            try:
                prompt = compose_real_mode_prompt(
                    media_type="image",
                    image_count=len(image_paths),
                    selections=real_mode_options.get("selections") or {},
                    extra_prompts=real_mode_options.get("extra_prompts") or {},
                )
            except GrokRealModePromptError as exc:
                await self._send_discord_message(f"Grok real-mode prompt failed: {exc}", context)
                return
        image_value = _reference_image_value(image_paths)

        if direct:
            reply = await self.loop.run_in_executor(
                _executor,
                self._submit_direct_grok_image_job,
                prompt,
                image_value,
                quality,
                context,
            )
        else:
            reply = await self.loop.run_in_executor(
                _executor,
                self._submit_enhanced_grok_image_job,
                prompt,
                image_value,
                quality,
                context,
            )
        await self._send_reply_async(reply, context)

    async def _handle_grok_video_interaction(
        self,
        interaction,
        prompt: str,
        *,
        image_attachment=None,
        image_attachments=None,
        duration: str = GROK_VIDEO_DEFAULT_DURATION,
        resolution: str = GROK_VIDEO_DEFAULT_RESOLUTION,
        mode: str = GROK_MEDIA_DEFAULT_MODE,
        direct: bool = False,
        real_mode_options: Optional[Dict[str, Any]] = None,
    ):
        if not self._is_allowed_interaction(interaction):
            await interaction.response.send_message("Discord channel is restricted to the configured administrator/channel.", ephemeral=True)
            return
        if real_mode_options is None:
            prompt = _apply_grok_media_mode_prompt(prompt, mode)
        else:
            prompt = str(prompt or "").strip()

        await interaction.response.defer(ephemeral=self.ephemeral_replies, thinking=True)
        context = self._build_interaction_context(interaction)
        image_paths = await self._save_optional_interaction_images(
            interaction,
            image_attachments if image_attachments is not None else _attachment_tuple(image_attachment),
            context,
            max_images=GROK_VIDEO_MAX_REFERENCE_IMAGES,
        )
        if image_paths is None:
            return
        if real_mode_options is not None:
            try:
                prompt = compose_real_mode_prompt(
                    media_type="video",
                    image_count=len(image_paths),
                    selections=real_mode_options.get("selections") or {},
                    extra_prompts=real_mode_options.get("extra_prompts") or {},
                )
            except GrokRealModePromptError as exc:
                await self._send_discord_message(f"Grok real-mode prompt failed: {exc}", context)
                return
        image_value = _reference_image_value(image_paths)

        if direct:
            reply = await self.loop.run_in_executor(
                _executor,
                self._submit_grok_video_job,
                prompt,
                image_value,
                duration,
                resolution,
                context,
                False,
            )
        else:
            reply = await self.loop.run_in_executor(
                _executor,
                self._submit_grok_video_job,
                prompt,
                image_value,
                duration,
                resolution,
                context,
                True,
            )
        await self._send_reply_async(reply, context)

    async def _save_optional_interaction_image(self, interaction, image_attachment, context: Context) -> Optional[str]:
        image_paths = await self._save_optional_interaction_images(
            interaction,
            _attachment_tuple(image_attachment),
            context,
            max_images=1,
        )
        if image_paths is None:
            return None
        if not image_paths:
            return ""
        return image_paths[0]

    async def _save_optional_interaction_images(
        self,
        interaction,
        image_attachments,
        context: Context,
        *,
        max_images: int,
    ) -> Optional[List[str]]:
        attachments = _attachment_tuple(image_attachments)
        if not attachments:
            return []
        if len(attachments) > max_images:
            await self._send_discord_message(f"Up to {max_images} reference images are supported.", context)
            return None
        bucket_base = str(getattr(interaction, "id", "") or "interaction")
        image_paths: List[str] = []
        for index, attachment in enumerate(attachments, start=1):
            image_path = await self._save_interaction_image_attachment(attachment, f"{bucket_base}-image{index}")
            if not image_path:
                await self._send_discord_message(f"The uploaded file for image{index} is not a supported image.", context)
                return None
            image_paths.append(image_path)
        return image_paths

    def _submit_enhanced_grok_image_job(self, prompt: str, image_path: Any, quality: str, context: Context) -> Reply:
        return self._submit_grok_image_job(prompt, image_path, quality, context, prompt_enhancement=True)

    def _submit_direct_grok_image_job(self, prompt: str, image_path: Any, quality: str, context: Context) -> Reply:
        return self._submit_grok_image_job(prompt, image_path, quality, context, prompt_enhancement=False)

    def _submit_grok_image_job(
        self,
        prompt: str,
        image_path: Any,
        quality: str,
        context: Context,
        *,
        prompt_enhancement: bool,
    ) -> Reply:
        try:
            from agent.tools.image_generation.job_manager import get_image_generation_job_manager
            from bridge.bridge import Bridge

            profile = self._resolve_background_profile(context)
            args = _build_grok_image_job_args(
                prompt,
                image_path,
                quality=quality,
                prompt_enhancement=prompt_enhancement,
            )
            manager = get_image_generation_job_manager(Bridge().get_agent_bridge())
            job = manager.submit(args, context, profile)
            position = manager.queue_position(job)
            state = "started" if position == 0 else f"queued at position {position}"
            mode = "prompt-enhanced" if prompt_enhancement else "direct"
            enhancement_note = (
                "The installed image prompt enhancer will run before submission."
                if prompt_enhancement
                else "Prompt enhancement is disabled for this direct task."
            )
            return Reply(
                ReplyType.TEXT,
                (
                    f"Grok {mode} image task {state}. Task ID: {job.job_id}.\n"
                    f"{enhancement_note}"
                ),
            )
        except Exception as e:
            logger.warning("[Discord] Grok image submit failed: %s", e, exc_info=True)
            return Reply(ReplyType.ERROR, f"Grok image task failed: {e}")

    def _submit_grok_video_job(
        self,
        prompt: str,
        image_path: Any,
        duration: str,
        resolution: str,
        context: Context,
        prompt_enhancement: bool = True,
    ) -> Reply:
        try:
            from agent.tools.video_generation.job_manager import get_grok_video_generation_job_manager
            from bridge.bridge import Bridge

            profile = self._resolve_background_profile(context)
            args = _build_grok_video_job_args(
                prompt,
                image_path,
                duration=duration,
                resolution=resolution,
                prompt_enhancement=prompt_enhancement,
            )
            manager = get_grok_video_generation_job_manager(Bridge().get_agent_bridge())
            job = manager.submit(args, context, profile)
            position = manager.queue_position(job)
            state = "started" if position == 0 else f"queued at position {position}"
            return Reply(
                ReplyType.TEXT,
                f"Grok video generation task {state}. Task ID: {job.job_id}.\n"
                "I will send the video to this Discord channel when it finishes.",
            )
        except Exception as e:
            logger.warning("[Discord] Grok video submit failed: %s", e, exc_info=True)
            return Reply(ReplyType.ERROR, f"Grok video task failed: {e}")

    @staticmethod
    def _resolve_background_profile(context: Context):
        profile = context.get("_actor_profile")
        if profile is not None:
            return profile
        from agent.user_profiles import apply_profile_to_context, resolve_agent_user_profile

        profile = resolve_agent_user_profile(context)
        apply_profile_to_context(context, profile)
        context["_actor_profile"] = profile
        return profile

    async def _handle_message(self, message):
        if not self._is_allowed_message(message):
            return
        text = str(getattr(message, "content", "") or "").strip()
        image_paths = await self._save_image_attachments(message)
        if text:
            if image_paths:
                text = text + "\n" + "\n".join(f"[image: {path}]" for path in image_paths)
            context = self._build_message_context(message, ContextType.TEXT, text)
            self._promote_grok_media_context_for_message(context)
            self.produce(context)
            return
        for path in image_paths:
            context = self._build_message_context(message, ContextType.IMAGE, path)
            self.produce(context)

    def _run_cow_cli_command(self, query: str, context: Context) -> Reply:
        context.type = ContextType.TEXT
        context.content = query
        plugin_cls = PluginManager().plugins.get("COW_CLI")
        if plugin_cls is None:
            return Reply(ReplyType.ERROR, "CowCli plugin is not loaded")
        e_context = EventContext(
            Event.ON_HANDLE_CONTEXT,
            {"channel": self, "context": context, "reply": Reply()},
        )
        try:
            plugin_cls().on_handle_context(e_context)
        except Exception as e:
            logger.warning("[Discord] CowCli command failed: %s", e, exc_info=True)
            return Reply(ReplyType.ERROR, f"Command failed: {e}")
        reply = e_context["reply"]
        if reply and reply.type:
            return reply
        return Reply(ReplyType.ERROR, f"Command did not produce a reply: {query}")

    def _build_interaction_context(self, interaction) -> Context:
        user = interaction.user
        channel = interaction.channel
        guild = interaction.guild
        context = Context(ContextType.TEXT, "")
        self._apply_common_discord_context(context, user, channel, guild)
        context["_discord_interaction"] = interaction
        return context

    def _build_message_context(self, message, ctype: ContextType, content: str) -> Context:
        msg = DiscordMessage(message, ctype, content)
        context = Context(ctype, content)
        context["msg"] = msg
        self._apply_common_discord_context(context, message.author, message.channel, message.guild)
        return context

    def _promote_grok_media_context_for_message(self, context: Context) -> None:
        if not context or context.type != ContextType.TEXT:
            return

        content = str(context.content or "").strip()
        video_match_prefix = match_video_create_prefix(content, conf().get("video_create_prefix", []))
        if video_match_prefix:
            content = content.replace(video_match_prefix, "", 1)
            context.type = ContextType.VIDEO_CREATE
        elif self._should_promote_grok_media_create(context, content, self._explicit_or_followup_video_requested):
            context.type = ContextType.VIDEO_CREATE
        else:
            image_match_prefix = match_image_create_prefix(content, conf().get("image_create_prefix", []))
            if image_match_prefix:
                content = content.replace(image_match_prefix, "", 1)
                context.type = ContextType.IMAGE_CREATE
            elif self._should_promote_grok_media_create(context, content, explicit_image_generation_requested):
                context.type = ContextType.IMAGE_CREATE
            else:
                context.type = ContextType.TEXT

        context.content = content.strip()
        if context.type == ContextType.VIDEO_CREATE:
            context["grok_video_default_duration"] = GROK_VIDEO_DEFAULT_DURATION
            context.content = self._append_recent_image_refs_for_video_create(
                context.get("session_id", ""),
                context.content,
            )
        elif context.type == ContextType.IMAGE_CREATE:
            context.content = self._append_recent_image_ref_for_image_create(
                context.get("session_id", ""),
                context.content,
            )

    def _explicit_or_followup_video_requested(self, content: str) -> bool:
        return bool(
            explicit_video_generation_requested(content)
            or (
                self._looks_like_image_to_video_followup(content)
                and not explicit_image_generation_requested(content)
            )
        )

    def _apply_common_discord_context(self, context: Context, user, channel, guild) -> None:
        user_id = str(getattr(user, "id", "") or "")
        channel_id = str(getattr(channel, "id", "") or "")
        guild_id = str(getattr(guild, "id", "") or "")
        actor_id = f"discord:{user_id}"
        session_id = f"discord:guild:{guild_id}:channel:{channel_id}" if guild_id else f"discord:dm:{user_id}"
        context["channel_type"] = "discord"
        context["receiver"] = channel_id
        context["session_id"] = session_id
        context["conversation_id"] = session_id
        context["isgroup"] = bool(guild_id)
        context["actor_id"] = actor_id
        context["actor_role"] = "admin"
        context["_discord_channel_id"] = channel_id
        context["_discord_guild_id"] = guild_id
        try:
            from agent.user_profiles import apply_profile_to_context, resolve_agent_user_profile, safe_actor_slug

            context["memory_user_id"] = safe_actor_slug(actor_id)
            profile = resolve_agent_user_profile(context)
            apply_profile_to_context(context, profile)
            context["_actor_profile"] = profile
        except Exception as e:
            logger.debug("[Discord] failed to apply actor profile: %s", e)
            context["memory_user_id"] = actor_id.replace(":", "_")
        display_name = str(getattr(user, "display_name", "") or getattr(user, "name", "") or user_id)
        if display_name:
            context["user_label"] = display_name

    def _is_allowed_interaction(self, interaction) -> bool:
        return self._is_allowed_actor(
            user_id=str(getattr(interaction.user, "id", "") or ""),
            channel_id=str(getattr(interaction.channel, "id", "") or ""),
            guild_id=str(getattr(interaction.guild, "id", "") or ""),
        )

    def _is_allowed_message(self, message) -> bool:
        return self._is_allowed_actor(
            user_id=str(getattr(message.author, "id", "") or ""),
            channel_id=str(getattr(message.channel, "id", "") or ""),
            guild_id=str(getattr(message.guild, "id", "") or ""),
        )

    def _is_allowed_actor(self, user_id: str, channel_id: str, guild_id: str) -> bool:
        if not user_id or user_id != self.admin_user_id:
            return False
        if self.guild_id and guild_id and guild_id != self.guild_id:
            return False
        if self.allowed_channel_ids and channel_id not in self.allowed_channel_ids:
            return False
        return True

    async def _save_image_attachments(self, message) -> List[str]:
        paths = []
        for attachment in getattr(message, "attachments", []) or []:
            path = await self._save_interaction_image_attachment(attachment, str(getattr(message, "id", "") or "message"))
            if path:
                paths.append(path)
        return paths

    async def _save_interaction_image_attachment(self, attachment, bucket_id: str) -> str:
        content_type = str(getattr(attachment, "content_type", "") or "").lower()
        filename = str(getattr(attachment, "filename", "") or "attachment")
        if not (content_type.startswith("image/") or filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))):
            return ""
        safe_name = os.path.basename(filename).replace("\\", "_").replace("/", "_")
        path = os.path.join(discord_attachment_cache_dir(bucket_id), safe_name)
        await attachment.save(path)
        return path

    def send(self, reply: Reply, context: Context):
        if not self.bot or not self.loop or self.loop.is_closed():
            logger.warning("[Discord] send skipped because bot loop is unavailable")
            return False
        future = asyncio.run_coroutine_threadsafe(self._send_reply_async(reply, context), self.loop)
        try:
            return future.result(timeout=30)
        except Exception as e:
            logger.warning("[Discord] send failed: %s", e, exc_info=True)
            return False

    async def _send_reply_async(self, reply: Reply, context: Context):
        if not reply or not reply.type:
            return False
        if reply.type in (ReplyType.TEXT, ReplyType.TEXT_, ReplyType.ERROR, ReplyType.INFO):
            return await self._send_discord_message(_truncate_discord_text(reply.content), context)
        if reply.type in (ReplyType.IMAGE, ReplyType.FILE, ReplyType.VIDEO):
            return await self._send_discord_file(str(reply.content or ""), context)
        if reply.type in (ReplyType.IMAGE_URL, ReplyType.VIDEO_URL):
            content = str(reply.content or "")
            if content.startswith("file://"):
                return await self._send_discord_file(content[7:], context)
            return await self._send_discord_message(content, context)
        return await self._send_discord_message(f"Unsupported reply type: {reply.type}", context)

    async def _send_discord_message(self, text: str, context: Context):
        text = text or "(empty)"
        interaction = context.get("_discord_interaction") if context else None
        if interaction is not None:
            await interaction.followup.send(text, ephemeral=self.ephemeral_replies)
            return True
        channel = await self._resolve_send_channel(context)
        if channel is None:
            return False
        await channel.send(text)
        return True

    async def _send_discord_file(self, path: str, context: Context):
        path = str(path or "").strip()
        if not path:
            return False
        if path.startswith("file://"):
            path = path[7:]
        if not os.path.exists(path):
            return await self._send_discord_message(path, context)
        filename = os.path.basename(path) or "file"
        interaction = context.get("_discord_interaction") if context else None
        discord_file = self.discord.File(path, filename=filename)
        if interaction is not None:
            await interaction.followup.send(file=discord_file, ephemeral=self.ephemeral_replies)
            return True
        channel = await self._resolve_send_channel(context)
        if channel is None:
            return False
        await channel.send(file=discord_file)
        return True

    async def _resolve_send_channel(self, context: Context):
        channel_id = str((context.get("_discord_channel_id") if context else "") or (context.get("receiver") if context else "") or "")
        if not channel_id:
            return None
        try:
            channel = self.bot.get_channel(int(channel_id))
            if channel is not None:
                return channel
            return await self.bot.fetch_channel(int(channel_id))
        except Exception as e:
            logger.warning("[Discord] failed to resolve channel %s: %s", channel_id, e)
            return None
