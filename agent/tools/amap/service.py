"""Business service for AMap commute, route, traffic, and travel analysis."""

from __future__ import annotations

import datetime as dt
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from agent.tools.amap.client import AmapApiError, AmapClient
from agent.tools.amap.formatter import format_commute, format_traffic_status, format_travel_analysis
from agent.tools.amap.models import (
    CommuteResult,
    CongestionSegment,
    GeoPoint,
    RoutePlan,
    RouteStep,
    TrafficRoad,
    TrafficStatusResult,
    TravelLeg,
    TravelRouteAnalysis,
)
from agent.tools.amap.state import AmapStateStore


LONLAT_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
ROUTE_SHOW_FIELDS = "cost,tmcs,navi,polyline,cities"
TRAFFIC_STATUS_EXTENSIONS = "all"
DEFAULT_TRAFFIC_LEVEL = 5
DRIVING_STRATEGIES: List[Tuple[str, str]] = [
    ("32", "高德推荐"),
    ("33", "躲避拥堵"),
    ("38", "速度最快"),
    ("36", "费用较低"),
]

STATUS_LABELS = {
    "unknown": "未知",
    "smooth": "畅通",
    "slow": "缓行",
    "congested": "拥堵",
    "severe_congested": "严重拥堵",
}

STATUS_SEVERITY = {
    "unknown": 0,
    "smooth": 0,
    "slow": 1,
    "congested": 2,
    "severe_congested": 3,
}


class AmapServiceError(RuntimeError):
    """Domain-level AMap tool error."""


