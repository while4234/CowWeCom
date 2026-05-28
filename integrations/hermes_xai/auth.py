# encoding:utf-8

"""CowWeCom auth store and OAuth PKCE flow for native xAI/Grok accounts.

This module intentionally mirrors Hermes' xAI OAuth constants and request
shape, but persists credentials in CowWeCom's own auth store.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from common.log import logger
from common.utils import expand_path
from config import conf


AUTH_STORE_VERSION = 1
PROVIDER_ID = "xai-oauth"
AUTH_LOCK_TIMEOUT_SECONDS = 15.0

DEFAULT_XAI_OAUTH_BASE_URL = "https://api.x.ai/v1"
XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_REDIRECT_HOST = "127.0.0.1"
XAI_OAUTH_REDIRECT_PORT = 56121
XAI_OAUTH_REDIRECT_PATH = "/callback"
XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120

_store_lock = threading.RLock()
_login_lock = threading.RLock()
_active_login: Optional["_LoginSession"] = None


class AuthError(RuntimeError):
    """Structured auth error with safe, user-facing semantics."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = PROVIDER_ID,
        code: Optional[str] = None,
        relogin_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.relogin_required = relogin_required


@dataclass
class _LoginSession:
    state: str
    nonce: str
    code_verifier: str
    code_challenge: str
    redirect_uri: str
    discovery: Dict[str, str]
    authorize_url: str
    server: Optional[ThreadingHTTPServer] = None
    thread: Optional[threading.Thread] = None
    callback: Optional[Dict[str, Any]] = None
    status: str = "pending"
    message: str = ""
    created_at: float = 0.0
    completed_at: float = 0.0


def start_xai_oauth_login() -> dict:
    """Start an xAI OAuth login and return a safe browser URL payload."""
    global _active_login

    with _login_lock:
        _stop_login_session(_active_login)
        discovery = _xai_oauth_discovery()
        server = None
        thread = None
        try:
            server, thread, redirect_uri = _start_callback_server()
            bind_message = ""
        except AuthError as exc:
            if exc.code != "xai_callback_bind_failed":
                raise
            redirect_uri = _default_redirect_uri()
            bind_message = str(exc)
        try:
            _xai_validate_loopback_redirect_uri(redirect_uri)
            code_verifier = _oauth_pkce_code_verifier()
            code_challenge = _oauth_pkce_code_challenge(code_verifier)
            state = uuid.uuid4().hex
            nonce = uuid.uuid4().hex
            authorize_url = _xai_oauth_build_authorize_url(
                authorization_endpoint=discovery["authorization_endpoint"],
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                state=state,
                nonce=nonce,
            )
            _active_login = _LoginSession(
                state=state,
                nonce=nonce,
                code_verifier=code_verifier,
                code_challenge=code_challenge,
                redirect_uri=redirect_uri,
                discovery=discovery,
                authorize_url=authorize_url,
                server=server,
                thread=thread,
                created_at=time.time(),
                message=bind_message,
            )
        except Exception:
            _shutdown_callback_server(server, thread)
            raise

        response = {
            "authorize_url": authorize_url,
            "state": "pending",
            "redirect_uri": redirect_uri,
            "manual_paste_supported": True,
        }
        if bind_message:
            response["message"] = bind_message
        return response


def complete_xai_oauth_with_callback_url(callback_url: str) -> dict:
    """Complete the current OAuth flow from a pasted loopback callback URL."""
    callback = _parse_callback_url(callback_url)
    return _complete_current_login(callback)


def poll_xai_oauth_login() -> dict:
    """Return the current loopback login session state without secrets."""
    global _active_login

    with _login_lock:
        session = _active_login
        if session is None:
            status = get_xai_oauth_status()
            return {
                "status": "complete" if status.get("logged_in") else "idle",
                "message": "",
                "auth": status,
            }

        if session.status == "pending" and session.callback:
            try:
                return {
                    "status": "complete",
                    "message": "",
                    "auth": _complete_login_session(session, session.callback),
                }
            except AuthError as exc:
                session.status = "failed"
                session.message = _safe_error_message(exc)
                session.completed_at = time.time()
                _stop_login_session(session)

        return {
            "status": session.status,
            "message": session.message,
            "auth": get_xai_oauth_status() if session.status == "complete" else None,
        }


