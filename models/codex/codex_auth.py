from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from config import conf


CODEX_AUTH_CREDENTIAL_ID = "codex"


def default_codex_auth_path() -> Path:
    codex_home = str(os.environ.get("CODEX_HOME") or "").strip()
    if codex_home:
        return Path(codex_home).expanduser() / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def resolve_codex_auth_path(config: Optional[Mapping[str, Any]] = None) -> Path:
    env_path = str(os.environ.get("CODEX_AUTH_FILE") or "").strip()
    if env_path:
        return Path(env_path).expanduser()

    cfg = config if config is not None else conf()
    for key in ("codex_auth_file", "codex_auth_path"):
        value = _text_value(cfg, key)
        if value:
            return Path(value).expanduser()

    skill_conf = _mapping_value(cfg, "skill")
    image_conf = {}
    if skill_conf:
        image_conf = _mapping_value(skill_conf, "image-generation") or _mapping_value(skill_conf, "image_generation")
    for key in ("codex_auth_file", "auth_file", "codex_auth_path"):
        value = _text_value(image_conf, key)
        if value:
            return Path(value).expanduser()

    return default_codex_auth_path()


def _text_value(mapping: Optional[Mapping[str, Any]], key: str, default: str = "") -> str:
    if not isinstance(mapping, Mapping):
        return default
    value = mapping.get(key, default)
    return str(value or "").strip()


def _mapping_value(mapping: Optional[Mapping[str, Any]], key: str) -> dict[str, Any]:
    if not isinstance(mapping, Mapping):
        return {}
    value = mapping.get(key, {})
    return value if isinstance(value, dict) else {}


def _jwt_claims(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def token_expires_at(tokens: Mapping[str, Any]) -> float:
    for key in ("expires_at", "expiresAt"):
        try:
            expires_at = float(tokens.get(key, 0) or 0)
        except (TypeError, ValueError):
            expires_at = 0.0
        if expires_at > 0:
            return expires_at

    for token_key in ("access_token", "id_token"):
        claims = _jwt_claims(str(tokens.get(token_key, "") or ""))
        try:
            expires_at = float(claims.get("exp", 0) or 0)
        except (TypeError, ValueError):
            expires_at = 0.0
        if expires_at > 0:
            return expires_at
    return 0.0


def account_id_from_tokens(tokens: Mapping[str, Any]) -> str:
    explicit = str(tokens.get("account_id", tokens.get("accountId", "")) or "").strip()
    if explicit:
        return explicit

    for token_key in ("id_token", "access_token"):
        claims = _jwt_claims(str(tokens.get(token_key, "") or ""))
        for key in ("account_id", "accountId", "https://api.openai.com/auth/account_id"):
            value = str(claims.get(key, "") or "").strip()
            if value:
                return value
        auth_claims = claims.get("https://api.openai.com/auth", {})
        if isinstance(auth_claims, dict):
            for key in ("chatgpt_account_id", "account_id", "accountId"):
                value = str(auth_claims.get(key, "") or "").strip()
                if value:
                    return value
    return ""


class CodexAuthCredentialSource:
    def __init__(self, auth_path: Optional[Path | str] = None) -> None:
        self.auth_path = Path(auth_path).expanduser() if auth_path else resolve_codex_auth_path()

    def load(self) -> dict[str, Any]:
        if not self.auth_path.exists():
            raise FileNotFoundError("Codex auth file not found; set codex_auth_file or CODEX_AUTH_FILE")
        payload = json.loads(self.auth_path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise RuntimeError("codex_auth_invalid: auth file must contain a JSON object")
        raw_tokens = payload.get("tokens", {})
        if not isinstance(raw_tokens, dict):
            raise RuntimeError("codex_auth_invalid: missing tokens object")
        tokens = dict(raw_tokens)
        access_token = str(tokens.get("access_token", "") or "").strip()
        if not access_token:
            raise RuntimeError("codex_auth_invalid: missing access token")
        expires_at = token_expires_at(tokens)
        if expires_at > 0:
            tokens["expires_at"] = expires_at
        account_id = account_id_from_tokens(tokens)
        if account_id:
            tokens["account_id"] = account_id
        return {
            "id": CODEX_AUTH_CREDENTIAL_ID,
            "name": "Current Codex login",
            "auth_mode": str(payload.get("auth_mode", "chatgpt") or "chatgpt"),
            "tokens": tokens,
            "updatedAt": str(payload.get("last_refresh", "")),
        }

    def resolve_access_tokens(self) -> dict[str, Any]:
        credential = self.load()
        tokens = credential.get("tokens", {}) if isinstance(credential.get("tokens", {}), dict) else {}
        expires_at = token_expires_at(tokens)
        if expires_at and expires_at <= time.time():
            raise RuntimeError("codex_auth_expired: current Codex access token is expired; refresh Codex and retry")
        access_token = str(tokens.get("access_token", "") or "").strip()
        if not access_token:
            raise RuntimeError("codex_auth_invalid: missing access token")
        return {
            "access_token": access_token,
            "account_id": account_id_from_tokens(tokens),
            "credential": credential,
        }
