import hashlib
import hmac
import time
import json
import logging
import mimetypes
import os
import re
import threading
import time
import uuid
from queue import Queue, Empty
from types import SimpleNamespace
from typing import Tuple

import web

from bridge.context import *
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel, check_prefix
from channel.chat_message import ChatMessage
from collections import OrderedDict
from common import const
from common.log import logger
from common.singleton import singleton
from config import conf
from channel.weixin.weixin_identity import (
    extract_real_wechat_id,
    is_real_wechat_id,
    looks_internal_weixin_id,
    normalize_role as normalize_weixin_role,
    remember_wechat_identity,
    weixin_role_for_identity,
)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".avi", ".mov", ".mkv"}
_FILE_SERVE_TOKEN_TTL_SECONDS = 3600
_file_serve_lock = threading.Lock()
_file_serve_tokens = {}

def _is_password_enabled():
    return bool(conf().get("web_password", ""))


def _session_expire_seconds():
    return int(conf().get("web_session_expire_days", 30)) * 86400


def _create_auth_token():
    """Create a stateless signed token: ``<timestamp_hex>.<hmac_hex>``."""
    ts = format(int(time.time()), "x")
    sig = hmac.new(
        conf().get("web_password", "").encode(),
        ts.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{ts}.{sig}"


def _verify_auth_token(token):
    """Verify a signed token is valid and not expired.

    The token is derived from the password, so it survives server restarts
    and automatically invalidates when the password changes.
    """
    if not token or "." not in token:
        return False
    ts_hex, sig = token.split(".", 1)
    try:
        ts = int(ts_hex, 16)
    except ValueError:
        return False
    if time.time() - ts > _session_expire_seconds():
        return False
    expected = hmac.new(
        conf().get("web_password", "").encode(),
        ts_hex.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(sig, expected)


def _check_auth():
    """Return True if request is authenticated or password not enabled."""
    if not _is_password_enabled():
        return True
    return _verify_auth_token(web.cookies().get("cow_auth_token", ""))


def _require_auth():
    """Raise 401 if not authenticated. Call at the top of protected handlers."""
    if not _check_auth():
        raise web.HTTPError("401 Unauthorized",
                            {"Content-Type": "application/json; charset=utf-8"},
                            json.dumps({"status": "error", "message": "Unauthorized"}))


def _get_upload_dir() -> str:
    from common.utils import expand_path
    ws_root = expand_path(conf().get("agent_workspace", "~/cow"))
    tmp_dir = os.path.join(ws_root, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    return tmp_dir


def _get_web_admin_profile():
    """Resolve the single configured admin for privacy-scoped web APIs."""
    try:
        from agent.user_profiles import resolve_single_admin_profile
        return resolve_single_admin_profile()
    except Exception as e:
        logger.warning(f"[WebChannel] Failed to resolve admin profile: {e}")
        return None


def _apply_web_admin_context(context) -> None:
    """Web console requests run as the configured administrator."""
    if context is None:
        return
    profile = _get_web_admin_profile()
    if profile is not None:
        try:
            from agent.user_profiles import apply_profile_to_context

            apply_profile_to_context(context, profile)
            context["_actor_profile"] = profile
            return
        except Exception as e:
            logger.warning(f"[WebChannel] Failed to apply admin profile: {e}")

    context["actor_id"] = context.get("actor_id") or "web:admin"
    context["actor_role"] = "admin"
    context["memory_user_id"] = context.get("memory_user_id") or "web_admin"


def _register_web_uploaded_images(session_id: str, attachments) -> int:
    session_id = str(session_id or "").strip()
    if not session_id or not attachments:
        return 0
    try:
        from channel.image_recognition import get_image_recognition_manager

        manager = get_image_recognition_manager()
    except Exception as e:
        logger.debug(f"[WebChannel] Image recognition manager unavailable for upload cache: {e}")
        return 0

    registered = 0
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        if str(attachment.get("file_type") or "").strip().lower() != "image":
            continue
        image_path = str(attachment.get("file_path") or "").strip()
        if not image_path:
            continue
        try:
            record = manager.register_image(
                session_id=session_id,
                channel_type="web",
                image_path=image_path,
                is_group=False,
                sender_label="Web",
            )
            if record:
                registered += 1
        except Exception as e:
            logger.debug(f"[WebChannel] Failed to cache uploaded image for Grok media refs: {e}")
    if registered:
        logger.info(f"[WebChannel] Cached {registered} uploaded image(s) for recent Grok media refs")
    return registered


def _register_web_file(file_path: str) -> str:
    token = uuid.uuid4().hex
    expires_at = time.time() + _FILE_SERVE_TOKEN_TTL_SECONDS
    with _file_serve_lock:
        now = time.time()
        for existing, (_, expiry) in list(_file_serve_tokens.items()):
            if expiry <= now:
                _file_serve_tokens.pop(existing, None)
        _file_serve_tokens[token] = (os.path.realpath(os.path.abspath(file_path)), expires_at)
    return f"/api/file?token={token}"


def _resolve_web_file_token(token: str) -> str:
    if not token:
        return ""
    with _file_serve_lock:
        item = _file_serve_tokens.get(token)
        if not item:
            return ""
        file_path, expires_at = item
        if expires_at <= time.time():
            _file_serve_tokens.pop(token, None)
            return ""
        return file_path


def _local_file_path_from_reply_content(content: str) -> str:
    value = str(content or "").strip()
    if not value:
        return ""
    if value.lower().startswith("file://"):
        raw_path = value[7:]
        if re.match(r"^[A-Za-z]:[\\/]", raw_path):
            return os.path.abspath(raw_path)
        from urllib.parse import unquote, urlparse

        parsed = urlparse(value)
        path = unquote(parsed.path or value[7:])
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        return os.path.abspath(path)
    if os.path.isabs(value):
        return os.path.abspath(value)
    return ""


def _is_within_path(path: str, root: str) -> bool:
    path = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    root = os.path.normcase(os.path.realpath(os.path.abspath(root)))
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _is_allowed_web_file_path(file_path: str) -> bool:
    from common.utils import expand_path

    workspace = _get_workspace_root()
    allowed_roots = [
        _get_upload_dir(),
        os.path.join(workspace, "tmp"),
        os.path.join(workspace, "downloads"),
        os.path.join(workspace, "attachments"),
        expand_path("~/.cow/browser_downloads"),
    ]
    if any(_is_within_path(file_path, root) for root in allowed_roots):
        return True

    users_root = os.path.join(workspace, "users")
    if _is_within_path(file_path, users_root):
        rel_parts = os.path.relpath(
            os.path.realpath(os.path.abspath(file_path)),
            os.path.realpath(os.path.abspath(users_root)),
        ).replace("\\", "/").split("/")
        return "files" in rel_parts
    return False


def _sanitize_upload_relative_path(relative_path: str) -> str:
    """Normalize relative upload path and reject escapes / absolute paths."""
    relative_path = (relative_path or "").replace("\\", "/").strip("/")
    if not relative_path:
        raise ValueError("Empty relative path")
    parts = []
    for part in relative_path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("Invalid relative path")
        parts.append(part)
    if not parts:
        raise ValueError("Invalid relative path")
    norm_path = "/".join(parts)
    if os.path.isabs(norm_path):
        raise ValueError("Invalid relative path")
    return norm_path


def _sanitize_upload_id(upload_id: str) -> str:
    """Allow only simple batch ids for directory uploads."""
    sanitized = "".join(ch for ch in (upload_id or "") if ch.isalnum() or ch in ("-", "_"))
    if not sanitized:
        raise ValueError("Invalid upload id")
    return sanitized[:80]


def _is_within_directory(root_path: str, target_path: str) -> bool:
    try:
        return os.path.commonpath([root_path, target_path]) == root_path
    except ValueError:
        return False


def _resolve_upload_path(upload_root: str, relative_path: str) -> Tuple[str, str]:
    """Resolve a relative upload path under upload_root and reject escapes."""
    safe_rel_path = _sanitize_upload_relative_path(relative_path)
    upload_root_real = os.path.realpath(upload_root)
    save_path = os.path.realpath(os.path.join(upload_root_real, *safe_rel_path.split("/")))
    if not _is_within_directory(upload_root_real, save_path):
        raise ValueError("Invalid directory upload path")
    return safe_rel_path, save_path


def _read_uploaded_file_bytes(file_obj) -> bytes:
    """Return uploaded content as bytes across web.py upload object variants."""
    if isinstance(file_obj, bytes):
        return file_obj
    if isinstance(file_obj, str):
        return file_obj.encode("utf-8")

    content = None

    if hasattr(file_obj, "file") and hasattr(file_obj.file, "read"):
        content = file_obj.file.read()
    elif hasattr(file_obj, "read"):
        content = file_obj.read()
    elif hasattr(file_obj, "value"):
        content = file_obj.value

    if content is None:
        raise ValueError("Unable to read uploaded file content")
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8")
    raise TypeError(f"Unsupported uploaded content type: {type(content).__name__}")


def _raw_web_input():
    """Return unprocessed multipart form data when web.py exposes rawinput."""
    rawinput = getattr(getattr(web, "webapi", None), "rawinput", None)
    if not callable(rawinput):
        raise RuntimeError("web.py rawinput is not available")
    try:
        return rawinput(method="post")
    except TypeError:
        return rawinput()


def _ensure_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _generate_session_title(user_message: str, assistant_reply: str = "") -> str:
    """Delegate to the shared SessionService implementation."""
    from agent.chat.session_service import generate_session_title
    return generate_session_title(user_message, assistant_reply)


class WebMessage(ChatMessage):
    def __init__(
            self,
            msg_id,
            content,
            ctype=ContextType.TEXT,
            from_user_id="User",
            to_user_id="Chatgpt",
            other_user_id="Chatgpt",
    ):
        self.msg_id = msg_id
        self.ctype = ctype
        self.content = content
        self.from_user_id = from_user_id
        self.to_user_id = to_user_id
        self.other_user_id = other_user_id


@singleton
class WebChannel(ChatChannel):
    NOT_SUPPORT_REPLYTYPE = [ReplyType.VOICE]
    _instance = None

    # def __new__(cls):
    #     if cls._instance is None:
    #         cls._instance = super(WebChannel, cls).__new__(cls)
    #     return cls._instance

    def __init__(self):
        super().__init__()
        self.msg_id_counter = 0
        self.session_queues = {}  # session_id -> Queue (fallback polling)
        self.request_to_session = {}  # request_id -> session_id
        self.sse_queues = {}  # request_id -> Queue (SSE streaming)
        self._http_server = None

    def _generate_msg_id(self):
        """生成唯一的消息ID"""
        self.msg_id_counter += 1
        return str(int(time.time())) + str(self.msg_id_counter)

    def _generate_request_id(self):
        """生成唯一的请求ID"""
        return str(uuid.uuid4())

    def send(self, reply: Reply, context: Context):
        try:
            if reply.type in self.NOT_SUPPORT_REPLYTYPE:
                logger.warning(f"Web channel doesn't support {reply.type} yet")
                return

            if reply.type == ReplyType.IMAGE_URL:
                time.sleep(0.5)

            request_id = context.get("request_id", None)
            if not request_id:
                logger.error("No request_id found in context, cannot send message")
                return

            session_id = self.request_to_session.get(request_id)
            if not session_id:
                logger.error(f"No session_id found for request {request_id}")
                return

            # SSE mode: push events to SSE queue
            if request_id in self.sse_queues:
                content = reply.content if reply.content is not None else ""
                media_payload = self._media_response_payload(reply)

                # Intermediate status lines (e.g. /install-browser phases) must NOT use "done",
                # or the frontend closes EventSource and drops subsequent events.
                if getattr(reply, "sse_phase", False):
                    self.sse_queues[request_id].put({
                        "type": "phase",
                        "content": content,
                        "request_id": request_id,
                        "timestamp": time.time(),
                    })
                    logger.debug(f"SSE phase for request {request_id}")
                    return

                # Files are already pushed via on_event (file_to_send) during agent execution.
                # Skip duplicate file pushes here; just let the done event through.
                if (
                    media_payload
                    and reply.type in (ReplyType.IMAGE_URL, ReplyType.FILE)
                    and content.startswith("file://")
                    and context.get("on_event") is not None
                ):
                    text_content = getattr(reply, 'text_content', '')
                    if text_content:
                        self.sse_queues[request_id].put({
                            "type": "done",
                            "content": text_content,
                            "request_id": request_id,
                            "timestamp": time.time()
                        })
                    logger.debug(f"SSE skipped duplicate file for request {request_id}")
                    return

                # Skip http-URL FILE/IMAGE_URL replies produced by chat_channel's media extraction:
                # the text reply (already sent as "done") contains the URL and the frontend will
                # render it via renderMarkdown/injectVideoPlayers, so no separate SSE event needed.
                if reply.type in (ReplyType.FILE, ReplyType.IMAGE_URL) and content.startswith(("http://", "https://")):
                    logger.debug(f"SSE skipped http media reply for request {request_id}")
                    return

                if media_payload:
                    media_payload.update({"request_id": request_id, "timestamp": time.time()})
                    self.sse_queues[request_id].put(media_payload)
                    logger.debug(f"SSE media sent for request {request_id}")
                    return

                self.sse_queues[request_id].put({
                    "type": "done",
                    "content": content,
                    "request_id": request_id,
                    "timestamp": time.time()
                })
                logger.debug(f"SSE done sent for request {request_id}")
                return

            # Fallback: polling mode
            if session_id in self.session_queues:
                content = reply.content if reply.content is not None else ""
                media_payload = self._media_response_payload(reply)
                # Skip file:// IMAGE_URL/FILE replies originating from an SSE-enabled
                # request: they were already pushed via the `file_to_send` event during
                # agent execution. By the time the chat_channel sends the IMAGE_URL reply,
                # the SSE stream has typically closed (after the text "done") and the
                # request_id is gone from sse_queues, so we'd otherwise duplicate the file
                # as a polling bubble. Scheduler/push tasks have no on_event and must
                # still go through polling normally.
                if (
                    reply.type in (ReplyType.IMAGE_URL, ReplyType.FILE)
                    and content.startswith("file://")
                    and context.get("on_event") is not None
                ):
                    logger.debug(f"Polling skipped duplicate file reply for session {session_id}")
                    return
                response_data = media_payload or {"type": str(reply.type), "content": content}
                response_data.update({"timestamp": time.time(), "request_id": request_id})
                self.session_queues[session_id].put(response_data)
                logger.debug(f"Response sent to poll queue for session {session_id}, request {request_id}")
            else:
                logger.warning(f"No response queue found for session {session_id}, response dropped")

        except Exception as e:
            logger.error(f"Error in send method: {e}")

    @staticmethod
    def _media_response_payload(reply: Reply):
        content = str(reply.content or "").strip()
        if not content:
            return None

        if reply.type in (ReplyType.IMAGE_URL, ReplyType.IMAGE):
            media_type = "image"
        elif reply.type in (ReplyType.VIDEO, ReplyType.VIDEO_URL):
            media_type = "video"
        elif reply.type == ReplyType.FILE:
            media_type = "file"
        else:
            return None

        if content.startswith(("http://", "https://")):
            url = content
            file_name = os.path.basename(content.split("?", 1)[0]) or media_type
        else:
            file_path = _local_file_path_from_reply_content(content)
            if not file_path or not os.path.isfile(file_path):
                return None
            url = _register_web_file(file_path)
            file_name = os.path.basename(file_path)
        return {"type": media_type, "content": url, "file_name": file_name}

    def _make_sse_callback(self, request_id: str):
        """Build an on_event callback that pushes agent stream events into the SSE queue."""

        # Cap reasoning bytes pushed to the frontend per request to avoid
        # browser stalls / crashes on very long chains-of-thought. Anything
        # beyond the cap is dropped from the stream (DB still persists a
        # truncated copy via _truncate_reasoning_for_storage).
        # Keep aligned with frontend REASONING_RENDER_CAP and backend
        # MAX_STORED_REASONING_CHARS.
        MAX_REASONING_STREAM_CHARS = 4 * 1024  # 4 KB
        # Use a single-element list as a mutable counter accessible from closure.
        reasoning_chars_sent = [0]
        reasoning_capped_notified = [False]

        def on_event(event: dict):
            if request_id not in self.sse_queues:
                return
            q = self.sse_queues[request_id]
            event_type = event.get("type")
            data = event.get("data", {})

            if event_type == "reasoning_update":
                delta = data.get("delta", "")
                if not delta:
                    return
                remaining = MAX_REASONING_STREAM_CHARS - reasoning_chars_sent[0]
                if remaining <= 0:
                    if not reasoning_capped_notified[0]:
                        reasoning_capped_notified[0] = True
                        q.put({
                            "type": "reasoning",
                            "content": "\n\n... [reasoning truncated for display] ...",
                        })
                    return
                if len(delta) > remaining:
                    delta = delta[:remaining]
                reasoning_chars_sent[0] += len(delta)
                q.put({"type": "reasoning", "content": delta})

            elif event_type == "message_update":
                delta = data.get("delta", "")
                if delta:
                    q.put({"type": "delta", "content": delta})

            elif event_type == "tool_execution_start":
                tool_name = data.get("tool_name", "tool")
                arguments = data.get("arguments", {})
                q.put({"type": "tool_start", "tool": tool_name, "arguments": arguments})

            elif event_type == "tool_execution_end":
                tool_name = data.get("tool_name", "tool")
                status = data.get("status", "success")
                result = data.get("result", "")
                exec_time = data.get("execution_time", 0)
                # Truncate long results to avoid huge SSE payloads
                result_str = str(result)
                if len(result_str) > 2000:
                    result_str = result_str[:2000] + "…"
                q.put({
                    "type": "tool_end",
                    "tool": tool_name,
                    "status": status,
                    "result": result_str,
                    "execution_time": round(exec_time, 2)
                })

            elif event_type == "message_end":
                tool_calls = data.get("tool_calls", [])
                if tool_calls:
                    q.put({"type": "message_end", "has_tool_calls": True})

            elif event_type == "agent_end":
                # Safety net: if the agent finishes with an empty final_response,
                # chat_channel skips _send_reply (because reply.content is empty),
                # which means no "done" event is ever emitted and the SSE stream
                # would hang until the 10-min idle timeout. Push a fallback "done"
                # here so the frontend always gets closure.
                final_response = data.get("final_response", "")
                if not final_response or not str(final_response).strip():
                    logger.warning(
                        f"[WebChannel] agent_end with empty final_response for "
                        f"request {request_id}, sending fallback done"
                    )
                    q.put({
                        "type": "done",
                        "content": "(模型未返回任何内容，请重试或换一种方式描述你的需求)",
                        "request_id": request_id,
                        "timestamp": time.time(),
                    })

            elif event_type == "file_to_send":
                file_path = data.get("path", "")
                file_name = data.get("file_name", os.path.basename(file_path))
                file_type = data.get("file_type", "file")
                web_url = _register_web_file(file_path)
                is_image = file_type == "image"
                q.put({
                    "type": "image" if is_image else "file",
                    "content": web_url,
                    "file_name": file_name,
                })

        return on_event

    def upload_file(self):
        """Handle file or directory upload via multipart/form-data."""
        try:
            params = _raw_web_input()
            file_obj = params.get("file")
            file_objs = params.get("files")
            session_id = params.get("session_id", "")
            relative_path = params.get("relative_path", "")
            relative_paths = params.get("relative_paths")
            upload_id = params.get("upload_id", "")

            directory_files = _ensure_list(file_objs)

            # NOTE: cgi.FieldStorage raises TypeError on truthy checks for single-file
            # uploads (Python 3.9+). Always use `is not None` instead of `if file_obj`.
            if not directory_files and file_obj is not None and relative_path:
                directory_files = [file_obj]

            directory_rel_paths = _ensure_list(relative_paths)

            if not directory_rel_paths and relative_path:
                directory_rel_paths = [relative_path]

            is_directory_upload = bool(directory_files) or bool(directory_rel_paths) or bool(relative_path) or bool(upload_id)

            upload_dir = _get_upload_dir()
            if is_directory_upload:
                if not upload_id:
                    return json.dumps({"status": "error", "message": "Missing upload_id for directory upload"})
                if not directory_files:
                    return json.dumps({"status": "error", "message": "No files uploaded"})
                if len(directory_files) != len(directory_rel_paths):
                    return json.dumps({"status": "error", "message": "Directory upload payload mismatch"})

                safe_upload_id = _sanitize_upload_id(upload_id)
                upload_root = os.path.join(upload_dir, f"webdir_{safe_upload_id}")
                upload_root_real = os.path.realpath(upload_root)

                root_name = None
                saved_files = 0
                for file_obj, rel_path in zip(directory_files, directory_rel_paths):
                    if file_obj is None:
                        raise ValueError("Invalid uploaded file")
                    safe_rel_path, save_path = _resolve_upload_path(upload_root_real, rel_path)
                    current_root_name = safe_rel_path.split("/", 1)[0]
                    if root_name is None:
                        root_name = current_root_name
                    elif root_name != current_root_name:
                        raise ValueError("Directory upload must use a single root folder")
                    os.makedirs(os.path.dirname(save_path), exist_ok=True)
                    content_bytes = _read_uploaded_file_bytes(file_obj)
                    with open(save_path, "wb") as f:
                        f.write(content_bytes)
                    saved_files += 1

                if not root_name:
                    raise ValueError("Directory root path missing")

                root_path = os.path.realpath(os.path.join(upload_root_real, root_name))
                if not _is_within_directory(upload_root_real, root_path):
                    raise ValueError("Invalid directory upload path")

                logger.info(f"[WebChannel] Directory uploaded: {root_name} -> {root_path} ({saved_files} files)")
                return json.dumps({
                    "status": "success",
                    "file_path": root_path,
                    "file_name": root_name,
                    "file_type": "directory",
                    "file_count": saved_files,
                    "root_path": root_path,
                    "root_name": root_name,
                    "upload_type": "directory",
                }, ensure_ascii=False)

            if file_obj is None or not hasattr(file_obj, "filename") or not file_obj.filename:
                return json.dumps({"status": "error", "message": "No file uploaded"})

            original_name = file_obj.filename
            ext = os.path.splitext(original_name)[1].lower()
            safe_name = f"web_{uuid.uuid4().hex[:8]}{ext}"
            save_path = os.path.join(upload_dir, safe_name)
            public_path = safe_name
            display_name = original_name

            content_bytes = _read_uploaded_file_bytes(file_obj)
            with open(save_path, "wb") as f:
                f.write(content_bytes)

            if ext in IMAGE_EXTENSIONS:
                file_type = "image"
            elif ext in VIDEO_EXTENSIONS:
                file_type = "video"
            else:
                file_type = "file"

            if file_type == "image" and session_id:
                _register_web_uploaded_images(
                    session_id,
                    [{
                        "file_type": file_type,
                        "file_path": save_path,
                    }],
                )

            from urllib.parse import quote
            preview_url = f"/uploads/{quote(public_path, safe='/')}"

            logger.info(f"[WebChannel] File uploaded: {original_name} -> {os.path.basename(save_path)} ({file_type})")

            return json.dumps({
                "status": "success",
                "file_path": save_path,
                "file_name": display_name,
                "file_type": file_type,
                "preview_url": preview_url,
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"[WebChannel] File upload error: {e}", exc_info=True)
            return json.dumps({"status": "error", "message": str(e)})

    def post_message(self):
        """
        Handle incoming messages from users via POST request.
        Returns a request_id for tracking this specific request.
        Supports optional attachments (file paths from /upload).
        """
        try:
            data = web.data()
            json_data = json.loads(data)
            session_id = json_data.get('session_id', f'session_{int(time.time())}')
            prompt = json_data.get('message', '')
            use_sse = json_data.get('stream', True)
            attachments = json_data.get('attachments', [])

            # Append file references to the prompt (same format as QQ channel)
            if attachments:
                file_refs = []
                for att in attachments:
                    ftype = att.get("file_type", "file")
                    fpath = att.get("file_path", "")
                    if not fpath:
                        continue
                    if ftype == "image":
                        file_refs.append(f"[图片: {fpath}]")
                    elif ftype == "video":
                        file_refs.append(f"[视频: {fpath}]")
                    elif ftype == "directory":
                        file_refs.append(f"[目录: {fpath}]")
                    else:
                        file_refs.append(f"[文件: {fpath}]")
                if file_refs:
                    prompt = prompt + "\n" + "\n".join(file_refs)
                    logger.info(f"[WebChannel] Attached {len(file_refs)} file(s) to message")

            request_id = self._generate_request_id()
            self.request_to_session[request_id] = session_id

            if session_id not in self.session_queues:
                self.session_queues[session_id] = Queue()

            if use_sse:
                self.sse_queues[request_id] = Queue()

            trigger_prefixs = conf().get("single_chat_prefix", [""])
            if check_prefix(prompt, trigger_prefixs) is None:
                if trigger_prefixs:
                    prompt = trigger_prefixs[0] + prompt
                    logger.debug(f"[WebChannel] Added prefix to message: {prompt}")

            msg = WebMessage(self._generate_msg_id(), prompt)
            msg.from_user_id = session_id

            context = self._compose_context(ContextType.TEXT, prompt, msg=msg, isgroup=False)

            if context is None:
                logger.warning(f"[WebChannel] Context is None for session {session_id}, message may be filtered")
                if request_id in self.sse_queues:
                    del self.sse_queues[request_id]
                return json.dumps({"status": "error", "message": "Message was filtered"})

            context["session_id"] = session_id
            context["receiver"] = session_id
            context["request_id"] = request_id
            context["web_authenticated"] = True
            _apply_web_admin_context(context)
            _register_web_uploaded_images(session_id, attachments)

            if use_sse:
                context["on_event"] = self._make_sse_callback(request_id)

            threading.Thread(target=self.produce, args=(context,)).start()

            return json.dumps({"status": "success", "request_id": request_id, "stream": use_sse})

        except Exception as e:
            logger.error(f"Error processing message: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def stream_response(self, request_id: str):
        """
        SSE generator for a given request_id.
        Yields UTF-8 encoded bytes to avoid WSGI Latin-1 mangling.
        Supports client reconnection: the queue is only removed after a
        "done" event is consumed, so a new GET /stream with the same
        request_id can resume reading remaining events.
        """
        if request_id not in self.sse_queues:
            yield b"data: {\"type\": \"error\", \"message\": \"invalid request_id\"}\n\n"
            return

        q = self.sse_queues[request_id]
        idle_timeout = 600  # 10 minutes without any real event
        deadline = time.time() + idle_timeout
        done = False

        try:
            while time.time() < deadline:
                try:
                    item = q.get(timeout=1)
                except Empty:
                    yield b": keepalive\n\n"
                    continue

                # Real event received, reset idle deadline
                deadline = time.time() + idle_timeout

                payload = json.dumps(item, ensure_ascii=False)
                yield f"data: {payload}\n\n".encode("utf-8")

                if item.get("type") == "done":
                    done = True
                    break
        finally:
            if done:
                self.sse_queues.pop(request_id, None)

    def poll_response(self):
        """
        Poll for responses using the session_id.
        """
        try:
            data = web.data()
            json_data = json.loads(data)
            session_id = json_data.get('session_id')

            if not session_id or session_id not in self.session_queues:
                return json.dumps({"status": "error", "message": "Invalid session ID"})

            # 尝试从队列获取响应，不等待
            try:
                # 使用peek而不是get，这样如果前端没有成功处理，下次还能获取到
                response = self.session_queues[session_id].get(block=False)

                # 返回响应，包含请求ID以区分不同请求
                return json.dumps({
                    "status": "success",
                    "has_content": True,
                    "type": response.get("type", ""),
                    "content": response["content"],
                    "file_name": response.get("file_name", ""),
                    "request_id": response["request_id"],
                    "timestamp": response["timestamp"]
                })

            except Empty:
                # 没有新响应
                return json.dumps({"status": "success", "has_content": False})

        except Exception as e:
            logger.error(f"Error polling response: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def chat_page(self):
        """Serve the chat HTML page."""
        file_path = os.path.join(os.path.dirname(__file__), 'chat.html')  # 使用绝对路径
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()

    def startup(self):
        configured_host = conf().get("web_host", "")
        host = configured_host or ("0.0.0.0" if _is_password_enabled() else "127.0.0.1")
        port = conf().get("web_port", 9899)
        is_public_bind = host in ("0.0.0.0", "::")

        # 打印可用渠道类型提示
        logger.info(
            "[WebChannel] 全部可用通道如下，可修改 config.json 配置文件中的 channel_type 字段进行切换，多个通道用逗号分隔：")
        logger.info("[WebChannel]   1. weixin           - 微信")
        logger.info("[WebChannel]   2. web              - 网页")
        logger.info("[WebChannel]   3. terminal         - 终端")
        logger.info("[WebChannel]   4. feishu           - 飞书")
        logger.info("[WebChannel]   5. dingtalk         - 钉钉")
        logger.info("[WebChannel]   6. wecom_bot        - 企微智能机器人")
        logger.info("[WebChannel]   7. wechatcom_app    - 企微自建应用")
        logger.info("[WebChannel]   8. wechatmp         - 个人公众号")
        logger.info("[WebChannel]   9. wechatmp_service - 企业公众号")
        logger.info("[WebChannel] ✅ Web控制台已运行")
        logger.info(f"[WebChannel] 🌐 本地访问: http://localhost:{port}")
        if is_public_bind:
            logger.info(f"[WebChannel] 🌍 服务器访问: http://YOUR_IP:{port} (将YOUR_IP替换为服务器IP)")
            if not _is_password_enabled():
                logger.info("[WebChannel] ⚠️  当前监听 0.0.0.0 且未设置 web_password，公网部署建议在 config.json 中配置访问密码")
        else:
            logger.info(f"[WebChannel] 🔒 当前仅监听 {host}，仅本机可访问。如需公网访问，请将 web_host 改为 0.0.0.0 并配置 web_password 密码")

        try:
            import webbrowser
            webbrowser.open(f"http://localhost:{port}")
            logger.debug(f"[WebChannel] Opened browser at http://localhost:{port}")
        except Exception as e:
            logger.debug(f"[WebChannel] Could not open browser: {e}")

        # 确保静态文件目录存在
        static_dir = os.path.join(os.path.dirname(__file__), 'static')
        if not os.path.exists(static_dir):
            os.makedirs(static_dir)
            logger.debug(f"[WebChannel] Created static directory: {static_dir}")

        kb_backend_routes = ()
        try:
            from channel.web import kb_backend_routes as kb_routes
            globals().update({
                "KnowledgeBackendAdminHandler": kb_routes.KnowledgeBackendAdminHandler,
                "KnowledgeBackendAdminUploadHandler": kb_routes.KnowledgeBackendAdminUploadHandler,
                "KnowledgeBackendProviderHandler": kb_routes.KnowledgeBackendProviderHandler,
            })
            kb_backend_routes = kb_routes.kb_backend_routes
        except Exception as e:
            logger.warning(f"[WebChannel] Knowledge backend routes not loaded: {e}")

        urls = (
            '/', 'RootHandler',
            '/auth/login', 'AuthLoginHandler',
            '/auth/check', 'AuthCheckHandler',
            '/auth/logout', 'AuthLogoutHandler',
            '/message', 'MessageHandler',
            '/upload', 'UploadHandler',
            '/uploads/(.*)', 'UploadsHandler',
            '/api/file', 'FileServeHandler',
            '/poll', 'PollHandler',
            '/stream', 'StreamHandler',
            '/chat', 'ChatHandler',
            '/grok', 'GrokPageHandler',
            '/config', 'ConfigHandler',
            '/api/channels', 'ChannelsHandler',
            '/api/grok/status', 'GrokStatusHandler',
            '/api/grok/login/start', 'GrokLoginStartHandler',
            '/api/grok/login/poll', 'GrokLoginPollHandler',
            '/api/grok/login/manual', 'GrokLoginManualHandler',
            '/api/grok/logout', 'GrokLogoutHandler',
            '/api/grok/test', 'GrokTestHandler',
            '/api/weixin/qrlogin', 'WeixinQrHandler',
            '/api/feishu/register', 'FeishuRegisterHandler',
            '/api/tools', 'ToolsHandler',
            '/api/commands', 'CommandsHandler',
            '/api/skills', 'SkillsHandler',
            '/api/memory', 'MemoryHandler',
            '/api/memory/content', 'MemoryContentHandler',
            '/api/knowledge/list', 'KnowledgeListHandler',
            '/api/knowledge/read', 'KnowledgeReadHandler',
            '/api/knowledge/graph', 'KnowledgeGraphHandler',
            '/api/scheduler', 'SchedulerHandler',
            '/api/sessions', 'SessionsHandler',
            '/api/sessions/(.*)/generate_title', 'SessionTitleHandler',
            '/api/sessions/(.*)/clear_context', 'SessionClearContextHandler',
            '/api/sessions/(.*)', 'SessionDetailHandler',
            '/api/history', 'HistoryHandler',
            '/api/cache-usage', 'CacheUsageHandler',
            '/api/logs', 'LogsHandler',
            '/api/version', 'VersionHandler',
            '/assets/(.*)', 'AssetsHandler',
        ) + kb_backend_routes
        app = web.application(urls, globals(), autoreload=False)

        # 完全禁用web.py的HTTP日志输出
        web.httpserver.LogMiddleware.log = lambda self, status, environ: None

        # 配置web.py的日志级别为ERROR
        logging.getLogger("web").setLevel(logging.ERROR)
        logging.getLogger("web.httpserver").setLevel(logging.ERROR)

        # Build WSGI app with middleware (same as runsimple but without print)
        func = web.httpserver.StaticMiddleware(app.wsgifunc())
        func = web.httpserver.LogMiddleware(func)
        server = web.httpserver.WSGIServer((host, port), func)
        server.daemon_threads = True
        # Default request_queue_size(5) / timeout(10s) / numthreads(10) are
        # too small: when SSE streams occupy many threads, the backlog fills
        # and new connections get refused (ERR_CONNECTION_ABORTED).
        server.request_queue_size = 128
        server.timeout = 300
        server.requests.min = 20
        server.requests.max = 80
        self._http_server = server
        try:
            server.start()
        except (KeyboardInterrupt, SystemExit):
            server.stop()
        except OSError as e:
            if e.errno in (48, 98):  # macOS/Linux EADDRINUSE
                logger.error(
                    f"[WebChannel] 端口 {port} 已被占用，可执行 `cow restart` 清理残留进程，"
                    f"或在 config.json 中修改 web_port"
                )
            raise

    def stop(self):
        if self._http_server:
            try:
                self._http_server.stop()
                logger.info("[WebChannel] HTTP server stopped")
            except Exception as e:
                logger.warning(f"[WebChannel] Error stopping HTTP server: {e}")
            self._http_server = None


class RootHandler:
    def GET(self):
        raise web.seeother('/chat')


class AuthCheckHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        if not _is_password_enabled():
            return json.dumps({"status": "success", "auth_required": False})
        if _check_auth():
            return json.dumps({"status": "success", "auth_required": True, "authenticated": True})
        return json.dumps({"status": "success", "auth_required": True, "authenticated": False})


class AuthLoginHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        if not _is_password_enabled():
            return json.dumps({"status": "success"})
        try:
            data = json.loads(web.data())
        except Exception:
            return json.dumps({"status": "error", "message": "Invalid request"})
        password = data.get("password", "")
        expected = conf().get("web_password", "")
        if not hmac.compare_digest(password, expected):
            logger.warning("[WebChannel] Invalid login attempt")
            return json.dumps({"status": "error", "message": "Wrong password"})
        token = _create_auth_token()
        web.setcookie("cow_auth_token", token, expires=_session_expire_seconds(),
                       path="/", httponly=True, samesite="Lax")
        return json.dumps({"status": "success"})


class AuthLogoutHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.setcookie("cow_auth_token", "", expires=-1, path="/")
        return json.dumps({"status": "success"})


def _safe_grok_error(exc: Exception) -> str:
    code = str(getattr(exc, "code", "") or "")
    if code == "xai_state_missing":
        return "Grok manual login requires both code and state. Paste the full callback URL or query string."
    if code == "xai_state_mismatch":
        return "Grok manual login state did not match the active login session."
    if code == "xai_callback_pending":
        return "Grok callback has not been received yet. Complete browser login, then click the poll button."
    if code == "xai_login_session_missing":
        return "No active Grok login session. Start Grok login again."
    text = str(exc) or "Grok OAuth request failed"
    try:
        from integrations.hermes_xai.auth import redact_sensitive_text

        redacted = redact_sensitive_text(text)
    except Exception:
        redacted = text
    sensitive_markers = (
        "access_token",
        "refresh_token",
        "authorization_code",
        "code_verifier",
        "id_token",
        "api_key",
        "authorization:",
        "bearer ",
        "callback?",
        "/callback?",
        "code=",
    )
    lowered = text.lower()
    if any(marker in lowered for marker in sensitive_markers):
        return "Grok OAuth request failed. Sensitive details were redacted."
    if "callback" in text.lower() or "code=" in text.lower():
        return "Grok OAuth callback validation failed"
    return redacted


class GrokStatusHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from integrations.hermes_xai.auth import get_xai_oauth_status

            return json.dumps(get_xai_oauth_status(), ensure_ascii=False)
        except Exception as e:
            logger.error(f"[GrokOAuth] status API error: {_safe_grok_error(e)}")
            return json.dumps({"logged_in": False, "needs_reauth": True, "message": _safe_grok_error(e)})


class GrokLoginStartHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from integrations.hermes_xai.auth import start_xai_oauth_login

            payload = start_xai_oauth_login()
            response = dict(payload)
            response["status"] = "pending"
            return json.dumps(response, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[GrokOAuth] login start error: {_safe_grok_error(e)}")
            return json.dumps({"status": "error", "message": _safe_grok_error(e)}, ensure_ascii=False)


class GrokLoginPollHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from integrations.hermes_xai.auth import poll_xai_oauth_login

            return json.dumps(poll_xai_oauth_login(), ensure_ascii=False)
        except Exception as e:
            logger.error(f"[GrokOAuth] login poll error: {_safe_grok_error(e)}")
            return json.dumps({"status": "failed", "message": _safe_grok_error(e)}, ensure_ascii=False)


class GrokLoginManualHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b"{}")
        except Exception:
            return json.dumps({"status": "error", "message": "Invalid request"}, ensure_ascii=False)

        callback_url = str(body.get("callback_url") or body.get("authorization_code") or "").strip()
        if not callback_url:
            return json.dumps({
                "status": "error",
                "message": "callback URL or query string with code and state required",
            }, ensure_ascii=False)

        try:
            from integrations.hermes_xai.auth import (
                complete_xai_oauth_with_callback_url,
                complete_xai_oauth_with_pending_callback,
            )

            status = complete_xai_oauth_with_callback_url(callback_url)
            return json.dumps({"status": "complete", **status}, ensure_ascii=False)
        except Exception as e:
            if getattr(e, "code", "") in {"xai_state_missing", "xai_callback_invalid"}:
                try:
                    status = complete_xai_oauth_with_pending_callback()
                    return json.dumps({"status": "complete", **status}, ensure_ascii=False)
                except Exception:
                    pass
            logger.error(f"[GrokOAuth] manual login error: {_safe_grok_error(e)}")
            return json.dumps({"status": "error", "message": _safe_grok_error(e)}, ensure_ascii=False)


class GrokLogoutHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from integrations.hermes_xai.auth import logout_xai_oauth

            return json.dumps({"status": "success", **logout_xai_oauth()}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[GrokOAuth] logout error: {_safe_grok_error(e)}")
            return json.dumps({"status": "error", "message": _safe_grok_error(e)}, ensure_ascii=False)


class GrokTestHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from integrations.hermes_xai.xai_http import resolve_xai_http_credentials

            creds = resolve_xai_http_credentials()
            return json.dumps({
                "status": "success",
                "provider": creds.get("provider"),
                "auth_mode": creds.get("auth_mode"),
                "base_url": creds.get("base_url"),
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[GrokOAuth] test API error: {_safe_grok_error(e)}")
            return json.dumps({"status": "error", "message": _safe_grok_error(e)}, ensure_ascii=False)


class MessageHandler:
    def POST(self):
        _require_auth()
        return WebChannel().post_message()


class UploadHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        return WebChannel().upload_file()


class UploadsHandler:
    def GET(self, file_name):
        _require_auth()
        try:
            upload_dir = _get_upload_dir()
            full_path = os.path.normpath(os.path.join(upload_dir, file_name))
            if not os.path.abspath(full_path).startswith(os.path.abspath(upload_dir)):
                raise web.notfound()
            if not os.path.isfile(full_path):
                raise web.notfound()
            content_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
            web.header('Content-Type', content_type)
            web.header('Cache-Control', 'public, max-age=86400')
            with open(full_path, 'rb') as f:
                return f.read()
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Error serving upload: {e}")
            raise web.notfound()


class FileServeHandler:
    def GET(self):
        _require_auth()
        try:
            params = web.input(path="", token="")
            file_path = _resolve_web_file_token(params.token) if params.token else params.path
            if not file_path or not os.path.isabs(file_path):
                raise web.notfound()
            file_path = os.path.normpath(file_path)
            if not params.token and not _is_allowed_web_file_path(file_path):
                raise web.notfound()
            if not os.path.isfile(file_path):
                raise web.notfound()
            content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            file_name = os.path.basename(file_path)
            from urllib.parse import quote
            web.header('Content-Type', content_type)
            web.header('Content-Disposition', f"inline; filename*=UTF-8''{quote(file_name)}")
            web.header('Cache-Control', 'public, max-age=3600')
            with open(file_path, 'rb') as f:
                return f.read()
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Error serving file: {e}")
            raise web.notfound()


class PollHandler:
    def POST(self):
        _require_auth()
        return WebChannel().poll_response()


class StreamHandler:
    def GET(self):
        _require_auth()
        params = web.input(request_id='')
        request_id = params.request_id
        if not request_id:
            raise web.badrequest()

        web.header('Content-Type', 'text/event-stream; charset=utf-8')
        web.header('Cache-Control', 'no-cache')
        web.header('X-Accel-Buffering', 'no')
        web.header('Access-Control-Allow-Origin', '*')

        return WebChannel().stream_response(request_id)


class ChatHandler:
    def GET(self):
        web.header('Cache-Control', 'no-cache, no-store, must-revalidate')
        web.header('Pragma', 'no-cache')
        file_path = os.path.join(os.path.dirname(__file__), 'chat.html')
        with open(file_path, 'r', encoding='utf-8') as f:
            html = f.read()
        cache_bust = str(int(time.time()))
        html = html.replace('assets/js/console.js', f'assets/js/console.js?v={cache_bust}')
        html = html.replace('assets/css/console.css', f'assets/css/console.css?v={cache_bust}')
        return html


class GrokPageHandler:
    def GET(self):
        _require_auth()
        web.header('Cache-Control', 'no-cache, no-store, must-revalidate')
        web.header('Pragma', 'no-cache')
        file_path = os.path.join(os.path.dirname(__file__), 'grok.html')
        with open(file_path, 'r', encoding='utf-8') as f:
            html = f.read()
        cache_bust = str(int(time.time()))
        return html.replace('assets/js/grok.js', f'assets/js/grok.js?v={cache_bust}')


class ConfigHandler:

    _RECOMMENDED_MODELS = [
        const.DEEPSEEK_V4_FLASH, const.DEEPSEEK_V4_PRO, const.DEEPSEEK_CHAT, const.DEEPSEEK_REASONER,
        const.MINIMAX_M2_7_HIGHSPEED, const.MINIMAX_M2_7, const.MINIMAX_M2_5, const.MINIMAX_M2_1, const.MINIMAX_M2_1_LIGHTNING,
        const.CLAUDE_4_6_SONNET, const.CLAUDE_4_7_OPUS, const.CLAUDE_4_6_OPUS, const.CLAUDE_4_5_SONNET,
        const.GEMINI_31_FLASH_LITE_PRE, const.GEMINI_31_PRO_PRE, const.GEMINI_3_FLASH_PRE,
        const.GPT_54, const.GPT_54_MINI, const.GPT_54_NANO, const.GPT_5, const.GPT_41, const.GPT_4o,
        const.GLM_5_1, const.GLM_5_TURBO, const.GLM_5, const.GLM_4_7,
        const.QWEN36_PLUS, const.QWEN35_PLUS, const.QWEN3_MAX,
        const.GROK_4_3, const.GROK_4, const.GROK_3_MINI,
        const.DOUBAO_SEED_2_PRO, const.DOUBAO_SEED_2_CODE,
        const.KIMI_K2_6, const.KIMI_K2_5, const.KIMI_K2,
        const.ERNIE_5_1, const.ERNIE_5, const.ERNIE_X1_1, const.ERNIE_45_TURBO_128K, const.ERNIE_45_TURBO_32K,
    ]

    # Generic placeholder hints surfaced in the web console. We deliberately
    # show the version-path tail (e.g. "/v1") so users are reminded to type
    # the full base URL. The form is intentionally vague (`...../v1`) so it
    # never looks like a real default a user might paste verbatim — and we
    # never auto-rewrite anything on the server side.
    _PLACEHOLDER_V1 = "https://...../v1"
    _PLACEHOLDER_QIANFAN = "https://...../v2"
    _PLACEHOLDER_ZHIPU = "https://...../api/paas/v4"
    _PLACEHOLDER_DOUBAO = "https://...../api/v3"
    _PLACEHOLDER_GEMINI = "https://....."

    PROVIDER_MODELS = OrderedDict([
        ("deepseek", {
            "label": "DeepSeek",
            "api_key_field": "deepseek_api_key",
            "api_base_key": "deepseek_api_base",
            "api_base_default": "https://api.deepseek.com/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.DEEPSEEK_V4_FLASH, const.DEEPSEEK_V4_PRO, const.DEEPSEEK_CHAT, const.DEEPSEEK_REASONER],
        }),
        ("minimax", {
            "label": "MiniMax",
            "api_key_field": "minimax_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "api_base_placeholder": "",
            "models": [const.MINIMAX_M2_7, const.MINIMAX_M2_7_HIGHSPEED, const.MINIMAX_M2_5, const.MINIMAX_M2_1, const.MINIMAX_M2_1_LIGHTNING],
        }),
        ("claudeAPI", {
            "label": "Claude",
            "api_key_field": "claude_api_key",
            "api_base_key": "claude_api_base",
            "api_base_default": "https://api.anthropic.com/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.CLAUDE_4_6_SONNET, const.CLAUDE_4_7_OPUS, const.CLAUDE_4_6_OPUS, const.CLAUDE_4_5_SONNET],
        }),
        ("gemini", {
            "label": "Gemini",
            "api_key_field": "gemini_api_key",
            "api_base_key": "gemini_api_base",
            "api_base_default": "https://generativelanguage.googleapis.com",
            "api_base_placeholder": _PLACEHOLDER_GEMINI,
            "models": [const.GEMINI_31_FLASH_LITE_PRE, const.GEMINI_31_PRO_PRE, const.GEMINI_3_FLASH_PRE],
        }),
        ("openai", {
            "label": "OpenAI",
            "api_key_field": "open_ai_api_key",
            "api_base_key": "open_ai_api_base",
            "api_base_default": "https://api.openai.com/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.GPT_54, const.GPT_54_MINI, const.GPT_54_NANO, const.GPT_5, const.GPT_41, const.GPT_4o],
        }),
        ("codex", {
            "label": "Codex",
            "api_key_field": None,
            "api_base_key": None,
            "api_base_default": None,
            "api_base_placeholder": "",
            "models": ["gpt-5.5", const.GPT_54, const.GPT_54_MINI, const.GPT_5, const.GPT_41],
        }),
        ("grok", {
            "label": "Grok",
            "api_key_field": "grok_api_key",
            "api_base_key": "grok_api_base",
            "api_base_default": "https://api.x.ai/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.GROK_4_3, const.GROK_4, const.GROK_3_MINI],
        }),
        ("zhipu", {
            "label": "智谱AI",
            "api_key_field": "zhipu_ai_api_key",
            "api_base_key": "zhipu_ai_api_base",
            "api_base_default": "https://open.bigmodel.cn/api/paas/v4",
            "api_base_placeholder": _PLACEHOLDER_ZHIPU,
            "models": [const.GLM_5_1, const.GLM_5_TURBO, const.GLM_5, const.GLM_4_7],
        }),
        ("dashscope", {
            "label": "通义千问",
            "api_key_field": "dashscope_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "api_base_placeholder": "",
            "models": [const.QWEN36_PLUS, const.QWEN35_PLUS, const.QWEN3_MAX],
        }),
        ("doubao", {
            "label": "豆包",
            "api_key_field": "ark_api_key",
            "api_base_key": "ark_base_url",
            "api_base_default": "https://ark.cn-beijing.volces.com/api/v3",
            "api_base_placeholder": _PLACEHOLDER_DOUBAO,
            "models": [const.DOUBAO_SEED_2_PRO, const.DOUBAO_SEED_2_CODE],
        }),
        ("moonshot", {
            "label": "Kimi",
            "api_key_field": "moonshot_api_key",
            "api_base_key": "moonshot_base_url",
            "api_base_default": "https://api.moonshot.cn/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.KIMI_K2_6, const.KIMI_K2_5, const.KIMI_K2],
        }),
        ("qianfan", {
            "label": "百度千帆",
            "api_key_field": "qianfan_api_key",
            "api_base_key": "qianfan_api_base",
            "api_base_default": "https://qianfan.baidubce.com/v2",
            "api_base_placeholder": _PLACEHOLDER_QIANFAN,
            "models": [const.ERNIE_5_1, const.ERNIE_5, const.ERNIE_X1_1, const.ERNIE_45_TURBO_128K, const.ERNIE_45_TURBO_32K],
        }),
        ("modelscope", {
            "label": "ModelScope",
            "api_key_field": "modelscope_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "api_base_placeholder": "",
            "models": [const.QWEN3_5_27B, const.QWEN3_235B_A22B_INSTRUCT_2507],
        }),
        ("linkai", {
            "label": "LinkAI",
            "api_key_field": "linkai_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "api_base_placeholder": "",
            "models": _RECOMMENDED_MODELS,
        }),
        ("custom", {
            "label": "自定义",
            "api_key_field": "custom_api_key",
            "api_base_key": "custom_api_base",
            "api_base_default": "",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [],
        }),
    ])

    EDITABLE_KEYS = {
        "model", "bot_type", "use_linkai",
        "open_ai_api_base", "deepseek_api_base", "qianfan_api_base", "claude_api_base", "gemini_api_base",
        "zhipu_ai_api_base", "moonshot_base_url", "ark_base_url", "custom_api_base", "grok_api_base",
        "open_ai_api_key", "deepseek_api_key", "qianfan_api_key", "claude_api_key", "gemini_api_key",
        "zhipu_ai_api_key", "dashscope_api_key", "moonshot_api_key",
        "ark_api_key", "minimax_api_key", "linkai_api_key", "custom_api_key", "grok_api_key",
        "grok_model", "grok_wire_api", "grok_auth_file", "grok_auth_prefer_oauth",
        "grok_gray_enabled", "grok_import_hermes_auth", "grok_import_hermes_auth_overwrite",
        "agent_max_context_tokens", "agent_max_context_turns", "agent_max_steps",
        "agent_development_max_steps", "agent_complex_planning_max_steps",
        "enable_thinking", "web_password",
    }

    @staticmethod
    def _mask_key(value: str) -> str:
        """Mask the middle part of an API key for display."""
        if not value or len(value) <= 8:
            return value
        return value[:4] + "*" * (len(value) - 8) + value[-4:]

    def _visible_provider_models(self, local_config):
        providers = OrderedDict(self.PROVIDER_MODELS)
        if not local_config.get("grok_gray_enabled", False):
            providers.pop("grok", None)
        return providers

    @staticmethod
    def _safe_backend_profiles(llm_cfg):
        providers = llm_cfg.get("providers") if isinstance(llm_cfg.get("providers"), dict) else {}
        result = {}
        for backend, provider in providers.items():
            if not isinstance(provider, dict):
                continue
            public = {
                "label": provider.get("label") or backend,
                "model": provider.get("model") or "",
                "api_base": provider.get("api_base") or provider.get("base_url") or "",
                "wire_api": provider.get("wire_api") or "",
                "auth": provider.get("auth") or "",
            }
            result[str(backend)] = public
        return result

    @staticmethod
    def _backend_profile_id(value: str) -> str:
        from common.llm_backend_router import (
            BACKEND_CAPI,
            BACKEND_CAPI_MONTHLY,
            BACKEND_CODEX,
            BACKEND_GROK,
            USER_BACKEND_DEFAULT,
            normalize_backend,
        )

        raw = str(value or "").strip().lower()
        if not raw:
            return ""
        if raw in {USER_BACKEND_DEFAULT, "global", "default", "auto", "gpt"}:
            return ""
        known_aliases = {
            BACKEND_CAPI,
            BACKEND_CAPI_MONTHLY,
            "capi-monthly",
            "capi_month",
            "capi-month",
            "monthly",
            "month",
            BACKEND_CODEX,
            "openai-codex",
            "codex-direct",
            BACKEND_GROK,
            const.XAI,
            "xai-oauth",
            "grok-account",
        }
        if raw in known_aliases:
            return normalize_backend(raw)
        if re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", raw):
            return raw
        return ""

    @staticmethod
    def _backend_target_profile(target: str):
        target = str(target or "").strip()
        if not target:
            return None
        admin_profile = ConfigHandler._web_admin_profile_or_default()
        if target in {"__admin__", "admin", "current_admin"}:
            return admin_profile
        return SimpleNamespace(
            actor_id=target,
            raw_user_id=target,
            memory_user_id=target,
            display_name=target,
            role="user",
            is_admin=False,
        )

    @staticmethod
    def _web_admin_profile_or_default():
        profile = _get_web_admin_profile()
        if profile is not None:
            return profile
        return SimpleNamespace(
            actor_id="web:admin",
            raw_user_id="admin",
            memory_user_id="web_admin",
            display_name="Web Admin",
            role="admin",
            is_admin=True,
        )

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            local_config = conf()
            use_agent = local_config.get("agent", True)
            title = "CowAgent" if use_agent else "AI Assistant"

            visible_provider_models = self._visible_provider_models(local_config)
            api_bases = {}
            api_keys_masked = {}
            for pid, pinfo in visible_provider_models.items():
                base_key = pinfo.get("api_base_key")
                if base_key:
                    api_bases[base_key] = local_config.get(base_key, pinfo["api_base_default"])
                key_field = pinfo.get("api_key_field")
                if key_field and key_field not in api_keys_masked:
                    raw = local_config.get(key_field, "")
                    api_keys_masked[key_field] = self._mask_key(raw) if raw else ""

            providers = {}
            for pid, p in visible_provider_models.items():
                providers[pid] = {
                    "label": p["label"],
                    "models": p["models"],
                    "api_base_key": p["api_base_key"],
                    "api_base_default": p["api_base_default"],
                    "api_base_placeholder": p.get("api_base_placeholder", ""),
                    "api_key_field": p.get("api_key_field"),
                }

            raw_pwd = local_config.get("web_password", "")
            masked_pwd = ("*" * len(raw_pwd)) if raw_pwd else ""
            from common.llm_backend_router import available_actor_backends, get_llm_backend_config, load_state, status_snapshot
            admin_profile = self._web_admin_profile_or_default()
            backend_status = status_snapshot(admin_profile)
            llm_cfg = get_llm_backend_config()
            restricted = llm_cfg.get("restricted_backends") if isinstance(llm_cfg.get("restricted_backends"), dict) else {}
            state = load_state()
            display_model = backend_status.get("effective_model") if backend_status.get("current_backend") == "codex" else local_config.get("model", "")
            display_bot_type = "codex" if backend_status.get("current_backend") == "codex" else (
                "openai" if local_config.get("bot_type") == "chatGPT" else local_config.get("bot_type", "")
            )

            return json.dumps({
                "status": "success",
                "use_agent": use_agent,
                "title": title,
                "model": display_model,
                "bot_type": display_bot_type,
                "use_linkai": bool(local_config.get("use_linkai", False)),
                "channel_type": local_config.get("channel_type", ""),
                "agent_max_context_tokens": local_config.get("agent_max_context_tokens", 50000),
                "agent_max_context_turns": local_config.get("agent_max_context_turns", 20),
                "agent_max_steps": local_config.get("agent_max_steps", 20),
                "agent_development_max_steps": local_config.get("agent_development_max_steps", 40),
                "agent_complex_planning_max_steps": local_config.get("agent_complex_planning_max_steps", 40),
                "enable_thinking": bool(local_config.get("enable_thinking", False)),
                "api_bases": api_bases,
                "api_keys": api_keys_masked,
                "providers": providers,
                "llm_backend": backend_status,
                "model_backends": {
                    "profiles": self._safe_backend_profiles(llm_cfg),
                    "available_for_admin": available_actor_backends(admin_profile),
                    "restricted_whitelist": restricted.get("whitelist") or [],
                    "user_overrides": state.get("user_backend_overrides", {}) if isinstance(state.get("user_backend_overrides"), dict) else {},
                },
                "web_password_masked": masked_pwd,
                "grok_gray_enabled": bool(local_config.get("grok_gray_enabled", False)),
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error getting config: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            data = json.loads(web.data())
            if not isinstance(data, dict):
                return json.dumps({"status": "error", "message": "invalid request body"})
            updates = data.get("updates", {})
            updates = updates if isinstance(updates, dict) else {}
            backend_profile_update = data.get("llm_backend_provider")
            actor_backend_update = data.get("actor_backend")
            if backend_profile_update is not None and not isinstance(backend_profile_update, dict):
                return json.dumps({"status": "error", "message": "invalid backend provider payload"})
            if actor_backend_update is not None and not isinstance(actor_backend_update, dict):
                return json.dumps({"status": "error", "message": "invalid actor backend payload"})
            if not updates and backend_profile_update is None and actor_backend_update is None:
                return json.dumps({"status": "error", "message": "no updates provided"})

            local_config = conf()
            applied = {}
            codex_selected = str(updates.get("bot_type") or "").strip().lower() == const.CODEX
            codex_model = updates.get("model") if codex_selected else None
            if codex_selected:
                updates = dict(updates)
                updates.pop("bot_type", None)
                updates.pop("model", None)
            for key, value in updates.items():
                if key not in self.EDITABLE_KEYS:
                    continue
                if key in (
                    "agent_max_context_tokens",
                    "agent_max_context_turns",
                    "agent_max_steps",
                    "agent_development_max_steps",
                    "agent_complex_planning_max_steps",
                ):
                    value = int(value)
                if key in (
                    "use_linkai",
                    "enable_thinking",
                    "grok_auth_prefer_oauth",
                    "grok_gray_enabled",
                    "grok_import_hermes_auth",
                    "grok_import_hermes_auth_overwrite",
                ):
                    value = bool(value)
                local_config[key] = value
                applied[key] = value

            if not applied and not codex_selected:
                if backend_profile_update is None and actor_backend_update is None:
                    return json.dumps({"status": "error", "message": "no valid keys to update"})

            config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "config.json")
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    file_cfg = json.load(f)
            else:
                file_cfg = {}
            if codex_selected:
                from common.llm_backend_router import get_llm_backend_config, set_user_backend_override

                llm_cfg = get_llm_backend_config()
                providers = llm_cfg.setdefault("providers", {})
                codex_cfg = providers.setdefault("codex", {})
                if codex_model:
                    codex_cfg["model"] = codex_model
                local_config["llm_backend"] = llm_cfg
                file_cfg["llm_backend"] = llm_cfg
                set_user_backend_override(
                    self._web_admin_profile_or_default(),
                    "codex",
                    manual=True,
                    reason="web_config",
                )
                applied["actor_backend"] = {"target": "__admin__", "backend": "codex"}
                if codex_model:
                    applied["codex_model"] = codex_model
            if backend_profile_update is not None:
                from common.llm_backend_router import BACKEND_GROK, get_llm_backend_config

                llm_cfg = get_llm_backend_config()
                backend = self._backend_profile_id(backend_profile_update.get("backend"))
                if not backend:
                    return json.dumps({"status": "error", "message": "unsupported backend"})
                providers_cfg = llm_cfg.setdefault("providers", {})
                provider_cfg = providers_cfg.setdefault(backend, {})
                for src_key, dst_key in (
                    ("label", "label"),
                    ("model", "model"),
                    ("api_base", "api_base"),
                    ("wire_api", "wire_api"),
                    ("auth", "auth"),
                ):
                    value = backend_profile_update.get(src_key)
                    if value is not None:
                        provider_cfg[dst_key] = str(value).strip()
                local_config["llm_backend"] = llm_cfg
                file_cfg["llm_backend"] = llm_cfg
                applied["llm_backend_provider"] = backend
                if backend == BACKEND_GROK:
                    model = str(provider_cfg.get("model") or "").strip()
                    api_base = str(provider_cfg.get("api_base") or "").strip()
                    wire_api = str(provider_cfg.get("wire_api") or "").strip()
                    if model:
                        local_config["grok_model"] = model
                        file_cfg["grok_model"] = model
                    if api_base:
                        local_config["grok_api_base"] = api_base
                        file_cfg["grok_api_base"] = api_base
                    if wire_api:
                        local_config["grok_wire_api"] = wire_api
                        file_cfg["grok_wire_api"] = wire_api
            if actor_backend_update is not None:
                from common.llm_backend_router import set_user_backend_override

                target_profile = self._backend_target_profile(actor_backend_update.get("target"))
                if target_profile is None:
                    return json.dumps({"status": "error", "message": "backend target is required"})
                set_user_backend_override(
                    target_profile,
                    actor_backend_update.get("backend"),
                    manual=True,
                    reason="web_config",
                )
                applied["actor_backend"] = {
                    "target": actor_backend_update.get("target"),
                    "backend": actor_backend_update.get("backend"),
                }
            file_cfg.update(applied)
            file_cfg.pop("llm_backend_current", None)
            file_cfg.pop("codex_model", None)
            file_cfg.pop("llm_backend_provider", None)
            file_cfg.pop("actor_backend", None)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(file_cfg, f, indent=4, ensure_ascii=False)

            logger.info(f"[WebChannel] Config updated: {list(applied.keys())}")

            # Reset Bridge so that bot routing reflects the new config.
            # Without this, Bridge keeps its cached bot instance (e.g. LinkAIBot)
            # even after the user switches bot_type / use_linkai / model in UI.
            bridge_routing_keys = {"bot_type", "use_linkai", "model", "llm_backend_current", "llm_backend_provider", "actor_backend"}
            if any(k in applied for k in bridge_routing_keys):
                try:
                    from bridge.bridge import Bridge
                    Bridge().reset_bot()
                    logger.info("[WebChannel] Bridge bot routing reset due to config change")
                except Exception as reset_err:
                    logger.warning(f"[WebChannel] Failed to reset bridge: {reset_err}")

            from common.llm_backend_router import status_snapshot

            return json.dumps(
                {"status": "success", "applied": applied, "llm_backend": status_snapshot(self._web_admin_profile_or_default())},
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error(f"Error updating config: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class ChannelsHandler:
    """API for managing external channel configurations (feishu, dingtalk, etc)."""

    CHANNEL_DEFS = OrderedDict([
        ("weixin", {
            "label": {"zh": "微信", "en": "WeChat"},
            "icon": "fa-comment",
            "color": "emerald",
            "fields": [],
        }),
        ("feishu", {
            "label": {"zh": "飞书", "en": "Feishu"},
            "icon": "fa-paper-plane",
            "color": "blue",
            "fields": [
                {"key": "feishu_app_id", "label": "App ID", "type": "text"},
                {"key": "feishu_app_secret", "label": "App Secret", "type": "secret"},
            ],
        }),
        ("dingtalk", {
            "label": {"zh": "钉钉", "en": "DingTalk"},
            "icon": "fa-comments",
            "color": "blue",
            "fields": [
                {"key": "dingtalk_client_id", "label": "Client ID", "type": "text"},
                {"key": "dingtalk_client_secret", "label": "Client Secret", "type": "secret"},
            ],
        }),
        ("wecom_bot", {
            "label": {"zh": "企微智能机器人", "en": "WeCom Bot"},
            "icon": "fa-robot",
            "color": "emerald",
            "fields": [
                {"key": "wecom_bot_id", "label": "Bot ID", "type": "text"},
                {"key": "wecom_bot_secret", "label": "Secret", "type": "secret"},
                {"key": "wecom_bot_auth_source", "label": "Auth Source", "type": "text", "default": "cowagent"},
            ],
        }),
        ("discord", {
            "label": {"zh": "Discord", "en": "Discord"},
            "icon": "fa-comments",
            "color": "blue",
            "fields": [
                {"key": "discord_bot_token", "label": "Bot Token", "type": "secret"},
                {"key": "discord_guild_id", "label": "Guild ID", "type": "text"},
                {"key": "discord_admin_user_id", "label": "Admin User ID", "type": "text"},
                {"key": "discord_allowed_channel_ids", "label": "Allowed Channel IDs", "type": "text"},
                {"key": "discord_proxy", "label": "Proxy URL", "type": "text"},
            ],
        }),
        ("qq", {
            "label": {"zh": "QQ 机器人", "en": "QQ Bot"},
            "icon": "fa-comment",
            "color": "blue",
            "fields": [
                {"key": "qq_app_id", "label": "App ID", "type": "text"},
                {"key": "qq_app_secret", "label": "App Secret", "type": "secret"},
            ],
        }),
        ("wechatcom_app", {
            "label": {"zh": "企微自建应用", "en": "WeCom App"},
            "icon": "fa-building",
            "color": "emerald",
            "fields": [
                {"key": "wechatcom_corp_id", "label": "Corp ID", "type": "text"},
                {"key": "wechatcomapp_agent_id", "label": "Agent ID", "type": "text"},
                {"key": "wechatcomapp_secret", "label": "Secret", "type": "secret"},
                {"key": "wechatcomapp_token", "label": "Token", "type": "secret"},
                {"key": "wechatcomapp_aes_key", "label": "AES Key", "type": "secret"},
                {"key": "wechatcomapp_port", "label": "Port", "type": "number", "default": 9898},
            ],
        }),
        ("wechatmp", {
            "label": {"zh": "公众号", "en": "WeChat MP"},
            "icon": "fa-comment-dots",
            "color": "emerald",
            "fields": [
                {"key": "wechatmp_app_id", "label": "App ID", "type": "text"},
                {"key": "wechatmp_app_secret", "label": "App Secret", "type": "secret"},
                {"key": "wechatmp_token", "label": "Token", "type": "secret"},
                {"key": "wechatmp_aes_key", "label": "AES Key", "type": "secret"},
                {"key": "wechatmp_port", "label": "Port", "type": "number", "default": 8080},
            ],
        }),
    ])

    @staticmethod
    def _is_weixin_instance(channel_name: str) -> bool:
        channel_name = str(channel_name or "")
        return channel_name == "weixin" or channel_name.startswith("weixin_")

    @staticmethod
    def _get_channel_manager():
        try:
            import sys
            app_module = sys.modules.get('__main__') or sys.modules.get('app')
            return getattr(app_module, '_channel_mgr', None) if app_module else None
        except Exception:
            return None

    @classmethod
    def _get_running_weixin_channel(cls, instance_id: str = "weixin"):
        mgr = cls._get_channel_manager()
        if not mgr:
            return None
        try:
            return mgr.get_channel(instance_id)
        except Exception:
            return None

    @staticmethod
    def _get_weixin_login_status(instance_id: str = "weixin") -> str:
        try:
            ch = ChannelsHandler._get_running_weixin_channel(instance_id)
            if ch and hasattr(ch, 'login_status'):
                return ch.login_status
        except Exception:
            pass
        return "unknown"

    @staticmethod
    def _normalize_role(value) -> str:
        return normalize_weixin_role(value)

    @classmethod
    def _configured_admin_actor_ids(cls) -> set:
        try:
            from config import global_config
        except Exception:
            global_config = {"admin_users": []}

        admin_users = conf().get("agent_admin_users", []) or []
        if isinstance(admin_users, str):
            values = [item.strip() for item in admin_users.split(",") if item.strip()]
        else:
            values = [str(item).strip() for item in admin_users if str(item).strip()]
        values.extend(
            str(item).strip()
            for item in (global_config.get("admin_users", []) or [])
            if str(item).strip()
        )

        profiles = conf().get("agent_user_profiles", {}) or {}
        if isinstance(profiles, dict):
            for actor_id, profile in profiles.items():
                if isinstance(profile, dict) and cls._normalize_role(profile.get("role")) == "admin":
                    values.append(str(actor_id).strip())
        weixin_channel = conf().get("weixin_channel", {}) or {}
        if isinstance(weixin_channel, dict) and cls._normalize_role(weixin_channel.get("role")) == "admin":
            raw_user_id = str(weixin_channel.get("user_id") or "").strip()
            values.append(cls._weixin_actor_id("weixin", raw_user_id) or "weixin")
        instances = conf().get("weixin_instances", {}) or {}
        if isinstance(instances, dict):
            for instance_id, instance_config in instances.items():
                if not isinstance(instance_config, dict):
                    continue
                if cls._normalize_role(instance_config.get("role")) != "admin":
                    continue
                raw_user_id = str(instance_config.get("user_id") or "").strip()
                values.append(cls._weixin_actor_id(str(instance_id), raw_user_id) or str(instance_id))
        return {value for value in values if value}

    @classmethod
    def _requested_weixin_role(cls, value, actor_id: str = "") -> str:
        role = cls._normalize_role(value)
        admin_actors = cls._configured_admin_actor_ids()
        actor_candidates = {actor_id} if actor_id else set()
        if actor_id and ":" in actor_id:
            actor_candidates.add(actor_id.split(":", 1)[1])
        admin_candidates = set(admin_actors)
        for admin_actor in admin_actors:
            if ":" in admin_actor:
                admin_candidates.add(admin_actor.split(":", 1)[1])
        if role == "admin" and admin_actors and actor_candidates.isdisjoint(admin_candidates):
            return "user"
        return role

    @classmethod
    def _connect_weixin_role(cls, channel_name: str, updates: dict) -> str:
        existing_role = cls._normalize_role(cls._weixin_instance_config(channel_name).get("role"))
        requested = (updates or {}).get("role")
        if existing_role == "admin":
            return "admin"
        if requested:
            return cls._requested_weixin_role(requested)
        return existing_role

    @staticmethod
    def _weixin_actor_id(instance_id: str, raw_user_id: str) -> str:
        return f"{instance_id}:{raw_user_id}" if instance_id and raw_user_id else ""

    @staticmethod
    def _upsert_agent_user_profile(
        *,
        actor_id: str,
        raw_user_id: str,
        role: str,
        wechat_id: str = "",
        channel_type: str = "weixin",
    ) -> dict:
        if not actor_id:
            return {}

        try:
            from agent.user_profiles import safe_actor_slug
        except Exception:
            safe_actor_slug = lambda value: str(value or "").replace(":", "_")

        profiles = conf().get("agent_user_profiles", {}) or {}
        if not isinstance(profiles, dict):
            profiles = {}
        profiles = dict(profiles)
        profile = dict(profiles.get(actor_id, {}) or {})

        defaults = {
            "role": ChannelsHandler._normalize_role(role),
            "raw_user_id": raw_user_id,
            "raw_weixin_user_id": raw_user_id,
            "platform": channel_type or "weixin",
            "memory_user_id": profile.get("memory_user_id") or safe_actor_slug(actor_id),
        }
        if wechat_id:
            defaults["wechat_id"] = wechat_id
            defaults.setdefault("display_name", profile.get("display_name") or wechat_id)

        for key, value in defaults.items():
            if value:
                profile[key] = value
        profiles[actor_id] = profile
        conf()["agent_user_profiles"] = profiles
        return profiles

    @classmethod
    def _role_options(cls) -> dict:
        admin_actors = sorted(cls._configured_admin_actor_ids())
        return {
            "admin_available": not bool(admin_actors),
            "admin_actor_id": admin_actors[0] if admin_actors else "",
            "default_role": "user" if admin_actors else "admin",
        }

    @classmethod
    def _channel_def(cls, channel_name: str) -> dict:
        if channel_name in cls.CHANNEL_DEFS:
            return cls.CHANNEL_DEFS[channel_name]
        if cls._is_weixin_instance(channel_name):
            return cls.CHANNEL_DEFS["weixin"]
        return {}

    @classmethod
    def _weixin_instance_names(cls) -> list:
        names = {"weixin"}
        for channel_name in cls._active_channel_set():
            if cls._is_weixin_instance(channel_name):
                names.add(channel_name)
        instances = conf().get("weixin_instances", {}) or {}
        if isinstance(instances, dict):
            for instance_id in instances:
                if cls._is_weixin_instance(instance_id):
                    names.add(instance_id)
        return sorted(names, key=lambda name: (name != "weixin", name))

    @classmethod
    def _weixin_instance_config(cls, instance_id: str) -> dict:
        if instance_id == "weixin":
            value = conf().get("weixin_channel", {}) or {}
            return value if isinstance(value, dict) else {}
        instances = conf().get("weixin_instances", {}) or {}
        if not isinstance(instances, dict):
            return {}
        value = instances.get(instance_id, {}) or {}
        return value if isinstance(value, dict) else {}

    @classmethod
    def _weixin_raw_credentials_path(cls, instance_id: str) -> str:
        if instance_id == "weixin":
            return conf().get("weixin_credentials_path", "~/.weixin_cow_credentials.json")
        inst_conf = cls._weixin_instance_config(instance_id)
        if inst_conf.get("credentials_path"):
            return inst_conf["credentials_path"]
        suffix = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in instance_id)
        return f"~/.weixin_cow_credentials_{suffix}.json"

    @classmethod
    def _read_weixin_credentials(cls, instance_id: str) -> dict:
        path = os.path.expanduser(cls._weixin_raw_credentials_path(instance_id))
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.debug(f"[WebChannel] Failed to read Weixin credentials for {instance_id}: {e}")
        return {}

    @classmethod
    def _weixin_identity(cls, instance_id: str) -> dict:
        inst_conf = cls._weixin_instance_config(instance_id)
        creds = cls._read_weixin_credentials(instance_id)
        raw_user_id = str(inst_conf.get("user_id") or creds.get("user_id") or "").strip()
        wechat_id = str(inst_conf.get("wechat_id") or creds.get("wechat_id") or "").strip()
        if not wechat_id:
            wechat_id = extract_real_wechat_id(inst_conf) or extract_real_wechat_id(creds)
        if not wechat_id:
            wechat_id = cls._resolve_weixin_identity_from_runtime(instance_id)
            if wechat_id:
                creds = cls._read_weixin_credentials(instance_id)
                raw_user_id = str(inst_conf.get("user_id") or creds.get("user_id") or "").strip()
        display_id = wechat_id or cls._configured_weixin_display_id(instance_id, raw_user_id)
        role = weixin_role_for_identity(
            channel_type=instance_id,
            raw_user_id=raw_user_id,
            wechat_id=wechat_id,
            configured_role=inst_conf.get("role", ""),
        )
        return {
            "raw_user_id": raw_user_id,
            "wechat_id": wechat_id,
            "display_wechat_id": display_id,
            "role": role,
            "credentials_path": cls._weixin_raw_credentials_path(instance_id),
        }

    @classmethod
    def _resolve_weixin_identity_from_runtime(cls, instance_id: str) -> str:
        ch = cls._get_running_weixin_channel(instance_id)
        if ch is None or not hasattr(ch, "_resolve_login_wechat_id_from_credentials"):
            return ""
        try:
            return str(ch._resolve_login_wechat_id_from_credentials() or "").strip()
        except Exception as e:
            logger.debug(f"[WebChannel] Failed to resolve runtime Weixin identity for {instance_id}: {e}")
            return ""

    @classmethod
    def _apply_weixin_identity_to_runtime(cls, instance_id: str, raw_user_id: str, wechat_id: str) -> None:
        ch = cls._get_running_weixin_channel(instance_id)
        if ch is None or not raw_user_id or not wechat_id:
            return
        try:
            if hasattr(ch, "_user_identity_cache"):
                ch._user_identity_cache[raw_user_id] = wechat_id
        except Exception as e:
            logger.debug(f"[WebChannel] Failed to update runtime Weixin identity cache for {instance_id}: {e}")

    @staticmethod
    def _configured_weixin_display_id(instance_id: str, raw_user_id: str) -> str:
        candidates = [candidate for candidate in (
            f"{instance_id}:{raw_user_id}" if instance_id and raw_user_id else "",
            raw_user_id,
        ) if candidate]

        profiles = conf().get("agent_user_profiles", {}) or {}
        if isinstance(profiles, dict):
            for candidate in candidates:
                profile = profiles.get(candidate)
                if not isinstance(profile, dict):
                    continue
                for key in ("wechat_id", "llm_usage_label", "display_name", "name"):
                    value = str(profile.get(key) or "").strip()
                    if value and not looks_internal_weixin_id(value):
                        return value

        labels = conf().get("llm_usage_user_labels", {}) or {}
        if isinstance(labels, dict):
            for candidate in candidates:
                value = str(labels.get(candidate) or "").strip()
                if value and not looks_internal_weixin_id(value):
                    return value

        return "" if looks_internal_weixin_id(raw_user_id) else raw_user_id

    @staticmethod
    def _mask_secret(value: str) -> str:
        if not value or len(value) <= 8:
            return value
        return value[:4] + "*" * (len(value) - 8) + value[-4:]

    @staticmethod
    def _configured_agent_role(actor_id: str, raw_user_id: str) -> str:
        profiles = conf().get("agent_user_profiles", {}) or {}
        if isinstance(profiles, dict):
            for candidate in (actor_id, raw_user_id):
                profile = profiles.get(candidate)
                if isinstance(profile, dict) and str(profile.get("role") or "").strip().lower() == "admin":
                    return "admin"

        admin_users = conf().get("agent_admin_users", []) or []
        if isinstance(admin_users, str):
            admin_users = [item.strip() for item in admin_users.split(",") if item.strip()]
        else:
            admin_users = [str(item).strip() for item in admin_users if str(item).strip()]

        return "admin" if actor_id in admin_users or raw_user_id in admin_users else "user"

    @classmethod
    def _bridge_channel_users(cls, channel_name: str, limit: int = 100) -> list:
        try:
            from agent.social_bridge import get_bridge_store, get_social_bridge_service

            get_social_bridge_service().sync_configured_users()
            store = get_bridge_store()
            list_users = getattr(store, "list_users", None)
            users = list_users(limit=limit) if callable(list_users) else store.list_visible_users("__channel_console__", limit=limit)
        except Exception as e:
            logger.debug(f"[WebChannel] Failed to load bridge users for {channel_name}: {e}")
            return []

        results = []
        for user in users:
            metadata = user.metadata or {}
            if str(metadata.get("channel_type") or "").strip() != channel_name:
                continue
            raw_user_id = str(metadata.get("raw_user_id") or metadata.get("receiver") or "").strip()
            role = cls._configured_agent_role(user.actor_user_id, raw_user_id)
            label = (
                str(metadata.get("public_name") or "").strip()
                or user.display_name
                or raw_user_id
                or user.actor_user_id
            )
            results.append({
                "actor_id": user.actor_user_id,
                "raw_user_id": raw_user_id,
                "display_name": label,
                "role": role,
                "can_active_send": bool(metadata.get("can_active_send")),
                "last_seen_at": user.updated_at,
            })
        return results

    @staticmethod
    def _parse_channel_list(raw) -> list:
        if isinstance(raw, list):
            return [ch.strip() for ch in raw if ch.strip()]
        if isinstance(raw, str):
            return [ch.strip() for ch in raw.split(",") if ch.strip()]
        return []

    @classmethod
    def _active_channel_set(cls) -> set:
        return set(cls._parse_channel_list(conf().get("channel_type", "")))

    @classmethod
    def _build_channel_info(cls, ch_name: str, local_config: dict, active_channels: set) -> dict:
        ch_def = cls._channel_def(ch_name)
        fields_out = []
        for f in ch_def["fields"]:
            raw_val = local_config.get(f["key"], f.get("default", ""))
            if f["type"] == "secret" and raw_val:
                display_val = cls._mask_secret(str(raw_val))
            else:
                display_val = raw_val
            fields_out.append({
                "key": f["key"],
                "label": f["label"],
                "type": f["type"],
                "value": display_val,
                "default": f.get("default", ""),
            })
        ch_info = {
            "name": ch_name,
            "label": ch_def["label"],
            "icon": ch_def["icon"],
            "color": ch_def["color"],
            "active": ch_name in active_channels,
            "fields": fields_out,
        }
        if cls._is_weixin_instance(ch_name):
            ch_info.update(cls._weixin_identity(ch_name))
            if ch_name in active_channels:
                ch_info["login_status"] = cls._get_weixin_login_status(ch_name)
        elif ch_name == "wecom_bot":
            ch_info["connected_users"] = cls._bridge_channel_users(ch_name)
        return ch_info

    @staticmethod
    def _save_config_patch(patch: dict):
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8-sig") as f:
                file_cfg = json.load(f)
        else:
            file_cfg = {}
        file_cfg.update(patch)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)

    @classmethod
    def _save_weixin_credentials_patch(cls, instance_id: str, patch: dict) -> dict:
        path = os.path.expanduser(cls._weixin_raw_credentials_path(instance_id))
        data = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        data.update(patch)
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return data

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            local_config = conf()
            active_channels = self._active_channel_set()
            channels = []
            for instance_id in self._weixin_instance_names():
                channels.append(self._build_channel_info(instance_id, local_config, active_channels))
            for ch_name, ch_def in self.CHANNEL_DEFS.items():
                if ch_name == "weixin":
                    continue
                channels.append(self._build_channel_info(ch_name, local_config, active_channels))
            return json.dumps({
                "status": "success",
                "channels": channels,
                "role_options": self._role_options(),
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Channels API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            action = body.get("action")
            channel_name = body.get("channel")

            if not action or not channel_name:
                return json.dumps({"status": "error", "message": "action and channel required"})

            if channel_name not in self.CHANNEL_DEFS and not self._is_weixin_instance(channel_name):
                return json.dumps({"status": "error", "message": f"unknown channel: {channel_name}"})

            if action == "save":
                return self._handle_save(channel_name, body.get("config", {}))
            elif action == "connect":
                return self._handle_connect(channel_name, body.get("config", {}))
            elif action == "disconnect":
                return self._handle_disconnect(channel_name)
            else:
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})
        except Exception as e:
            logger.error(f"[WebChannel] Channels POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def _handle_save(self, channel_name: str, updates: dict):
        if self._is_weixin_instance(channel_name) and "wechat_id" in updates:
            return self._handle_save_weixin_identity(channel_name, updates)

        ch_def = self._channel_def(channel_name)
        valid_keys = {f["key"] for f in ch_def["fields"]}
        secret_keys = {f["key"] for f in ch_def["fields"] if f["type"] == "secret"}

        local_config = conf()
        applied = {}
        for key, value in updates.items():
            if key not in valid_keys:
                continue
            if key in secret_keys:
                if not value or (len(value) > 8 and "*" * 4 in value):
                    continue
            field_def = next((f for f in ch_def["fields"] if f["key"] == key), None)
            if field_def:
                if field_def["type"] == "number":
                    value = int(value)
                elif field_def["type"] == "bool":
                    value = bool(value)
            local_config[key] = value
            applied[key] = value

        if not applied:
            return json.dumps({"status": "error", "message": "no valid fields to update"})

        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
        else:
            file_cfg = {}
        file_cfg.update(applied)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)

        logger.info(f"[WebChannel] Channel '{channel_name}' config updated: {list(applied.keys())}")

        should_restart = False
        active_channels = self._active_channel_set()
        if channel_name in active_channels:
            should_restart = True
            try:
                import sys
                app_module = sys.modules.get('__main__') or sys.modules.get('app')
                mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
                if mgr:
                    threading.Thread(
                        target=mgr.restart,
                        args=(channel_name,),
                        daemon=True,
                    ).start()
                    logger.info(f"[WebChannel] Channel '{channel_name}' restart triggered")
            except Exception as e:
                logger.warning(f"[WebChannel] Failed to restart channel '{channel_name}': {e}")

        return json.dumps({
            "status": "success",
            "applied": list(applied.keys()),
            "restarted": should_restart,
        }, ensure_ascii=False)

    def _handle_save_weixin_identity(self, channel_name: str, updates: dict):
        wechat_id = str(updates.get("wechat_id") or "").strip()
        if not is_real_wechat_id(wechat_id):
            return json.dumps({"status": "error", "message": "invalid wechat_id"}, ensure_ascii=False)

        inst_conf = self._weixin_instance_config(channel_name)
        creds = self._save_weixin_credentials_patch(channel_name, {"wechat_id": wechat_id})
        raw_user_id = str(inst_conf.get("user_id") or creds.get("user_id") or "").strip()
        role = self._requested_weixin_role(
            updates.get("role") or inst_conf.get("role") or "user",
            self._weixin_actor_id(channel_name, raw_user_id),
        )

        if channel_name == "weixin":
            channel_conf = dict(conf().get("weixin_channel", {}) or {})
            channel_conf["wechat_id"] = wechat_id
            channel_conf["role"] = role
            if raw_user_id:
                channel_conf["user_id"] = raw_user_id
            conf()["weixin_channel"] = channel_conf
            self._save_config_patch({"weixin_channel": channel_conf})
        else:
            instances = conf().get("weixin_instances", {}) or {}
            if not isinstance(instances, dict):
                instances = {}
            inst_conf = dict(instances.get(channel_name, {}) or {})
            inst_conf["wechat_id"] = wechat_id
            inst_conf.setdefault("credentials_path", self._weixin_raw_credentials_path(channel_name))
            inst_conf["role"] = role
            if raw_user_id:
                inst_conf["user_id"] = raw_user_id
            instances[channel_name] = inst_conf
            conf()["weixin_instances"] = instances
            self._save_config_patch({"weixin_instances": instances})

        if raw_user_id:
            self._upsert_agent_user_profile(
                actor_id=self._weixin_actor_id(channel_name, raw_user_id),
                raw_user_id=raw_user_id,
                role=role,
                wechat_id=wechat_id,
                channel_type=channel_name,
            )
            remember_wechat_identity(
                channel_type=channel_name,
                raw_user_id=raw_user_id,
                wechat_id=wechat_id,
                role=role,
            )
            self._apply_weixin_identity_to_runtime(channel_name, raw_user_id, wechat_id)

        logger.info(f"[WebChannel] Manual WeChat id saved for instance={channel_name}")

        return json.dumps({
            "status": "success",
            "applied": ["wechat_id"],
            "wechat_id": wechat_id,
            "role": role,
        }, ensure_ascii=False)

    def _handle_connect(self, channel_name: str, updates: dict):
        """Save config fields, add channel to channel_type, and start it."""
        ch_def = self._channel_def(channel_name)
        valid_keys = {f["key"] for f in ch_def["fields"]}
        secret_keys = {f["key"] for f in ch_def["fields"] if f["type"] == "secret"}
        weixin_role = None
        if self._is_weixin_instance(channel_name):
            weixin_role = self._connect_weixin_role(channel_name, updates or {})

        # Feishu connected via web console must use websocket (long connection) mode
        if channel_name == "feishu":
            updates.setdefault("feishu_event_mode", "websocket")
            valid_keys.add("feishu_event_mode")

        local_config = conf()
        applied = {}
        for key, value in updates.items():
            if key not in valid_keys:
                continue
            if key in secret_keys:
                if not value or (len(value) > 8 and "*" * 4 in value):
                    continue
            field_def = next((f for f in ch_def["fields"] if f["key"] == key), None)
            if field_def:
                if field_def["type"] == "number":
                    value = int(value)
                elif field_def["type"] == "bool":
                    value = bool(value)
            local_config[key] = value
            applied[key] = value

        existing = self._parse_channel_list(conf().get("channel_type", ""))
        if channel_name not in existing:
            existing.append(channel_name)
        new_channel_type = ",".join(existing)
        local_config["channel_type"] = new_channel_type

        if self._is_weixin_instance(channel_name) and channel_name != "weixin":
            instances = local_config.get("weixin_instances", {}) or {}
            if not isinstance(instances, dict):
                instances = {}
            inst_conf = dict(instances.get(channel_name, {}) or {})
            inst_conf.setdefault("credentials_path", self._weixin_raw_credentials_path(channel_name))
            inst_conf["role"] = weixin_role or inst_conf.get("role") or "user"
            instances[channel_name] = inst_conf
            local_config["weixin_instances"] = instances
            applied["weixin_instances"] = instances
        elif self._is_weixin_instance(channel_name):
            channel_conf = dict(local_config.get("weixin_channel", {}) or {})
            channel_conf["role"] = weixin_role or channel_conf.get("role") or "user"
            local_config["weixin_channel"] = channel_conf
            applied["weixin_channel"] = channel_conf

        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
        else:
            file_cfg = {}
        file_cfg.update(applied)
        file_cfg["channel_type"] = new_channel_type
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)

        logger.info(f"[WebChannel] Channel '{channel_name}' connecting, channel_type={new_channel_type}")

        def _do_start():
            try:
                import sys
                app_module = sys.modules.get('__main__') or sys.modules.get('app')
                clear_fn = getattr(app_module, '_clear_singleton_cache', None) if app_module else None
                mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
                if mgr is None:
                    logger.warning(f"[WebChannel] ChannelManager not available, cannot start '{channel_name}'")
                    return
                # Stop existing instance first if still running (e.g. re-connect without disconnect)
                existing_ch = mgr.get_channel(channel_name)
                if existing_ch is not None:
                    logger.info(f"[WebChannel] Stopping existing '{channel_name}' before reconnect...")
                    mgr.stop(channel_name)
                # Always wait for the remote service to release the old connection before
                # establishing a new one (DingTalk drops callbacks on duplicate connections)
                logger.info(f"[WebChannel] Waiting for '{channel_name}' old connection to close...")
                time.sleep(5)
                if clear_fn:
                    clear_fn(channel_name)
                logger.info(f"[WebChannel] Starting channel '{channel_name}'...")
                mgr.start([channel_name], first_start=False)
                logger.info(f"[WebChannel] Channel '{channel_name}' start completed")
            except Exception as e:
                logger.error(f"[WebChannel] Failed to start channel '{channel_name}': {e}",
                             exc_info=True)

        threading.Thread(target=_do_start, daemon=True).start()

        return json.dumps({
            "status": "success",
            "channel_type": new_channel_type,
        }, ensure_ascii=False)

    def _handle_disconnect(self, channel_name: str):
        existing = self._parse_channel_list(conf().get("channel_type", ""))
        existing = [ch for ch in existing if ch != channel_name]
        new_channel_type = ",".join(existing)

        local_config = conf()
        local_config["channel_type"] = new_channel_type

        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
        else:
            file_cfg = {}
        file_cfg["channel_type"] = new_channel_type
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)

        def _do_stop():
            try:
                import sys
                app_module = sys.modules.get('__main__') or sys.modules.get('app')
                mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
                clear_fn = getattr(app_module, '_clear_singleton_cache', None) if app_module else None
                if mgr:
                    mgr.stop(channel_name)
                else:
                    logger.warning(f"[WebChannel] ChannelManager not found, cannot stop '{channel_name}'")
                if clear_fn:
                    clear_fn(channel_name)
                logger.info(f"[WebChannel] Channel '{channel_name}' disconnected, "
                            f"channel_type={new_channel_type}")
            except Exception as e:
                logger.warning(f"[WebChannel] Failed to stop channel '{channel_name}': {e}",
                               exc_info=True)

        threading.Thread(target=_do_stop, daemon=True).start()

        return json.dumps({
            "status": "success",
            "channel_type": new_channel_type,
        }, ensure_ascii=False)


class WeixinQrHandler:
    """Handle WeChat QR code login from the web console.

    GET  /api/weixin/qrlogin          → fetch a new QR code
    POST /api/weixin/qrlogin          → poll QR status or start channel after login
    """

    _qr_state = {}
    _qr_poll_threads = {}

    @staticmethod
    def _normalize_instance(instance_id: str) -> str:
        instance_id = str(instance_id or "weixin").strip()
        if instance_id == "wx":
            return "weixin"
        if instance_id == "weixin" or instance_id.startswith("weixin_"):
            return instance_id
        raise ValueError("invalid weixin instance")

    @staticmethod
    def _default_credentials_path(instance_id: str) -> str:
        if instance_id == "weixin":
            return conf().get("weixin_credentials_path", "~/.weixin_cow_credentials.json")
        suffix = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in instance_id)
        return f"~/.weixin_cow_credentials_{suffix}.json"

    @staticmethod
    def _instance_config(instance_id: str) -> dict:
        instances = conf().get("weixin_instances", {}) or {}
        if not isinstance(instances, dict):
            return {}
        value = instances.get(instance_id, {}) or {}
        return value if isinstance(value, dict) else {}

    @classmethod
    def _raw_credentials_path(cls, instance_id: str) -> str:
        return cls._instance_config(instance_id).get(
            "credentials_path",
            cls._default_credentials_path(instance_id),
        )

    @classmethod
    def _credentials_path(cls, instance_id: str) -> str:
        return os.path.expanduser(cls._raw_credentials_path(instance_id))

    @classmethod
    def _base_url(cls, instance_id: str) -> str:
        return cls._instance_config(instance_id).get(
            "base_url",
            conf().get("weixin_base_url", "https://ilinkai.weixin.qq.com"),
        )

    @staticmethod
    def _channel_list(raw) -> list:
        if isinstance(raw, list):
            return [str(ch).strip() for ch in raw if str(ch).strip()]
        if isinstance(raw, str):
            return [ch.strip() for ch in raw.split(",") if ch.strip()]
        return []

    @staticmethod
    def _save_config_patch(patch: dict):
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8-sig") as f:
                file_cfg = json.load(f)
        else:
            file_cfg = {}
        file_cfg.update(patch)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)

    @staticmethod
    def _get_manager():
        try:
            import sys
            app_module = sys.modules.get('__main__') or sys.modules.get('app')
            return getattr(app_module, '_channel_mgr', None) if app_module else None
        except Exception:
            return None

    @classmethod
    def _start_background_poll(cls, instance_id: str):
        current = cls._qr_poll_threads.get(instance_id)
        if current and current.is_alive():
            return

        def _worker():
            deadline = time.time() + 130
            handler = cls()
            try:
                while time.time() < deadline:
                    time.sleep(2)
                    if instance_id not in cls._qr_state:
                        break
                    try:
                        data = json.loads(handler._poll_status(instance_id))
                    except Exception as e:
                        logger.warning(f"[WebChannel] WeixinQr background poll error: {e}")
                        continue
                    if data.get("qr_status") in ("confirmed", "expired"):
                        break
            finally:
                cls._qr_poll_threads.pop(instance_id, None)

        thread = threading.Thread(target=_worker, daemon=True)
        cls._qr_poll_threads[instance_id] = thread
        thread.start()

    @staticmethod
    def _qr_to_data_uri(data: str) -> str:
        """Generate a QR code as a PNG data URI."""
        try:
            import qrcode as qr_lib
            import io
            import base64
            qr = qr_lib.QRCode(error_correction=qr_lib.constants.ERROR_CORRECT_L, box_size=6, border=2)
            qr.add_data(data)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/png;base64,{b64}"
        except ImportError:
            return ""

    @staticmethod
    def _get_running_channel(instance_id: str = "weixin"):
        mgr = WeixinQrHandler._get_manager()
        if mgr:
            return mgr.get_channel(instance_id)
        return None

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(instance="weixin", role="")
            instance_id = self._normalize_instance(params.instance)
            requested_role = ChannelsHandler._requested_weixin_role(
                params.role or self._instance_config(instance_id).get("role") or "user",
            )
            running_ch = self._get_running_channel(instance_id)
            if running_ch and hasattr(running_ch, '_current_qr_url') and running_ch._current_qr_url:
                qr_image = self._qr_to_data_uri(running_ch._current_qr_url)
                return json.dumps({
                    "status": "success",
                    "instance": instance_id,
                    "qrcode_url": running_ch._current_qr_url,
                    "qr_image": qr_image,
                    "source": "channel",
                    "role": requested_role,
                })

            from channel.weixin.weixin_api import WeixinApi, DEFAULT_BASE_URL
            base_url = self._base_url(instance_id) or DEFAULT_BASE_URL
            api = WeixinApi(base_url=base_url)
            qr_resp = api.fetch_qr_code()
            qrcode = qr_resp.get("qrcode", "")
            qrcode_url = qr_resp.get("qrcode_img_content", "")
            if not qrcode:
                return json.dumps({"status": "error", "message": "No QR code returned"})
            qr_image = self._qr_to_data_uri(qrcode_url)
            WeixinQrHandler._qr_state[instance_id] = {
                "qrcode": qrcode,
                "qrcode_url": qrcode_url,
                "base_url": base_url,
                "instance": instance_id,
                "role": requested_role,
            }
            self._start_background_poll(instance_id)
            return json.dumps({
                "status": "success",
                "instance": instance_id,
                "qrcode_url": qrcode_url,
                "qr_image": qr_image,
                "role": requested_role,
            })
        except Exception as e:
            logger.error(f"[WebChannel] WeixinQr GET error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            action = body.get("action", "poll")
            params = web.input(instance="")
            instance_id = self._normalize_instance(body.get("instance") or params.instance or "weixin")

            if action == "poll":
                return self._poll_status(instance_id)
            elif action == "refresh":
                return self.GET()
            else:
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})
        except Exception as e:
            logger.error(f"[WebChannel] WeixinQr POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def _poll_status(self, instance_id: str = "weixin"):
        state = WeixinQrHandler._qr_state.get(instance_id, {})
        qrcode = state.get("qrcode", "")
        base_url = state.get("base_url", "")
        if not qrcode:
            return json.dumps({"status": "error", "message": "No active QR session"})

        from channel.weixin.weixin_api import WeixinApi, DEFAULT_BASE_URL
        api = WeixinApi(base_url=base_url or DEFAULT_BASE_URL)
        try:
            status_resp = api.poll_qr_status(qrcode, timeout=10)
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})

        qr_status = status_resp.get("status", "wait")

        if qr_status == "confirmed":
            bot_token = status_resp.get("bot_token", "")
            bot_id = status_resp.get("ilink_bot_id", "")
            result_base_url = status_resp.get("baseurl", base_url)
            user_id = status_resp.get("ilink_user_id", "")
            wechat_id = extract_real_wechat_id(status_resp)
            role = ChannelsHandler._requested_weixin_role(
                state.get("role") or self._instance_config(instance_id).get("role") or "user",
                ChannelsHandler._weixin_actor_id(instance_id, user_id),
            )

            if not bot_token or not bot_id:
                return json.dumps({"status": "error", "message": "Login confirmed but missing token"})

            cred_path = self._credentials_path(instance_id)
            from channel.weixin.weixin_channel import _save_credentials
            credentials = {
                "token": bot_token,
                "base_url": result_base_url,
                "bot_id": bot_id,
                "user_id": user_id,
                "role": role,
            }
            if wechat_id:
                credentials["wechat_id"] = wechat_id
            _save_credentials(cred_path, credentials)

            profiles = {}
            if user_id:
                profiles = ChannelsHandler._upsert_agent_user_profile(
                    actor_id=ChannelsHandler._weixin_actor_id(instance_id, user_id),
                    raw_user_id=user_id,
                    role=role,
                    wechat_id=wechat_id,
                    channel_type=instance_id,
                )
                if wechat_id:
                    remember_wechat_identity(
                        channel_type=instance_id,
                        raw_user_id=user_id,
                        wechat_id=wechat_id,
                        role=role,
                    )

            if instance_id == "weixin":
                conf()["weixin_token"] = bot_token
                conf()["weixin_base_url"] = result_base_url
                channel_conf = dict(conf().get("weixin_channel", {}) or {})
                channel_conf["role"] = role
                channel_conf["user_id"] = user_id
                if wechat_id:
                    channel_conf["wechat_id"] = wechat_id
                conf()["weixin_channel"] = channel_conf
                self._save_config_patch({
                    "weixin_channel": channel_conf,
                    "agent_user_profiles": profiles,
                    "weixin_token": bot_token,
                    "weixin_base_url": result_base_url,
                })
            else:
                instances = conf().get("weixin_instances", {}) or {}
                if not isinstance(instances, dict):
                    instances = {}
                inst_conf = dict(instances.get(instance_id, {}) or {})
                inst_conf["credentials_path"] = self._raw_credentials_path(instance_id)
                inst_conf["base_url"] = result_base_url
                inst_conf["role"] = role
                inst_conf["user_id"] = user_id
                if wechat_id:
                    inst_conf["wechat_id"] = wechat_id
                instances[instance_id] = inst_conf
                conf()["weixin_instances"] = instances

                channels = self._channel_list(conf().get("channel_type", ""))
                if instance_id not in channels:
                    channels.append(instance_id)
                conf()["channel_type"] = ",".join(channels)
                self._save_config_patch({
                    "weixin_instances": instances,
                    "channel_type": conf()["channel_type"],
                    "agent_user_profiles": profiles,
                })

                mgr = self._get_manager()
                if mgr:
                    threading.Thread(target=mgr.add_channel, args=(instance_id,), daemon=True).start()

            WeixinQrHandler._qr_state.pop(instance_id, None)
            logger.info(f"[WebChannel] WeChat QR login confirmed: instance={instance_id} bot_id={bot_id}")

            return json.dumps({
                "status": "success",
                "instance": instance_id,
                "qr_status": "confirmed",
                "bot_id": bot_id,
                "role": role,
            })

        if qr_status == "expired":
            new_resp = api.fetch_qr_code()
            new_qrcode = new_resp.get("qrcode", "")
            new_qrcode_url = new_resp.get("qrcode_img_content", "")
            new_qr_image = self._qr_to_data_uri(new_qrcode_url)
            WeixinQrHandler._qr_state.setdefault(instance_id, {})
            WeixinQrHandler._qr_state[instance_id]["qrcode"] = new_qrcode
            WeixinQrHandler._qr_state[instance_id]["qrcode_url"] = new_qrcode_url
            WeixinQrHandler._qr_state[instance_id]["role"] = state.get("role") or "user"
            return json.dumps({
                "status": "success",
                "instance": instance_id,
                "qr_status": "expired",
                "qrcode_url": new_qrcode_url,
                "qr_image": new_qr_image,
                "role": WeixinQrHandler._qr_state[instance_id]["role"],
            })

        return json.dumps({"status": "success", "instance": instance_id, "qr_status": qr_status})


class FeishuRegisterHandler:
    """飞书智能体应用一键创建（OAuth 设备授权流，基于 lark.register_app SDK）。

    GET  /api/feishu/register   → 启动注册：调用 SDK 生成二维码 URL，立即返回；
                                   后台线程继续轮询飞书侧直到用户扫码授权。
    POST /api/feishu/register   → 轮询当前会话状态（pending / done / error / expired）。
                                   注册成功后不直接写 config，由前端再调
                                   /api/channels {action:'connect'} 走标准启用流程。
    """

    # 进程内单例状态（{url, expire_in, status, app_id, app_secret, error, thread}）。
    # 简单的本地自部署场景下不需要 session 隔离。
    _state = {}
    _lock = threading.Lock()

    @staticmethod
    def _qr_to_data_uri(data: str) -> str:
        """复用 WeixinQrHandler 的二维码渲染。"""
        return WeixinQrHandler._qr_to_data_uri(data)

    @classmethod
    def _reset_state(cls):
        with cls._lock:
            cls._state = {}

    @classmethod
    def _start_register_thread(cls):
        """启动一次新的注册会话。如已有进行中的会话，先取消（通过 cancel_event）。"""
        # 先取消可能存在的上一次会话，避免两个 SDK 线程并发 poll 同一个端点
        with cls._lock:
            old_cancel = cls._state.get("cancel_event") if cls._state else None
            if old_cancel is not None:
                old_cancel.set()
            cancel_event = threading.Event()
            cls._state = {"status": "starting", "cancel_event": cancel_event}

        def _worker():
            try:
                import lark_oapi as lark
            except ImportError:
                with cls._lock:
                    cls._state["status"] = "error"
                    cls._state["error"] = "lark-oapi SDK 未安装，请执行 pip install -U lark-oapi"
                return

            def _on_qr(info):
                # SDK 拿到二维码 URL 后立即回调；写入 state 让前端 GET 立刻能拿到
                with cls._lock:
                    cls._state["url"] = info.get("url", "")
                    cls._state["expire_in"] = info.get("expire_in", 600)
                    cls._state["qr_image"] = cls._qr_to_data_uri(info.get("url", ""))
                    cls._state["status"] = "pending"
                logger.info(f"[FeishuRegister] QR ready, expire_in={info.get('expire_in')}s")

            def _on_status(info):
                # 过滤掉 polling 心跳（每 5 秒一次，纯噪音）；
                # 保留 slow_down / domain_switched 等真正的状态切换事件
                status = info.get("status")
                if status == "polling":
                    return
                logger.info(f"[FeishuRegister] SDK status: {info}")

            try:
                result = lark.register_app(
                    on_qr_code=_on_qr,
                    on_status_change=_on_status,
                    source="cowagent",
                    cancel_event=cancel_event,
                )
                with cls._lock:
                    cls._state["status"] = "done"
                    cls._state["app_id"] = result.get("client_id", "")
                    cls._state["app_secret"] = result.get("client_secret", "")
                logger.info(f"[FeishuRegister] App created: app_id={result.get('client_id')}")
            except Exception as e:
                err_msg = str(e)
                err_cls = e.__class__.__name__
                # 飞书 SDK 抛出的 AppExpiredError / AppAccessDeniedError / RegisterAppError
                if "Expired" in err_cls:
                    status = "expired"
                elif "Denied" in err_cls:
                    status = "denied"
                elif "abort" in err_msg.lower() or "cancel" in err_msg.lower():
                    # 被新一轮注册抢占，保持安静
                    return
                else:
                    status = "error"
                with cls._lock:
                    # 仅当当前 state 仍属于本次 worker 时才写入，避免覆盖更新的会话
                    if cls._state.get("cancel_event") is cancel_event:
                        cls._state["status"] = status
                        cls._state["error"] = err_msg
                logger.warning(f"[FeishuRegister] Register failed ({err_cls}): {err_msg}")

        threading.Thread(target=_worker, daemon=True, name="feishu-register").start()

    def GET(self):
        """启动一次新的注册会话。如果已有 pending/done 会话则覆盖。"""
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            self._start_register_thread()
            # 等待 SDK 拿到二维码 URL（最多 10s）。SDK 内部会马上回调 _on_qr。
            import time as _t
            for _ in range(100):
                with self._lock:
                    if self._state.get("url") or self._state.get("status") in ("error", "expired", "denied"):
                        break
                _t.sleep(0.1)
            with self._lock:
                if self._state.get("status") in ("error", "expired", "denied"):
                    return json.dumps({
                        "status": "error",
                        "message": self._state.get("error", "register failed"),
                    })
                if not self._state.get("url"):
                    return json.dumps({
                        "status": "error",
                        "message": "等待飞书二维码超时，请重试",
                    })
                return json.dumps({
                    "status": "success",
                    "qrcode_url": self._state["url"],
                    "qr_image": self._state.get("qr_image", ""),
                    "expire_in": self._state.get("expire_in", 600),
                })
        except Exception as e:
            logger.error(f"[WebChannel] FeishuRegister GET error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        """轮询注册结果。"""
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b"{}")
            action = body.get("action", "poll")
            if action != "poll":
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})

            with self._lock:
                status = self._state.get("status", "idle")
                if status == "done":
                    payload = {
                        "status": "success",
                        "register_status": "done",
                        "app_id": self._state.get("app_id", ""),
                        "app_secret": self._state.get("app_secret", ""),
                    }
                    # 一次性返回凭据后清掉，避免敏感信息长期驻留内存
                    self._state = {}
                    return json.dumps(payload)
                if status in ("error", "expired", "denied"):
                    return json.dumps({
                        "status": "success",
                        "register_status": status,
                        "message": self._state.get("error", ""),
                    })
                # pending / starting：还在等用户扫码
                return json.dumps({
                    "status": "success",
                    "register_status": "pending",
                })
        except Exception as e:
            logger.error(f"[WebChannel] FeishuRegister POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


def _get_workspace_root():
    """Resolve the agent workspace directory."""
    from common.utils import expand_path
    return expand_path(conf().get("agent_workspace") or "~/cow")


def _get_project_root():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _get_knowledge_roots():
    """Return knowledge roots visible in the Web console."""

    from common.utils import expand_path

    roots = []
    for root in (_get_workspace_root(),):
        root = os.path.abspath(expand_path(root))
        if root not in roots:
            roots.append(root)
    try:
        from agent.knowledge.backend import KnowledgeBackendConfig

        backend_config = KnowledgeBackendConfig.from_project_config()
        document_root = backend_config.ingest.document_library_root
        if document_root:
            backend_root = os.path.abspath(expand_path(str(document_root)))
            if backend_root not in roots:
                roots.append(backend_root)
    except Exception:
        pass
    return roots


def _merge_knowledge_list_results(results):
    merged = {
        "root_files": [],
        "tree": [],
        "stats": {"pages": 0, "size": 0},
        "enabled": conf().get("knowledge", True),
    }
    root_seen = set()
    tree_by_dir = {}
    for result in results:
        stats = result.get("stats", {}) or {}
        merged["stats"]["pages"] += int(stats.get("pages") or 0)
        merged["stats"]["size"] += int(stats.get("size") or 0)
        for file_info in result.get("root_files", []) or []:
            key = file_info.get("name")
            if key and key not in root_seen:
                root_seen.add(key)
                merged["root_files"].append(file_info)
        for group in result.get("tree", []) or []:
            _merge_knowledge_group(tree_by_dir, group)
    merged["tree"] = list(tree_by_dir.values())
    return merged


def _merge_knowledge_group(group_map, group):
    name = group.get("dir")
    if not name:
        return
    target = group_map.setdefault(name, {"dir": name, "files": [], "children": []})
    seen_files = {item.get("name") for item in target["files"]}
    for file_info in group.get("files", []) or []:
        if file_info.get("name") not in seen_files:
            target["files"].append(file_info)
            seen_files.add(file_info.get("name"))
    child_map = {child.get("dir"): child for child in target["children"]}
    for child in group.get("children", []) or []:
        _merge_knowledge_group(child_map, child)
    target["children"] = list(child_map.values())


def _read_knowledge_file_from_roots(rel_path):
    from agent.knowledge.service import KnowledgeService

    errors = []
    for root in _get_knowledge_roots():
        svc = KnowledgeService(root)
        try:
            return svc.read_file(rel_path)
        except FileNotFoundError as exc:
            errors.append(exc)
            continue
    if errors:
        raise errors[-1]
    raise FileNotFoundError(f"file not found: {rel_path}")


def _merge_knowledge_graphs(graphs):
    nodes = {}
    links = []
    seen_links = set()
    for graph in graphs:
        for node in graph.get("nodes", []) or []:
            node_id = node.get("id")
            if node_id and node_id not in nodes:
                nodes[node_id] = node
        for link in graph.get("links", []) or []:
            key = (link.get("source"), link.get("target"))
            if key[0] and key[1] and key not in seen_links:
                seen_links.add(key)
                links.append(link)
    return {"nodes": list(nodes.values()), "links": links}


class ToolsHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.tools.tool_manager import ToolManager
            tm = ToolManager()
            if not tm.tool_classes:
                tm.load_tools()
            tools = []
            for name, cls in tm.tool_classes.items():
                try:
                    instance = cls()
                    tools.append({
                        "name": name,
                        "description": instance.description,
                    })
                except Exception:
                    tools.append({"name": name, "description": ""})
            return json.dumps({"status": "success", "tools": tools}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Tools API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class CommandsHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            commands = _cow_cli_slash_command_suggestions(is_admin=True)
            return json.dumps({"status": "success", "commands": commands}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Commands API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


def _cow_cli_slash_command_suggestions(is_admin: bool = True) -> list:
    from plugins import PluginManager

    plugin_cls = PluginManager().plugins.get("COW_CLI")
    if plugin_cls is None:
        raise RuntimeError("CowCli plugin is not registered")
    return plugin_cls().slash_command_suggestions(is_admin=is_admin)


class SkillsHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.skills.service import SkillService
            from agent.skills.manager import SkillManager
            workspace_root = _get_workspace_root()
            manager = SkillManager(custom_dir=os.path.join(workspace_root, "skills"))
            service = SkillService(manager)
            skills = service.query()
            return json.dumps({"status": "success", "skills": skills}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Skills API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.skills.service import SkillService
            from agent.skills.manager import SkillManager
            body = json.loads(web.data())
            action = body.get("action")
            name = body.get("name")
            if not action or not name:
                return json.dumps({"status": "error", "message": "action and name are required"})
            workspace_root = _get_workspace_root()
            manager = SkillManager(custom_dir=os.path.join(workspace_root, "skills"))
            service = SkillService(manager)
            if action == "open":
                service.open({"name": name})
            elif action == "close":
                service.close({"name": name})
            else:
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})
            return json.dumps({"status": "success"}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Skills POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class MemoryHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.memory.service import MemoryService
            params = web.input(page='1', page_size='20', category='memory')
            workspace_root = _get_workspace_root()
            profile = _get_web_admin_profile()
            service = MemoryService(
                workspace_root,
                user_id=getattr(profile, "memory_user_id", None),
                include_shared_memory=False,
            )
            result = service.list_files(
                page=int(params.page), page_size=int(params.page_size),
                category=params.category,
            )
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Memory API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class MemoryContentHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.memory.service import MemoryService
            params = web.input(filename='', category='memory')
            if not params.filename:
                return json.dumps({"status": "error", "message": "filename required"})
            workspace_root = _get_workspace_root()
            profile = _get_web_admin_profile()
            service = MemoryService(
                workspace_root,
                user_id=getattr(profile, "memory_user_id", None),
                include_shared_memory=False,
            )
            result = service.get_content(params.filename, category=params.category)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except ValueError:
            return json.dumps({"status": "error", "message": "invalid filename"})
        except FileNotFoundError:
            return json.dumps({"status": "error", "message": "file not found"})
        except Exception as e:
            logger.error(f"[WebChannel] Memory content API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.tools.scheduler.task_store import TaskStore
            workspace_root = _get_workspace_root()
            store_path = os.path.join(workspace_root, "scheduler", "tasks.json")
            store = TaskStore(store_path)
            profile = _get_web_admin_profile()
            tasks = store.list_tasks_for_owner(getattr(profile, "actor_id", ""))
            return json.dumps({"status": "success", "tasks": tasks}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SessionsHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(page='1', page_size='50')
            from agent.memory import get_conversation_store
            store = get_conversation_store()
            result = store.list_sessions(
                channel_type="web",
                page=int(params.page),
                page_size=int(params.page_size),
            )
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Sessions API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SessionDetailHandler:
    def DELETE(self, session_id: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        logger.info(f"[WebChannel] DELETE session request: {session_id}")
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            from agent.memory import get_conversation_store
            store = get_conversation_store()
            store.clear_session(session_id)

            # Also remove the Agent instance from AgentBridge if exists
            try:
                from bridge.bridge import Bridge
                ab = Bridge().get_agent_bridge()
                if session_id in ab.agents:
                    del ab.agents[session_id]
                    logger.info(f"[WebChannel] Removed agent instance for session {session_id}")
            except Exception:
                pass

            channel = WebChannel()
            channel.session_queues.pop(session_id, None)

            logger.info(f"[WebChannel] Session deleted: {session_id}")
            return json.dumps({"status": "success"})
        except Exception as e:
            logger.error(f"[WebChannel] Session delete error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def PUT(self, session_id: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})
            body = json.loads(web.data())
            title = body.get("title", "").strip()
            if not title:
                return json.dumps({"status": "error", "message": "title required"})

            from agent.memory import get_conversation_store
            store = get_conversation_store()
            found = store.rename_session(session_id, title)
            if not found:
                return json.dumps({"status": "error", "message": "session not found"})
            return json.dumps({"status": "success"})
        except Exception as e:
            logger.error(f"[WebChannel] Session rename error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SessionTitleHandler:
    def POST(self, session_id: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            body = json.loads(web.data())
            user_message = body.get("user_message", "")
            assistant_reply = body.get("assistant_reply", "")
            if not user_message:
                return json.dumps({"status": "error", "message": "user_message required"})

            title = _generate_session_title(user_message, assistant_reply)

            from agent.memory import get_conversation_store
            store = get_conversation_store()
            updated = store.rename_session(session_id, title)
            logger.info(f"[WebChannel] Session title set: sid={session_id}, title='{title}', db_updated={updated}")

            return json.dumps({"status": "success", "title": title}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Title generation error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SessionClearContextHandler:
    def POST(self, session_id: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            from agent.memory import get_conversation_store
            store = get_conversation_store()
            new_seq = store.clear_context(session_id)

            # Delete the agent instance so a fresh one is created on the next message
            try:
                from bridge.bridge import Bridge
                bridge = Bridge()
                ab = bridge.get_agent_bridge()
                if session_id in ab.agents:
                    del ab.agents[session_id]
                    logger.info(f"[WebChannel] Cleared agent instance for session {session_id}")
            except Exception:
                pass

            return json.dumps({"status": "success", "context_start_seq": new_seq})
        except Exception as e:
            logger.error(f"[WebChannel] Clear context error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class HistoryHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            params = web.input(session_id='', page='1', page_size='20')
            session_id = params.session_id.strip()
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            from agent.memory import get_conversation_store
            store = get_conversation_store()
            result = store.load_history_page(
                session_id=session_id,
                page=int(params.page),
                page_size=int(params.page_size),
            )
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] History API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class LogsHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'text/event-stream; charset=utf-8')
        web.header('Cache-Control', 'no-cache')
        web.header('X-Accel-Buffering', 'no')

        from config import get_root
        log_path = os.path.join(get_root(), "run.log")

        def generate():
            if not os.path.isfile(log_path):
                yield b"data: {\"type\": \"error\", \"message\": \"run.log not found\"}\n\n"
                return

            # Read last 200 lines for initial display
            try:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
                tail_lines = lines[-200:]
                chunk = ''.join(tail_lines)
                payload = json.dumps({"type": "init", "content": chunk}, ensure_ascii=False)
                yield f"data: {payload}\n\n".encode('utf-8')
            except Exception as e:
                yield f"data: {{\"type\": \"error\", \"message\": \"{e}\"}}\n\n".encode('utf-8')
                return

            # Tail new lines
            try:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    f.seek(0, 2)  # seek to end
                    deadline = time.time() + 600  # 10 min max
                    while time.time() < deadline:
                        line = f.readline()
                        if line:
                            payload = json.dumps({"type": "line", "content": line}, ensure_ascii=False)
                            yield f"data: {payload}\n\n".encode('utf-8')
                        else:
                            yield b": keepalive\n\n"
                            time.sleep(1)
            except GeneratorExit:
                return
            except Exception:
                return

        return generate()


class CacheUsageHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(limit='50')
            from common.llm_usage_tracker import get_cache_usage_report
            report = get_cache_usage_report(limit=int(params.limit or 50))
            return json.dumps({"status": "success", **report}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Cache usage API error: {e}")
            return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)


class AssetsHandler:
    def GET(self, file_path):  # 修改默认参数
        try:
            # 如果请求是/static/，需要处理
            if file_path == '':
                # 返回目录列表...
                pass

            # 获取当前文件的绝对路径
            current_dir = os.path.dirname(os.path.abspath(__file__))
            static_dir = os.path.join(current_dir, 'static')

            full_path = os.path.normpath(os.path.join(static_dir, file_path))

            # 安全检查：确保请求的文件在static目录内
            if not os.path.abspath(full_path).startswith(os.path.abspath(static_dir)):
                logger.error(f"Security check failed for path: {full_path}")
                raise web.notfound()

            if not os.path.exists(full_path) or not os.path.isfile(full_path):
                logger.error(f"File not found: {full_path}")
                raise web.notfound()

            # 设置正确的Content-Type
            content_type = mimetypes.guess_type(full_path)[0]
            if content_type:
                web.header('Content-Type', content_type)
            else:
                # 默认为二进制流
                web.header('Content-Type', 'application/octet-stream')

            # 读取并返回文件内容
            with open(full_path, 'rb') as f:
                return f.read()

        except Exception as e:
            logger.error(f"Error serving static file: {e}", exc_info=True)  # 添加更详细的错误信息
            raise web.notfound()


class KnowledgeListHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.knowledge.service import KnowledgeService
            results = []
            for root in _get_knowledge_roots():
                svc = KnowledgeService(root)
                results.append(svc.list_tree())
            result = _merge_knowledge_list_results(results)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge list error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class KnowledgeReadHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(path='')
            result = _read_knowledge_file_from_roots(params.path)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps({"status": "error", "message": str(e)})
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge read error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class KnowledgeGraphHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.knowledge.service import KnowledgeService
            graphs = []
            for root in _get_knowledge_roots():
                svc = KnowledgeService(root)
                graphs.append(svc.build_graph())
            return json.dumps(_merge_knowledge_graphs(graphs), ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge graph error: {e}")
            return json.dumps({"nodes": [], "links": []})


class VersionHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        from cli import __version__
        return json.dumps({"version": __version__})