def read_xai_oauth_tokens() -> dict:
    """Read the CowWeCom xai-oauth provider state, including tokens."""
    with _store_lock:
        state = _read_provider_state()
    tokens = state.get("tokens") if isinstance(state, dict) else None
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise AuthError(
            "Grok account is not logged in.",
            code="xai_auth_missing",
            relogin_required=True,
        )
    return state


def save_xai_oauth_tokens(
    tokens: dict,
    *,
    discovery: dict | None = None,
    redirect_uri: str = "",
) -> None:
    """Persist token state atomically without logging token contents."""
    if not isinstance(tokens, dict):
        raise AuthError("xAI OAuth token payload was invalid.", code="xai_token_invalid")
    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not access_token:
        raise AuthError("xAI OAuth token payload is missing access_token.", code="xai_token_invalid")

    now = _utc_now_iso()
    token_state = dict(tokens)
    token_state["access_token"] = access_token
    if refresh_token:
        token_state["refresh_token"] = refresh_token
    if not token_state.get("expires_at"):
        expires_at = _token_expiry_from_payload(token_state)
        if expires_at:
            token_state["expires_at"] = expires_at

    with _store_lock:
        store = _load_auth_store()
        providers = store.setdefault("providers", {})
        previous = providers.get(PROVIDER_ID) if isinstance(providers.get(PROVIDER_ID), dict) else {}
        previous_tokens = previous.get("tokens") if isinstance(previous.get("tokens"), dict) else {}
        if not token_state.get("refresh_token") and previous_tokens.get("refresh_token"):
            token_state["refresh_token"] = previous_tokens["refresh_token"]
        state = dict(previous)
        state.update({
            "provider": PROVIDER_ID,
            "auth_mode": "oauth_pkce",
            "base_url": _xai_validate_inference_base_url(
                conf().get("grok_api_base", ""),
                fallback=DEFAULT_XAI_OAUTH_BASE_URL,
            ),
            "redirect_uri": redirect_uri or previous.get("redirect_uri") or _default_redirect_uri(),
            "last_refresh": now,
            "tokens": token_state,
        })
        if discovery:
            state["discovery"] = _sanitize_discovery(discovery)
        providers[PROVIDER_ID] = state
        store["active_provider"] = PROVIDER_ID
        _save_auth_store(store)


def refresh_xai_oauth(force: bool = False) -> dict:
    """Refresh the xAI access token if needed and persist the updated token."""
    with _store_lock:
        state = _read_provider_state()
        tokens = dict(state.get("tokens") or {})
        access_token = str(tokens.get("access_token") or "").strip()
        refresh_token = str(tokens.get("refresh_token") or "").strip()
        if not refresh_token:
            raise AuthError(
                "Grok OAuth refresh token is missing. Please log in again.",
                code="xai_auth_missing_refresh_token",
                relogin_required=True,
            )
        if not force and not _xai_token_state_is_expiring(tokens, access_token):
            return get_xai_oauth_status()

        discovery = dict(state.get("discovery") or {})
        token_endpoint = str(discovery.get("token_endpoint") or "").strip()
        if not token_endpoint:
            discovery = _xai_oauth_discovery()
            token_endpoint = discovery["token_endpoint"]
        refreshed = _refresh_xai_oauth_tokens(
            tokens,
            token_endpoint=token_endpoint,
            timeout_seconds=_refresh_timeout_seconds(),
        )
        save_xai_oauth_tokens(
            refreshed,
            discovery=discovery,
            redirect_uri=str(state.get("redirect_uri") or _default_redirect_uri()),
        )
        return get_xai_oauth_status()


