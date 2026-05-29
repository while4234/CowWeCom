import os
import time

from bridge.context import ContextType
from channel.chat_message import ChatMessage
from common.utils import expand_path
from config import conf


def discord_attachment_cache_dir(message_id: str) -> str:
    workspace = expand_path(conf().get("agent_workspace", "~/cow") or "~/cow")
    base_dir = os.path.join(workspace, "attachments", "discord", str(message_id or "unknown"))
    os.makedirs(base_dir, exist_ok=True)
    return base_dir


class DiscordMessage(ChatMessage):
    def __init__(self, raw_message, ctype: ContextType, content: str):
        super().__init__(raw_message)
        author = getattr(raw_message, "author", None)
        channel = getattr(raw_message, "channel", None)
        guild = getattr(raw_message, "guild", None)
        channel_id = str(getattr(channel, "id", "") or "")
        guild_id = str(getattr(guild, "id", "") or "")
        author_id = str(getattr(author, "id", "") or "")

        self.msg_id = str(getattr(raw_message, "id", "") or "")
        self.create_time = int(time.time())
        self.ctype = ctype
        self.content = content
        self.from_user_id = author_id
        self.from_user_nickname = str(getattr(author, "display_name", None) or getattr(author, "name", "") or author_id)
        self.to_user_id = channel_id
        self.to_user_nickname = str(getattr(channel, "name", "") or channel_id)
        self.other_user_id = channel_id or author_id
        self.other_user_nickname = self.to_user_nickname or self.from_user_nickname
        self.is_group = bool(guild_id)
        self.is_at = False
        self.actual_user_id = author_id
        self.actual_user_nickname = self.from_user_nickname
        self.at_list = []
        self.my_msg = False
