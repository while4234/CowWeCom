# encoding:utf-8
"""web.py route adapter for knowledge backend APIs.

This module is intentionally import-light: the concrete backend lives in
``agent.knowledge.backend`` and is imported lazily from request handlers so the
web channel can compose these routes without creating startup import cycles.
"""

import dataclasses
import hmac
import json
import logging
from typing import Any, Dict, Iterable, List, Mapping, Tuple

import web

logger = logging.getLogger(__name__)


kb_backend_routes = (
    "/api/knowledge/v1", "KnowledgeBackendProviderHandler",
    "/api/knowledge/v1/(.*)", "KnowledgeBackendProviderHandler",
    "/api/knowledge/admin", "KnowledgeBackendAdminHandler",
    "/api/knowledge/admin/upload", "KnowledgeBackendAdminUploadHandler",
    "/api/knowledge/admin/(.*)", "KnowledgeBackendAdminHandler",
    "/api/knowledge/provider", "KnowledgeBackendProviderHandler",
    "/api/knowledge/provider/(.*)", "KnowledgeBackendProviderHandler",
    "/api/kb/admin", "KnowledgeBackendAdminHandler",
    "/api/kb/admin/upload", "KnowledgeBackendAdminUploadHandler",
    "/api/kb/admin/(.*)", "KnowledgeBackendAdminHandler",
    "/api/kb/provider", "KnowledgeBackendProviderHandler",
    "/api/kb/provider/(.*)", "KnowledgeBackendProviderHandler",
)

KB_BACKEND_ROUTES = kb_backend_routes
urls = kb_backend_routes


class BackendRouteError(Exception):
    def __init__(self, message: str, status: str = "400 Bad Request"):
        super().__init__(message)
        self.status = status


def _backend_module():
    from agent.knowledge import backend

    return backend


def _require_web_auth() -> None:
    config = _backend_config()
    security = getattr(config, "security", {}) or {}
    if not _enabled(security.get("require_web_auth", True)):
        return
    from channel.web.web_channel import _require_auth

    _require_auth()


def _backend_config() -> Any:
    backend = _backend_module()
    config_factory = getattr(backend, "KnowledgeBackendConfig", None)
    if config_factory is None or not hasattr(config_factory, "from_project_config"):
        return None
    return config_factory.from_project_config()


def _admin_api_enabled() -> bool:
    config = _backend_config()
    if config is None:
        return True
    if not _enabled(getattr(config, "admin_api_enabled", True)):
        return False
    security = getattr(config, "security", {}) or {}
    if not _enabled(security.get("disable_admin_api_when_web_password_empty", True)):
        return True
    try:
        from config import conf

        return bool(conf().get("web_password", ""))
    except Exception:
        return False


def _provider_api_enabled() -> bool:
    config = _backend_config()
    if config is None:
        return True
    return _enabled(getattr(config, "provider_api_enabled", False))


def _enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on", "enabled")


def _json_response(payload: Any, status: str = "200 OK") -> str:
    web.ctx.status = status
    web.header("Content-Type", "application/json; charset=utf-8")
    return json.dumps(_to_jsonable(payload), ensure_ascii=False)


def _json_error(message: str, status: str = "400 Bad Request", **extra: Any) -> str:
    payload = {"status": "error", "message": message}
    payload.update(extra)
    return _json_response(payload, status=status)


def _to_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _to_jsonable(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v) for v in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _to_jsonable(value.to_dict())
    return value


def _read_json_body() -> Dict[str, Any]:
    raw = web.data() or b""
    if not raw:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackendRouteError(f"Invalid JSON body: {exc}", "400 Bad Request")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BackendRouteError("JSON body must be an object", "400 Bad Request")
    return value


def _query_params() -> Dict[str, Any]:
    return dict(web.input())


def _request_payload() -> Dict[str, Any]:
    method = (web.ctx.method or "").upper()
    if method in ("GET", "DELETE"):
        return _query_params()

    content_type = (web.ctx.env.get("CONTENT_TYPE") or "").lower()
    if "multipart/form-data" in content_type:
        parsed = _read_multipart()
        return {"fields": parsed["fields"], "files": parsed["files"]}
    if "application/json" in content_type:
        payload = _read_json_body()
    else:
        payload = _query_params()
        if not payload and web.data():
            payload = _read_json_body()
    return payload