def resolve_xai_oauth_runtime_credentials(force_refresh: bool = False) -> dict:
    """Return xAI OAuth credentials for runtime HTTP calls."""
    state = read_xai_oauth_tokens()
    tokens = dict(state.get("tokens") or {})
    access_token = str(tokens.get("access_token") or "").strip()
    if force_refresh or _xai_token_state_is_expiring(tokens, access_token):
        refresh_xai_oauth(force=force_refresh)
        state = read_xai_oauth_tokens()
        tokens = dict(state.get("tokens") or {})
        access_token = str(tokens.get("access_token") or "").strip()
    if not access_token:
        raise AuthError(
            "Grok account is not logged in.",
            code="xai_auth_missing",
            relogin_required=True,
        )
    return {
        "api_key": access_token,
        "base_url": _xai_validate_inference_base_url(
            str(state.get("base_url") or conf().get("grok_api_base", "")),
            fallback=DEFAULT_XAI_OAUTH_BASE_URL,
        ),
        "provider": PROVIDER_ID,
        "auth_mode": "oauth_pkce",
    }


def logout_xai_oauth() -> dict:
    """Remove the xai-oauth provider state from CowWeCom's auth store."""
    global _active_login

    with _login_lock:
        _stop_login_session(_active_login)
        _active_login = None
    with _store_lock:
        store = _load_auth_store()
        providers = store.setdefault("providers", {})
        if isinstance(providers, dict):
            providers.pop(PROVIDER_ID, None)
        if store.get("active_provider") == PROVIDER_ID:
            store["active_provider"] = ""
        _save_auth_store(store)
    return get_xai_oauth_status()


def get_xai_oauth_status() -> dict:
    """Return a token-free Grok OAuth status payload."""
    try:
        state = _read_provider_state()
    except AuthError:
        return _logged_out_status(needs_reauth=True)

    tokens = state.get("tokens") if isinstance(state.get("tokens"), dict) else {}
    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    expires_at = _coerce_expires_at(tokens.get("expires_at")) or _access_token_exp(access_token) or 0
    logged_in = bool(access_token and refresh_token)
    needs_reauth = False
    if not logged_in:
        needs_reauth = bool(state)
    elif expires_at and float(expires_at) <= time.time():
        needs_reauth = not bool(refresh_token)
    profile = state.get("profile") if isinstance(state.get("profile"), dict) else {}
    return {
        "logged_in": logged_in,
        "provider": PROVIDER_ID if logged_in else "",
        "base_url": _xai_validate_inference_base_url(
            str(state.get("base_url") or conf().get("grok_api_base", "")),
            fallback=DEFAULT_XAI_OAUTH_BASE_URL,
        ),
        "email": str(profile.get("email") or "") if profile else "",
        "expires_at": expires_at,
        "needs_reauth": needs_reauth,
    }


def _complete_current_login(callback: Dict[str, Any]) -> dict:
    with _login_lock:
        session = _active_login
        if session is None:
            raise AuthError(
                "No active Grok login session. Start login again.",
                code="xai_login_session_missing",
            )
        return _complete_login_session(session, callback)


def _complete_login_session(session: _LoginSession, callback: Dict[str, Any]) -> dict:
    global _active_login

    if session.status == "complete":
        return get_xai_oauth_status()
    if callback.get("error"):
        detail = str(callback.get("error_description") or callback.get("error") or "authorization failed")
        raise AuthError(f"xAI authorization failed: {detail}", code="xai_authorization_failed")
    if callback.get("state") != session.state:
        raise AuthError("xAI authorization failed: state mismatch.", code="xai_state_mismatch")
    code = str(callback.get("code") or "").strip()
    if not code:
        raise AuthError("xAI authorization failed: missing authorization code.", code="xai_code_missing")

    token_payload = _xai_oauth_exchange_code_for_tokens(
        token_endpoint=session.discovery["token_endpoint"],
        code=code,
        redirect_uri=session.redirect_uri,
        code_verifier=session.code_verifier,
        code_challenge=session.code_challenge,
    )
    access_token = str(token_payload.get("access_token") or "").strip()
    refresh_token = str(token_payload.get("refresh_token") or "").strip()
    if not access_token:
        raise AuthError("xAI token exchange did not return an access_token.", code="xai_token_exchange_invalid")
    if not refresh_token:
        raise AuthError("xAI token exchange did not return a refresh_token.", code="xai_token_exchange_invalid")
    id_token = str(token_payload.get("id_token") or "").strip()
    if id_token:
        _validate_id_token_nonce(id_token, session.nonce)

    tokens = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "expires_in": token_payload.get("expires_in"),
        "token_type": str(token_payload.get("token_type") or "Bearer").strip() or "Bearer",
    }
    save_xai_oauth_tokens(tokens, discovery=session.discovery, redirect_uri=session.redirect_uri)
    session.status = "complete"
    session.message = ""
    session.completed_at = time.time()
    _stop_login_session(session)
    _active_login = None
    return get_xai_oauth_status()


