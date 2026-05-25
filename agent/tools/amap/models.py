"""Normalized models for AMap route and commute results."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class GeoPoint:
    name: str
    location: str
    address: str = ""
    city: str = ""
    adcode: str = ""

    @property
    def lonlat(self) -> str:
        return self.location


@dataclass
class CongestionSegment:
    road_name: str
    status: str
    distance_meters: int = 0
    polyline: str = ""
    severity_score: int = 0


@dataclass
class RouteStep:
    instruction: str = ""
    road_name: str = ""
    distance_meters: int = 0
    duration_seconds: int = 0
    polyline: str = ""


@dataclass
class RoutePlan:
    mode: str
    origin: GeoPoint
    destination: GeoPoint
    distance_meters: int = 0
    duration_seconds: int = 0
    eta: str = ""
    strategy: str = ""
    tolls: float = 0.0
    traffic_lights: int = 0
    congestion_summary: str = "未知"
    congestion_segments: List[CongestionSegment] = field(default_factory=list)
    steps: List[RouteStep] = field(default_factory=list)
    polyline: str = ""
    score: float = 0.0
    raw: Optional[Dict[str, Any]] = None


@dataclass
class CommuteResult:
    from_name: str
    to_name: str
    recommended_route: RoutePlan
    alternatives: List[RoutePlan] = field(default_factory=list)
    eta: str = ""
    summary_text: str = ""


@dataclass
class TravelLeg:
    origin: GeoPoint
    destination: GeoPoint
    recommended_mode: str
    route: Optional[RoutePlan] = None
    alternatives: List[RoutePlan] = field(default_factory=list)
    warning: str = ""


@dataclass
class TravelRouteAnalysis:
    original_points: List[str]
    resolved_points: List[GeoPoint]
    recommended_order: List[GeoPoint]
    legs: List[TravelLeg]
    total_duration_seconds: int = 0
    total_distance_meters: int = 0
    reasonableness_score: int = 100
    warnings: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    summary_text: str = ""


def public_dict(value: Any) -> Any:
    """Convert dataclass values to dictionaries without raw provider payloads."""
    if hasattr(value, "__dataclass_fields__"):
        data = asdict(value)
        return public_dict(data)
    if isinstance(value, dict):
        return {
            key: public_dict(child)
            for key, child in value.items()
            if key != "raw"
        }
    if isinstance(value, list):
        return [public_dict(item) for item in value]
    return value
