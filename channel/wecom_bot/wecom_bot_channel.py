"""
WeCom (企业微信) AI Bot channel via WebSocket long connection.

Supports:
- Single chat and group chat (text / image / file input & output)
- Scheduled task push via aibot_send_msg
- Heartbeat keep-alive and auto-reconnect
"""

import base64
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import uuid

import websocket

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel
from channel.wecom_bot.wecom_bot_message import WecomBotMessage
from common.expired_dict import ExpiredDict
from common.image_generation_routing import (
    explicit_image_generation_requested,
    explicit_video_generation_requested,
    match_image_create_prefix,
    match_video_create_prefix,
)
from common.image_send_limits import image_send_dimensions_from_config, prepare_image_for_send
from common.log import logger
from common.singleton import singleton
from common.utils import expand_path
from common.ws_client_compat import websocket_app_run_forever
from config import conf
from agent.user_profiles import safe_actor_slug
from integrations.hermes_xai.media_download import (
    cleanup_generated_reply_media,
    remove_file_quietly,
    safe_download_to_file,
)
from voice.audio_convert import split_audio_by_wecom_voice_limits

WECOM_WS_URL = "wss://openws.work.weixin.qq.com"
HEARTBEAT_INTERVAL = 30
SUBSCRIBE_ACK_TIMEOUT = 20
MEDIA_CHUNK_SIZE = 512 * 1024  # 512KB per chunk (before base64 encoding)
MARKDOWN_TEXT_CHUNK_LIMIT = 3500
LONG_REPLY_PART_DELAY_SECONDS = 0.2
REMOTE_IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
REMOTE_VIDEO_CONTENT_TYPES = {"video/mp4", "application/octet-stream"}
MAX_REMOTE_IMAGE_BYTES = 25 * 1024 * 1024
MAX_REMOTE_VIDEO_BYTES = 512 * 1024 * 1024
WECOM_BOT_IMAGE_MAX_BYTES = 2 * 1024 * 1024


def _wecom_group_actor_id(channel_type: str, chat_id: str) -> str:
    return f"{channel_type}:group:{chat_id}"


def _wecom_group_memory_user_id(channel_type: str, chat_id: str) -> str:
    return safe_actor_slug(_wecom_group_actor_id(channel_type, chat_id))


def _wecom_actor_id(channel_type: str, raw_user_id: str) -> str:
    return f"{channel_type}:{raw_user_id}" if channel_type and raw_user_id else ""


def _normalise_role(value) -> str:
    return "admin" if str(value or "").strip().lower() == "admin" else "user"


def _configured_admin_user_values() -> set:
    try:
        from config import global_config
    except Exception:
        global_config = {"admin_users": []}

    admin_users = conf().get("agent_admin_users", []) or []
    if isinstance(admin_users, str):
        configured = {item.strip() for item in admin_users.split(",") if item.strip()}
    else:
        configured = {str(item).strip() for item in admin_users if str(item).strip()}
    configured.update(
        str(item).strip()
        for item in (global_config.get("admin_users", []) or [])
        if str(item).strip()
    )
    return configured


def _read_relative_workspace_file(relative_path: str) -> str:
    relative_path = str(relative_path or "").replace("\\", "/").lstrip("/")
    if not relative_path:
        return ""
    try:
        workspace = Path(expand_path(conf().get("agent_workspace", "~/cow"))).resolve()
        path = (workspace / relative_path).resolve()
        path.relative_to(workspace)
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8")
    except Exception as e:
        logger.debug(f"[WecomBot] Failed to read group member profile: {e}")
    return ""


def _single_line(value) -> str:
    return " ".join(str(value or "").split())


def _escape_control_chars_inside_json_strings(s: str) -> str:
    """Escape U+0000–U+001F inside JSON string values so json.loads accepts WeCom payloads.

    The server occasionally emits raw newlines/tabs inside quoted fields, which is
    invalid strict JSON but recoverable without touching escapes like \\n or \\".
    """
    out = []
    in_string = False
    escape = False
    for c in s:
        if escape:
            out.append(c)
            escape = False
            continue
        if in_string and c == "\\":
            out.append(c)
            escape = True
            continue
        if c == '"':
            out.append(c)
            in_string = not in_string
            continue
        if in_string and ord(c) < 32:
            out.append("\\u%04x" % ord(c))
            continue
        out.append(c)
    return "".join(out)


