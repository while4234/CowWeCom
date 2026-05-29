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
from common.log import logger
from common.singleton import singleton
from config import conf
from plugins import Event, EventContext
from plugins.plugin_manager import PluginManager


CHAT_COMMAND_FALLBACKS = {
    "help": "查看可用 CowCli 命令",
    "version": "查看版本",
    "status": "查看运行状态",
    "logs": "查看日志",
    "skill": "管理或查询 Skills",
    "context": "管理上下文",
    "config": "查看或修改配置",
    "knowledge": "管理知识库",
    "memory": "管理记忆",
    "backend": "查看或切换模型后端",
    "voice": "查看或切换语音模式",
    "updates": "查看项目更新",
    "ledger": "查询本地账本",
    "tokens": "查询本地 token 用量",
    "grok-direct": "Grok 直出图片或视频",
    "install-browser": "安装浏览器依赖",
}
CLI_ONLY_COMMANDS = {"start", "stop", "restart"}
DISCORD_COMMAND_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
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
        from plugins.cow_cli.cow_cli import CowCliPlugin

        suggestions = CowCliPlugin().slash_command_suggestions(is_admin=True)
    except Exception as e:
        logger.warning("[Discord] CowCli suggestions unavailable, using fallback commands: %s", e)
        suggestions = [
            {"cmd": f"/{name}", "desc": desc}
            for name, desc in CHAT_COMMAND_FALLBACKS.items()
            if name not in CLI_ONLY_COMMANDS
        ]
    return _normalise_cow_cli_suggestions(suggestions)


def _normalise_cow_cli_suggestions(suggestions: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    commands = []
    seen = set()
    for item in suggestions:
        command_text = str(item.get("cmd") or "").strip()
        name = _discord_command_name(command_text)
        if not name or name in CLI_ONLY_COMMANDS or name in seen:
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


def _build_grok_media_shortcut_query(kind: str, prompt: str, image_path: str = "") -> str:
    prompt = str(prompt or "").strip()
    if image_path:
        prompt = (prompt + "\n" if prompt else "") + f"[image: {image_path}]"
    return f"/grok-direct {kind} -- {prompt}".strip()


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
        self.message_content_enabled = _is_truthy(conf().get("discord_message_content_enabled", False))

        intents = discord.Intents.default()
        if self.message_content_enabled:
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
            if not self.message_content_enabled:
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
            bot.tree.add_command(command)
            self._registered_commands.append(command_name)

        async def imagine_callback(interaction, prompt: str):
            await self._handle_slash_interaction(interaction, "/grok-direct image", f"-- {prompt}")

        imagine_callback.__name__ = "discord_imagine"
        imagine_callback.__annotations__ = {"interaction": self.discord.Interaction, "prompt": str}
        bot.tree.add_command(
            self.app_commands.Command(
                name="imagine",
                description="Grok image generation shortcut",
                callback=self.app_commands.describe(prompt="Image prompt")(imagine_callback),
            )
        )
        self._registered_commands.append("imagine")

        async def video_callback(interaction, prompt: str):
            await self._handle_media_shortcut_interaction(interaction, "video", prompt)

        video_callback.__name__ = "discord_video"
        video_callback.__annotations__ = {"interaction": self.discord.Interaction, "prompt": str}
        bot.tree.add_command(
            self.app_commands.Command(
                name="video",
                description="Grok text-to-video shortcut",
                callback=self.app_commands.describe(prompt="Video prompt")(video_callback),
            )
        )
        self._registered_commands.append("video")

        async def image_to_video_callback(interaction, prompt: str, image):
            await self._handle_media_shortcut_interaction(interaction, "video", prompt, image)

        image_to_video_callback.__name__ = "discord_image_to_video"
        image_to_video_callback.__annotations__ = {
            "interaction": self.discord.Interaction,
            "prompt": str,
            "image": self.discord.Attachment,
        }
        bot.tree.add_command(
            self.app_commands.Command(
                name="image-to-video",
                description="Grok image-to-video shortcut",
                callback=self.app_commands.describe(prompt="Video prompt", image="Reference image")(image_to_video_callback),
            )
        )
        self._registered_commands.append("image-to-video")

    async def _sync_commands(self):
        if self.guild_id:
            guild = self.discord.Object(id=int(self.guild_id))
            self.bot.tree.copy_global_to(guild=guild)
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

    async def _handle_media_shortcut_interaction(self, interaction, kind: str, prompt: str, image_attachment=None):
        if not self._is_allowed_interaction(interaction):
            await interaction.response.send_message("Discord channel is restricted to the configured administrator/channel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=self.ephemeral_replies, thinking=True)
        context = self._build_interaction_context(interaction)
        image_path = ""
        if image_attachment is not None:
            image_path = await self._save_interaction_image_attachment(image_attachment, str(getattr(interaction, "id", "") or "interaction"))
        query = _build_grok_media_shortcut_query(kind, prompt, image_path)
        reply = await self.loop.run_in_executor(_executor, self._run_cow_cli_command, query, context)
        await self._send_reply_async(reply, context)

    async def _handle_message(self, message):
        if not self._is_allowed_message(message):
            return
        text = str(getattr(message, "content", "") or "").strip()
        image_paths = await self._save_image_attachments(message)
        if text:
            if image_paths:
                text = text + "\n" + "\n".join(f"[image: {path}]" for path in image_paths)
            context = self._build_message_context(message, ContextType.TEXT, text)
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
