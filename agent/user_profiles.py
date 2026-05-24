"""
User identity and per-actor profile resolution for multi-user agent sessions.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from common.utils import expand_path


ROLE_ADMIN = "admin"
ROLE_USER = "user"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _common_user_read_roots(shared_workspace: str) -> Tuple[str, ...]:
    """Default low-risk roots that make attachment/download workflows smooth."""
    return _as_tuple([
        os.path.join(shared_workspace, "tmp"),
        os.path.join(shared_workspace, "downloads"),
        os.path.join(shared_workspace, "attachments"),
        tempfile.gettempdir(),
        "~/Downloads",
        "~/Desktop",
    ])


def _as_list(value: Any) -> list:
    if not value:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return list(value)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


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
    display_name: str
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
    can_delete_files: bool = False

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


def _configured_display_name(actor_id: str, raw_user_id: str, profile: Dict[str, Any]) -> str:
    from config import conf

    for key in ("llm_usage_label", "wechat_id", "raw_user_id", "display_name", "name"):
        value = profile.get(key)
        if value:
            return str(value)

    labels = conf().get("llm_usage_user_labels", {}) or {}
    if not isinstance(labels, dict):
        return ""

    candidates = (
        actor_id,
        raw_user_id,
        hashlib.sha256(actor_id.encode("utf-8", errors="ignore")).hexdigest()[:16],
        hashlib.sha256(raw_user_id.encode("utf-8", errors="ignore")).hexdigest()[:16],
    )
    for candidate in candidates:
        value = labels.get(candidate)
        if value:
            return str(value)
    return ""


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


def _configured_admin_user_ids() -> list:
    from config import conf, global_config

    configured_admin_users = conf().get("agent_admin_users", []) or []
    if isinstance(configured_admin_users, str):
        admin_users = [item.strip() for item in configured_admin_users.split(",") if item.strip()]
    else:
        admin_users = [str(item).strip() for item in configured_admin_users if str(item).strip()]
    admin_users.extend(
        str(item).strip()
        for item in (global_config.get("admin_users", []) or [])
        if str(item).strip()
    )
    return admin_users


def resolve_single_admin_profile() -> Optional[AgentUserProfile]:
    """Resolve the configured single administrator for non-chat admin surfaces."""
    from config import conf

    candidates = []
    candidates.extend(_configured_admin_user_ids())

    profiles = conf().get("agent_user_profiles", {}) or {}
    if isinstance(profiles, dict):
        for key, profile in profiles.items():
            if not isinstance(profile, dict):
                continue
            if _normalise_role(str(profile.get("role") or "")) == ROLE_ADMIN:
                candidates.append(str(key))

    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        context = {
            "actor_id": candidate,
            "actor_role": ROLE_ADMIN,
            "channel_type": candidate.split(":", 1)[0] if ":" in candidate else "admin",
        }
        return resolve_agent_user_profile(context)

    return None


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
    display_name = str(
        _get_context_value(context, "user_label")
        or _get_context_value(context, "wechat_id")
        or _get_context_value(context, "display_name")
        or _configured_display_name(actor_id, raw_user_id, configured)
        or ""
    )
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

    extra_read_roots = _as_list(configured.get("readable_roots"))
    extra_write_roots = _as_list(configured.get("writable_roots"))
    if role == ROLE_ADMIN:
        readable_roots: Tuple[str, ...] = tuple()
        writable_roots: Tuple[str, ...] = tuple()
        denied_roots: Tuple[str, ...] = tuple()
        denied_files: Tuple[str, ...] = tuple()
        can_use_bash = bool(configured.get("can_use_bash", True))
        can_use_env_config = bool(configured.get("can_use_env_config", True))
        can_delete_files = True
    else:
        default_read_roots = (
            _common_user_read_roots(shared_workspace)
            if _as_bool(conf().get("agent_normal_user_enable_common_read_roots", True), True)
            else tuple()
        )
        global_read_roots = _as_list(conf().get("agent_normal_user_read_roots", []))
        global_write_roots = _as_list(conf().get("agent_normal_user_write_roots", []))
        can_write_knowledge = _as_bool(
            configured.get(
                "can_write_knowledge",
                conf().get("agent_normal_user_can_write_knowledge", True),
            ),
            True,
        )
        readable_roots = _as_tuple([
            tool_workspace,
            private_memory_root,
            knowledge_root,
            skills_root,
            *default_read_roots,
            *global_read_roots,
            *extra_read_roots,
        ])
        default_write_roots = [knowledge_root] if can_write_knowledge else []
        writable_roots = _as_tuple([
            tool_workspace,
            private_memory_root,
            *default_write_roots,
            *global_write_roots,
            *extra_write_roots,
        ])
        denied_roots = _as_tuple([
            project_root,
            project_root / ".git",
            "~/.cow",
            *_as_list(conf().get("agent_sensitive_roots", [])),
            *_as_list(configured.get("denied_roots")),
        ])
        denied_files = _as_tuple([
            project_root / "config.json",
            project_root / "config-template.json",
            project_root / ".env",
            "~/.cow/.env",
            *_as_list(conf().get("agent_sensitive_files", [])),
            *_as_list(configured.get("denied_files")),
        ])
        can_use_bash = bool(configured.get("can_use_bash", True))
        can_use_env_config = bool(configured.get("can_use_env_config", False))
        can_delete_files = _as_bool(
            configured.get(
                "can_delete_files",
                conf().get("agent_normal_user_allow_delete_files", False),
            ),
            False,
        )

    return AgentUserProfile(
        actor_id=actor_id,
        raw_user_id=raw_user_id,
        display_name=display_name,
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
        can_delete_files=can_delete_files,
    )


def apply_profile_to_context(context: Any, profile: AgentUserProfile) -> None:
    if context is None:
        return
    context["actor_id"] = profile.actor_id
    context["actor_role"] = profile.role
    context["conversation_id"] = profile.conversation_id
    context["memory_user_id"] = profile.memory_user_id
    context["tool_workspace"] = profile.tool_workspace
    if profile.display_name:
        context["user_label"] = profile.display_name
