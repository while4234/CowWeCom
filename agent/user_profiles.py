"""
User identity and per-actor profile resolution for multi-user agent sessions.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from common.utils import expand_path


ROLE_ADMIN = "admin"
ROLE_USER = "user"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def safe_actor_slug(value: str) -> str:
    """Return a deterministic filesystem-safe id for a chat actor."""
    raw = str(value or "unknown")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._-").lower()
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    if not slug:
        slug = "user"
    if len(slug) > 48:
        slug = slug[:48].rstrip("._-") or "user"
    return f"{slug}_{digest}"


def _normalise_role(role: str) -> str:
    role = (role or ROLE_USER).strip().lower()
    return ROLE_ADMIN if role == ROLE_ADMIN else ROLE_USER


def _as_tuple(paths: Iterable[Any]) -> Tuple[str, ...]:
    resolved = []
    for path in paths:
        if not path:
            continue
        resolved.append(os.path.abspath(expand_path(str(path))))
    return tuple(resolved)


@dataclass(frozen=True)
class AgentUserProfile:
    actor_id: str
    raw_user_id: str
    channel_type: str
    role: str
    conversation_id: str
    memory_user_id: str
    shared_workspace: str
    tool_workspace: str
    readable_roots: Tuple[str, ...] = field(default_factory=tuple)
    writable_roots: Tuple[str, ...] = field(default_factory=tuple)
    denied_roots: Tuple[str, ...] = field(default_factory=tuple)
    denied_files: Tuple[str, ...] = field(default_factory=tuple)
    can_use_bash: bool = False
    can_use_env_config: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


def _get_context_value(context: Any, key: str, default: Any = None) -> Any:
    if context is None:
        return default
    try:
        return context.get(key, default)
    except Exception:
        return default


def _get_context_msg(context: Any) -> Any:
    msg = _get_context_value(context, "msg")
    if msg is not None:
        return msg
    kwargs = getattr(context, "kwargs", None) or {}
    return kwargs.get("msg")


def _resolve_raw_user_id(context: Any) -> str:
    msg = _get_context_msg(context)
    if msg is not None:
        is_group = bool(_get_context_value(context, "isgroup", False))
        if is_group:
            for attr in ("actual_user_id", "from_user_id", "sender_staff_id"):
                value = getattr(msg, attr, None)
                if value:
                    return str(value)
        for attr in ("from_user_id", "actual_user_id", "sender_staff_id", "other_user_id"):
            value = getattr(msg, attr, None)
            if value:
                return str(value)

    session_id = _get_context_value(context, "session_id")
    if session_id:
        return str(session_id)
    return "unknown"


def _configured_profile(actor_id: str, raw_user_id: str) -> Dict[str, Any]:
    from config import conf

    profiles = conf().get("agent_user_profiles", {}) or {}
    if not isinstance(profiles, dict):
        return {}
    profile = profiles.get(actor_id) or profiles.get(raw_user_id) or {}
    return profile if isinstance(profile, dict) else {}


def _configured_role(actor_id: str, raw_user_id: str, profile: Dict[str, Any]) -> str:
    from config import conf, global_config

    role = profile.get("role")
    if role:
        return _normalise_role(role)

    configured_admin_users = conf().get("agent_admin_users", []) or []
    if isinstance(configured_admin_users, str):
        admin_users = [item.strip() for item in configured_admin_users.split(",") if item.strip()]
    else:
        admin_users = list(configured_admin_users)
    admin_users.extend(global_config.get("admin_users", []) or [])
    if actor_id in admin_users or raw_user_id in admin_users:
        return ROLE_ADMIN

    return _normalise_role(conf().get("agent_default_role", ROLE_USER))


def resolve_agent_user_profile(context: Any = None) -> AgentUserProfile:
    """Resolve the current chat actor into an isolated agent profile."""
    from config import conf

    explicit_actor_id = _get_context_value(context, "actor_id")
    if explicit_actor_id:
        actor_id = str(explicit_actor_id)
        if ":" in actor_id:
            channel_type, raw_user_id = actor_id.split(":", 1)
        else:
            channel_type = str(_get_context_value(context, "channel_type", "unknown") or "unknown")
            raw_user_id = actor_id
    else:
        channel_type = str(_get_context_value(context, "channel_type", "unknown") or "unknown")
        raw_user_id = _resolve_raw_user_id(context)
        actor_id = f"{channel_type}:{raw_user_id}"
    configured = _configured_profile(actor_id, raw_user_id)
    explicit_role = _get_context_value(context, "actor_role")
    role = _normalise_role(explicit_role) if explicit_role else _configured_role(actor_id, raw_user_id, configured)

    shared_workspace = os.path.abspath(expand_path(conf().get("agent_workspace", "~/cow")))
    memory_user_id = str(
        _get_context_value(context, "memory_user_id")
        or configured.get("memory_user_id")
        or safe_actor_slug(actor_id)
    )
    conversation_id = str(
        _get_context_value(context, "conversation_id")
        or configured.get("conversation_id")
        or actor_id
    )

    user_root = conf().get("agent_user_workspace_root") or os.path.join(shared_workspace, "users")
    user_root = os.path.abspath(expand_path(user_root))
    default_tool_workspace = (
        shared_workspace if role == ROLE_ADMIN
        else os.path.join(user_root, memory_user_id, "files")
    )
    tool_workspace = os.path.abspath(expand_path(
        configured.get("tool_workspace") or default_tool_workspace
    ))

    project_root = _project_root()
    private_memory_root = os.path.join(shared_workspace, "memory", "users", memory_user_id)
    knowledge_root = os.path.join(shared_workspace, "knowledge")
    skills_root = os.path.join(shared_workspace, "skills")

    extra_read_roots = configured.get("readable_roots") or []
    extra_write_roots = configured.get("writable_roots") or []
    if role == ROLE_ADMIN:
        readable_roots: Tuple[str, ...] = tuple()
        writable_roots: Tuple[str, ...] = tuple()
        denied_roots: Tuple[str, ...] = tuple()
        denied_files: Tuple[str, ...] = tuple()
        can_use_bash = bool(configured.get("can_use_bash", True))
        can_use_env_config = bool(configured.get("can_use_env_config", True))
    else:
        readable_roots = _as_tuple([
            tool_workspace,
            private_memory_root,
            knowledge_root,
            skills_root,
            *extra_read_roots,
        ])
        writable_roots = _as_tuple([
            tool_workspace,
            private_memory_root,
            *extra_write_roots,
        ])
        denied_roots = _as_tuple([
            project_root,
            project_root / ".git",
            "~/.cow",
            *(conf().get("agent_sensitive_roots", []) or []),
            *(configured.get("denied_roots") or []),
        ])
        denied_files = _as_tuple([
            project_root / "config.json",
            project_root / "config-template.json",
            project_root / ".env",
            "~/.cow/.env",
            *(conf().get("agent_sensitive_files", []) or []),
            *(configured.get("denied_files") or []),
        ])
        can_use_bash = bool(configured.get("can_use_bash", True))
        can_use_env_config = bool(configured.get("can_use_env_config", False))

    return AgentUserProfile(
        actor_id=actor_id,
        raw_user_id=raw_user_id,
        channel_type=channel_type,
        role=role,
        conversation_id=conversation_id,
        memory_user_id=memory_user_id,
        shared_workspace=shared_workspace,
        tool_workspace=tool_workspace,
        readable_roots=readable_roots,
        writable_roots=writable_roots,
        denied_roots=denied_roots,
        denied_files=denied_files,
        can_use_bash=can_use_bash,
        can_use_env_config=can_use_env_config,
    )


def apply_profile_to_context(context: Any, profile: AgentUserProfile) -> None:
    if context is None:
        return
    context["actor_id"] = profile.actor_id
    context["actor_role"] = profile.role
    context["conversation_id"] = profile.conversation_id
    context["memory_user_id"] = profile.memory_user_id
    context["tool_workspace"] = profile.tool_workspace