def _raw_web_input():
    rawinput = getattr(getattr(web, "webapi", None), "rawinput", None)
    if not callable(rawinput):
        raise BackendRouteError("web.py rawinput is not available", "500 Internal Server Error")
    try:
        return rawinput(method="post")
    except TypeError:
        return rawinput()


def _read_multipart() -> Dict[str, Any]:
    params = _raw_web_input()
    fields: Dict[str, Any] = {}
    files: List[Dict[str, Any]] = []

    for key, value in _iter_form_items(params):
        values = value if isinstance(value, list) else [value]
        for item in values:
            if _looks_like_file(item):
                files.append(_normalize_upload(key, item))
            else:
                _add_field_value(fields, key, item)

    return {"fields": fields, "files": files}


def _iter_form_items(params: Any) -> Iterable[Tuple[str, Any]]:
    if hasattr(params, "items") and callable(params.items):
        return params.items()
    return []


def _looks_like_file(value: Any) -> bool:
    return hasattr(value, "filename") or hasattr(value, "file")


def _add_field_value(fields: Dict[str, Any], key: str, value: Any) -> None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if key in fields:
        if not isinstance(fields[key], list):
            fields[key] = [fields[key]]
        fields[key].append(value)
    else:
        fields[key] = value


def _normalize_upload(field_name: str, file_obj: Any) -> Dict[str, Any]:
    content = _read_uploaded_file_bytes(file_obj)
    filename = getattr(file_obj, "filename", "") or getattr(file_obj, "name", "") or field_name
    content_type = (
        getattr(file_obj, "content_type", None)
        or getattr(file_obj, "type", None)
        or "application/octet-stream"
    )
    return {
        "field_name": field_name,
        "filename": filename,
        "content_type": content_type,
        "size": len(content),
        "content": content,
    }


def _read_uploaded_file_bytes(file_obj: Any) -> bytes:
    if isinstance(file_obj, bytes):
        return file_obj
    if isinstance(file_obj, str):
        return file_obj.encode("utf-8")

    content = None
    stream = getattr(file_obj, "file", None)
    if hasattr(stream, "read"):
        content = stream.read()
    elif hasattr(file_obj, "read"):
        content = file_obj.read()
    elif hasattr(file_obj, "value"):
        content = file_obj.value

    if content is None:
        raise BackendRouteError("Unable to read uploaded file", "400 Bad Request")
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8")
    raise BackendRouteError(f"Unsupported upload content type: {type(content).__name__}", "400 Bad Request")


def _authorization_bearer() -> str:
    header = web.ctx.env.get("HTTP_AUTHORIZATION") or ""
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


def _require_provider_bearer() -> None:
    token = _authorization_bearer()
    if not token:
        raise BackendRouteError("Missing bearer token", "401 Unauthorized")

    backend = _backend_module()
    verifier = _first_callable(
        backend,
        "verify_provider_bearer_token",
        "check_provider_bearer_token",
        "verify_provider_token",
        "check_provider_token",
    )
    if verifier is not None:
        if not bool(verifier(token)):
            raise BackendRouteError("Invalid bearer token", "403 Forbidden")
        return

    expected = _provider_bearer_token(backend)
    if not expected:
        raise BackendRouteError("Provider bearer token is not configured", "503 Service Unavailable")
    if not hmac.compare_digest(str(token), str(expected)):
        raise BackendRouteError("Invalid bearer token", "403 Forbidden")


def _provider_bearer_token(backend: Any) -> str:
    getter = _first_callable(
        backend,
        "get_provider_bearer_token",
        "get_provider_token",
        "provider_bearer_token",
        "provider_token",
    )
    if getter is not None:
        return str(getter() or "")

    for attr in ("PROVIDER_BEARER_TOKEN", "KNOWLEDGE_PROVIDER_BEARER_TOKEN", "provider_token"):
        value = getattr(backend, attr, None)
        if value:
            return str(value)

    try:
        from config import conf

        for key in ("knowledge_provider_bearer_token", "knowledge_provider_token", "kb_provider_token"):
            value = conf().get(key, "")
            if value:
                return str(value)
    except Exception:
        logger.debug("[KbBackendRoutes] Could not read provider token from config", exc_info=True)
    return ""