def _read_provider_state() -> Dict[str, Any]:
    store = _load_auth_store()
    providers = store.get("providers")
    if not isinstance(providers, dict):
        return {}
    state = providers.get(PROVIDER_ID)
    return dict(state) if isinstance(state, dict) else {}


def _load_auth_store() -> Dict[str, Any]:
    path = _auth_file_path()
    if not os.path.exists(path):
        return {"version": AUTH_STORE_VERSION, "providers": {}}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception as exc:
        raise AuthError(
            "Grok auth store could not be read. Move or repair the auth file and retry.",
            code="xai_auth_store_invalid",
        ) from exc
    if not isinstance(raw, dict):
        raise AuthError("Grok auth store is invalid.", code="xai_auth_store_invalid")
    raw.setdefault("version", AUTH_STORE_VERSION)
    raw.setdefault("providers", {})
    if not isinstance(raw.get("providers"), dict):
        raw["providers"] = {}
    return raw


def _save_auth_store(store: Dict[str, Any]) -> None:
    path = _auth_file_path()
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    store["version"] = AUTH_STORE_VERSION
    store["updated_at"] = _utc_now_iso()
    payload = json.dumps(store, ensure_ascii=False, indent=2) + "\n"
    tmp_path = os.path.join(parent, f"{os.path.basename(path)}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(tmp_path, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _auth_file_path() -> str:
    configured = str(conf().get("grok_auth_file") or "").strip()
    path = expand_path(configured) if configured else os.path.join("data", "auth", "grok_auth.json")
    return os.path.abspath(path)


def _start_callback_server() -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    host = XAI_OAUTH_REDIRECT_HOST
    handler_cls = _make_callback_handler()

    class _Server(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    try:
        server = _Server((host, XAI_OAUTH_REDIRECT_PORT), handler_cls)
    except OSError as exc:
        raise AuthError(
            "Could not bind Grok OAuth callback server on "
            f"{host}:{XAI_OAUTH_REDIRECT_PORT}. Use manual paste after opening the login URL.",
            code="xai_callback_bind_failed",
        ) from exc
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    redirect_uri = f"http://{host}:{XAI_OAUTH_REDIRECT_PORT}{XAI_OAUTH_REDIRECT_PATH}"
    return server, thread, redirect_uri


def _make_callback_handler():
    class _XAICallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if not _is_loopback_http_request(self.client_address[0], self.headers.get("Host", "")):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid callback host.")
                return
            parsed = urlparse(self.path)
            if parsed.path != XAI_OAUTH_REDIRECT_PATH:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found.")
                return
            params = parse_qs(parsed.query)
            incoming = {
                "code": params.get("code", [None])[0],
                "state": params.get("state", [None])[0],
                "error": params.get("error", [None])[0],
                "error_description": params.get("error_description", [None])[0],
            }
            has_code = incoming["code"] is not None
            has_state = incoming["state"] is not None
            logger.info(
                "Grok OAuth loopback callback received: path=%s has_code=%s has_state=%s has_error=%s",
                parsed.path,
                has_code,
                has_state,
                incoming["error"] is not None,
            )
            with _login_lock:
                if _active_login and _active_login.status == "pending" and not _active_login.callback:
                    _active_login.callback = incoming

            self.send_response(200 if has_code or incoming["error"] else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if incoming["error"]:
                body = "<html><body><h1>Grok authorization failed.</h1>You can close this tab.</body></html>"
            elif has_code:
                body = "<html><body><h1>Grok authorization received.</h1>You can close this tab.</body></html>"
            else:
                body = "<html><body><h1>Grok authorization not received.</h1>No authorization code was present.</body></html>"
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    return _XAICallbackHandler


def _is_loopback_http_request(remote_addr: str, host_header: str) -> bool:
    remote = str(remote_addr or "").strip()
    host = str(host_header or "").strip().split(":", 1)[0].lower()
    return remote == XAI_OAUTH_REDIRECT_HOST and host == XAI_OAUTH_REDIRECT_HOST


def _parse_callback_url(callback_url: str) -> Dict[str, Any]:
    parsed = urlparse(str(callback_url or "").strip())
    if parsed.scheme != "http":
        raise AuthError("Grok callback URL must be the loopback http URL.", code="xai_callback_invalid")
    if parsed.hostname != XAI_OAUTH_REDIRECT_HOST or parsed.path != XAI_OAUTH_REDIRECT_PATH:
        raise AuthError("Grok callback host or path did not match the login session.", code="xai_callback_invalid")
    if parsed.port != XAI_OAUTH_REDIRECT_PORT:
        raise AuthError("Grok callback port did not match the login session.", code="xai_callback_invalid")
    params = parse_qs(parsed.query)
    return {
        "code": params.get("code", [None])[0],
        "state": params.get("state", [None])[0],
        "error": params.get("error", [None])[0],
        "error_description": params.get("error_description", [None])[0],
    }


def _shutdown_callback_server(server: Optional[ThreadingHTTPServer], thread: Optional[threading.Thread]) -> None:
    if server:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
    if thread:
        try:
            thread.join(timeout=1.0)
        except Exception:
            pass


def _stop_login_session(session: Optional[_LoginSession]) -> None:
    if session:
        _shutdown_callback_server(session.server, session.thread)


def _oauth_pkce_code_verifier(length: int = 64) -> str:
    raw = base64.urlsafe_b64encode(os.urandom(length)).decode("ascii")
    return raw.rstrip("=")[:128]


def _oauth_pkce_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _xai_oauth_build_authorize_url(
    *,
    authorization_endpoint: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    nonce: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": XAI_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": XAI_OAUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        "plan": "generic",
        "referrer": "hermes-agent",
    }
    return f"{authorization_endpoint}?{urlencode(params)}"


def _xai_oauth_exchange_code_for_tokens(
    *,
    token_endpoint: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    code_challenge: str,
    timeout_seconds: float = 20.0,
) -> Dict[str, Any]:
    if not code_verifier:
        raise AuthError("xAI token exchange refused locally: PKCE verifier is empty.", code="xai_pkce_verifier_missing")
    _xai_validate_oauth_endpoint(token_endpoint, field="token_endpoint")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": XAI_OAUTH_CLIENT_ID,
        "code_verifier": code_verifier,
    }
    if code_challenge:
        data["code_challenge"] = code_challenge
        data["code_challenge_method"] = "S256"
    try:
        response = requests.post(
            token_endpoint,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data=data,
            timeout=max(20.0, timeout_seconds),
        )
    except Exception as exc:
        raise AuthError(f"xAI token exchange failed: {exc}", code="xai_token_exchange_failed") from exc
    if response.status_code != 200:
        raise AuthError(
            f"xAI token exchange failed (HTTP {response.status_code}).",
            code="xai_token_exchange_failed",
            relogin_required=response.status_code in {400, 401},
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise AuthError("xAI token exchange returned invalid JSON.", code="xai_token_exchange_invalid") from exc
    if not isinstance(payload, dict):
        raise AuthError("xAI token exchange response was not a JSON object.", code="xai_token_exchange_invalid")
    return payload


def _refresh_xai_oauth_tokens(
    tokens: Dict[str, Any],
    *,
    token_endpoint: str,
    timeout_seconds: float = 20.0,
) -> Dict[str, Any]:
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not refresh_token:
        raise AuthError(
            "Grok OAuth refresh token is missing. Please log in again.",
            code="xai_auth_missing_refresh_token",
            relogin_required=True,
        )
    _xai_validate_oauth_endpoint(token_endpoint, field="token_endpoint")
    try:
        response = requests.post(
            token_endpoint,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data={
                "grant_type": "refresh_token",
                "client_id": XAI_OAUTH_CLIENT_ID,
                "refresh_token": refresh_token,
            },
            timeout=max(20.0, timeout_seconds),
        )
    except Exception as exc:
        raise AuthError(f"xAI token refresh failed: {exc}", code="xai_refresh_failed") from exc
    if response.status_code != 200:
        raise AuthError(
            f"xAI token refresh failed (HTTP {response.status_code}).",
            code="xai_refresh_failed",
            relogin_required=response.status_code in {400, 401},
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise AuthError("xAI token refresh returned invalid JSON.", code="xai_refresh_invalid_json") from exc
    if not isinstance(payload, dict):
        raise AuthError("xAI token refresh response was not a JSON object.", code="xai_refresh_invalid_response")

    updated = dict(tokens)
    refreshed_access = str(payload.get("access_token") or "").strip()
    if not refreshed_access:
        raise AuthError("xAI token refresh did not return an access_token.", code="xai_refresh_invalid_response")
    updated["access_token"] = refreshed_access
    if payload.get("refresh_token"):
        updated["refresh_token"] = str(payload["refresh_token"]).strip()
    if payload.get("expires_in") is not None:
        updated["expires_in"] = payload.get("expires_in")
    updated["token_type"] = str(payload.get("token_type") or updated.get("token_type") or "Bearer")
    expires_at = _token_expiry_from_payload(updated)
    if expires_at:
        updated["expires_at"] = expires_at
    return updated


def _xai_oauth_discovery(timeout_seconds: float = 15.0) -> Dict[str, str]:
    try:
        response = requests.get(
            XAI_OAUTH_DISCOVERY_URL,
            headers={"Accept": "application/json"},
            timeout=max(5.0, timeout_seconds),
        )
    except Exception as exc:
        raise AuthError(f"xAI OIDC discovery failed: {exc}", code="xai_discovery_failed") from exc
    if response.status_code != 200:
        raise AuthError(f"xAI OIDC discovery returned status {response.status_code}.", code="xai_discovery_failed")
    try:
        payload = response.json()
    except Exception as exc:
        raise AuthError("xAI OIDC discovery returned invalid JSON.", code="xai_discovery_invalid_json") from exc
    if not isinstance(payload, dict):
        raise AuthError("xAI OIDC discovery response was not a JSON object.", code="xai_discovery_incomplete")
    discovery = _sanitize_discovery(payload)
    if not discovery.get("authorization_endpoint") or not discovery.get("token_endpoint"):
        raise AuthError("xAI OIDC discovery response was missing required endpoints.", code="xai_discovery_incomplete")
    return discovery


def _sanitize_discovery(discovery: dict) -> Dict[str, str]:
    authorization_endpoint = str(discovery.get("authorization_endpoint") or "").strip()
    token_endpoint = str(discovery.get("token_endpoint") or "").strip()
    issuer = str(discovery.get("issuer") or XAI_OAUTH_ISSUER).strip()
    if issuer:
        _xai_validate_oauth_endpoint(issuer, field="issuer")
    return {
        "issuer": issuer or XAI_OAUTH_ISSUER,
        "authorization_endpoint": _xai_validate_oauth_endpoint(
            authorization_endpoint,
            field="authorization_endpoint",
        ),
        "token_endpoint": _xai_validate_oauth_endpoint(token_endpoint, field="token_endpoint"),
    }


def _xai_validate_oauth_endpoint(url: str, *, field: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise AuthError(f"xAI OIDC discovery returned an invalid {field}.", code="xai_discovery_invalid")
    host = (parsed.hostname or "").lower()
    if host != "x.ai" and not host.endswith(".x.ai"):
        raise AuthError(f"xAI OIDC discovery {field} is not on the xAI origin.", code="xai_discovery_invalid")
    return url


def _xai_validate_inference_base_url(value: str, *, fallback: str) -> str:
    candidate = (value or "").strip().rstrip("/")
    if not candidate:
        return fallback
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or (host != "x.ai" and not host.endswith(".x.ai")):
        logger.warning("Refusing non-xAI Grok OAuth base URL override; falling back to xAI API.")
        return fallback
    return candidate


def _xai_validate_loopback_redirect_uri(redirect_uri: str) -> None:
    parsed = urlparse(redirect_uri)
    if (
        parsed.scheme != "http"
        or parsed.hostname != XAI_OAUTH_REDIRECT_HOST
        or parsed.path != XAI_OAUTH_REDIRECT_PATH
        or not parsed.port
    ):
        raise AuthError("xAI OAuth redirect_uri must use the configured loopback callback.", code="xai_redirect_invalid")


def _xai_token_state_is_expiring(tokens: Dict[str, Any], access_token: str) -> bool:
    expires_at = _coerce_expires_at(tokens.get("expires_at")) or _access_token_exp(access_token)
    if not expires_at:
        return _xai_access_token_is_expiring(access_token, XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS)
    return float(expires_at) <= time.time() + XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS


def _xai_access_token_is_expiring(access_token: str, skew_seconds: int = 0) -> bool:
    exp = _access_token_exp(access_token)
    if not exp:
        return False
    return float(exp) <= time.time() + max(0, int(skew_seconds))


def _access_token_exp(access_token: str) -> int:
    payload = _jwt_payload(access_token)
    if not payload:
        return 0
    exp = payload.get("exp")
    return int(exp) if isinstance(exp, (int, float)) else 0


def _validate_id_token_nonce(id_token: str, expected_nonce: str) -> None:
    payload = _jwt_payload(id_token)
    nonce = str(payload.get("nonce") or "").strip() if payload else ""
    if nonce and nonce != expected_nonce:
        raise AuthError("xAI authorization failed: nonce mismatch.", code="xai_nonce_mismatch")


def _jwt_payload(access_token: str) -> Dict[str, Any]:
    if not isinstance(access_token, str) or "." not in access_token:
        return {}
    try:
        payload_b64 = access_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _token_expiry_from_payload(tokens: Dict[str, Any]) -> int:
    exp = _access_token_exp(str(tokens.get("access_token") or ""))
    if exp:
        return exp
    try:
        expires_in = int(float(tokens.get("expires_in") or 0))
    except Exception:
        expires_in = 0
    if expires_in > 0:
        return int(time.time() + expires_in)
    return 0


def _coerce_expires_at(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value))
        except ValueError:
            try:
                return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
            except Exception:
                return 0
    return 0


def _refresh_timeout_seconds() -> float:
    try:
        return float(os.getenv("GROK_OAUTH_REFRESH_TIMEOUT_SECONDS", "20"))
    except Exception:
        return 20.0


def _default_redirect_uri() -> str:
    return f"http://{XAI_OAUTH_REDIRECT_HOST}:{XAI_OAUTH_REDIRECT_PORT}{XAI_OAUTH_REDIRECT_PATH}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _logged_out_status(*, needs_reauth: bool = False) -> dict:
    return {
        "logged_in": False,
        "provider": "",
        "base_url": DEFAULT_XAI_OAUTH_BASE_URL,
        "email": "",
        "expires_at": 0,
        "needs_reauth": needs_reauth,
    }


def _safe_error_message(exc: Exception) -> str:
    if isinstance(exc, AuthError):
        return str(exc)
    return "Grok OAuth failed. Please retry login."
