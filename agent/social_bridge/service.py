"""Service layer for cross-user social bridge operations."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import sys
import types
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from bridge.reply import Reply, ReplyType
from common.log import logger
from config import conf
from agent.memory.config import get_default_memory_config

from agent.social_bridge.store import (
    BridgeMessage,
    BridgeStore,
    BridgeUser,
    PendingBridgeMessage,
    get_bridge_store,
)


class ActiveMessageRouter:
    """Route proactive bridge messages to a supported running chat channel."""

    SUPPORTED_WEIXIN_PREFIX = "weixin"
    RUNNING_CHANNEL_ONLY = {"wecom_bot"}

    def send_text(self, target: BridgeUser, text: str) -> Dict[str, Any]:
        metadata = target.metadata or {}
        channel_type = str(metadata.get("channel_type") or "").strip()
        receiver = str(metadata.get("receiver") or metadata.get("raw_user_id") or "").strip()
        context_token = str(metadata.get("context_token") or "").strip()
        is_group = bool(metadata.get("is_group"))

        if not channel_type:
            return {
                "delivered": False,
                "reason": "unsupported_channel",
                "channel_type": channel_type,
            }
        if not receiver:
            return {
                "delivered": False,
                "reason": "unreachable",
                "channel_type": channel_type,
                "receiver": receiver,
            }

        channel = self._get_running_channel(channel_type)
        running_channel = channel is not None
        if channel is None and not self._requires_running_channel(channel_type):
            channel = self._create_standalone_channel(channel_type)
        if channel is None:
            return {
                "delivered": False,
                "reason": "channel_not_running",
                "channel_type": channel_type,
                "receiver": receiver,
            }

        try:
            if hasattr(channel, "active_send_text_result"):
                channel_result = self._call_active_send_text_result(
                    channel,
                    receiver,
                    text,
                    channel_type=channel_type,
                    context_token=context_token,
                    is_group=is_group,
                    running_channel=running_channel,
                )
                send_result = self._normalize_channel_send_result(channel_result)
            elif hasattr(channel, "active_send_text"):
                channel_result = channel.active_send_text(receiver, text, context_token=context_token)
                send_result = self._normalize_channel_send_result(channel_result)
            elif self._is_supported_weixin(channel_type):
                if not context_token:
                    send_result = {
                        "delivered": False,
                        "reason": "needs_fresh_context",
                        "context_token_present": False,
                    }
                else:
                    ok = self._send_text_with_channel_send(channel, receiver, text, context_token)
                    send_result = {"delivered": bool(ok), "reason": "sent" if ok else "send_rejected"}
            else:
                send_result = {
                    "delivered": False,
                    "reason": "unsupported_channel",
                    "channel_type": channel_type,
                }
        except Exception as e:
            logger.warning(f"[SocialBridge] Active send failed: {e}")
            return {
                "delivered": False,
                "reason": "send_error",
                "error": str(e),
                "channel_type": channel_type,
                "receiver": receiver,
            }

        send_result["channel_type"] = channel_type
        send_result["receiver"] = receiver
        if not send_result.get("context_token_present") and context_token:
            send_result["stored_context_token_present"] = True
        return send_result

    @staticmethod
    def _normalize_channel_send_result(result: Any) -> Dict[str, Any]:
        if isinstance(result, dict):
            if "delivered" in result:
                delivered = bool(result.get("delivered"))
            elif "ok" in result:
                delivered = bool(result.get("ok"))
            elif "ret" in result:
                delivered = str(result.get("ret")).strip() == "0"
            else:
                delivered = False

            reason = str(result.get("reason") or "").strip()
            if not reason:
                reason = "sent" if delivered else "send_rejected"
            if reason == "missing_context_token":
                reason = "needs_fresh_context"

            normalized = {
                key: value
                for key, value in result.items()
                if key not in {"ok", "delivered"}
            }
            normalized["delivered"] = delivered
            normalized["reason"] = reason
            return normalized

        delivered = bool(result)
        return {
            "delivered": delivered,
            "reason": "sent" if delivered else "send_rejected",
        }

    @classmethod
    def _requires_running_channel(cls, channel_type: str) -> bool:
        return channel_type in cls.RUNNING_CHANNEL_ONLY

    @classmethod
    def _is_supported_weixin(cls, channel_type: str) -> bool:
        return channel_type == "weixin" or channel_type.startswith("weixin_")

    @classmethod
    def _call_active_send_text_result(
        cls,
        channel,
        receiver: str,
        text: str,
        *,
        channel_type: str,
        context_token: str,
        is_group: bool,
        running_channel: bool,
    ) -> Any:
        if channel_type == "wecom_bot":
            return channel.active_send_text_result(receiver, text, is_group=is_group)

        # Running Weixin channels have the only trustworthy fresh context cache.
        # Persisted context tokens can survive restarts and become stale.
        if cls._is_supported_weixin(channel_type) and not running_channel:
            return channel.active_send_text_result(
                receiver,
                text,
                context_token=context_token,
            )
        return channel.active_send_text_result(receiver, text)

    @staticmethod
    def _get_running_channel(channel_type: str):
        manager = ActiveMessageRouter._get_channel_manager()
        if manager is None:
            return None
        try:
            return manager.get_channel(channel_type)
        except Exception as e:
            logger.debug(f"[SocialBridge] Failed to get channel manager: {e}")
            return None

    @staticmethod
    def _create_standalone_channel(channel_type: str):
        try:
            from channel.channel_factory import create_channel

            return create_channel(channel_type)
        except Exception as e:
            logger.debug(f"[SocialBridge] Failed to create standalone channel '{channel_type}': {e}")
            return None

    @staticmethod
    def _get_channel_manager():
        for module_name in ("app", "__main__"):
            manager = ActiveMessageRouter._get_channel_manager_from_module(module_name)
            if manager is not None:
                return manager
        return None

    @staticmethod
    def _get_channel_manager_from_module(module_name: str):
        module = sys.modules.get(module_name)
        if module is None and module_name == "app":
            try:
                module = importlib.import_module(module_name)
            except Exception as e:
                logger.debug(f"[SocialBridge] Failed to import app module: {e}")
                return None
        if module is None:
            return None

        getter = getattr(module, "get_channel_manager", None)
        if callable(getter):
            try:
                manager = getter()
                if manager is not None:
                    return manager
            except Exception as e:
                logger.debug(f"[SocialBridge] Failed to call {module_name}.get_channel_manager: {e}")
        return getattr(module, "_channel_mgr", None)

    @staticmethod
    def _send_text_with_channel_send(channel, receiver: str, text: str, context_token: str) -> bool:
        from types import SimpleNamespace

        from bridge.context import Context, ContextType

        context = Context(ContextType.TEXT, text)
        context["receiver"] = receiver
        context["session_id"] = receiver
        context["channel_type"] = getattr(channel, "channel_type", "")
        context["isgroup"] = False
        context["msg"] = SimpleNamespace(context_token=context_token)
        channel.send(Reply(ReplyType.TEXT, text), context)
        return True


class SocialBridgeService:
    """Application service used by bridge tools and Weixin registration."""

    def __init__(
        self,
        store: Optional[BridgeStore] = None,
        router: Optional[ActiveMessageRouter] = None,
    ):
        self.store = store or get_bridge_store()
        self.router = router or ActiveMessageRouter()

    def list_users(
        self,
        actor_id: str,
        include_relationships: bool = False,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not self._enabled():
            return {"enabled": False, "users": []}
        max_users = int(limit or conf().get("social_bridge_max_users", 100) or 100)
        users = self.store.list_visible_users(actor_id, limit=max_users)
        visible = [self._public_user(user, viewer_actor_id=actor_id) for user in users]
        if include_relationships:
            for item in visible:
                item["relationship"] = item.get("relationship_to_viewer", "")
        self.store.audit(actor_id, "bridge_list_users", {"count": len(visible)})
        return {"enabled": True, "users": visible}

    def bridge_list_users(self, **kwargs: Any) -> Dict[str, Any]:
        return self.list_users(**kwargs)

    def set_relationship(
        self,
        actor_id: str,
        target_user_id: str,
        relationship: str,
        notes: str = "",
        model: Any = None,
    ) -> Dict[str, Any]:
        text = relationship.strip()
        if notes.strip():
            text = f"{text}: {notes.strip()}"
        target_actor_id = self._resolve_target_actor_id(actor_id, target_user_id, model=model)
        relation = self.store.set_relationship(actor_id, target_actor_id, text)
        target = self.get_user(target_actor_id)
        return {
            "relationship": {
                "target": self._public_user(target, viewer_actor_id=actor_id) if target is not None else None,
                "relation_text": text,
                "updated_at": relation.updated_at,
            }
        }

    def bridge_set_relationship(self, **kwargs: Any) -> Dict[str, Any]:
        return self.set_relationship(**kwargs)

    def send_message(
        self,
        actor_id: str,
        target_user_id: str,
        message: str,
        model: Any = None,
    ) -> Dict[str, Any]:
        if not self._enabled():
            return {"status": "disabled", "delivered": False}

        if not str(message or "").strip():
            raise ValueError("message is required")

        target_actor_id = self._resolve_target_actor_id(actor_id, target_user_id, model=model)
        actor = self.get_user(actor_id)
        target = self.get_user(target_actor_id)
        relationship = self.get_relationship(actor_id, target_actor_id)
        context = self._collect_delivery_context(actor, target, relationship)
        body = self._compose_authorized_delivery(actor_id, target_actor_id, message, model, relationship, context)
        bridge_message = self.store.create_bridge_message(
            actor_id,
            target_actor_id,
            body,
            {"privacy": "explicit_authorization"},
        )

        if not bool(conf().get("social_bridge_auto_send", True)):
            pending = self.store.mark_pending(
                bridge_message.message_id,
                {"delivered": False, "reason": "auto_send_disabled"},
            )
            return self._message_result(pending or bridge_message, delivered=False)

        if target is None:
            pending = self.store.mark_pending(
                bridge_message.message_id,
                {"delivered": False, "reason": "target_not_found"},
            )
            return self._message_result(pending or bridge_message, delivered=False)

        send_result = self.router.send_text(target, body)
        if send_result.get("delivered"):
            updated = self.store.mark_sent(bridge_message.message_id, send_result)
            return self._message_result(updated or bridge_message, delivered=True)

        self._mark_active_send_stale(target, send_result)
        updated = self.store.mark_pending(bridge_message.message_id, send_result)
        return self._message_result(updated or bridge_message, delivered=False)

    def bridge_send_message(self, **kwargs: Any) -> Dict[str, Any]:
        return self.send_message(**kwargs)

    def pending_messages(
        self,
        actor_id: str,
        limit: int = 20,
        mark_seen: bool = False,
        retry_message_id: str = "",
    ) -> Dict[str, Any]:
        retried = None
        if retry_message_id:
            retried = self.retry_pending_message(actor_id, retry_message_id)
        pending = self.store.list_pending_for_actor(actor_id, limit=limit)
        messages = [self._public_pending(item) for item in pending]
        if mark_seen:
            self.store.audit(actor_id, "bridge_pending_seen", {"count": len(messages)})
        result = {"messages": messages}
        if retried is not None:
            result["retry"] = retried
        return result

    def bridge_pending_messages(self, **kwargs: Any) -> Dict[str, Any]:
        return self.pending_messages(**kwargs)

    def retry_pending_for_target(self, actor_id: str, limit: int = 5) -> Dict[str, Any]:
        pending = self.store.list_pending_for_actor(actor_id, limit=limit)
        retried = []
        for item in pending:
            if item.message.target_actor_user_id != actor_id:
                continue
            retried.append(self.retry_pending_message(actor_id, item.message.message_id))
        return {"retried": retried}

    def retry_pending_message(self, actor_id: str, message_id: str) -> Dict[str, Any]:
        getter = getattr(self.store, "get_message", None)
        if not callable(getter):
            return {"message_id": message_id, "delivered": False, "reason": "message_lookup_unavailable"}

        message = getter(message_id)
        if message is None:
            return {"message_id": message_id, "delivered": False, "reason": "message_not_found"}
        if actor_id not in {message.sender_actor_user_id, message.target_actor_user_id}:
            return {"message_id": message_id, "delivered": False, "reason": "not_message_participant"}
        if message.status != "pending":
            return {"message_id": message_id, "delivered": False, "reason": f"not_pending:{message.status}"}

        target = self.get_user(message.target_actor_user_id)
        if target is None:
            updated = self.store.mark_pending(
                message.message_id,
                {"delivered": False, "reason": "target_not_found"},
            )
            return self._message_result(updated or message, delivered=False)

        send_result = self.router.send_text(target, message.body)
        if send_result.get("delivered"):
            updated = self.store.mark_sent(message.message_id, send_result)
            return self._message_result(updated or message, delivered=True)

        self._mark_active_send_stale(target, send_result)
        updated = self.store.mark_pending(message.message_id, send_result)
        return self._message_result(updated or message, delivered=False)

    def register_user(
        self,
        actor_id: str,
        memory_user_id: str,
        display_name: str = "",
        channel_type: str = "",
        raw_user_id: str = "",
        receiver: str = "",
        context_token: str = "",
        can_active_send: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> BridgeUser:
        merged = dict(metadata or {})
        if channel_type:
            merged["channel_type"] = channel_type
        if raw_user_id:
            merged["raw_user_id"] = raw_user_id
        if receiver:
            merged["receiver"] = receiver
        if context_token:
            merged["context_token"] = context_token
        merged["can_active_send"] = bool(can_active_send or context_token)
        return self.store.register_user(
            actor_user_id=actor_id,
            memory_user_id=memory_user_id,
            display_name=display_name,
            metadata=merged,
        )

    def sync_configured_users(self) -> Dict[str, Any]:
        """Backfill bridge directory rows from configured agent/channel profiles."""
        from agent.user_profiles import safe_actor_slug

        synced = []
        profiles = conf().get("agent_user_profiles", {}) or {}
        if not isinstance(profiles, dict):
            profiles = {}

        for actor_id, profile in profiles.items():
            if not isinstance(profile, dict):
                continue
            actor_text = str(actor_id or "").strip()
            if not actor_text or ":" not in actor_text:
                continue
            channel_type, raw_user_id = actor_text.split(":", 1)
            memory_user_id = str(profile.get("memory_user_id") or safe_actor_slug(actor_text)).strip()
            display_name = str(
                profile.get("display_name")
                or profile.get("wechat_id")
                or profile.get("name")
                or raw_user_id
            ).strip()
            metadata = {
                "channel_type": channel_type,
                "platform": profile.get("platform") or channel_type,
                "raw_user_id": str(profile.get("raw_user_id") or profile.get("raw_weixin_user_id") or raw_user_id),
                "receiver": str(profile.get("receiver") or profile.get("raw_user_id") or raw_user_id),
                "public_name": display_name,
                "can_active_send": bool(profile.get("can_active_send", channel_type == "wecom_bot")),
            }
            if profile.get("wechat_id"):
                metadata["wechat_id"] = profile.get("wechat_id")
            try:
                self.store.register_user(
                    actor_user_id=actor_text,
                    memory_user_id=memory_user_id,
                    display_name=display_name,
                    metadata=metadata,
                )
                synced.append(actor_text)
            except Exception as e:
                logger.debug(f"[SocialBridge] Failed to sync configured user {actor_text}: {e}")

        return {"synced": synced, "count": len(synced)}

    def _mark_active_send_stale(
        self,
        target: Optional[BridgeUser],
        send_result: Dict[str, Any],
    ) -> None:
        if target is None or not isinstance(send_result, dict):
            return

        reason = str(send_result.get("reason") or "").strip()
        if reason not in {"needs_fresh_context", "send_rejected", "weixin_send_rejected", "malformed_response"}:
            return

        metadata = dict(target.metadata or {})
        if not metadata.get("context_token") and not metadata.get("can_active_send"):
            return

        metadata["context_token"] = ""
        metadata["can_active_send"] = False
        metadata["active_send_stale_reason"] = reason
        if "ret" in send_result:
            metadata["active_send_stale_ret"] = send_result.get("ret")
        if "errmsg" in send_result:
            metadata["active_send_stale_errmsg"] = send_result.get("errmsg")

        try:
            self.store.register_user(
                actor_user_id=target.actor_user_id,
                memory_user_id=target.memory_user_id,
                display_name=target.display_name,
                metadata=metadata,
            )
        except Exception as e:
            logger.debug(f"[SocialBridge] Failed to mark active send stale: {e}")

    def get_user(self, actor_id: str) -> Optional[BridgeUser]:
        getter = getattr(self.store, "get_user", None)
        if callable(getter):
            return getter(actor_id)

        return None

    def get_relationship(self, actor_id: str, target_user_id: str):
        getter = getattr(self.store, "get_relationship", None)
        if callable(getter):
            return getter(actor_id, target_user_id)
        return None

    def _resolve_target_actor_id(self, actor_id: str, target_user_id: str, model: Any = None) -> str:
        target_ref = str(target_user_id or "").strip()
        if not target_ref:
            raise ValueError("target_user_id is required")

        direct = self.get_user(target_ref)
        if direct is not None:
            if direct.actor_user_id == actor_id:
                raise ValueError("target_user_id must differ from actor_id")
            return direct.actor_user_id

        max_users = int(conf().get("social_bridge_max_users", 100) or 100)
        visible_users: List[tuple[BridgeUser, Dict[str, Any]]] = []
        for user in self.store.list_visible_users(actor_id, limit=max_users):
            public_user = self._public_user(user, viewer_actor_id=actor_id)
            visible_users.append((user, public_user))
            if self._target_ref_matches_public_user(target_ref, public_user):
                return user.actor_user_id

        model_target = self._resolve_target_actor_id_with_model(target_ref, visible_users, model)
        if model_target:
            return model_target

        raise ValueError("target_user_id not found")

    @classmethod
    def _target_ref_matches_public_user(cls, target_ref: str, public_user: Dict[str, Any]) -> bool:
        candidates = {
            public_user.get("bridge_user_id", ""),
            public_user.get("wechat_id", ""),
            public_user.get("nickname", ""),
            public_user.get("display_label", ""),
            public_user.get("relationship_to_viewer", ""),
        }
        candidates.update(cls._relationship_aliases(public_user.get("relationship_to_viewer", "")))
        candidates.update(public_user.get("known_names", []))
        clean_candidates = {str(item or "").strip() for item in candidates if str(item or "").strip()}
        target = str(target_ref or "").strip()
        return target in clean_candidates or target.casefold() in {item.casefold() for item in clean_candidates}

    def _resolve_target_actor_id_with_model(
        self,
        target_ref: str,
        visible_users: List[tuple[BridgeUser, Dict[str, Any]]],
        model: Any = None,
    ) -> str:
        if model is None or not hasattr(model, "call") or not visible_users:
            return ""

        candidates = []
        for index, (_, public_user) in enumerate(visible_users, start=1):
            candidates.append(
                {
                    "index": index,
                    "bridge_user_id": public_user.get("bridge_user_id", ""),
                    "wechat_id": public_user.get("wechat_id", ""),
                    "nickname": public_user.get("nickname", ""),
                    "known_names": public_user.get("known_names", []),
                    "display_label": public_user.get("display_label", ""),
                    "relationship": public_user.get("relationship_to_viewer", ""),
                }
            )

        try:
            from agent.protocol.models import LLMRequest

            request = LLMRequest(
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "请根据用户给出的目标称呼，从候选社交桥用户中选出最可能的接收人。\n"
                            f"目标称呼: {target_ref}\n"
                            f"候选用户 JSON: {json.dumps(candidates, ensure_ascii=False)}\n\n"
                            "只输出 JSON，格式为 {\"bridge_user_id\":\"...\"}。"
                            "如果没有足够把握，输出 {\"bridge_user_id\":\"\"}。"
                        ),
                    }
                ],
                temperature=0,
                max_tokens=120,
                stream=False,
                quota_refresh_silent=True,
                cache_shape_metadata={"request_kind": "social_bridge_target_resolution"},
                system=(
                    "你只做接收人消歧，不编造新用户。只能从候选用户中选择。"
                    "可以利用 nickname、known_names、display_label、relationship 里的关系词，"
                    "例如“我老婆”“我太太”“配偶”可对应 relationship 中的老婆/妻子/配偶。"
                ),
            )
            response = model.call(request)
            selected_ref = self._extract_model_selected_target_ref(self._extract_response_text(response))
        except Exception as e:
            logger.debug(f"[SocialBridge] LLM target resolution failed: {e}")
            return ""

        if not selected_ref:
            return ""

        for user, public_user in visible_users:
            if self._target_ref_matches_public_user(selected_ref, public_user):
                return user.actor_user_id
            if selected_ref.isdigit():
                index = int(selected_ref)
                if 1 <= index <= len(visible_users):
                    return visible_users[index - 1][0].actor_user_id
        return ""

    @staticmethod
    def _extract_model_selected_target_ref(response_text: str) -> str:
        text = str(response_text or "").strip()
        if not text:
            return ""
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                return str(payload.get("bridge_user_id") or payload.get("target") or payload.get("index") or "").strip()
        except json.JSONDecodeError:
            pass
        match = re.search(r"bridge_[A-Fa-f0-9]{16}", text)
        if match:
            return match.group(0)
        if re.fullmatch(r"\d{1,3}", text):
            return text
        if text.casefold() in {"none", "null", "unknown", "no_match", "no match"}:
            return ""
        return text.strip('"\'` \t\r\n')

    @staticmethod
    def _enabled() -> bool:
        return bool(conf().get("social_bridge_enabled", True))

    @staticmethod
    def _compose_authorized_delivery(
        actor_id: str,
        target_user_id: str,
        message: str,
        model: Any = None,
        relationship: Any = None,
        context: Optional[Dict[str, str]] = None,
    ) -> str:
        text = message.strip()
        generated = SocialBridgeService._generate_delivery_text(
            actor_id,
            target_user_id,
            text,
            model,
            relationship,
            context or {},
        )
        if generated:
            return generated
        return (
            "我帮对方带句话，尽量只说这次他想让我带到的部分，不替他多解释。\n\n"
            f"{text}"
        )

    @staticmethod
    def _generate_delivery_text(
        actor_id: str,
        target_user_id: str,
        message: str,
        model: Any = None,
        relationship: Any = None,
        context: Optional[Dict[str, str]] = None,
    ) -> str:
        if model is None or not hasattr(model, "call"):
            return ""

        try:
            from agent.protocol.models import LLMRequest

            relationship_text = getattr(relationship, "relation_text", "") if relationship is not None else ""
            safe_context = context or {}
            request = LLMRequest(
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "请把下面这段发送方明确让我带给接收方的内容，改写成发给接收方的一段自然消息。\n"
                            f"发送方公开代号: {SocialBridgeService._public_user_id(actor_id)}\n"
                            f"接收方公开代号: {SocialBridgeService._public_user_id(target_user_id)}\n"
                            f"双方关系档案（仅可作为措辞参考）: {relationship_text or '未记录'}\n"
                            f"发送方私有记忆摘要（仅供理解这次要带的话，不得外泄）: {safe_context.get('actor_private', '未检索到')}\n"
                            f"接收方私有记忆摘要（仅供理解接收视角，不得提及来源）: {safe_context.get('target_private', '未检索到')}\n"
                            f"关系记忆（双方共同/已允许进入关系上下文）: {safe_context.get('pair_memory', '未检索到')}\n"
                            f"要带的话: {message}\n\n"
                            "输出只包含最终要发送给接收方的正文。正文要像一个懂双方关系的朋友在帮忙递话，"
                            "温和、具体、有人味；不要像公告、客服、法律声明或工具回执。"
                        ),
                    }
                ],
                temperature=0.55,
                max_tokens=700,
                stream=False,
                quota_refresh_silent=True,
                cache_shape_metadata={"request_kind": "social_bridge_rewrite"},
                system=(
                    "你是一个温柔、聪明、懂分寸的朋友式沟通桥梁。只能使用发送方这次明确让你带的话，"
                    "关系记忆中双方共同参与或已允许进入关系上下文的信息，以及接收方自己的视角来调整措辞。"
                    "发送方/接收方私有记忆只能用于理解语气和避免误伤，不得添加成正文事实，不得泄露任何一方"
                    "私下和 Agent 说过的话，也不得声称你读取了记忆。不要假装自己就是发送方；"
                    "可以自然地说“他让我带句话”“我想帮你们把这句话说柔和一点”。"
                    "避免使用“授权我转述”“明确授权”“隐私记忆”“边界”等生硬术语。"
                ),
            )
            response = model.call(request)
            return SocialBridgeService._extract_response_text(response).strip()
        except Exception as e:
            logger.warning(f"[SocialBridge] LLM bridge rewrite failed, using safe fallback: {e}")
            return ""

    @staticmethod
    def _collect_delivery_context(
        actor: Optional[BridgeUser],
        target: Optional[BridgeUser],
        relationship: Any = None,
    ) -> Dict[str, str]:
        workspace = get_default_memory_config().get_workspace()
        actor_private = SocialBridgeService._read_user_memory_excerpt(workspace, actor)
        target_private = SocialBridgeService._read_user_memory_excerpt(workspace, target)
        pair_memory = SocialBridgeService._read_pair_memory_excerpt(workspace, relationship)
        return {
            "actor_private": actor_private or "未检索到",
            "target_private": target_private or "未检索到",
            "pair_memory": pair_memory or "未检索到",
        }

    @staticmethod
    def _read_user_memory_excerpt(workspace: Path, user: Optional[BridgeUser], limit: int = 1200) -> str:
        if user is None:
            return ""
        path = workspace / "memory" / "users" / user.memory_user_id / "MEMORY.md"
        return SocialBridgeService._read_file_tail(path, limit)

    @staticmethod
    def _read_user_identity_excerpt(workspace: Path, user: Optional[BridgeUser], limit: int = 5000) -> str:
        if user is None:
            return ""

        user_dir = workspace / "memory" / "users" / user.memory_user_id
        profile = SocialBridgeService._read_file_tail(user_dir / "USER.md", limit)
        remaining = max(0, limit - len(profile))
        memory = SocialBridgeService._read_file_tail(user_dir / "MEMORY.md", remaining) if remaining else ""
        return "\n".join(part for part in (profile, memory) if part)

    @staticmethod
    def _read_pair_memory_excerpt(workspace: Path, relationship: Any, limit: int = 1600) -> str:
        pair_id = getattr(relationship, "pair_id", "") if relationship is not None else ""
        if not pair_id:
            return ""
        path = workspace / "memory" / "relations" / pair_id / "MEMORY.md"
        return SocialBridgeService._read_file_tail(path, limit)

    @staticmethod
    def _read_file_tail(path: Path, limit: int) -> str:
        try:
            if not path.exists() or not path.is_file():
                return ""
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
            if len(text) <= limit:
                return text
            return text[-limit:]
        except OSError as e:
            logger.debug(f"[SocialBridge] Failed to read context file {path}: {e}")
            return ""

    @staticmethod
    def _extract_response_text(response: Any) -> str:
        if isinstance(response, types.GeneratorType):
            try:
                response = next(response)
            except StopIteration:
                return ""

        if not response:
            return ""

        if isinstance(response, str):
            return response

        if isinstance(response, dict):
            if response.get("error"):
                raise RuntimeError(str(response.get("message") or "LLM call failed"))

            content = response.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        parts.append(str(block.get("text") or block.get("content") or ""))
                    elif isinstance(block, str):
                        parts.append(block)
                return "".join(parts)

            choices = response.get("choices") or []
            if choices:
                first = choices[0]
                if isinstance(first, dict):
                    message = first.get("message") or {}
                    if isinstance(message, dict):
                        return str(message.get("content") or "")
                    return str(first.get("text") or "")

        choices = getattr(response, "choices", None)
        if choices:
            message = getattr(choices[0], "message", None)
            if message is not None:
                return str(getattr(message, "content", "") or "")
            return str(getattr(choices[0], "text", "") or "")

        return ""

    @staticmethod
    def _mask_display_name(name: str) -> str:
        name = str(name or "").strip()
        if len(name) <= 2:
            return name
        if len(name) <= 6:
            return name[0] + "*" * (len(name) - 2) + name[-1]
        return name[:2] + "***" + name[-2:]

    @staticmethod
    def _public_user_id(actor_user_id: str) -> str:
        digest = hashlib.sha256(str(actor_user_id).encode("utf-8")).hexdigest()[:16]
        return f"bridge_{digest}"

    def _public_user(self, user: BridgeUser, viewer_actor_id: str = "") -> Dict[str, Any]:
        metadata = user.metadata or {}
        wechat_id = self._public_wechat_id(user)
        known_names = self._known_public_names(user, wechat_id)
        nickname = known_names[0] if known_names else ""
        relationship_text = self._relationship_text(viewer_actor_id, user.actor_user_id) if viewer_actor_id else ""
        return {
            "bridge_user_id": self._public_user_id(user.actor_user_id),
            "wechat_id": wechat_id,
            "nickname": nickname,
            "known_names": known_names,
            "relationship_to_viewer": relationship_text,
            "display_label": self._display_label(nickname, wechat_id, known_names),
            "channel_type": metadata.get("channel_type", ""),
            "can_active_send": bool(metadata.get("can_active_send")),
            "last_seen_at": user.updated_at,
        }

    def _relationship_text(self, viewer_actor_id: str, target_actor_id: str) -> str:
        relationship = self.get_relationship(viewer_actor_id, target_actor_id)
        return str(getattr(relationship, "relation_text", "") or "").strip()

    @staticmethod
    def _relationship_aliases(relationship_text: str) -> List[str]:
        text = str(relationship_text or "").casefold()
        aliases: List[str] = []
        if any(word in text for word in ("老公", "丈夫", "先生", "husband")):
            aliases.extend(["老公", "丈夫", "先生", "husband"])
        if any(word in text for word in ("老婆", "妻子", "太太", "媳妇", "wife")):
            aliases.extend(["老婆", "妻子", "太太", "媳妇", "wife"])
        if "配偶" in text or "spouse" in text:
            aliases.extend(["配偶", "spouse", "老公", "丈夫", "老婆", "妻子"])
        if any(word in text for word in ("父亲", "爸爸", "father", "dad")):
            aliases.extend(["父亲", "爸爸", "father", "dad"])
        if any(word in text for word in ("母亲", "妈妈", "mother", "mom")):
            aliases.extend(["母亲", "妈妈", "mother", "mom"])
        return aliases

    def _public_wechat_id(self, user: BridgeUser) -> str:
        metadata = user.metadata or {}
        candidates = [
            metadata.get("wechat_id", ""),
            metadata.get("display_wechat_id", ""),
            self._configured_channel_wechat_id(user),
        ]
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text and self._looks_like_public_wechat_id(text):
                return text
        return ""

    def _configured_channel_wechat_id(self, user: BridgeUser) -> str:
        metadata = user.metadata or {}
        channel_type = str(metadata.get("channel_type") or "").strip()
        raw_user_id = str(metadata.get("raw_user_id") or metadata.get("receiver") or "").strip()
        if not channel_type or not raw_user_id:
            return ""

        channel_conf = self._configured_weixin_channel(channel_type)
        configured_raw_id = str(channel_conf.get("user_id") or "").strip()
        if not configured_raw_id:
            return ""
        actor_id = f"{channel_type}:{configured_raw_id}"
        if raw_user_id != configured_raw_id and user.actor_user_id != actor_id:
            return ""
        return str(channel_conf.get("wechat_id") or "").strip()

    @staticmethod
    def _configured_weixin_channel(channel_type: str) -> Dict[str, Any]:
        local_config = conf()
        if channel_type == "weixin":
            value = local_config.get("weixin_channel", {}) or {}
            return value if isinstance(value, dict) else {}
        instances = local_config.get("weixin_instances", {}) or {}
        if not isinstance(instances, dict):
            return {}
        value = instances.get(channel_type, {}) or {}
        return value if isinstance(value, dict) else {}

    def _known_public_names(self, user: BridgeUser, wechat_id: str) -> List[str]:
        metadata = user.metadata or {}
        names: List[str] = []
        for key in ("nickname", "declared_name", "name", "public_name", "display_name"):
            self._append_public_name(names, metadata.get(key, ""), wechat_id)
        if not names:
            for name in self._declared_names_from_memory(user):
                self._append_public_name(names, name, wechat_id)
        return names[:5]

    @classmethod
    def _append_public_name(cls, names: List[str], value: Any, wechat_id: str) -> None:
        name = cls._normalize_public_name(value)
        if not cls._looks_like_public_name(name, wechat_id):
            return
        if name.casefold() not in {item.casefold() for item in names}:
            names.append(name)

    @staticmethod
    def _normalize_public_name(value: Any) -> str:
        return str(value or "").strip(" \t\r\n，。,.：:;；'\"“”「」『』（）()[]【】")

    @staticmethod
    def _looks_like_public_wechat_id(value: str) -> bool:
        text = str(value or "").strip()
        if not text or "@im.wechat" in text:
            return False
        if len(text) > 64 or any(ch.isspace() for ch in text):
            return False
        return bool(re.match(r"^[A-Za-z][A-Za-z0-9_-]{2,}$", text) or re.match(r"^wxid_[A-Za-z0-9_-]+$", text))

    @staticmethod
    def _looks_like_public_name(value: str, wechat_id: str) -> bool:
        text = SocialBridgeService._normalize_public_name(value)
        if not text or text == wechat_id:
            return False
        if text in {"未填写", "待填写", "可选", "在首次对话时询问", "用户希望被如何称呼"}:
            return False
        if "@im.wechat" in text or "://" in text or len(text) > 40:
            return False
        if text.startswith("wxid_"):
            return False
        return bool(re.search(r"[\w\u4e00-\u9fff]", text))

    def _declared_names_from_memory(self, user: BridgeUser) -> List[str]:
        names: List[str] = []
        for path in self._public_user_memory_paths(user.memory_user_id):
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            for name in self._extract_user_declared_names(text):
                if name.casefold() not in {item.casefold() for item in names}:
                    names.append(name)
        return names[:5]

    @staticmethod
    def _public_user_memory_paths(memory_user_id: str) -> List[Path]:
        safe_id = str(memory_user_id or "").strip()
        if not safe_id:
            return []
        workspace = get_default_memory_config().get_workspace()
        return [
            workspace / "users" / safe_id / "files" / "USER.md",
            workspace / "memory" / "users" / safe_id / "USER.md",
            workspace / "memory" / "users" / safe_id / "MEMORY.md",
        ]

    @staticmethod
    def _extract_user_declared_names(memory_text: str) -> List[str]:
        names: List[str] = []
        if not memory_text:
            return names
        patterns = [
            r"(?:用户称呼|用户昵称|用户名字|用户姓名)\s*[:：]\s*[「『\"'“]?([A-Za-z0-9_\-\u4e00-\u9fff]{1,30})",
            r"(?:用户告知|用户表示|用户说)\s*[:：]?\s*(?:自己)?(?:叫|名叫|称呼为)\s*[「『\"'“]?([A-Za-z0-9_\-\u4e00-\u9fff]{1,30})",
            r"(?:用户叫|用户自称)\s*[「『\"'“]?([A-Za-z0-9_\-\u4e00-\u9fff]{1,30})",
            r"(?:我叫|我的名字是|叫我|可以叫我)\s*[「『\"'“]?([A-Za-z0-9_\-\u4e00-\u9fff]{1,30})",
            r"(?:my name is|call me|i am|i'm)\s+([A-Za-z][A-Za-z0-9_\-]{0,29})",
        ]
        for line in memory_text.splitlines():
            if re.search(r"(助手|Agent|agent|机器人|你叫|助手名字|助手在)", line):
                if not re.search(r"用户(?:称呼|昵称|名字|姓名)\s*[:：]", line):
                    continue
            for pattern in patterns:
                for match in re.finditer(pattern, line, flags=re.IGNORECASE):
                    name = SocialBridgeService._normalize_public_name(match.group(1))
                    if name.casefold() not in {item.casefold() for item in names}:
                        names.append(name)
        return names

    @staticmethod
    def _extract_declared_names(memory_text: str) -> List[str]:
        if not memory_text:
            return []
        patterns = [
            r"(?:我叫|我的名字是|我的名字叫|名字就是|叫我|可以叫我)\s*[「“\"']?([A-Za-z0-9_\-\u4e00-\u9fff]{1,30})",
            r"(?:用户希望被称为|用户希望称呼为|用户称呼为|用户称呼|称呼|昵称|姓名|名字)\s*(?:是|为|叫|就是|:|：)?\s*[「“\"']?([A-Za-z0-9_\-\u4e00-\u9fff]{1,30})",
            r"(?:my name is|call me|i am|i'm)\s+([A-Za-z][A-Za-z0-9_\-]{0,29})",
        ]
        names: List[str] = []
        for pattern in patterns:
            for match in re.finditer(pattern, memory_text, flags=re.IGNORECASE):
                name = SocialBridgeService._normalize_public_name(match.group(1))
                if name.casefold() not in {item.casefold() for item in names}:
                    names.append(name)
        return names

    @staticmethod
    def _display_label(nickname: str, wechat_id: str, known_names: List[str]) -> str:
        names = "/".join(known_names[:2])
        public_name = nickname or names
        if public_name and wechat_id:
            return f"{public_name} / {wechat_id}"
        return public_name or wechat_id or "未知微信用户"

    def _public_pending(self, item: PendingBridgeMessage) -> Dict[str, Any]:
        return {
            "message_id": item.message.message_id,
            "from": self._public_user(item.sender),
            "text": item.message.body,
            "created_at": item.message.created_at,
            "relationship": self._json(item.relationship) if item.relationship else None,
            "result": item.message.result,
        }

    def _message_result(self, message: BridgeMessage, delivered: bool) -> Dict[str, Any]:
        return {
            "message_id": message.message_id,
            "status": message.status,
            "delivered": delivered,
            "target_bridge_user_id": self._public_user_id(message.target_actor_user_id),
            "result": message.result,
        }

    @classmethod
    def _json(cls, value: Any) -> Any:
        if value is None:
            return None
        if is_dataclass(value):
            return asdict(value)
        if isinstance(value, dict):
            return {str(k): cls._json(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._json(v) for v in value]
        return value


_service: Optional[SocialBridgeService] = None


def get_social_bridge_service() -> SocialBridgeService:
    global _service
    if _service is None:
        _service = SocialBridgeService()
    return _service


def list_users(**kwargs: Any) -> Dict[str, Any]:
    return get_social_bridge_service().list_users(**kwargs)


def set_relationship(**kwargs: Any) -> Dict[str, Any]:
    return get_social_bridge_service().set_relationship(**kwargs)


def send_message(**kwargs: Any) -> Dict[str, Any]:
    return get_social_bridge_service().send_message(**kwargs)


def pending_messages(**kwargs: Any) -> Dict[str, Any]:
    return get_social_bridge_service().pending_messages(**kwargs)
