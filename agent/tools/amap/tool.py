"""Agent tool adapter for AMap Web Service capabilities."""

from __future__ import annotations

import os
from typing import Any, Dict, List

from agent.tools.base_tool import BaseTool, ToolResult
from agent.tools.amap.client import AmapApiError, AmapClient, MissingAmapKeyError
from agent.tools.amap.formatter import format_route
from agent.tools.amap.models import public_dict
from agent.tools.amap.service import (
    AmapService,
    AmapServiceError,
    format_commute_result,
    format_travel_result,
    parse_points_text,
)
from agent.tools.amap.state import AmapStateStore


class AmapTool(BaseTool):
    """High-level AMap commute, route, traffic, and travel analysis tool."""

    name: str = "amap"
    description: str = (
        "Use AMap Web Service APIs for Chinese commute, route planning, traffic, ETA, "
        "POI/geocoding, and travel route reasonableness analysis. "
        "Supports actions: set_profile_location, commute_status, route_plan, "
        "traffic_status, analyze_travel_route, geocode, reverse_geocode, poi_search."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "set_profile_location",
                    "commute_status",
                    "route_plan",
                    "traffic_status",
                    "analyze_travel_route",
                    "geocode",
                    "reverse_geocode",
                    "poi_search",
                ],
                "description": "AMap operation to run."
            },
            "location_type": {
                "type": "string",
                "description": "For set_profile_location: home or company."
            },
            "place": {
                "type": "string",
                "description": "Address, POI name, lon,lat, or alias such as 家/公司."
            },
            "origin": {
                "type": "string",
                "description": "Route origin address, POI, lon,lat, 家, or 公司."
            },
            "destination": {
                "type": "string",
                "description": "Route destination address, POI, lon,lat, 家, or 公司."
            },
            "direction": {
                "type": "string",
                "description": "Commute direction: home_to_company or company_to_home."
            },
            "mode": {
                "type": "string",
                "description": "Route mode: driving, transit, walking, bicycling, electrobike."
            },
            "city": {
                "type": "string",
                "description": "Default city/region hint for geocoding, POI, and transit."
            },
            "points": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Travel route points as names, addresses, or lon,lat values."
            },
            "text": {
                "type": "string",
                "description": "Travel route text, e.g. 北京：故宫、景山公园、南锣鼓巷."
            },
            "preferences": {
                "type": "object",
                "description": "Travel preferences, e.g. preserve_start/preserve_end booleans."
            },
            "include_raw": {
                "type": "boolean",
                "description": "Return provider raw payloads. Default false; avoid for WeChat summaries."
            },
        },
        "required": ["action"],
    }

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.cwd = self.config.get("cwd") or os.path.expanduser("~/cow")

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        try:
            action = str(args.get("action") or "").strip()
            if not action:
                return ToolResult.fail("Error: action is required.")
            service = self._create_service()

            if action == "set_profile_location":
                return self._set_profile_location(service, args)
            if action == "commute_status":
                result = service.get_commute_status(
                    args.get("direction") or "home_to_company",
                    origin=args.get("origin") or "",
                    destination=args.get("destination") or "",
                )
                return self._success(format_commute_result(result), result, args)
            if action == "route_plan":
                route = service.route_plan(
                    args.get("origin") or "",
                    args.get("destination") or "",
                    args.get("mode") or "driving",
                    city=args.get("city") or "",
                    include_alternatives=True,
                )
                return self._success(format_route(route, title="推荐"), route, args)
            if action == "traffic_status":
                route = service.traffic_status(
                    args.get("origin") or args.get("place") or "",
                    args.get("destination") or "",
                    city=args.get("city") or "",
                )
                return self._success(format_route(route, title="路况"), route, args)
            if action == "analyze_travel_route":
                return self._analyze_travel_route(service, args)
            if action == "geocode":
                point = service.geocode(args.get("place") or "", args.get("city") or "")
                return self._success(f"{point.name}\n坐标：{point.location}", point, args)
            if action == "reverse_geocode":
                point = service.reverse_geocode(args.get("place") or args.get("origin") or "")
                return self._success(f"{point.location}\n地址：{point.address}", point, args)
            if action == "poi_search":
                point = service.poi_search(args.get("place") or "", args.get("city") or "")
                return self._success(f"{point.name}\n地址：{point.address}\n坐标：{point.location}", point, args)
            return ToolResult.fail(f"Error: unsupported amap action '{action}'.")
        except MissingAmapKeyError:
            return ToolResult.fail("未配置高德 Web服务 Key。请在本机设置 AMAP_WEBSERVICE_KEY，或使用 env_config 安全写入该环境变量。")
        except AmapApiError as exc:
            return ToolResult.fail(f"高德接口错误：{exc.safe_message()}")
        except AmapServiceError as exc:
            return ToolResult.fail(str(exc))
        except Exception as exc:
            return ToolResult.fail(f"高德工具执行失败：{exc}")

    def _create_service(self) -> AmapService:
        api_key = self.config.get("api_key", "")
        cache_dir = self.config.get("cache_dir") or os.path.join(self.cwd, "data", "amap-cowwechat")
        client = AmapClient(
            api_key=api_key,
            timeout=int(self.config.get("timeout", 12) or 12),
            retries=int(self.config.get("retries", 2) or 2),
        )
        return AmapService(
            client=client,
            state=AmapStateStore(cache_dir),
            default_city=self.config.get("default_city", ""),
            default_adcode=self.config.get("default_adcode", ""),
        )

    def _set_profile_location(self, service: AmapService, args: Dict[str, Any]) -> ToolResult:
        kind = args.get("location_type") or ""
        place = args.get("place") or args.get("origin") or ""
        if not kind:
            return ToolResult.fail("请指定 location_type 为 home 或 company。")
        if not place:
            return ToolResult.fail("请提供家或公司的详细地址/坐标。")
        point = service.set_profile_location(kind, place, args.get("city") or "")
        label = "公司" if str(kind).lower() in ("company", "work", "office") or str(kind) == "公司" else "家"
        return self._success(f"已设置{label}：{point.address or point.name}\n坐标：{point.location}", point, args)

    def _analyze_travel_route(self, service: AmapService, args: Dict[str, Any]) -> ToolResult:
        city = str(args.get("city") or "").strip()
        points: List[str] = [str(item).strip() for item in (args.get("points") or []) if str(item).strip()]
        if not points and args.get("text"):
            parsed_city, parsed_points = parse_points_text(args.get("text") or "")
            city = city or parsed_city
            points = parsed_points
        result = service.analyze_travel_route(points, city, args.get("preferences") or {})
        return self._success(format_travel_result(result), result, args)

    @staticmethod
    def _success(summary: str, payload: Any, args: Dict[str, Any]) -> ToolResult:
        data = public_dict(payload)
        if args.get("include_raw") and hasattr(payload, "raw"):
            data["raw"] = getattr(payload, "raw")
        return ToolResult.success({"summary": summary, "data": data})
