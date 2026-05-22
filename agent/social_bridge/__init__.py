"""Domain and storage helpers for the planned social bridge feature."""

from agent.social_bridge.store import (
    BridgeAuditEntry,
    BridgeMessage,
    BridgeRelationship,
    BridgeStore,
    BridgeUser,
    PendingBridgeMessage,
    compute_pair_id,
    get_bridge_store,
)
from agent.social_bridge.service import (
    ActiveMessageRouter,
    SocialBridgeService,
    get_social_bridge_service,
    list_users,
    pending_messages,
    send_message,
    set_relationship,
)

__all__ = [
    "ActiveMessageRouter",
    "BridgeAuditEntry",
    "BridgeMessage",
    "BridgeRelationship",
    "BridgeStore",
    "BridgeUser",
    "PendingBridgeMessage",
    "SocialBridgeService",
    "compute_pair_id",
    "get_bridge_store",
    "get_social_bridge_service",
    "list_users",
    "pending_messages",
    "send_message",
    "set_relationship",
]