def _first_callable(module: Any, *names: str):
    for name in names:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    return None


def _dispatch_admin(method: str, path: str, payload: Dict[str, Any]) -> Any:
    backend = _backend_module()
    dispatcher = _first_callable(
        backend,
        "dispatch_admin_request",
        "handle_admin_request",
        "admin_dispatch",
        "admin_api",
    )
    if dispatcher is None:
        raise BackendRouteError("Knowledge admin backend dispatcher is unavailable", "501 Not Implemented")
    return _invoke_dispatch(dispatcher, method, path, payload)


def _dispatch_provider(method: str, path: str, payload: Dict[str, Any]) -> Any:
    backend = _backend_module()
    dispatcher = _first_callable(
        backend,
        "dispatch_provider_request",
        "handle_provider_request",
        "provider_dispatch",
        "provider_api",
    )
    if dispatcher is None:
        raise BackendRouteError("Knowledge provider backend dispatcher is unavailable", "501 Not Implemented")
    return _invoke_dispatch(dispatcher, method, path, payload)


def _invoke_dispatch(dispatcher: Any, method: str, path: str, payload: Dict[str, Any]) -> Any:
    try:
        return dispatcher(method=method, path=path, payload=payload)
    except TypeError as exc:
        logger.debug("[KbBackendRoutes] Keyword dispatch failed; retrying positional call: %s", exc)
    return dispatcher(method, path, payload)


def _handle_backend_error(exc: Exception) -> str:
    if isinstance(exc, BackendRouteError):
        return _json_error(str(exc), status=exc.status)
    status = getattr(exc, "http_status", None) or getattr(exc, "status", None) or "500 Internal Server Error"
    if isinstance(status, int):
        status = f"{status} Error"
    logger.error("[KbBackendRoutes] Request failed: %s", exc, exc_info=True)
    return _json_error(str(exc), status=str(status))


class KnowledgeBackendAdminHandler:
    def GET(self, path: str = ""):
        return self._handle("GET", path)

    def POST(self, path: str = ""):
        return self._handle("POST", path)

    def PUT(self, path: str = ""):
        return self._handle("PUT", path)

    def DELETE(self, path: str = ""):
        return self._handle("DELETE", path)

    def _handle(self, method: str, path: str):
        try:
            if not _admin_api_enabled():
                return _json_response(
                    {"status": "disabled", "message": "knowledge admin API is disabled"},
                    status="404 Not Found",
                )
            _require_web_auth()
            result = _dispatch_admin(method, path, _request_payload())
            return _json_response(result)
        except Exception as exc:
            return _handle_backend_error(exc)


class KnowledgeBackendAdminUploadHandler:
    def POST(self):
        try:
            if not _admin_api_enabled():
                return _json_response(
                    {"status": "disabled", "message": "knowledge admin API is disabled"},
                    status="404 Not Found",
                )
            _require_web_auth()
            payload = _request_payload()
            if not payload.get("files"):
                raise BackendRouteError("No files uploaded", "400 Bad Request")
            result = _dispatch_admin("POST", "upload", payload)
            return _json_response(result)
        except Exception as exc:
            return _handle_backend_error(exc)


class KnowledgeBackendProviderHandler:
    def GET(self, path: str = ""):
        return self._handle("GET", path)

    def POST(self, path: str = ""):
        return self._handle("POST", path)

    def PUT(self, path: str = ""):
        return self._handle("PUT", path)

    def DELETE(self, path: str = ""):
        return self._handle("DELETE", path)

    def _handle(self, method: str, path: str):
        try:
            if not _provider_api_enabled():
                return _json_response(
                    {"status": "disabled", "message": "knowledge provider API is disabled"},
                    status="404 Not Found",
                )
            _require_provider_bearer()
            result = _dispatch_provider(method, path, _request_payload())
            return _json_response(result)
        except Exception as exc:
            return _handle_backend_error(exc)
