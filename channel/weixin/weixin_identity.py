"""Helpers for mapping Weixin iLink raw ids to human WeChat ids."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Iterable

from common.log import logger
from config import conf


ROLE_ADMIN = "admin"
ROLE_USER = "user"

_CONFIG_LOCK = threading.Lock()
_INTERNAL_ID_MARKERS = ("@im.wechat", "@im.bot")
_PRIMARY_ID_KEYS = (
    "wechat_id",
    "wechatid",
    "weixin_id",
    "weixinid",
    "wxid",
    "wx_id",
    "wx_alias",
    "alias",
)
_SECONDARY_ID_KEYS = (
    "user_name",
    "username",
    "account",
    "login_name",
    "login_user_name",
    "ilink_user_id",
    "user_id",
)
_NICKNAME_KEYS = (
    "nickname",
    "nick_name",
    "nick",
    "user_nickname",
    "from_user_nickname",
    "sender_nickname",
    "remark_name",
    "remark",
    "display_name",
)


def looks_internal_weixin_id(value: Any) -> bool:
    text = _safe_text(value).lower()
    return bool(text and any(marker in text for marker in _INTERNAL_ID_MARKERS))


def is_real_wechat_id(value: Any) -> bool:
    text = _safe_text(value)
    if not text or looks_internal_weixin_id(text):
        return False
    if text.startswith(("http://", "https://")):
        return False
    return 2 <= len(text) <= 80


def extract_real_wechat_id(payload: Any) -> str:
    """Extract a non-iLink WeChat id from a nested API payload."""
    if not isinstance(payload, dict):
        return ""

    for keys in (_PRIMARY_ID_KEYS, _SECONDARY_ID_KEYS):
        found = _find_value_by_keys(payload, keys)
        if found:
            return found
    return ""


def extract_wechat_nickname(payload: Any) -> str:
    """Extract a public nickname from a nested Weixin API payload."""
    if not isinstance(payload, dict):
        return ""
    return _find_text_by_keys(payload, _NICKNAME_KEYS)


def remember_wechat_identity(
    *,
    channel_type: str,
    raw_user_id: str,
    wechat_id: str,
) -> bool:
    """Persist a raw-id -> display WeChat id mapping for future reports."""
    channel_type = _safe_text(channel_type)
    raw_user_id = _safe_text(raw_user_id)
    wechat_id = _safe_text(wechat_id, max_len=160)
    if not channel_type or not raw_user_id or not is_real_wechat_id(wechat_id):
        return False

    actor_id = f"{channel_type}:{raw_user_id}"
    changed = False
    local_config = conf()

    labels = local_config.get("llm_usage_user_labels", {}) or {}
    if not isinstance(labels, dict):
        labels = {}
    labels = dict(labels)
    for key in (actor_id, raw_user_id):
        if labels.get(key) != wechat_id:
            labels[key] = wechat_id
            changed = True

    profiles = local_config.get("agent_user_profiles", {}) or {}
    if not isinstance(profiles, dict):
        profiles = {}
    profiles = dict(profiles)
    profile = dict(profiles.get(actor_id, {}) or {})
    if profile.get("wechat_id") != wechat_id:
        profile["wechat_id"] = wechat_id
        changed = True
    if not profile.get("display_name"):
        profile["display_name"] = wechat_id
        changed = True
    if profile.get("raw_weixin_user_id") != raw_user_id:
        profile["raw_weixin_user_id"] = raw_user_id
        changed = True
    profiles[actor_id] = profile

    if not changed:
        return False

    local_config["llm_usage_user_labels"] = labels
    local_config["agent_user_profiles"] = profiles
    _save_config_patch({
        "llm_usage_user_labels": labels,
        "agent_user_profiles": profiles,
    })
    return True


def normalize_role(role: Any) -> str:
    return ROLE_ADMIN if _safe_text(role).lower() == ROLE_ADMIN else ROLE_USER


def weixin_role_for_identity(
    *,
    channel_type: str,
    raw_user_id: str = "",
    wechat_id: str = "",
    configured_role: str = "",
) -> str:
    explicit_role = _safe_text(configured_role)
    if explicit_role:
        return normalize_role(explicit_role)

    try:
        from config import global_config
    except Exception:
        global_config = {"admin_users": []}

    admin_users = conf().get("agent_admin_users", []) or []
    if isinstance(admin_users, str):
        admin_users = [item.strip() for item in admin_users.split(",") if item.strip()]
    else:
        admin_users = [str(item).strip() for item in admin_users if str(item).strip()]
    admin_users.extend(global_config.get("admin_users", []) or [])

    candidates = {
        _safe_text(raw_user_id),
        _safe_text(wechat_id),
        f"{channel_type}:{raw_user_id}" if channel_type and raw_user_id else "",
        f"{channel_type}:{wechat_id}" if channel_type and wechat_id else "",
    }
    return ROLE_ADMIN if any(candidate in admin_users for candidate in candidates if candidate) else ROLE_USER


def _find_value_by_keys(payload: Dict[str, Any], keys: Iterable[str]) -> str:
    wanted = {key.lower() for key in keys}
    stack = [payload]
    while stack:
        current = stack.pop()
        if not isinstance(current, dict):
            continue
        for key, value in current.items():
            lowered = str(key).lower()
            if lowered in wanted and is_real_wechat_id(value):
                return _safe_text(value, max_len=160)
            if isinstance(value, dict):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(item for item in value if isinstance(item, dict))
    return ""


def _find_text_by_keys(payload: Dict[str, Any], keys: Iterable[str]) -> str:
    wanted = {key.lower() for key in keys}
    stack = [payload]
    while stack:
        current = stack.pop()
        if not isinstance(current, dict):
            continue
        for key, value in current.items():
            lowered = str(key).lower()
            if lowered in wanted:
                text = _safe_text(value, max_len=80)
                if _is_public_nickname(text):
                    return text
            if isinstance(value, dict):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(item for item in value if isinstance(item, dict))
    return ""


def _is_public_nickname(value: str) -> bool:
    text = _safe_text(value, max_len=80)
    if not text or looks_internal_weixin_id(text):
        return False
    if text.startswith(("http://", "https://")):
        return False
    return True


def _safe_text(value: Any, max_len: int = 96) -> str:
    if value is None:
        return ""
    return str(value).strip()[:max_len]


def _save_config_patch(patch: Dict[str, Any]) -> None:
    config_path = Path(__file__).resolve().parents[2] / "config.json"
    with _CONFIG_LOCK:
        try:
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8-sig") as f:
                    file_cfg = json.load(f)
            else:
                file_cfg = {}
            file_cfg.update(patch)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(file_cfg, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[Weixin] Failed to persist WeChat identity mapping: {e}")