class AmapService:
    """Coordinates AMap APIs and normalizes business results."""

    def __init__(
        self,
        client: Optional[AmapClient] = None,
        state: Optional[AmapStateStore] = None,
        *,
        default_city: str = "",
        default_adcode: str = "",
        enable_advanced_traffic: Optional[bool] = None,
    ):
        self.client = client or AmapClient()
        self.state = state or AmapStateStore()
        self.default_city = (
            default_city
            or os.environ.get("AMAP_DEFAULT_CITY", "").strip()
            or os.environ.get("SKILL_AMAP_COWWECHAT_DEFAULT_CITY", "").strip()
        )
        self.default_adcode = (
            default_adcode
            or os.environ.get("AMAP_DEFAULT_ADCODE", "").strip()
            or os.environ.get("SKILL_AMAP_COWWECHAT_DEFAULT_ADCODE", "").strip()
        )
        if enable_advanced_traffic is None:
            enable_advanced_traffic = _parse_bool(
                os.environ.get("AMAP_ENABLE_ADVANCED_TRAFFIC")
                or os.environ.get("SKILL_AMAP_COWWECHAT_ENABLE_ADVANCED_TRAFFIC")
                or "false"
            )
        else:
            enable_advanced_traffic = _parse_bool(enable_advanced_traffic)
        self.enable_advanced_traffic = enable_advanced_traffic

    def geocode(self, address: str, city: str = "") -> GeoPoint:
        address = str(address or "").strip()
        if not address:
            raise AmapServiceError("地址不能为空。")
        city = (city or self.default_city).strip()

        cached = self.state.get_cached_geocode(address, city)
        if cached:
            return cached

        params: Dict[str, Any] = {"address": address}
        if city:
            params["city"] = city
        data = self.client.request("/v3/geocode/geo", params)
        geocodes = data.get("geocodes") or []
        if not geocodes:
            raise AmapServiceError(f"地址解析失败：{address}。请提供更完整的地址。")
        first = geocodes[0]
        location = str(first.get("location") or "").strip()
        if not location:
            raise AmapServiceError(f"地址解析失败：{address} 未返回坐标。")
        point = GeoPoint(
            name=str(first.get("formatted_address") or address),
            location=location,
            address=str(first.get("formatted_address") or address),
            city=str(first.get("city") or city),
            adcode=str(first.get("adcode") or ""),
        )
        self.state.set_cached_geocode(address, city, point)
        return point

    def reverse_geocode(self, location: str) -> GeoPoint:
        location = self._normalize_lonlat(location)
        data = self.client.request(
            "/v3/geocode/regeo",
            {"location": location, "extensions": "base"},
        )
        regeocode = data.get("regeocode") or {}
        comp = regeocode.get("addressComponent") or {}
        address = str(regeocode.get("formatted_address") or location)
        return GeoPoint(
            name=address,
            location=location,
            address=address,
            city=str(comp.get("city") or comp.get("province") or ""),
            adcode=str(comp.get("adcode") or ""),
        )

    @staticmethod
    def normalize_congestion_status(status: Any) -> str:
        normalized = normalize_congestion_status(status)
        return "severe" if normalized == "severe_congested" else normalized

    _normalize_congestion_status = normalize_congestion_status

    def poi_search(self, keywords: str, city: str = "") -> GeoPoint:
        keywords = str(keywords or "").strip()
        if not keywords:
            raise AmapServiceError("POI 关键词不能为空。")
        city = (city or self.default_city).strip()

        try:
            return self._poi_search_v5(keywords, city)
        except AmapApiError:
            return self._poi_search_v3(keywords, city)
        except AmapServiceError:
            return self._poi_search_v3(keywords, city)

    def resolve_point(self, value: str, city: str = "", *, prefer_poi: bool = False) -> GeoPoint:
        raw = str(value or "").strip()
        if not raw:
            raise AmapServiceError("地点不能为空。")

        alias = _normalize_place_alias(raw)
        if alias:
            point = self.state.get_profile_location(alias)
            if point:
                return point
            address_env = os.environ.get("AMAP_HOME_ADDRESS" if alias == "home" else "AMAP_COMPANY_ADDRESS", "").strip()
            if address_env:
                try:
                    point = self.geocode(address_env, city)
                except Exception:
                    point = self.poi_search(address_env, city)
                point.name = "家" if alias == "home" else "公司"
                return point
            raise AmapServiceError(f"还没有配置{'家' if alias == 'home' else '公司'}，请先发送“高德 设置{'家' if alias == 'home' else '公司'} 详细地址”。")

        if LONLAT_RE.match(raw):
            return GeoPoint(name=raw, location=self._normalize_lonlat(raw), address=raw)

        if prefer_poi:
            try:
                return self.poi_search(raw, city)
            except Exception:
                return self.geocode(raw, city)
        return self.geocode(raw, city)

    def set_profile_location(self, kind: str, place: str, city: str = "") -> GeoPoint:
        point = self.resolve_point(place, city, prefer_poi=True)
        normalized = "company" if _normalize_place_alias(kind) == "company" else "home"
        point.name = "公司" if normalized == "company" else "家"
        self.state.set_profile_location(normalized, point)
        self.state.clear_cached_geocode(point.address or place, city or self.default_city)
        return point

    def score_routes(self, origin: str, destination: str, strategies: Sequence[Any]) -> List[Dict[str, Any]]:
        scored: List[Dict[str, Any]] = []
        for strategy in strategies:
            route = self.client.driving_route(origin=origin, destination=destination, strategy=strategy)
            item = dict(route) if isinstance(route, dict) else {"route": route}
            duration = _to_int(item.get("duration") or item.get("duration_seconds"))
            congestion_score = _to_float(item.get("congestion_score"))
            item["score"] = duration + congestion_score * 300
            scored.append(item)
        return sorted(scored, key=lambda item: item.get("score", 0))

    rank_driving_strategies = score_routes
    compare_driving_strategies = score_routes

    def plan_route(
        self,
        origin: str,
        destination: str,
        origin_city: str = "",
        destination_city: str = "",
        mode: str = "auto",
        **kwargs,
    ) -> Dict[str, Any]:
        origin_point = self._compat_geocode(origin, origin_city)
        destination_point = self._compat_geocode(destination, destination_city)
        if _is_cross_city(origin_point, destination_point) or (
            origin_city and destination_city and origin_city != destination_city
        ):
            return {
                "type": "cross_city",
                "warning": "跨城市路线需要接入火车、航班或长途交通，高德同城路线 API 不能完整解决。",
                "origin": origin_point.__dict__,
                "destination": destination_point.__dict__,
                "suggested_modes": ["rail", "flight", "long_distance_bus", "driving", "transit"],
            }
        route = self.route_plan(origin_point.location, destination_point.location, mode if mode != "auto" else "driving")
        return {"route": route, "mode": route.mode}

    route = plan_route
    plan_trip = plan_route

    def route_plan(
        self,
        origin: str,
        destination: str,
        mode: str = "driving",
        *,
        city: str = "",
        strategy: str = "",
        include_alternatives: bool = False,
    ) -> RoutePlan:
        origin_point = self.resolve_point(origin, city, prefer_poi=True)
        destination_point = self.resolve_point(destination, city, prefer_poi=True)
        plans = self._request_routes(
            origin_point,
            destination_point,
            mode=mode,
            strategy=strategy,
            include_alternatives=include_alternatives,
        )
        if not plans:
            raise AmapServiceError("高德未返回可用路线。")
        return plans[0]

    def traffic_status(
        self,
        origin: str = "",
        destination: str = "",
        city: str = "",
        *,
        query_type: str = "auto",
        road_name: str = "",
        location: str = "",
        radius: int = 1000,
        rectangle: str = "",
        adcode: str = "",
        level: int = DEFAULT_TRAFFIC_LEVEL,
    ) -> Union[RoutePlan, TrafficStatusResult]:
        query_type = str(query_type or "auto").strip().lower()
        if query_type in ("road", "道路"):
            query = road_name or origin
            try:
                return self.advanced_traffic_road(query, city=city, adcode=adcode, level=level)
            except AmapApiError as exc:
                return _advanced_traffic_unavailable("road", query, exc)
        if query_type in ("circle", "nearby", "附近", "圆形区域"):
            query_location = location or origin
            if not LONLAT_RE.match(str(query_location or "")):
                query_location = self.resolve_point(query_location, city, prefer_poi=True).location
            try:
                return self.advanced_traffic_circle(query_location, radius=radius, level=level)
            except AmapApiError as exc:
                return _advanced_traffic_unavailable("circle", str(query_location), exc)
        if query_type in ("rectangle", "rect", "矩形区域"):
            query = rectangle or origin
            try:
                return self.advanced_traffic_rectangle(query, level=level)
            except AmapApiError as exc:
                return _advanced_traffic_unavailable("rectangle", query, exc)

        if destination:
            return self.route_plan(origin, destination, "driving", city=city, include_alternatives=True)

        if not self.enable_advanced_traffic:
            raise AmapServiceError("高级交通态势未开启。请设置 AMAP_ENABLE_ADVANCED_TRAFFIC=true 后重启，或输入“高德 路线 起点 到 终点”使用基础路况。")
        place = road_name or origin or location
        if LONLAT_RE.match(str(place or "")):
            try:
                return self.advanced_traffic_circle(place, radius=radius, level=level)
            except AmapApiError as exc:
                return _advanced_traffic_unavailable("circle", str(place), exc)
        try:
            return self.advanced_traffic_road(place, city=city, adcode=adcode, level=level)
        except AmapApiError as exc:
            return _advanced_traffic_unavailable("road", str(place), exc)

    def advanced_traffic_road(
        self,
        road_name: str,
        *,
        city: str = "",
        adcode: str = "",
        level: int = DEFAULT_TRAFFIC_LEVEL,
    ) -> TrafficStatusResult:
        self._ensure_advanced_traffic_enabled()
        road_name = str(road_name or "").strip()
        if not road_name:
            raise AmapServiceError("道路名不能为空，例如“东三环”。")
        resolved_adcode = str(adcode or self.default_adcode or "").strip()
        resolved_city = str(city or self.default_city or "").strip()
        if not resolved_adcode and not resolved_city:
            raise AmapServiceError("道路交通态势需要 city 或 adcode，建议配置 AMAP_DEFAULT_ADCODE。")
        params: Dict[str, Any] = {
            "name": road_name,
            "level": _traffic_level(level),
            "extensions": TRAFFIC_STATUS_EXTENSIONS,
        }
        if resolved_adcode:
            params["adcode"] = resolved_adcode
        else:
            params["city"] = resolved_city
        data = self.client.request("/v3/traffic/status/road", params)
        return self._parse_traffic_status(data, "road", road_name)

    def advanced_traffic_circle(
        self,
        location: str,
        *,
        radius: int = 1000,
        level: int = DEFAULT_TRAFFIC_LEVEL,
    ) -> TrafficStatusResult:
        self._ensure_advanced_traffic_enabled()
        normalized_location = self._normalize_lonlat(location)
        bounded_radius = max(1, min(4999, _to_int(radius) or 1000))
        data = self.client.request(
            "/v3/traffic/status/circle",
            {
                "location": normalized_location,
                "radius": bounded_radius,
                "level": _traffic_level(level),
                "extensions": TRAFFIC_STATUS_EXTENSIONS,
            },
        )
        return self._parse_traffic_status(data, "circle", f"{normalized_location} 半径 {bounded_radius} 米")

    def advanced_traffic_rectangle(
        self,
        rectangle: str,
        *,
        level: int = DEFAULT_TRAFFIC_LEVEL,
    ) -> TrafficStatusResult:
        self._ensure_advanced_traffic_enabled()
        normalized_rectangle = _normalize_rectangle(rectangle)
        data = self.client.request(
            "/v3/traffic/status/rectangle",
            {
                "rectangle": normalized_rectangle,
                "level": _traffic_level(level),
                "extensions": TRAFFIC_STATUS_EXTENSIONS,
            },
        )
        return self._parse_traffic_status(data, "rectangle", normalized_rectangle)

    def _ensure_advanced_traffic_enabled(self) -> None:
        if not self.enable_advanced_traffic:
            raise AmapServiceError("高级交通态势未开启。请设置 AMAP_ENABLE_ADVANCED_TRAFFIC=true 后重启。")

    def _parse_traffic_status(
        self,
        data: Dict[str, Any],
        query_type: str,
        query: str,
    ) -> TrafficStatusResult:
        traffic_info = data.get("trafficinfo") or data.get("trafficInfo") or {}
        if not isinstance(traffic_info, dict):
            traffic_info = {}
        evaluation = traffic_info.get("evaluation") or data.get("evaluation") or {}
        if not isinstance(evaluation, dict):
            evaluation = {}

        description = str(
            traffic_info.get("description")
            or evaluation.get("description")
            or ""
        )
        status = normalize_congestion_status(evaluation.get("status") or traffic_info.get("status"))
        roads = _parse_traffic_roads(traffic_info.get("roads") or data.get("roads") or [])
        warning = ""
        if not roads:
            warning = "未返回道路明细，可能是接口仅返回基础态势或该区域暂无道路数据。"

        return TrafficStatusResult(
            query_type=query_type,
            query=query,
            description=description,
            status=status,
            expedite_percent=_to_float(evaluation.get("expedite")),
            slow_percent=_to_float(evaluation.get("congested")),
            congested_percent=_to_float(evaluation.get("blocked")),
            unknown_percent=_to_float(evaluation.get("unknown")),
            roads=roads,
            warning=warning,
            raw=data,
        )

    def get_commute_status(
        self,
        direction: str = "home_to_company",
        *,
        origin: str = "",
        destination: str = "",
    ) -> CommuteResult:
        direction = str(direction or "home_to_company").strip()
        if origin and destination:
            origin_point = self.resolve_point(origin, prefer_poi=True)
            destination_point = self.resolve_point(destination, prefer_poi=True)
        elif direction in ("company_to_home", "下班", "公司到家"):
            origin_point = self.resolve_point("公司")
            destination_point = self.resolve_point("家")
        else:
            origin_point = self.resolve_point("家")
            destination_point = self.resolve_point("公司")

        plans: List[RoutePlan] = []
        for strategy, label in DRIVING_STRATEGIES:
            try:
                routes = self._request_routes(
                    origin_point,
                    destination_point,
                    mode="driving",
                    strategy=strategy,
                    strategy_label=label,
                    include_alternatives=True,
                )
                plans.extend(routes)
            except AmapApiError:
                continue

        unique = _dedupe_routes(plans)
        if not unique:
            raise AmapServiceError("没有获取到可用通勤路线。")

        for plan in unique:
            plan.score = self._score_route(plan)
        ordered = sorted(unique, key=lambda route: route.score)
        result = CommuteResult(
            from_name=origin_point.name,
            to_name=destination_point.name,
            recommended_route=ordered[0],
            alternatives=ordered[1:],
            eta=ordered[0].eta,
        )
        result.summary_text = self._commute_summary(result)
        return result

    def analyze_travel_route(
        self,
        points: Sequence[str],
        city: str = "",
        preferences: Optional[Dict[str, Any]] = None,
    ) -> TravelRouteAnalysis:
        original = [str(point).strip() for point in points if str(point).strip()]
        if len(original) < 2:
            raise AmapServiceError("旅游路线至少需要两个地点。")
        preferences = preferences or {}

        resolved = [self.resolve_point(point, city, prefer_poi=True) for point in original]
        preserve_start = bool(preferences.get("preserve_start", False))
        preserve_end = bool(preferences.get("preserve_end", False))
        ordered = self._optimize_order(resolved, preserve_start=preserve_start, preserve_end=preserve_end)

        legs: List[TravelLeg] = []
        warnings: List[str] = []
        suggestions: List[str] = []
        total_duration = 0
        total_distance = 0

        for origin_point, destination_point in zip(ordered, ordered[1:]):
            if _is_cross_city(origin_point, destination_point):
                warning = "疑似跨城市路线，高德同城路线 API 不能完整覆盖火车/航班/长途交通。"
                warnings.append(f"{origin_point.name} → {destination_point.name}：{warning}")
                suggestions.append("跨城市段建议接入火车、航班或长途交通能力后再精排。")
                legs.append(TravelLeg(origin_point, destination_point, "long_distance", None, [], warning))
                continue

            leg = self._choose_travel_leg(origin_point, destination_point)
            legs.append(leg)
            if leg.route:
                total_duration += leg.route.duration_seconds
                total_distance += leg.route.distance_meters
                if leg.route.duration_seconds > 90 * 60:
                    warnings.append(f"{origin_point.name} → {destination_point.name} 驾车或通勤时间超过 90 分钟，建议拆分。")
                severe_count = sum(1 for seg in leg.route.congestion_segments if seg.status == "severe_congested")
                if leg.recommended_mode == "driving" and severe_count >= 2:
                    warnings.append(f"{origin_point.name} → {destination_point.name} 严重拥堵路段较多，建议比较地铁/公交。")
            if leg.warning:
                warnings.append(f"{origin_point.name} → {destination_point.name}：{leg.warning}")

        if len(ordered) > 5:
            warnings.append("单日景点超过 5 个，行程可能过密。")
            suggestions.append("建议拆成两天，或删减低优先级景点。")
        if total_duration > 3 * 3600:
            warnings.append("单日交通总耗时超过 3 小时，交通成本偏高。")
            suggestions.append("建议按区域拆分行程，减少跨区移动。")
        if self._has_backtracking(ordered):
            warnings.append("路线存在明显折返，建议调整顺序。")

        score = self._travel_score(len(ordered), total_duration, warnings)
        analysis = TravelRouteAnalysis(
            original_points=original,
            resolved_points=resolved,
            recommended_order=ordered,
            legs=legs,
            total_duration_seconds=total_duration,
            total_distance_meters=total_distance,
            reasonableness_score=score,
            warnings=_dedupe_text(warnings),
            suggestions=_dedupe_text(suggestions),
        )
        analysis.summary_text = format_travel_analysis(analysis)
        return analysis

    def _poi_search_v5(self, keywords: str, city: str = "") -> GeoPoint:
        params: Dict[str, Any] = {"keywords": keywords, "show_fields": "business"}
        if city:
            params["region"] = city
        data = self.client.request("/v5/place/text", params)
        pois = data.get("pois") or []
        if not pois:
            raise AmapServiceError(f"未找到 POI：{keywords}")
        return _point_from_poi(pois[0], keywords)

    def _compat_geocode(self, address: str, city: str = "") -> GeoPoint:
        if hasattr(self.client, "geocode") and not isinstance(self.client, AmapClient):
            data = self.client.geocode(address, city=city)
            return GeoPoint(
                name=str(data.get("formatted_address") or address),
                location=str(data.get("location") or ""),
                address=str(data.get("formatted_address") or address),
                city=str(data.get("city") or city),
                adcode=str(data.get("adcode") or ""),
            )
        return self.geocode(address, city)

    def _poi_search_v3(self, keywords: str, city: str = "") -> GeoPoint:
        params: Dict[str, Any] = {"keywords": keywords, "offset": 10, "page": 1, "extensions": "base"}
        if city:
            params["city"] = city
        data = self.client.request("/v3/place/text", params)
        pois = data.get("pois") or []
        if not pois:
            raise AmapServiceError(f"未找到 POI：{keywords}")
        return _point_from_poi(pois[0], keywords)

    def _request_routes(
        self,
        origin: GeoPoint,
        destination: GeoPoint,
        *,
        mode: str,
        strategy: str = "",
        strategy_label: str = "",
        include_alternatives: bool = False,
    ) -> List[RoutePlan]:
        mode = _normalize_mode(mode)
        endpoint = {
            "driving": "/v5/direction/driving",
            "walking": "/v5/direction/walking",
            "bicycling": "/v5/direction/bicycling",
            "electrobike": "/v5/direction/electrobike",
            "transit": "/v5/direction/transit/integrated",
        }.get(mode)
        if not endpoint:
            raise AmapServiceError(f"不支持的路线方式：{mode}")

        params: Dict[str, Any] = {
            "origin": origin.location,
            "destination": destination.location,
            "show_fields": ROUTE_SHOW_FIELDS,
        }
        if mode == "driving":
            if strategy:
                params["strategy"] = strategy
            if include_alternatives:
                params["alternative_route"] = 3
        if mode == "transit":
            city = origin.city or destination.city or self.default_city
            if city:
                params["city1"] = city
                params["city2"] = destination.city or city

        data = self.client.request(endpoint, params)
        return self._parse_routes(data, mode, origin, destination, strategy_label or _strategy_label(strategy, mode))

    def _parse_routes(
        self,
        data: Dict[str, Any],
        mode: str,
        origin: GeoPoint,
        destination: GeoPoint,
        strategy_label: str,
    ) -> List[RoutePlan]:
        route = data.get("route") or {}
        if mode == "transit":
            transits = route.get("transits") or data.get("transits") or []
            return [
                self._parse_transit_route(item, origin, destination, strategy_label)
                for item in transits
            ]

        paths = route.get("paths") or data.get("paths") or []
        return [
            self._parse_path(item, mode, origin, destination, strategy_label)
            for item in paths
        ]

    def _parse_path(
        self,
        path: Dict[str, Any],
        mode: str,
        origin: GeoPoint,
        destination: GeoPoint,
        strategy_label: str,
    ) -> RoutePlan:
        cost = path.get("cost") or {}
        distance = _to_int(path.get("distance"))
        duration = _to_int(cost.get("duration") or path.get("duration"))
        steps = _parse_steps(path.get("steps") or [])
        segments = _collect_congestion_segments(path, steps)
        plan = RoutePlan(
            mode=mode,
            origin=origin,
            destination=destination,
            distance_meters=distance,
            duration_seconds=duration,
            eta=_eta(duration),
            strategy=strategy_label or str(path.get("strategy") or ""),
            tolls=_to_float(cost.get("tolls") or path.get("tolls")),
            traffic_lights=_to_int(cost.get("traffic_lights") or path.get("traffic_lights")),
            congestion_summary=_summarize_congestion(segments),
            congestion_segments=_important_congestion_segments(segments),
            steps=steps,
            polyline=_path_polyline(path, steps),
            raw=path,
        )
        plan.score = self._score_route(plan)
        return plan

    def _parse_transit_route(
        self,
        item: Dict[str, Any],
        origin: GeoPoint,
        destination: GeoPoint,
        strategy_label: str,
    ) -> RoutePlan:
        duration = _to_int(item.get("duration") or (item.get("cost") or {}).get("duration"))
        distance = _to_int(item.get("distance") or item.get("walking_distance"))
        return RoutePlan(
            mode="transit",
            origin=origin,
            destination=destination,
            distance_meters=distance,
            duration_seconds=duration,
            eta=_eta(duration),
            strategy=strategy_label or "公交/地铁",
            congestion_summary="公交/地铁方案",
            steps=[],
            polyline="",
            raw=item,
        )

    def _score_route(self, route: RoutePlan) -> float:
        slow_m = sum(seg.distance_meters for seg in _all_segments(route) if seg.status == "slow")
        congested_m = sum(seg.distance_meters for seg in _all_segments(route) if seg.status == "congested")
        severe_m = sum(seg.distance_meters for seg in _all_segments(route) if seg.status == "severe_congested")
        return (
            route.duration_seconds
            + (slow_m / 1000.0) * 60
            + (congested_m / 1000.0) * 180
            + (severe_m / 1000.0) * 360
            + route.traffic_lights * 5
            + route.tolls * 0.5
        )

    def _commute_summary(self, result: CommuteResult) -> str:
        route = result.recommended_route
        severe = sum(1 for seg in route.congestion_segments if seg.status == "severe_congested")
        congested = sum(1 for seg in route.congestion_segments if seg.status == "congested")
        if severe:
            return "建议预留缓冲时间：当前存在严重拥堵路段。"
        if congested:
            return "建议关注路况变化：当前存在拥堵路段。"
        return "当前路线整体可走，按 ETA 出发即可。"

    def _choose_travel_leg(self, origin: GeoPoint, destination: GeoPoint) -> TravelLeg:
        driving = self._safe_single_route(origin, destination, "driving")
        distance = driving.distance_meters if driving else _haversine_meters(origin.location, destination.location)
        candidate_modes = _candidate_modes(distance)
        routes: List[RoutePlan] = []
        for mode in candidate_modes:
            route = driving if mode == "driving" and driving else self._safe_single_route(origin, destination, mode)
            if route:
                routes.append(route)

        if not routes:
            return TravelLeg(origin, destination, "driving", driving, [], "未获取到稳定路线，建议手动确认。")

        preferred = sorted(routes, key=lambda route: (route.duration_seconds, _mode_priority(route.mode)))[0]
        warning = ""
        if preferred.mode == "transit" and preferred.duration_seconds > 75 * 60:
            warning = "公交/地铁耗时较长，可考虑打车/驾车。"
        if preferred.mode in ("walking", "bicycling") and distance > 2500:
            warning = "步行或骑行距离较长，注意体力和天气。"
        return TravelLeg(origin, destination, preferred.mode, preferred, routes, warning)

    def _safe_single_route(self, origin: GeoPoint, destination: GeoPoint, mode: str) -> Optional[RoutePlan]:
        try:
            routes = self._request_routes(origin, destination, mode=mode)
        except Exception:
            return None
        return routes[0] if routes else None

    def _optimize_order(self, points: List[GeoPoint], *, preserve_start: bool, preserve_end: bool) -> List[GeoPoint]:
        if len(points) > 10:
            return list(points)
        if len(points) <= 2:
            return list(points)

        start = points[0] if preserve_start else points[0]
        end = points[-1] if preserve_end else None
        pool = list(points[1:-1] if preserve_end else points[1:])
        ordered = [start]
        while pool:
            current = ordered[-1]
            next_point = min(pool, key=lambda p: _haversine_meters(current.location, p.location))
            pool.remove(next_point)
            ordered.append(next_point)
        if end:
            ordered.append(end)
        return _two_opt(ordered, keep_end=preserve_end)

    def _has_backtracking(self, points: Sequence[GeoPoint]) -> bool:
        if len(points) < 4:
            return False
        original_distance = _route_distance(points)
        optimized_distance = _route_distance(_two_opt(list(points), keep_end=False))
        return optimized_distance and original_distance > optimized_distance * 1.25

    @staticmethod
    def _travel_score(point_count: int, total_duration: int, warnings: Sequence[str]) -> int:
        score = 100
        if point_count > 5:
            score -= (point_count - 5) * 8
        if total_duration > 3 * 3600:
            score -= int((total_duration - 3 * 3600) / 900) * 4
        score -= min(30, len(warnings) * 6)
        return max(0, min(100, score))

    @staticmethod
    def _normalize_lonlat(value: str) -> str:
        match = LONLAT_RE.match(str(value or ""))
        if not match:
            raise AmapServiceError(f"经纬度格式无效：{value}，应为 lng,lat。")
        return f"{float(match.group(1)):.6f},{float(match.group(2)):.6f}"


