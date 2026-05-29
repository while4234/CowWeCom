# encoding:utf-8

"""Shared xAI HTTP credential resolver for CowWeCom."""

from __future__ import annotations

import os
from typing import Dict

from config import conf

from .auth import (
    AuthError,
    DEFAULT_XAI_OAUTH_BASE_URL,
    resolve_xai_oauth_runtime_credentials,
)


def has_xai_credentials() -> bool:
    """Cheap credential probe that avoids network refresh."""
    if _config_bool("grok_auth_prefer_oauth", True):
        try:
            from .auth import get_xai_oauth_status

            if get_xai_oauth_status().get("logged_in"):
                return True
        except Exception:
            pass
    return bool(_get_config_or_env("XAI_API_KEY", "grok_api_key"))


def resolve_xai_http_credentials(force_refresh: bool = False, account_id: str = "") -> Dict[str, str]:
    """Resolve Grok bearer credentials: OAuth first, API key fallback second."""
    if _config_bool("grok_auth_prefer_oauth", True):
        try:
            creds = resolve_xai_oauth_runtime_credentials(force_refresh=force_refresh, account_id=account_id)
            access_token = str(creds.get("api_key") or "").strip()
            if access_token:
                return {
                    "provider": "xai-oauth",
                    "auth_mode": "oauth_pkce",
                    "api_key": access_token,
                    "base_url": str(creds.get("base_url") or DEFAULT_XAI_OAUTH_BASE_URL).rstrip("/"),
                    "account_id": str(creds.get("account_id") or ""),
                    "account_name": str(creds.get("account_name") or ""),
                }
        except AuthError:
            if force_refresh:
                raise
        except Exception:
            if force_refresh:
                raise

    api_key = _get_config_or_env("XAI_API_KEY", "grok_api_key")
    if not api_key:
        raise AuthError(
            "Grok account is not logged in. Please complete Grok login in the Web admin page.",
            code="xai_auth_missing",
            relogin_required=True,
        )
    base_url = str(conf().get("grok_api_base") or os.environ.get("XAI_BASE_URL") or DEFAULT_XAI_OAUTH_BASE_URL).strip().rstrip("/")
    return {
        "provider": "xai",
        "auth_mode": "api_key",
        "api_key": api_key,
        "base_url": base_url or DEFAULT_XAI_OAUTH_BASE_URL,
    }


def hermes_xai_user_agent() -> str:
    """Return a stable User-Agent, matching Hermes' direct xAI convention."""
    return "CowWeCom-Hermes-xAI/1"


def _get_config_or_env(env_key: str, config_key: str) -> str:
    return str(os.environ.get(env_key) or conf().get(config_key) or "").strip()


def _config_bool(key: str, default: bool) -> bool:
    value = conf().get(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
