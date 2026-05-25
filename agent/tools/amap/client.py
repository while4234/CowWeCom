"""HTTP client for AMap Web Service APIs."""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import requests

from common.log import logger


AMAP_BASE_URL = "https://restapi.amap.com"
KEY_ENV_CANDIDATES = (
    "AMAP_WEBSERVICE_KEY",
    "SKILL_AMAP_COWWECHAT_WEBSERVICE_KEY",
    "AMAP_KEY",
    "AMAP_API_KEY",
)


class MissingAmapKeyError(RuntimeError):
    """Raised when no AMap Web Service key is configured."""


class AmapApiError(RuntimeError):
    """Raised for HTTP or AMap business errors."""

    def __init__(
        self,
        message: str,
        *,
        info: str = "",
        infocode: str = "",
        status_code: Optional[int] = None,
        endpoint: str = "",
    ):
        super().__init__(message)
        self.info = info
        self.infocode = infocode
        self.status_code = status_code
        self.endpoint = endpoint

    def safe_message(self) -> str:
        detail = self.info or str(self)
        if self.infocode:
            return f"{detail}（infocode: {self.infocode}）"
        if self.status_code:
            return f"{detail}（HTTP {self.status_code}）"
        return detail

    def __str__(self) -> str:
        text = super().__str__()
        if self.infocode and self.infocode not in text:
            return f"{text}（infocode: {self.infocode}）"
        return text


def resolve_amap_key(explicit_key: str = "") -> str:
    if explicit_key:
        return explicit_key.strip()
    for env_name in KEY_ENV_CANDIDATES:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return ""


def mask_key(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}***{value[-4:]}"


class AmapClient:
    """Small AMap HTTP client with retry and response normalization."""

    def __init__(
        self,
        api_key: str = "",
        *,
        key: str = "",
        amap_key: str = "",
        config: Optional[Dict[str, Any]] = None,
        base_url: str = AMAP_BASE_URL,
        timeout: int = 12,
        retries: int = 2,
        max_retries: Optional[int] = None,
        session: Optional[requests.Session] = None,
    ):
        if config:
            api_key = api_key or config.get("api_key", "")
            timeout = int(config.get("timeout", timeout) or timeout)
            retries = int(config.get("retries", retries) or retries)
        api_key = api_key or key or amap_key
        self.api_key = resolve_amap_key(api_key)
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        if max_retries is not None:
            retries = max_retries
        self.retries = max(0, int(retries))
        self.session = session or requests.Session()

    def geocode(self, address: str, city: str = "") -> Dict[str, Any]:
        params: Dict[str, Any] = {"address": address}
        if city:
            params["city"] = city
        data = self.request("/v3/geocode/geo", params)
        geocodes = data.get("geocodes") or []
        if not geocodes:
            raise AmapApiError(
                f"地址解析失败: {address}",
                info="GEOCODE_NOT_FOUND",
                infocode=str(data.get("infocode") or ""),
                endpoint="/v3/geocode/geo",
            )
        return geocodes[0]

    def search_poi(self, keywords: str, city: str = "") -> Dict[str, Any]:
        params: Dict[str, Any] = {"keywords": keywords, "show_fields": "business"}
        if city:
            params["region"] = city
        try:
            data = self.request("/v5/place/text", params)
        except AmapApiError:
            v3_params: Dict[str, Any] = {"keywords": keywords, "extensions": "base"}
            if city:
                v3_params["city"] = city
            data = self.request("/v3/place/text", v3_params)
        pois = data.get("pois") or []
        if not pois:
            raise AmapApiError(
                f"POI 搜索失败: {keywords}",
                info="POI_NOT_FOUND",
                infocode=str(data.get("infocode") or ""),
            )
        return pois[0]

    poi_search = search_poi
    search_pois = search_poi

    def driving_route(self, origin: str, destination: str, strategy: Any = "", **kwargs) -> Dict[str, Any]:
        from agent.tools.amap.service import AmapService
        from agent.tools.amap.models import public_dict

        route = AmapService(client=self).route_plan(
            origin,
            destination,
            "driving",
            strategy=str(strategy) if strategy not in (None, "") else "",
            include_alternatives=True,
        )
        data = public_dict(route)
        if route.raw:
            data["raw"] = route.raw
        return data

    route_driving = driving_route
    driving = driving_route

    def request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.api_key:
            raise MissingAmapKeyError(
                "未配置高德 Web服务 Key，请设置 AMAP_WEBSERVICE_KEY。"
            )

        url = self._build_url(endpoint)
        safe_params = dict(params or {})
        safe_params["key"] = self.api_key

        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                response = self.session.get(url, params=safe_params, timeout=self.timeout)
                if response.status_code >= 500 and attempt < self.retries:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                if response.status_code >= 400:
                    raise AmapApiError(
                        f"高德接口 HTTP 错误: {response.status_code}",
                        status_code=response.status_code,
                        endpoint=endpoint,
                    )
                payload = response.json()
                self._raise_for_amap_error(payload, endpoint)
                return payload
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                time.sleep(0.4 * (attempt + 1))
            except ValueError as exc:
                raise AmapApiError("高德接口返回了无法解析的 JSON。", endpoint=endpoint) from exc

        logger.warning(
            "[AMap] Request failed after retries endpoint=%s key=%s error=%s",
            endpoint,
            mask_key(self.api_key),
            last_error,
        )
        raise AmapApiError(f"高德接口请求失败: {last_error}", endpoint=endpoint)

    def _build_url(self, endpoint: str) -> str:
        endpoint = str(endpoint or "").strip()
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            return endpoint
        return urljoin(self.base_url, endpoint.lstrip("/"))

    @staticmethod
    def _raise_for_amap_error(payload: Dict[str, Any], endpoint: str) -> None:
        status = str(payload.get("status", "1"))
        if status == "1":
            return

        info = str(payload.get("info") or payload.get("message") or "高德接口返回失败")
        infocode = str(payload.get("infocode") or payload.get("code") or "")
        raise AmapApiError(
            f"高德接口返回失败: {info}",
            info=info,
            infocode=infocode,
            endpoint=endpoint,
        )