def parse_points_text(text: str) -> Tuple[str, List[str]]:
    raw = str(text or "").strip()
    city = ""
    if "：" in raw:
        city, raw = raw.split("：", 1)
    elif ":" in raw:
        city, raw = raw.split(":", 1)
    parts = [
        item.strip()
        for item in re.split(r"\s*(?:->|→|,|，|、|;|；)\s*", raw)
        if item.strip()
    ]
    return city.strip(), parts


def _point_from_poi(poi: Dict[str, Any], fallback_name: str) -> GeoPoint:
    return GeoPoint(
        name=str(poi.get("name") or fallback_name),
        location=str(poi.get("location") or ""),
        address=str(poi.get("address") or poi.get("name") or fallback_name),
        city=str(poi.get("cityname") or poi.get("city") or ""),
        adcode=str(poi.get("adcode") or ""),
    )


def _parse_steps(raw_steps: Iterable[Dict[str, Any]]) -> List[RouteStep]:
    steps: List[RouteStep] = []
    for step in raw_steps:
        steps.append(
            RouteStep(
                instruction=str(step.get("instruction") or step.get("instruction_description") or ""),
                road_name=str(step.get("road") or step.get("name") or ""),
                distance_meters=_to_int(step.get("distance")),
                duration_seconds=_to_int(step.get("cost", {}).get("duration") if isinstance(step.get("cost"), dict) else step.get("duration")),
                polyline=str(step.get("polyline") or ""),
            )
        )
    return steps


