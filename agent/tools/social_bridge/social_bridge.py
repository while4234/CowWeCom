from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Optional

from agent.tools.base_tool import BaseTool, ToolResult


SERVICE_UNAVAILABLE = (
    "Social bridge service is not available yet. "
    "Expected a future agent.social_bridge service facade."
)

AUTHORIZATION_PHRASES = {
    "authorize",
    "authorized",
    "i authorize",
    "i authorize sending",
    "confirm",
    "confirmed",
    "yes send",
    "send message",
}


def _json_compatible(value: Any) -> Any:
    if is_dataclass(value):
        return _json_compatible(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_compatible(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class SocialBridgeServiceClient:
    """Thin import-and-call facade for the future social bridge service."""

    MODULE_CANDIDATES = (
        "agent.social_bridge",
        "agent.social_bridge.service",
    )

    SERVICE_FACTORY_NAMES = (
        "get_social_bridge_service",
        "get_service",
        "service",
    )
    DIRECT_METHOD_NAMES = (
        "bridge_list_users",
        "list_users",
        "bridge_set_relationship",
        "set_relationship",
        "bridge_send_message",
        "send_message",
        "bridge_pending_messages",
        "pending_messages",
    )

    @classmethod
    def load(cls) -> "SocialBridgeServiceClient":
        last_error = None
        for module_name in cls.MODULE_CANDIDATES:
            try:
                module = __import__(module_name, fromlist=["*"])
            except ImportError as exc:
                last_error = exc
                continue

            service = cls._service_from_module(module)
            if service is not None:
                return cls(service)
            if cls._has_direct_facade_methods(module):
                return cls(module)

        raise ImportError(SERVICE_UNAVAILABLE) from last_error

    @classmethod
    def _service_from_module(cls, module: Any) -> Any:
        for name in cls.SERVICE_FACTORY_NAMES:
            value = getattr(module, name, None)
            if value is None:
                continue
            return value() if callable(value) else value
        return None

    @classmethod
    def _has_direct_facade_methods(cls, module: Any) -> bool:
        return any(callable(getattr(module, name, None)) for name in cls.DIRECT_METHOD_NAMES)

    def __init__(self, service: Any):
        self.service = service

    def call(self, method_names: tuple[str, ...], **kwargs: Any) -> Any:
        for method_name in method_names:
            method = getattr(self.service, method_name, None)
            if callable(method):
                return method(**kwargs)
        expected = " or ".join(method_names)
        raise AttributeError(
            f"Social bridge service does not implement {expected}."
        )


class SocialBridgeTool(BaseTool):
    """Shared behavior for social bridge tools."""

    service_methods: tuple[str, ...] = ()

    def _actor_id(self) -> Optional[str]:
        context = getattr(self, "context", None)
        profile = getattr(context, "_actor_profile", None)
        actor_id = getattr(profile, "actor_id", None)
        if actor_id:
            return str(actor_id)

        current_user_id = getattr(context, "_current_user_id", None)
        if current_user_id:
            return str(current_user_id)

        return None

    def _call_service(self, **kwargs: Any) -> ToolResult:
        actor_id = self._actor_id()
        if not actor_id:
            return ToolResult.fail(
                "Error: social bridge tools require a current actor profile "
                "or current user id on the tool context."
            )

        try:
            service = SocialBridgeServiceClient.load()
            result = service.call(self.service_methods, actor_id=actor_id, **kwargs)
            return ToolResult.success(_json_compatible(result))
        except ImportError:
            return ToolResult.fail(SERVICE_UNAVAILABLE)
        except AttributeError as exc:
            return ToolResult.fail(f"Error: {exc}")
        except Exception as exc:
            return ToolResult.fail(
                f"Error calling social bridge service method "
                f"{self.service_methods[0]}: {exc}"
            )


class BridgeListUsersTool(SocialBridgeTool):
    name = "bridge_list_users"
    description = (
        "List users visible to the current actor through the social bridge. "
        "Requires an initialized social bridge service."
    )
    params = {
        "type": "object",
        "properties": {
            "include_relationships": {
                "type": "boolean",
                "description": "Whether to include relationship metadata.",
                "default": False,
            }
        },
    }
    service_methods = ("bridge_list_users", "list_users")

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        return self._call_service(
            include_relationships=bool((params or {}).get("include_relationships", False))
        )


class BridgeSetRelationshipTool(SocialBridgeTool):
    name = "bridge_set_relationship"
    description = (
        "Set or update the current actor's relationship metadata with another "
        "social bridge user."
    )
    params = {
        "type": "object",
        "properties": {
            "target_user_id": {
                "type": "string",
                "description": "Target bridge_user_id, WeChat id, known name, or relationship alias.",
            },
            "relationship": {
                "type": "string",
                "description": "Relationship label or state to set.",
            },
            "notes": {
                "type": "string",
                "description": "Optional relationship notes.",
            },
        },
        "required": ["target_user_id", "relationship"],
    }
    service_methods = ("bridge_set_relationship", "set_relationship")

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        params = params or {}
        target_user_id = str(params.get("target_user_id", "")).strip()
        relationship = str(params.get("relationship", "")).strip()
        notes = str(params.get("notes", "")).strip()

        if not target_user_id:
            return ToolResult.fail("Error: target_user_id parameter is required")
        if not relationship:
            return ToolResult.fail("Error: relationship parameter is required")

        kwargs = {
            "target_user_id": target_user_id,
            "relationship": relationship,
        }
        if notes:
            kwargs["notes"] = notes
        if getattr(self, "model", None) is not None:
            kwargs["model"] = self.model
        return self._call_service(**kwargs)


class BridgeSendMessageTool(SocialBridgeTool):
    name = "bridge_send_message"
    description = (
        "Send a message through the social bridge service on behalf of the "
        "current actor. Requires explicit authorization in the parameters."
    )
    params = {
        "type": "object",
        "properties": {
            "target_user_id": {
                "type": "string",
                "description": "Target bridge_user_id, WeChat id, known name, or relationship alias.",
            },
            "message": {
                "type": "string",
                "description": "Message text to send.",
            },
            "authorized": {
                "type": "boolean",
                "description": "Set to true only after explicit user authorization.",
                "default": False,
            },
            "authorization_phrase": {
                "type": "string",
                "description": "Optional explicit authorization phrase.",
            },
        },
        "required": ["target_user_id", "message"],
    }
    service_methods = ("bridge_send_message", "send_message")

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        params = params or {}
        target_user_id = str(params.get("target_user_id", "")).strip()
        message = str(params.get("message", "")).strip()

        if not target_user_id:
            return ToolResult.fail("Error: target_user_id parameter is required")
        if not message:
            return ToolResult.fail("Error: message parameter is required")
        if not self._is_authorized(params):
            return ToolResult.fail(
                "Error: bridge_send_message requires explicit authorization. "
                "Pass authorized=true or a recognized authorization_phrase."
            )

        kwargs = {"target_user_id": target_user_id, "message": message}
        if getattr(self, "model", None) is not None:
            kwargs["model"] = self.model
        return self._call_service(**kwargs)

    def _is_authorized(self, params: Dict[str, Any]) -> bool:
        if params.get("authorized") is True:
            return True
        phrase = str(params.get("authorization_phrase", "")).strip().casefold()
        return phrase in AUTHORIZATION_PHRASES


class BridgePendingMessagesTool(SocialBridgeTool):
    name = "bridge_pending_messages"
    description = (
        "Fetch pending social bridge messages for the current actor. "
        "Requires an initialized social bridge service."
    )
    params = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum pending messages to return.",
                "default": 20,
            },
            "mark_seen": {
                "type": "boolean",
                "description": "Whether to mark returned messages as seen.",
                "default": False,
            },
            "retry_message_id": {
                "type": "string",
                "description": "Optional pending message id to retry before listing messages.",
                "default": "",
            },
        },
    }
    service_methods = ("bridge_pending_messages", "pending_messages")

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        params = params or {}
        try:
            limit = int(params.get("limit", 20))
        except (TypeError, ValueError):
            return ToolResult.fail("Error: limit must be an integer")
        if limit <= 0:
            return ToolResult.fail("Error: limit must be greater than 0")

        return self._call_service(
            limit=limit,
            mark_seen=bool(params.get("mark_seen", False)),
            retry_message_id=str(params.get("retry_message_id", "")).strip(),
        )
