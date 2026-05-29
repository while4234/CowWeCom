# encoding:utf-8

"""Proxy resolution for xAI/Grok HTTP calls."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, MutableMapping, Optional

from config import conf


def normalize_proxy_url(value: Any) -> str:
    proxy_url = str(value or "").strip()
    if not proxy_url:
        return ""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", proxy_url):
        return proxy_url
    return f"http://{proxy_url}"


def resolve_xai_proxy_url(environ: Optional[MutableMapping[str, str]] = None) -> str:
    env = environ if environ is not None else os.environ
    for value in (
        _config_value("grok_proxy"),
        env.get("GROK_PROXY"),
        _config_value("proxy"),
        _config_value("discord_proxy"),
        env.get("DISCORD_PROXY"),
    ):
        proxy_url = normalize_proxy_url(value)
        if proxy_url:
            return proxy_url
    return ""


def xai_request_proxies() -> Optional[Dict[str, str]]:
    proxy_url = resolve_xai_proxy_url()
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def xai_request_kwargs() -> Dict[str, Dict[str, str]]:
    proxies = xai_request_proxies()
    return {"proxies": proxies} if proxies else {}


def apply_xai_proxy_env(env: MutableMapping[str, str]) -> str:
    proxy_url = resolve_xai_proxy_url(env)
    if not proxy_url:
        return ""
    env.setdefault("GROK_PROXY", proxy_url)
    env.setdefault("HTTPS_PROXY", proxy_url)
    env.setdefault("HTTP_PROXY", proxy_url)
    return proxy_url


def _config_value(key: str) -> Any:
    try:
        return conf().get(key)
    except Exception:
        return ""