def _collect_congestion_segments(path: Dict[str, Any], steps: List[RouteStep]) -> List[CongestionSegment]:
    collected: List[CongestionSegment] = []

    def add_tmc(tmc: Dict[str, Any], fallback_road: str = "") -> None:
        status = normalize_congestion_status(tmc.get("status"))
        road = str(tmc.get("road") or tmc.get("name") or fallback_road or "未命名道路")
        collected.append(
            CongestionSegment(
                road_name=road,
                status=status,
                distance_meters=_to_int(tmc.get("distance")),
                polyline=str(tmc.get("polyline") or ""),
                severity_score=STATUS_SEVERITY.get(status, 0),
            )
        )

    for tmc in path.get("tmcs") or []:
        if isinstance(tmc, dict):
            add_tmc(tmc)

    for step in path.get("steps") or []:
        if not isinstance(step, dict):
            continue
        fallback = str(step.get("road") or step.get("instruction") or "")
        for tmc in step.get("tmcs") or []:
            if isinstance(tmc, dict):
                add_tmc(tmc, fallback)
    return collected


def normalize_congestion_status(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw or raw in ("0", "unknown", "未知"):
        return "unknown"
    if raw in ("1", "smooth", "畅通", "通畅", "快速"):
        return "smooth"
    if raw in ("2", "slow", "缓行", "缓慢"):
        return "slow"
    if raw in ("3", "congested", "拥堵"):
        return "congested"
    if raw in ("4", "severe", "severe_congested", "严重拥堵"):
        return "severe_congested"
    if "严重" in raw:
        return "severe_congested"
    if "拥堵" in raw:
        return "congested"
    if "缓" in raw:
        return "slow"
    if "畅" in raw or "smooth" in raw:
        return "smooth"
    return "unknown"


def _parse_traffic_roads(raw_roads: Any) -> List[TrafficRoad]:
    if isinstance(raw_roads, dict):
        raw_items = [raw_roads]
    elif isinstance(raw_roads, list):
        raw_items = raw_roads
    else:
        raw_items = []

    roads: List[TrafficRoad] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        roads.append(
            TrafficRoad(
                name=str(raw.get("name") or ""),
                status=normalize_congestion_status(raw.get("status")),
                direction=str(raw.get("direction") or ""),
                speed_kmh=_to_int(raw.get("speed")),
                polyline=str(raw.get("polyline") or ""),
            )
        )
    roads.sort(
        key=lambda road: (
            -STATUS_SEVERITY.get(road.status, 0),
            road.speed_kmh if road.speed_kmh else 9999,
        )
    )
    return roads


def _advanced_traffic_unavailable(query_type: str, query: str, exc: AmapApiError) -> TrafficStatusResult:
    return TrafficStatusResult(
        query_type=query_type,
        query=query,
        status="unknown",
        warning=f"高级交通态势不可用：{exc.safe_message()}。基础路线规划和 tmcs 路况仍可使用。",
    )


def _important_congestion_segments(segments: List[CongestionSegment]) -> List[CongestionSegment]:
    interesting = [seg for seg in segments if seg.status in ("slow", "congested", "severe_congested")]
    interesting.sort(key=lambda seg: (seg.severity_score, seg.distance_meters), reverse=True)
    return interesting[:3]


def _summarize_congestion(segments: List[CongestionSegment]) -> str:
    if not segments:
        return "未返回详细路况段"
    max_status = max(segments, key=lambda seg: seg.severity_score).status
    if max_status == "severe_congested":
        return "局部严重拥堵"
    if max_status == "congested":
        return "局部拥堵"
    if max_status == "slow":
        return "整体缓行"
    if max_status == "smooth":
        return "整体畅通"
    return "未知"


def _all_segments(route: RoutePlan) -> List[CongestionSegment]:
    if route.raw:
        return _collect_congestion_segments(route.raw, route.steps)
    return route.congestion_segments


def _path_polyline(path: Dict[str, Any], steps: List[RouteStep]) -> str:
    if path.get("polyline"):
        return str(path.get("polyline"))
    return ";".join(step.polyline for step in steps if step.polyline)


def _strategy_label(strategy: str, mode: str) -> str:
    for code, label in DRIVING_STRATEGIES:
        if str(strategy) == code:
            return label
    return {
        "driving": "驾车",
        "walking": "步行",
        "bicycling": "骑行",
        "electrobike": "电动车",
        "transit": "公交/地铁",
    }.get(mode, mode)


def _normalize_mode(mode: str) -> str:
    raw = str(mode or "driving").strip().lower()
    mapping = {
        "drive": "driving",
        "car": "driving",
        "驾车": "driving",
        "开车": "driving",
        "taxi": "driving",
        "打车": "driving",
        "walk": "walking",
        "步行": "walking",
        "bike": "bicycling",
        "bicycle": "bicycling",
        "骑行": "bicycling",
        "ebike": "electrobike",
        "电动车": "electrobike",
        "公交": "transit",
        "地铁": "transit",
        "transit": "transit",
        "bus": "transit",
    }
    return mapping.get(raw, raw)


def _traffic_level(value: Any) -> int:
    level = _to_int(value)
    if level < 1 or level > 6:
        return DEFAULT_TRAFFIC_LEVEL
    return level


def _normalize_rectangle(value: str) -> str:
    raw = str(value or "").strip().replace("；", ";")
    parts = [part.strip() for part in raw.split(";") if part.strip()]
    if len(parts) != 2:
        raise AmapServiceError("矩形区域格式无效，应为 左下经纬度;右上经纬度，例如 116.351147,39.966309;116.357134,39.968727。")
    first = AmapService._normalize_lonlat(parts[0])
    second = AmapService._normalize_lonlat(parts[1])
    if _haversine_meters(first, second) > 10000:
        raise AmapServiceError("矩形区域对角线不能超过 10 公里，请缩小查询范围。")
    return f"{first};{second}"


def _normalize_place_alias(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in ("家", "home", "amap_home"):
        return "home"
    if raw in ("公司", "单位", "company", "work", "office", "amap_company"):
        return "company"
    return ""


def _dedupe_routes(routes: List[RoutePlan]) -> List[RoutePlan]:
    seen = set()
    result = []
    for route in routes:
        key = (route.strategy, route.duration_seconds, route.distance_meters, route.polyline[:80])
        if key in seen:
            continue
        seen.add(key)
        result.append(route)
    return result


def _dedupe_text(values: Sequence[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _candidate_modes(distance_meters: int) -> List[str]:
    if distance_meters < 800:
        return ["walking", "bicycling", "electrobike"]
    if distance_meters <= 3000:
        return ["bicycling", "electrobike", "walking"]
    if distance_meters <= 15000:
        return ["transit", "driving"]
    return ["transit", "driving"]


def _mode_priority(mode: str) -> int:
    return {"walking": 0, "bicycling": 1, "electrobike": 2, "transit": 3, "driving": 4}.get(mode, 9)


def _is_cross_city(a: GeoPoint, b: GeoPoint) -> bool:
    if a.adcode and b.adcode and a.adcode[:4] != b.adcode[:4]:
        return True
    if a.city and b.city and a.city != b.city:
        return True
    return False


def _route_distance(points: Sequence[GeoPoint]) -> float:
    return sum(_haversine_meters(a.location, b.location) for a, b in zip(points, points[1:]))


def _two_opt(points: List[GeoPoint], keep_end: bool) -> List[GeoPoint]:
    best = list(points)
    improved = True
    end_limit = len(best) - 1 if keep_end else len(best)
    while improved:
        improved = False
        for i in range(1, end_limit - 1):
            for j in range(i + 1, end_limit):
                candidate = best[:i] + list(reversed(best[i:j])) + best[j:]
                if _route_distance(candidate) + 1 < _route_distance(best):
                    best = candidate
                    improved = True
    return best


def _haversine_meters(a: str, b: str) -> float:
    lon1, lat1 = _split_lonlat(a)
    lon2, lat2 = _split_lonlat(b)
    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    x = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(x), math.sqrt(1 - x))


def _split_lonlat(value: str) -> Tuple[float, float]:
    match = LONLAT_RE.match(str(value or ""))
    if not match:
        return 0.0, 0.0
    return float(match.group(1)), float(match.group(2))


def _eta(duration_seconds: int) -> str:
    target = dt.datetime.now() + dt.timedelta(seconds=max(0, int(duration_seconds or 0)))
    return target.strftime("%H:%M")


def _to_int(value: Any) -> int:
    try:
        if isinstance(value, dict):
            return 0
        return int(float(str(value).strip() or "0"))
    except Exception:
        return 0


def _to_float(value: Any) -> float:
    try:
        return float(str(value).strip() or "0")
    except Exception:
        return 0.0


def _parse_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "y", "on")


def format_commute_result(result: CommuteResult) -> str:
    return format_commute(result)


def format_travel_result(result: TravelRouteAnalysis) -> str:
    return format_travel_analysis(result)


def format_traffic_result(result: TrafficStatusResult) -> str:
    return format_traffic_status(result)


def load_skill_frontmatter(skills_root: str) -> Dict[str, Any]:
    """Load the first SKILL.md frontmatter under a skills root."""
    from agent.skills.frontmatter import parse_frontmatter

    root = Path(skills_root)
    candidates = [root / "SKILL.md"] if (root / "SKILL.md").exists() else []
    candidates.extend(root.rglob("SKILL.md"))
    for path in candidates:
        if path.is_file():
            return parse_frontmatter(path.read_text(encoding="utf-8"))
    raise AmapServiceError(f"未找到 SKILL.md：{skills_root}")


discover_amap_skill = load_skill_frontmatter
read_skill_frontmatter = load_skill_frontmatter