def _loads_wecom_ws_json(raw):
    """Parse WebSocket JSON; tolerate unescaped control characters inside strings."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        raw = str(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        msg = str(e).lower()
        if "control character" in msg:
            return json.loads(_escape_control_chars_inside_json_strings(raw))
        raise


def _save_config_patch(patch: dict) -> None:
    config_path = Path(__file__).resolve().parents[2] / "config.json"
    try:
        if config_path.exists():
            with config_path.open("r", encoding="utf-8-sig") as f:
                file_cfg = json.load(f)
        else:
            file_cfg = {}
        file_cfg.update(patch)
        with config_path.open("w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"[WecomBot] Failed to persist user profile mapping: {e}")


@singleton
class WecomBotChannel(ChatChannel):
    NOT_SUPPORT_REPLYTYPE = []

    def __init__(self):
        super().__init__()
        self.bot_id = ""
        self.bot_secret = ""
        self.received_msgs = ExpiredDict(60 * 60 * 7.1)
        self._ws = None
        self._ws_thread = None
        self._heartbeat_thread = None
        self._subscribe_timeout_timer = None
        self._connected = False
        self._stop_event = threading.Event()
        self._pending_responses = {}  # req_id -> (threading.Event, result_holder)
        self._pending_lock = threading.Lock()
        self._stream_states = {}  # req_id -> {"stream_id": str, "content": str}

        conf()["group_name_white_list"] = ["ALL_GROUP"]
        conf()["single_chat_prefix"] = [""]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def startup(self):
        self.bot_id = conf().get("wecom_bot_id", "")
        self.bot_secret = conf().get("wecom_bot_secret", "")

        if not self.bot_id or not self.bot_secret:
            err = "[WecomBot] wecom_bot_id and wecom_bot_secret are required"
            logger.error(err)
            self.report_startup_error(err)
            return

        self._stop_event.clear()
        self._start_ws()

    def stop(self):
        logger.info("[WecomBot] stop() called")
        self._stop_event.set()
        self._cancel_subscribe_timeout()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = None
        self._connected = False

    # ------------------------------------------------------------------
    # WebSocket connection
    # ------------------------------------------------------------------

    def _start_ws(self):
        def _on_open(ws):
            logger.info("[WecomBot] WebSocket connected, sending subscribe...")
            self._send_subscribe()
            self._arm_subscribe_timeout(ws)

        def _on_message(ws, raw):
            try:
                data = _loads_wecom_ws_json(raw)
                self._handle_ws_message(data)
            except Exception as e:
                logger.error(f"[WecomBot] Failed to handle ws message: {e}", exc_info=True)

        def _on_error(ws, error):
            logger.error(f"[WecomBot] WebSocket error: {error}")

        def _on_close(ws, close_status_code, close_msg):
            logger.warning(f"[WecomBot] WebSocket closed: status={close_status_code}, msg={close_msg}")
            self._cancel_subscribe_timeout()
            self._connected = False
            if not self._stop_event.is_set():
                logger.info("[WecomBot] Will reconnect in 5s...")
                time.sleep(5)
                if not self._stop_event.is_set():
                    self._start_ws()

        self._ws = websocket.WebSocketApp(
            WECOM_WS_URL,
            on_open=_on_open,
            on_message=_on_message,
            on_error=_on_error,
            on_close=_on_close,
        )

        def run_forever():
            try:
                websocket_app_run_forever(self._ws, ping_interval=0, reconnect=0)
            except (SystemExit, KeyboardInterrupt):
                logger.info("[WecomBot] WebSocket thread interrupted")
            except Exception as e:
                logger.error(f"[WecomBot] WebSocket run_forever error: {e}")

        self._ws_thread = threading.Thread(target=run_forever, daemon=True)
        self._ws_thread.start()
        self._ws_thread.join()

    def _ws_send(self, data: dict) -> bool:
        if not self._ws:
            logger.warning("[WecomBot] Cannot send websocket payload: channel is not connected")
            return False
        if data.get("cmd") != "aibot_subscribe" and not getattr(self, "_connected", True):
            logger.warning("[WecomBot] Cannot send websocket payload: channel is not ready")
            return False
        try:
            self._ws.send(json.dumps(data, ensure_ascii=False))
            return True
        except Exception as e:
            logger.error(f"[WecomBot] Websocket send failed: {e}", exc_info=True)
            return False

    def _gen_req_id(self) -> str:
        return uuid.uuid4().hex[:16]

    # ------------------------------------------------------------------
    # Subscribe & heartbeat
    # ------------------------------------------------------------------

    def _send_subscribe(self):
        self._ws_send({
            "cmd": "aibot_subscribe",
            "headers": {"req_id": self._gen_req_id()},
            "body": {
                "bot_id": self.bot_id,
                "secret": self.bot_secret,
            },
        })

    def _arm_subscribe_timeout(self, ws, timeout: float = SUBSCRIBE_ACK_TIMEOUT):
        self._cancel_subscribe_timeout()

        def close_if_unsubscribed():
            if self._stop_event.is_set() or self._connected or ws is not self._ws:
                return
            logger.warning("[WecomBot] Subscribe ack timed out; closing websocket to reconnect")
            try:
                ws.close()
            except Exception as e:
                logger.warning(f"[WecomBot] Failed to close websocket after subscribe timeout: {e}")

        timer = threading.Timer(timeout, close_if_unsubscribed)
        timer.daemon = True
        self._subscribe_timeout_timer = timer
        timer.start()

    def _cancel_subscribe_timeout(self):
        timer = self._subscribe_timeout_timer
        self._subscribe_timeout_timer = None
        if timer:
            timer.cancel()

    def _start_heartbeat(self):
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return

        def heartbeat_loop():
            while not self._stop_event.is_set() and self._connected:
                try:
                    self._ws_send({
                        "cmd": "ping",
                        "headers": {"req_id": self._gen_req_id()},
                    })
                except Exception as e:
                    logger.warning(f"[WecomBot] Heartbeat send failed: {e}")
                    break
                self._stop_event.wait(HEARTBEAT_INTERVAL)

        self._heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    # ------------------------------------------------------------------
    # Incoming message dispatch
    # ------------------------------------------------------------------

    def _send_and_wait(self, data: dict, timeout: float = 15) -> dict:
        """Send a ws message and wait for the matching response by req_id."""
        req_id = data.get("headers", {}).get("req_id", "")
        event = threading.Event()
        holder = {"data": None}
        with self._pending_lock:
            self._pending_responses[req_id] = (event, holder)
        self._ws_send(data)
        event.wait(timeout=timeout)
        with self._pending_lock:
            self._pending_responses.pop(req_id, None)
        return holder["data"] or {}

    def _handle_ws_message(self, data: dict):
        cmd = data.get("cmd", "")
        errcode = data.get("errcode")
        req_id = data.get("headers", {}).get("req_id", "")

        # Check if this is a response to a pending request
        if req_id:
            with self._pending_lock:
                pending = self._pending_responses.get(req_id)
            if pending:
                event, holder = pending
                holder["data"] = data
                event.set()
                return

        # Subscribe response (only handle once before connected)
        if errcode is not None and cmd == "":
            if not self._connected:
                self._cancel_subscribe_timeout()
                if errcode == 0:
                    logger.info("[WecomBot] ✅ Subscribe success")
                    self._connected = True
                    self._start_heartbeat()
                    self.report_startup_success()
                else:
                    errmsg = data.get("errmsg", "unknown error")
                    logger.error(f"[WecomBot] Subscribe failed: errcode={errcode}, errmsg={errmsg}")
                    self.report_startup_error(errmsg)
            return

        if cmd == "aibot_msg_callback":
            self._handle_msg_callback(data)
        elif cmd == "aibot_event_callback":
            self._handle_event_callback(data)
        elif cmd == "":
            if errcode and errcode != 0:
                logger.warning(f"[WecomBot] Response error: {data}")

    # ------------------------------------------------------------------
    # Message callback
    # ------------------------------------------------------------------

    def _handle_msg_callback(self, data: dict):
        body = data.get("body", {})
        req_id = data.get("headers", {}).get("req_id", "")
        msg_id = body.get("msgid", "")

        if self.received_msgs.get(msg_id):
            logger.debug(f"[WecomBot] Duplicate msg filtered: {msg_id}")
            return
        self.received_msgs[msg_id] = True

        chattype = body.get("chattype", "single")
        is_group = chattype == "group"
        if is_group and not str(body.get("chatid", "") or "").strip():
            logger.warning("[WecomBot] Dropping group message without chatid")
            return

        try:
            wecom_msg = WecomBotMessage(body, is_group=is_group)
        except NotImplementedError as e:
            logger.warning(f"[WecomBot] {e}")
            return
        except Exception as e:
            logger.error(f"[WecomBot] Failed to parse message: {e}", exc_info=True)
            return

        wecom_msg.req_id = req_id

        # File cache logic (same pattern as feishu)
        from channel.file_cache import get_file_cache
        file_cache = get_file_cache()

        session_id = self._message_cache_session_id(wecom_msg)
        if not session_id:
            logger.warning("[WecomBot] Dropping message without a stable session id")
            return

        if wecom_msg.ctype == ContextType.IMAGE:
            if hasattr(wecom_msg, "image_path") and wecom_msg.image_path:
                context = self._compose_context(
                    ContextType.IMAGE,
                    wecom_msg.image_path,
                    isgroup=is_group,
                    msg=wecom_msg,
                    no_need_at=True,
                )
                if context:
                    self._remember_social_bridge_user(context, wecom_msg)
                    if self._register_image_recognition_context(context):
                        return
                file_cache.add(session_id, wecom_msg.image_path, file_type="image")
                logger.info(f"[WecomBot] Image cached for session {session_id}")
            return

        if wecom_msg.ctype == ContextType.FILE:
            wecom_msg.prepare()
            file_cache.add(session_id, wecom_msg.content, file_type="file")
            logger.info(f"[WecomBot] File cached for session {session_id}: {wecom_msg.content}")
            return

        if wecom_msg.ctype == ContextType.TEXT:
            cached_files = file_cache.get(session_id)
            if cached_files:
                file_refs = []
                for fi in cached_files:
                    ftype = fi["type"]
                    fpath = fi["path"]
                    if ftype == "image":
                        file_refs.append(f"[图片: {fpath}]")
                    elif ftype == "video":
                        file_refs.append(f"[视频: {fpath}]")
                    else:
                        file_refs.append(f"[文件: {fpath}]")
                wecom_msg.content = wecom_msg.content + "\n" + "\n".join(file_refs)
                logger.info(f"[WecomBot] Attached {len(cached_files)} cached file(s)")
                file_cache.clear(session_id)
            wecom_msg.content = self._append_image_recognition_context(session_id, wecom_msg.content)

        context = self._compose_context(
            wecom_msg.ctype,
            wecom_msg.content,
            isgroup=is_group,
            msg=wecom_msg,
            no_need_at=True,
        )
        if context:
            self._remember_social_bridge_user(context, wecom_msg)
            if req_id:
                mention_user_ids, mention_display_names = self._reply_mention_target(context)
                context["on_event"] = self._make_stream_callback(
                    req_id,
                    mention_user_ids=mention_user_ids,
                    mention_display_names=mention_display_names,
                    context=context,
                )
            self.produce(context)

    def _message_cache_session_id(self, wecom_msg: WecomBotMessage) -> str:
        if wecom_msg.is_group:
            return str(wecom_msg.other_user_id or "").strip()
        return str(wecom_msg.from_user_id or "").strip()

    def _remember_social_bridge_user(self, context: Context, wecom_msg: WecomBotMessage) -> None:
        """Best-effort bridge directory registration for reachable WeCom single-chat users."""
        if wecom_msg.is_group:
            return

        raw_user_id = str(getattr(wecom_msg, "from_user_id", "") or "").strip()
        if not raw_user_id:
            return

        display_name = (
            str(getattr(wecom_msg, "actual_user_nickname", "") or "").strip()
            or raw_user_id
        )
        self._register_single_chat_user(raw_user_id, display_name, context=context)

    def _register_single_chat_user(self, raw_user_id: str, display_name: str = "", context: Context = None) -> None:
        """Best-effort bridge directory registration for reachable WeCom single-chat users."""
        raw_user_id = str(raw_user_id or "").strip()
        if not raw_user_id:
            return
        try:
            from agent.social_bridge import get_bridge_store, get_social_bridge_service
            from agent.user_profiles import apply_profile_to_context, resolve_agent_user_profile

            if context is None:
                context = {
                    "channel_type": self.channel_type,
                    "msg": SimpleNamespace(from_user_id=raw_user_id, actual_user_id=None),
                    "session_id": raw_user_id,
                }
            channel_type = str(context.get("channel_type") or self.channel_type or "wecom_bot")
            context["channel_type"] = channel_type
            context["actor_id"] = _wecom_actor_id(channel_type, raw_user_id)
            context["actor_role"] = self._resolve_single_chat_role(raw_user_id, channel_type)
            profile = resolve_agent_user_profile(context)
            apply_profile_to_context(context, profile)
            display_name = display_name or profile.display_name or raw_user_id
            self._remember_agent_user_profile(profile, raw_user_id, display_name)
            get_bridge_store().register_user(
                actor_user_id=profile.actor_id,
                memory_user_id=profile.memory_user_id,
                display_name=display_name,
                metadata={
                    "channel_type": profile.channel_type,
                    "platform": "wecom_bot",
                    "raw_user_id": raw_user_id,
                    "receiver": raw_user_id,
                    "public_name": display_name,
                    "can_active_send": True,
                    "is_group": False,
                },
            )
            limit = int(conf().get("social_bridge_auto_retry_limit", 5) or 5)
            result = get_social_bridge_service().retry_pending_for_target(profile.actor_id, limit=limit)
            retried = [item for item in result.get("retried", []) if item.get("delivered")]
            if retried:
                logger.info(
                    f"[WecomBot] Retried {len(retried)} pending social bridge message(s) "
                    f"after single-chat activity"
                )
        except Exception as e:
            logger.debug(f"[WecomBot] Social bridge user registration skipped: {e}")

    @classmethod
    def _configured_wecom_admin_actor_ids(cls, channel_type: str = "wecom_bot") -> set:
        channel_type = str(channel_type or "wecom_bot").strip() or "wecom_bot"
        prefix = f"{channel_type}:"
        values = set()

        values.update(item for item in _configured_admin_user_values() if item.startswith(prefix))

        profiles = conf().get("agent_user_profiles", {}) or {}
        if isinstance(profiles, dict):
            for actor_id, profile in profiles.items():
                if not isinstance(profile, dict):
                    continue
                if _normalise_role(profile.get("role")) != "admin":
                    continue
                actor_text = str(actor_id or "").strip()
                platform = str(profile.get("platform") or profile.get("channel_type") or "").strip()
                if actor_text.startswith(prefix) or platform == channel_type:
                    values.add(actor_text)

        return {value for value in values if value}

    @classmethod
    def _is_configured_wecom_admin(cls, raw_user_id: str, channel_type: str = "wecom_bot") -> bool:
        raw_user_id = str(raw_user_id or "").strip()
        if not raw_user_id:
            return False

        actor_id = _wecom_actor_id(channel_type, raw_user_id)
        prefix = f"{channel_type}:"
        candidates = {raw_user_id, actor_id}

        if candidates & _configured_admin_user_values():
            return True

        profiles = conf().get("agent_user_profiles", {}) or {}
        if not isinstance(profiles, dict):
            return False
        for candidate in candidates:
            profile = profiles.get(candidate)
            if not isinstance(profile, dict) or _normalise_role(profile.get("role")) != "admin":
                continue
            actor_text = str(candidate or "").strip()
            platform = str(profile.get("platform") or profile.get("channel_type") or "").strip()
            if (
                actor_text == actor_id
                or actor_text.startswith(prefix)
                or platform == channel_type
                or (actor_text == raw_user_id and not platform)
            ):
                return True
        return False

    @classmethod
    def _resolve_single_chat_role(cls, raw_user_id: str, channel_type: str = "wecom_bot") -> str:
        if cls._is_configured_wecom_admin(raw_user_id, channel_type):
            return "admin"
        if cls._configured_wecom_admin_actor_ids(channel_type):
            return "user"
        return "admin"

    @staticmethod
    def _remember_agent_user_profile(profile, raw_user_id: str, display_name: str) -> None:
        """Persist discovered WeCom users so console/user management stays in sync."""
        try:
            profiles = conf().get("agent_user_profiles", {}) or {}
            if not isinstance(profiles, dict):
                profiles = {}
            profiles = dict(profiles)

            current = dict(profiles.get(profile.actor_id, {}) or {})
            changed = False
            role = "admin" if _normalise_role(current.get("role")) == "admin" else (profile.role or "user")
            defaults = {
                "display_name": display_name or raw_user_id,
                "raw_user_id": raw_user_id,
                "platform": "wecom_bot",
                "channel_type": profile.channel_type,
                "role": role,
                "memory_user_id": profile.memory_user_id,
            }
            for key, value in defaults.items():
                if value and current.get(key) != value:
                    current[key] = value
                    changed = True

            if profiles.get(profile.actor_id) != current:
                profiles[profile.actor_id] = current
                changed = True

            if changed:
                conf()["agent_user_profiles"] = profiles
                _save_config_patch({"agent_user_profiles": profiles})
        except Exception as e:
            logger.debug(f"[WecomBot] Failed to remember agent profile for {raw_user_id}: {e}")

    # ------------------------------------------------------------------
    # Event callback
    # ------------------------------------------------------------------

    def _handle_event_callback(self, data: dict):
        body = data.get("body", {})
        event = body.get("event", {})
        event_type = event.get("eventtype", "")

        if event_type == "enter_chat":
            from_user = body.get("from", {}) if isinstance(body.get("from"), dict) else {}
            user_id = str(from_user.get("userid") or "").strip()
            display_name = str(
                from_user.get("name")
                or from_user.get("nickname")
                or from_user.get("display_name")
                or user_id
            ).strip()
            logger.info(f"[WecomBot] User entered chat: {user_id}")
            self._register_single_chat_user(user_id, display_name)
        elif event_type == "disconnected_event":
            logger.warning("[WecomBot] Received disconnected_event, another connection took over")
        else:
            logger.debug(f"[WecomBot] Event: {event_type}")

    # ------------------------------------------------------------------
    # Stream callback (for agent on_event)
    # ------------------------------------------------------------------

    def _make_stream_callback(self, req_id: str, mention_user_ids=None, mention_display_names=None, context: Context = None):
        """Build an on_event callback that pushes agent stream deltas to wecom via stream message.

        All intermediate segments (thinking before tool calls) and the final answer
        are accumulated into a single stream message, separated by '---'.
        Throttles push to at most once per 100ms to avoid WebSocket congestion.
        """
        stream_id = uuid.uuid4().hex[:16]
        self._stream_states[req_id] = {
            "stream_id": stream_id,
            "committed": "",
            "current": "",
            "last_push_time": 0,
            "last_push_len": 0,
            "mention_user_ids": self._as_list(mention_user_ids),
            "mention_display_names": self._as_list(mention_display_names),
        }

        def _push_stream(state: dict, force: bool = False):
            """Push current stream content to wecom (throttled unless forced)."""
            now = time.time()
            if not force and now - state["last_push_time"] < 0.1:
                return False
            content = self._with_mentions(
                state["committed"] + state["current"],
                state.get("mention_user_ids"),
                state.get("mention_display_names"),
                enabled=True,
            )
            if len(content) == state["last_push_len"]:
                return False
            sent = self._ws_send({
                "cmd": "aibot_respond_msg",
                "headers": {"req_id": req_id},
                "body": {
                    "msgtype": "stream",
                    "stream": {
                        "id": state["stream_id"],
                        "finish": False,
                        "content": content,
                    },
                },
            })
            if not sent:
                logger.warning("[WecomBot] Stream push failed")
                return False
            state["last_push_time"] = now
            state["last_push_len"] = len(content)
            return True

        def on_event(event: dict):
            event_type = event.get("type")
            data = event.get("data", {})
            state = self._stream_states.get(req_id)
            if not state:
                return
            if context and context.get("voice_stream_active"):
                if event_type in {"agent_end", "error", "cancelled"} or (
                    event_type == "message_end" and not data.get("tool_calls")
                ):
                    self._stream_states.pop(req_id, None)
                return False

            if event_type == "turn_start":
                state["current"] = ""
                return False

            elif event_type == "message_update":
                delta = data.get("delta", "")
                if delta:
                    state["current"] += delta
                    return _push_stream(state)
                return False

            elif event_type == "message_end":
                tool_calls = data.get("tool_calls", [])
                if tool_calls:
                    if state["current"].strip():
                        state["committed"] += state["current"].strip() + "\n\n---\n\n"
                        state["current"] = ""
                else:
                    state["committed"] += state["current"]
                    state["current"] = ""
                return _push_stream(state, force=True)
            return False

        return on_event

    # ------------------------------------------------------------------
    # _compose_context (same pattern as feishu)
    # ------------------------------------------------------------------

    def _compose_context(self, ctype: ContextType, content, **kwargs):
        context = Context(ctype, content)
        context.kwargs = kwargs
        if "channel_type" not in context:
            context["channel_type"] = self.channel_type
        if "origin_ctype" not in context:
            context["origin_ctype"] = ctype
        if getattr(context["msg"], "input_is_voice", False) or getattr(context["msg"], "source_msgtype", "") == "voice":
            context["input_is_voice"] = True
            context["source_msgtype"] = "voice"
            context["origin_ctype"] = ContextType.VOICE

        cmsg = context["msg"]

        if cmsg.is_group:
            chat_id = str(cmsg.other_user_id or "").strip()
            if not chat_id:
                logger.warning("[WecomBot] Cannot compose group context without chatid")
                return None
            group_actor_id = _wecom_group_actor_id(self.channel_type, chat_id)
            group_memory_user_id = _wecom_group_memory_user_id(self.channel_type, chat_id)
            sender_id = str(cmsg.actual_user_id or cmsg.from_user_id or "").strip()
            raw_sender_label = (
                str(getattr(cmsg, "actual_user_nickname", "") or "").strip()
                or str(getattr(cmsg, "from_user_nickname", "") or "").strip()
                or sender_id
            )
            sender_label = self._configured_member_display_name(sender_id, raw_sender_label, chat_id)
            group_chat_name = str(getattr(cmsg, "other_user_nickname", "") or "").strip()
            if group_chat_name == chat_id:
                group_chat_name = ""
            self._remember_group_member(chat_id, sender_id, sender_label)
            known_group_members = self._known_group_members(chat_id)
            if sender_id and not any(member.get("user_id") == sender_id for member in known_group_members):
                known_group_members.insert(0, {"user_id": sender_id, "name": sender_label})

            context["session_id"] = chat_id
            context["actor_id"] = group_actor_id
            context["actor_role"] = "user"
            context["conversation_id"] = group_actor_id
            context["memory_user_id"] = group_memory_user_id
            context["group_chat_id"] = chat_id
            context["group_chat_name"] = group_chat_name
            context["group_known_members"] = known_group_members
            context["group_sender_id"] = sender_id
            context["group_sender_label"] = sender_label
            context["group_member_profile_path"] = (
                f"memory/users/{group_memory_user_id}/members/"
                f"{safe_actor_slug(sender_id)}/USER.md"
            )
        else:
            context["session_id"] = cmsg.from_user_id

        context["receiver"] = cmsg.other_user_id

        if ctype == ContextType.TEXT:
            video_match_prefix = match_video_create_prefix(content, conf().get("video_create_prefix", []))
            if video_match_prefix:
                content = self._strip_create_prefix(content, video_match_prefix)
                context.type = ContextType.VIDEO_CREATE
            elif self._should_promote_grok_media_create(context, content, explicit_video_generation_requested):
                context.type = ContextType.VIDEO_CREATE
            else:
                img_match_prefix = match_image_create_prefix(content, conf().get("image_create_prefix"))
                if img_match_prefix:
                    content = self._strip_create_prefix(content, img_match_prefix)
                    context.type = ContextType.IMAGE_CREATE
                elif self._should_promote_grok_media_create(context, content, explicit_image_generation_requested):
                    context.type = ContextType.IMAGE_CREATE
                else:
                    context.type = ContextType.TEXT
            content = content.strip()
            context["_visible_task_summary"] = content
            if cmsg.is_group and context.type == ContextType.TEXT:
                context.content = self._format_group_member_query(context, content)
            else:
                context.content = content

        return context

    @staticmethod
    def _strip_create_prefix(content: str, prefix: str) -> str:
        return str(content or "").replace(str(prefix or ""), "", 1).lstrip(" \t:：,，")

    @staticmethod
    def _format_group_member_query(context: Context, content: str) -> str:
        sender_id = context.get("group_sender_id", "")
        sender_label = context.get("group_sender_label", "") or sender_id or "未知成员"
        group_chat_name = str(context.get("group_chat_name", "") or "").strip()
        member_profile_path = context.get("group_member_profile_path", "")
        known_profile = _read_relative_workspace_file(member_profile_path)
        known_profile_note = known_profile.strip() if known_profile.strip() else "暂无明确称呼"
        group_lines = []
        if group_chat_name:
            group_lines.append(f"- 群名称: {group_chat_name}")
        group_lines.append(f"- 群会话ID: {context.get('group_chat_id', '')}")
        known_members = context.get("group_known_members") or []
        member_lines = []
        if isinstance(known_members, list):
            for member in known_members[:20]:
                if not isinstance(member, dict):
                    continue
                user_id = _single_line(member.get("user_id", ""))
                name = _single_line(member.get("name", "")) or user_id
                if user_id:
                    member_lines.append(f"  - {name}: {user_id}")
        return (
            "[群聊消息元信息]\n"
            + "\n".join(group_lines)
            + "\n"
            f"- 发言人ID: {sender_id}\n"
            f"- 发言人企微显示名: {sender_label}\n"
            f"- 发言人成员档案: {member_profile_path}\n"
            f"- 已知成员称呼档案内容: {known_profile_note}\n"
            + (
                "- 已知群成员（仅用于创建定时提醒时填写 target_user_id，不要在回复正文中复述ID）:\n"
                + "\n".join(member_lines)
                + "\n"
                if member_lines
                else ""
            )
            + "- 如果用户正在告诉你希望怎么称呼TA，请把称呼写入上述成员档案；"
            "不要写入任何私聊记忆。\n"
            "- 回复时不要复述这段元信息。\n\n"
            f"[群成员: {sender_label}] {content}"
        )

    @staticmethod
    def _single_line(value) -> str:
        return _single_line(value)

    @classmethod
    def _configured_member_display_name(cls, user_id: str, display_name: str = "", chat_id: str = "") -> str:
        user_id = cls._single_line(user_id)
        display_name = cls._single_line(display_name)
        chat_id = cls._single_line(chat_id)
        for aliases in cls._member_alias_sources(chat_id):
            for key in (user_id, display_name):
                alias = cls._single_line(aliases.get(key, ""))
                if alias:
                    return alias
        profiles = conf().get("agent_user_profiles", {}) or {}
        if isinstance(profiles, dict) and user_id:
            for key in (f"wecom_bot:{user_id}", user_id):
                profile = profiles.get(key)
                if not isinstance(profile, dict):
                    continue
                alias = cls._single_line(
                    profile.get("display_name")
                    or profile.get("name")
                    or profile.get("llm_usage_label")
                    or ""
                )
                if alias and alias not in {user_id, display_name}:
                    return alias
        return display_name or user_id

    @classmethod
    def _member_alias_sources(cls, chat_id: str) -> list:
        sources = []
        group_aliases = conf().get("wecom_bot_group_member_aliases", {}) or {}
        if chat_id and isinstance(group_aliases, dict):
            aliases = group_aliases.get(chat_id, {})
            if isinstance(aliases, dict):
                sources.append(aliases)
        global_aliases = conf().get("wecom_bot_member_aliases", {}) or {}
        if isinstance(global_aliases, dict):
            sources.append(global_aliases)
        return sources

    @staticmethod
    def _group_members_path() -> Path:
        workspace = expand_path(conf().get("agent_workspace", "~/cow"))
        return Path(workspace) / "data" / "wecom_bot_group_members.json"

    def _remember_group_member(self, chat_id: str, user_id: str, display_name: str) -> None:
        chat_id = self._single_line(chat_id)
        user_id = self._single_line(user_id)
        display_name = self._configured_member_display_name(user_id, display_name, chat_id)
        if not chat_id or not user_id:
            return
        try:
            path = self._group_members_path()
            data = {}
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        data = loaded
            groups = data.setdefault("groups", {})
            group = groups.setdefault(chat_id, {})
            group[user_id] = {
                "user_id": user_id,
                "name": display_name,
                "last_seen": time.time(),
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"[WecomBot] Failed to remember group member: {e}")

    def _known_group_members(self, chat_id: str, limit: int = 20) -> list:
        chat_id = self._single_line(chat_id)
        if not chat_id:
            return []
        try:
            path = self._group_members_path()
            if not path.exists():
                return []
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            group = ((data or {}).get("groups") or {}).get(chat_id) or {}
            members = [m for m in group.values() if isinstance(m, dict)]
            members.sort(key=lambda m: float(m.get("last_seen") or 0), reverse=True)
            return [
                {
                    "user_id": self._single_line(member.get("user_id", "")),
                    "name": self._configured_member_display_name(
                        member.get("user_id", ""),
                        member.get("name", ""),
                        chat_id,
                    ),
                }
                for member in members[:limit]
                if self._single_line(member.get("user_id", ""))
            ]
        except Exception as e:
            logger.debug(f"[WecomBot] Failed to read group members: {e}")
            return []

    # ------------------------------------------------------------------
    # Send reply
    # ------------------------------------------------------------------

    def send(self, reply: Reply, context: Context):
        msg = context.get("msg")
        is_group = context.get("isgroup", False)
        receiver = context.get("receiver", "")
        mention_user_ids, mention_display_names = self._reply_mention_target(context)

        # Determine req_id for responding or use send_msg for scheduled push
        req_id = getattr(msg, "req_id", None) if msg else None

        if reply.type == ReplyType.TEXT:
            return self._send_text(
                reply.content,
                receiver,
                is_group,
                req_id,
                mention_user_ids=mention_user_ids,
                mention_display_names=mention_display_names,
            )
        elif reply.type in (ReplyType.IMAGE_URL, ReplyType.IMAGE):
            try:
                return self._send_image(reply.content, receiver, is_group, req_id)
            finally:
                cleanup_generated_reply_media(reply)
        elif reply.type == ReplyType.FILE:
            text_sent = True
            if hasattr(reply, "text_content") and reply.text_content:
                text_sent = self._send_text(
                    reply.text_content,
                    receiver,
                    is_group,
                    req_id,
                    mention_user_ids=mention_user_ids,
                    mention_display_names=mention_display_names,
                )
                time.sleep(0.3)
            return text_sent and self._send_file(reply.content, receiver, is_group, req_id)
        elif reply.type == ReplyType.VOICE:
            return self._send_voice(reply.content, receiver, is_group, req_id)
        elif reply.type == ReplyType.VIDEO or reply.type == ReplyType.VIDEO_URL:
            try:
                return self._send_file(reply.content, receiver, is_group, req_id, media_type="video")
            finally:
                cleanup_generated_reply_media(reply)
        else:
            logger.warning(f"[WecomBot] Unsupported reply type: {reply.type}, falling back to text")
            return self._send_text(
                str(reply.content),
                receiver,
                is_group,
                req_id,
                mention_user_ids=mention_user_ids,
                mention_display_names=mention_display_names,
            )

    def _send_silence_notice(self, context: Context, notice: str):
        receiver = context.get("receiver", "")
        is_group = bool(context.get("isgroup", False))
        mention_user_ids, mention_display_names = self._reply_mention_target(context)
        sent = self._send_text(
            notice,
            receiver,
            is_group,
            req_id=None,
            mention_user_ids=mention_user_ids,
            mention_display_names=mention_display_names,
        )
        runtime = context.get("_session_runtime") if context else None
        if sent is not False and runtime and hasattr(runtime, "mark_visible_output"):
            runtime.mark_visible_output("silence_notice")
        return sent

    def active_send_text_result(self, receiver: str, text: str, is_group: bool = False, **kwargs) -> dict:
        """Proactively send text to a reachable WeCom chat and return a normalized result."""
        clean_receiver = str(receiver or "").strip()
        if not clean_receiver:
            return {
                "ok": False,
                "delivered": False,
                "reason": "unreachable",
                "receiver": clean_receiver,
            }
        if not self._connected or self._ws is None:
            return {
                "ok": False,
                "delivered": False,
                "reason": "channel_not_running",
                "receiver": clean_receiver,
            }

        try:
            mention_user_ids = kwargs.get("mention_user_ids") or kwargs.get("mention_user_id")
            mention_display_names = self._configured_mention_display_names(
                clean_receiver if is_group else "",
                mention_user_ids,
                kwargs.get("mention_display_names") or kwargs.get("mention_display_name"),
            )
            content = self._with_mentions(
                str(text or ""),
                mention_user_ids,
                mention_display_names,
                enabled=bool(is_group),
            )
            if not self._active_send_markdown(content, clean_receiver, bool(is_group)):
                return {
                    "ok": False,
                    "delivered": False,
                    "reason": "send_error",
                    "receiver": clean_receiver,
                }
            return {
                "ok": True,
                "delivered": True,
                "reason": "sent",
                "receiver": clean_receiver,
            }
        except Exception as e:
            logger.warning(f"[WecomBot] Active send failed: {e}")
            return {
                "ok": False,
                "delivered": False,
                "reason": "send_error",
                "error": str(e),
                "receiver": clean_receiver,
            }

    # ------------------------------------------------------------------
    # Respond message (via websocket)
    # ------------------------------------------------------------------

    @staticmethod
    def _append_stream_segment(prefix: str, segment: str) -> str:
        prefix = str(prefix or "")
        segment = str(segment or "")
        if not prefix:
            return segment
        if not segment:
            return prefix
        if prefix.endswith("\n\n---\n\n"):
            return prefix + segment
        return prefix.rstrip() + "\n\n---\n\n" + segment

    @classmethod
    def _merge_final_stream_content(cls, state: dict, content: str) -> str:
        committed = str(state.get("committed") or "")
        current = str(state.get("current") or "")
        final_content = str(content or "")
        streamed_content = committed + current
        if not final_content.strip():
            return streamed_content
        if not streamed_content.strip():
            return final_content
        if final_content.strip() in streamed_content:
            return streamed_content
        return cls._append_stream_segment(committed, final_content)

    @staticmethod
    def _split_text_chunks(content: str, limit: int = MARKDOWN_TEXT_CHUNK_LIMIT) -> list:
        text = str(content or "").strip()
        if not text:
            return [""]
        if len(text) <= limit:
            return [text]

        chunks = []
        remaining = text
        soft_floor = max(1, int(limit * 0.55))
        while len(remaining) > limit:
            cut = remaining.rfind("\n\n", 0, limit + 1)
            if cut < soft_floor:
                cut = remaining.rfind("\n", 0, limit + 1)
            if cut < soft_floor:
                cut = remaining.rfind(" ", 0, limit + 1)
            if cut <= 0:
                cut = limit

            chunk = remaining[:cut].rstrip()
            if not chunk:
                chunk = remaining[:limit]
            chunks.append(chunk)
            remaining = remaining[len(chunk):].lstrip()

        if remaining:
            chunks.append(remaining)
        return chunks

    def _send_markdown_chunks(
        self,
        chunks: list,
        receiver: str,
        is_group: bool,
        *,
        start_index: int = 1,
        total: int = None,
    ) -> bool:
        total = total or len(chunks)
        for offset, chunk in enumerate(chunks):
            part_index = start_index + offset
            content = str(chunk or "")
            if total > 1 and part_index > 1:
                content = f"({part_index}/{total})\n\n{content}"
            if not self._active_send_markdown(content, receiver, is_group):
                return False
            if offset < len(chunks) - 1:
                time.sleep(LONG_REPLY_PART_DELAY_SECONDS)
        return True

    def _send_text(
        self,
        content: str,
        receiver: str,
        is_group: bool,
        req_id: str = None,
        mention_user_ids=None,
        mention_display_names=None,
    ):
        """Send text/markdown reply. Reuses stream state if available (streaming mode)."""
        if req_id:
            state = self._stream_states.pop(req_id, None)
            if state:
                final_content = self._merge_final_stream_content(state, content)
                stream_id = state["stream_id"]
            else:
                final_content = content
                stream_id = uuid.uuid4().hex[:16]
            final_content = self._with_mentions(
                final_content,
                mention_user_ids,
                mention_display_names,
                enabled=bool(is_group),
            )
            chunks = self._split_text_chunks(final_content)
            primary_content = chunks[0]

            # Brief pause so the server finishes processing the last intermediate chunk
            # before receiving the finish packet
            time.sleep(0.15)

            sent = self._ws_send({
                "cmd": "aibot_respond_msg",
                "headers": {"req_id": req_id},
                "body": {
                    "msgtype": "stream",
                    "stream": {
                        "id": stream_id,
                        "finish": True,
                        "content": primary_content,
                    },
                },
            })
            if not sent:
                return False
            if len(chunks) > 1:
                logger.info(
                    "[WecomBot] Splitting long stream reply into %s parts (chars=%s)",
                    len(chunks),
                    len(final_content),
                )
                extras_sent = self._send_markdown_chunks(
                    chunks[1:],
                    receiver,
                    is_group,
                    start_index=2,
                    total=len(chunks),
                )
                if not extras_sent:
                    logger.warning("[WecomBot] Failed to send one or more long reply follow-up chunks")
            return True
        else:
            content = self._with_mentions(
                content,
                mention_user_ids,
                mention_display_names,
                enabled=bool(is_group),
            )
            chunks = self._split_text_chunks(content)
            if len(chunks) > 1:
                logger.info(
                    "[WecomBot] Splitting long active text into %s parts (chars=%s)",
                    len(chunks),
                    len(content),
                )
            return self._send_markdown_chunks(chunks, receiver, is_group, total=len(chunks))

    @staticmethod
    def _as_list(value) -> list:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item or "").strip()]
        text = str(value or "").strip()
        return [text] if text else []

    def _configured_mention_display_names(self, chat_id: str, mention_user_ids=None, mention_display_names=None) -> list:
        user_ids = self._as_list(mention_user_ids)
        names = self._as_list(mention_display_names)
        count = max(len(user_ids), len(names))
        resolved = []
        for index in range(count):
            user_id = user_ids[index] if index < len(user_ids) else ""
            name = names[index] if index < len(names) else ""
            display_name = self._configured_member_display_name(user_id, name, chat_id)
            if display_name:
                resolved.append(display_name)
        return resolved or names

    @classmethod
    def _mention_tokens(cls, mention_user_ids=None, mention_display_names=None) -> list:
        display_tokens = [f"@{name}" for name in cls._as_list(mention_display_names)]
        if display_tokens:
            return display_tokens
        return [f"<@{user_id}>" for user_id in cls._as_list(mention_user_ids)]

    @classmethod
    def _with_mentions(
        cls,
        content: str,
        mention_user_ids=None,
        mention_display_names=None,
        enabled: bool = True,
    ) -> str:
        text = str(content or "").strip()
        if not enabled:
            return text
        user_ids = cls._as_list(mention_user_ids)
        names = cls._as_list(mention_display_names)
        tokens = cls._mention_tokens(user_ids, names)
        if not tokens:
            return text
        if any(text.startswith(token) for token in tokens):
            return text
        stripped = cls._strip_leading_target_label(text, user_ids + names)
        return " ".join(tokens) + ("\n" + stripped if stripped else "")

    @classmethod
    def _strip_leading_target_label(cls, content: str, labels) -> str:
        text = str(content or "").lstrip()
        for _ in range(3):
            changed = False
            for label in cls._as_list(labels):
                candidates = [
                    f"<@{label}>",
                    f"@{label}",
                    label,
                ]
                for candidate in candidates:
                    if not candidate or not text.startswith(candidate):
                        continue
                    text = text[len(candidate):].lstrip()
                    text = text.lstrip("，,：:、-— ")
                    changed = True
                    break
                if changed:
                    break
            if not changed:
                break
        return text

    @classmethod
    def _reply_mention_target(cls, context: Context):
        if not context or not context.get("isgroup", False):
            return [], []
        user_id = cls._single_line(context.get("group_sender_id", ""))
        display_name = cls._single_line(context.get("group_sender_label", ""))
        msg = context.get("msg")
        if not user_id and msg is not None:
            user_id = cls._single_line(getattr(msg, "actual_user_id", ""))
        if not display_name and msg is not None:
            display_name = cls._single_line(getattr(msg, "actual_user_nickname", ""))
        display_name = cls._configured_member_display_name(
            user_id,
            display_name,
            context.get("group_chat_id", "") or context.get("receiver", "") or context.get("session_id", ""),
        )
        return ([user_id] if user_id else []), ([display_name] if display_name else [])

    def _send_image(self, img_path_or_url: str, receiver: str, is_group: bool, req_id: str = None):
        transient_paths = []
        try:
            return self._send_image_impl(img_path_or_url, receiver, is_group, req_id, transient_paths)
        finally:
            for path in transient_paths:
                remove_file_quietly(path)

    def _send_image_impl(self, img_path_or_url: str, receiver: str, is_group: bool, req_id: str, transient_paths: list):
        """Send image reply. Converts to JPG/PNG and compresses if >2MB."""
        local_path = str(img_path_or_url or "").strip()
        if local_path.startswith("file://"):
            local_path = local_path[7:]

        if local_path.startswith(("http://", "https://")):
            try:
                tmp_path = safe_download_to_file(
                    local_path,
                    prefix="wecom_img",
                    suffix=None,
                    allowed_content_types=REMOTE_IMAGE_CONTENT_TYPES,
                    max_bytes=MAX_REMOTE_IMAGE_BYTES,
                    timeout=30.0,
                )
                transient_paths.append(tmp_path)
                logger.info("[WecomBot] Image downloaded safely: path=%s", tmp_path)
                local_path = tmp_path
            except Exception as e:
                logger.error(f"[WecomBot] Failed to download image for sending: {e}")
                self._send_text("[Image send failed]", receiver, is_group, req_id)
                return False

        if not os.path.exists(local_path):
            logger.error(f"[WecomBot] Image file not found: {local_path}")
            return False

        prepared_path = self._prepare_image_for_send(local_path)
        if not prepared_path:
            self._send_text("[Image too large]", receiver, is_group, req_id)
            return False
        if prepared_path != local_path:
            transient_paths.append(prepared_path)
        local_path = prepared_path

        file_size = os.path.getsize(local_path)
        logger.info(f"[WecomBot] Uploading image: path={local_path}, size={file_size} bytes")
        media_id = self._upload_media(local_path, "image")
        if not media_id:
            logger.error("[WecomBot] Failed to upload image")
            self._send_text("[Image upload failed]", receiver, is_group, req_id)
            return False

        if req_id:
            return self._ws_send({
                "cmd": "aibot_respond_msg",
                "headers": {"req_id": req_id},
                "body": {
                    "msgtype": "image",
                    "image": {"media_id": media_id},
                },
            })
        else:
            return self._ws_send({
                "cmd": "aibot_send_msg",
                "headers": {"req_id": self._gen_req_id()},
                "body": {
                    "chatid": receiver,
                    "chat_type": 2 if is_group else 1,
                    "msgtype": "image",
                    "image": {"media_id": media_id},
                },
            })

    @staticmethod
    def _prepare_image_for_send(file_path: str) -> str:
        max_width, max_height = image_send_dimensions_from_config(conf())
        max_bytes = int(conf().get("wecom_image_send_max_bytes", WECOM_BOT_IMAGE_MAX_BYTES) or WECOM_BOT_IMAGE_MAX_BYTES)
        return prepare_image_for_send(
            file_path,
            max_bytes=max_bytes,
            max_width=max_width,
            max_height=max_height,
            prefix="wecom_img",
        )

    def _send_file(self, file_path: str, receiver: str, is_group: bool,
                   req_id: str = None, media_type: str = "file"):
        transient_paths = []
        try:
            return self._send_file_impl(file_path, receiver, is_group, req_id, media_type, transient_paths)
        finally:
            for path in transient_paths:
                remove_file_quietly(path)

    def _send_file_impl(self, file_path: str, receiver: str, is_group: bool,
                        req_id: str, media_type: str, transient_paths: list):
        """Send file/video reply by uploading media first."""
        local_path = str(file_path or "").strip()
        if local_path.startswith("file://"):
            local_path = local_path[7:]

        if local_path.startswith(("http://", "https://")):
            if media_type != "video":
                logger.error("[WecomBot] Refusing remote file URL; only local files are accepted")
                return False
            try:
                tmp_path = safe_download_to_file(
                    local_path,
                    prefix="wecom_video",
                    suffix=".mp4",
                    allowed_content_types=REMOTE_VIDEO_CONTENT_TYPES,
                    max_bytes=MAX_REMOTE_VIDEO_BYTES,
                    timeout=60.0,
                )
                transient_paths.append(tmp_path)
                local_path = tmp_path
            except Exception as e:
                logger.error(f"[WecomBot] Failed to download file for sending: {e}")
                return False

        if not os.path.exists(local_path):
            logger.error(f"[WecomBot] File not found: {local_path}")
            return False

        media_id = self._upload_media(local_path, media_type)
        if not media_id:
            logger.error(f"[WecomBot] Failed to upload {media_type}")
            return False

        if req_id:
            return self._ws_send({
                "cmd": "aibot_respond_msg",
                "headers": {"req_id": req_id},
                "body": {
                    "msgtype": media_type,
                    media_type: {"media_id": media_id},
                },
            })
        else:
            return self._ws_send({
                "cmd": "aibot_send_msg",
                "headers": {"req_id": self._gen_req_id()},
                "body": {
                    "chatid": receiver,
                    "chat_type": 2 if is_group else 1,
                    "msgtype": media_type,
                    media_type: {"media_id": media_id},
                },
            })

    def _send_voice(self, file_path: str, receiver: str, is_group: bool, req_id: str = None) -> bool:
        """Send a WeCom voice message as AMR, split to platform limits."""
        local_path = str(file_path or "").strip()
        if local_path.startswith("file://"):
            local_path = local_path[7:]
        if not os.path.exists(local_path):
            logger.error(f"[WeComBot] Voice file not found: {local_path}")
            return False

        segment_paths = []
        try:
            duration_ms, segment_paths = split_audio_by_wecom_voice_limits(local_path)
            if len(segment_paths) > 1:
                logger.info(
                    "[WeComBot] Splitting voice into %s parts (duration=%.1fs)",
                    len(segment_paths),
                    duration_ms / 1000.0,
                )
            sent_any = False
            for path in segment_paths:
                media_id = self._upload_media(path, "voice")
                if not media_id:
                    logger.error("[WeComBot] Failed to upload voice")
                    return False
                if not self._send_voice_media(media_id, receiver, is_group, req_id):
                    logger.error("[WeComBot] Failed to send voice media")
                    return False
                sent_any = True
                if len(segment_paths) > 1:
                    time.sleep(0.3)
            return sent_any
        except Exception as e:
            logger.error(f"[WeComBot] Voice send failed: {e}")
            return False
        finally:
            for path in segment_paths:
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass
            try:
                if local_path and os.path.exists(local_path):
                    os.remove(local_path)
            except OSError:
                pass

    def _send_voice_media(self, media_id: str, receiver: str, is_group: bool, req_id: str = None) -> bool:
        if req_id:
            return self._ws_send({
                "cmd": "aibot_respond_msg",
                "headers": {"req_id": req_id},
                "body": {
                    "msgtype": "voice",
                    "voice": {"media_id": media_id},
                },
            })
        return self._ws_send({
            "cmd": "aibot_send_msg",
            "headers": {"req_id": self._gen_req_id()},
            "body": {
                "chatid": receiver,
                "chat_type": 2 if is_group else 1,
                "msgtype": "voice",
                "voice": {"media_id": media_id},
            },
        })

    def _active_send_markdown(self, content: str, receiver: str, is_group: bool):
        """Proactively send markdown message (for scheduled tasks, no req_id)."""
        return self._ws_send({
            "cmd": "aibot_send_msg",
            "headers": {"req_id": self._gen_req_id()},
            "body": {
                "chatid": receiver,
                "chat_type": 2 if is_group else 1,
                "msgtype": "markdown",
                "markdown": {"content": content},
            },
        })

    # ------------------------------------------------------------------
    # Media upload (chunked)
    # ------------------------------------------------------------------

    def _upload_media(self, file_path: str, media_type: str = "file") -> str:
        """
        Upload a local file to wecom bot via chunked upload protocol.
        Returns media_id on success, empty string on failure.
        """
        if not os.path.exists(file_path):
            logger.error(f"[WecomBot] Upload file not found: {file_path}")
            return ""

        file_size = os.path.getsize(file_path)
        if file_size < 5:
            logger.error(f"[WecomBot] File too small: {file_size} bytes")
            return ""

        filename = os.path.basename(file_path)
        total_chunks = math.ceil(file_size / MEDIA_CHUNK_SIZE)
        if total_chunks > 100:
            logger.error(f"[WecomBot] Too many chunks: {total_chunks} > 100")
            return ""

        file_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for block in iter(lambda: f.read(8192), b""):
                file_md5.update(block)
        md5_hex = file_md5.hexdigest()

        # 1. Init upload
        init_resp = self._send_and_wait({
            "cmd": "aibot_upload_media_init",
            "headers": {"req_id": self._gen_req_id()},
            "body": {
                "type": media_type,
                "filename": filename,
                "total_size": file_size,
                "total_chunks": total_chunks,
                "md5": md5_hex,
            },
        }, timeout=15)

        if init_resp.get("errcode") != 0:
            logger.error(f"[WecomBot] Upload init failed: {init_resp}")
            return ""

        upload_id = init_resp.get("body", {}).get("upload_id")
        if not upload_id:
            logger.error("[WecomBot] Failed to get upload_id")
            return ""

        # 2. Upload chunks
        with open(file_path, "rb") as f:
            for idx in range(total_chunks):
                chunk = f.read(MEDIA_CHUNK_SIZE)
                b64_data = base64.b64encode(chunk).decode("utf-8")
                chunk_resp = self._send_and_wait({
                    "cmd": "aibot_upload_media_chunk",
                    "headers": {"req_id": self._gen_req_id()},
                    "body": {
                        "upload_id": upload_id,
                        "chunk_index": idx,
                        "base64_data": b64_data,
                    },
                }, timeout=30)
                if chunk_resp.get("errcode") != 0:
                    logger.error(f"[WecomBot] Chunk {idx} upload failed: {chunk_resp}")
                    return ""

        # 3. Finish upload
        finish_resp = self._send_and_wait({
            "cmd": "aibot_upload_media_finish",
            "headers": {"req_id": self._gen_req_id()},
            "body": {"upload_id": upload_id},
        }, timeout=30)

        if finish_resp.get("errcode") != 0:
            logger.error(f"[WecomBot] Upload finish failed: {finish_resp}")
            return ""

        media_id = finish_resp.get("body", {}).get("media_id", "")
        if media_id:
            logger.info(f"[WecomBot] Media uploaded: media_id={media_id}")
        else:
            logger.error("[WecomBot] Failed to get media_id from finish response")
        return media_id
