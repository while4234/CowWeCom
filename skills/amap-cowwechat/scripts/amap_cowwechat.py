#!/usr/bin/env python3
"""CLI wrapper for the CowWechat AMap service."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _bootstrap_project_imports() -> None:
    candidates = [Path.cwd(), *Path(__file__).resolve().parents]
    for raw in (os.environ.get("COWWECHAT_ROOT"), r"D:\CowWechat", r"D:\cowwechat"):
        if raw:
            candidates.append(Path(raw))
    for candidate in candidates:
        if (candidate / "agent" / "tools").exists():
            sys.path.insert(0, str(candidate))
            return


_bootstrap_project_imports()

from agent.tools.amap.client import AmapApiError, MissingAmapKeyError  # noqa: E402
from agent.tools.amap.formatter import format_route  # noqa: E402
from agent.tools.amap.models import public_dict  # noqa: E402
from agent.tools.amap.service import (  # noqa: E402
    AmapService,
    AmapServiceError,
    LONLAT_RE,
    format_commute_result,
    format_traffic_result,
    format_travel_result,
    format_weather_result,
    parse_points_text,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="CowWechat AMap commute and route helper.")
    parser.add_argument("--json", action="store_true", help="Print normalized JSON instead of Chinese summary.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    set_home = subparsers.add_parser("set-home", help="Set home address or lon,lat.")
    set_home.add_argument("place")
    set_home.add_argument("--city", default="")

    set_company = subparsers.add_parser("set-company", help="Set company address or lon,lat.")
    set_company.add_argument("place")
    set_company.add_argument("--city", default="")

    commute = subparsers.add_parser("commute", help="Query commute status.")
    commute.add_argument("--direction", default="home_to_company", choices=["home_to_company", "company_to_home"])
    commute.add_argument("--origin", default="")
    commute.add_argument("--destination", default="")

    route = subparsers.add_parser("route", help="Plan a route.")
    route.add_argument("origin")
    route.add_argument("destination")
    route.add_argument("--mode", default="driving", choices=["driving", "transit", "walking", "bicycling", "electrobike"])
    route.add_argument("--city", default="")

    traffic = subparsers.add_parser("traffic", help="Analyze traffic on a route.")
    traffic.add_argument("origin")
    traffic.add_argument("destination")
    traffic.add_argument("--city", default="")

    traffic_road = subparsers.add_parser("traffic-road", help="Analyze advanced traffic status for one road.")
    traffic_road.add_argument("name")
    traffic_road.add_argument("--city", default="")
    traffic_road.add_argument("--adcode", default="")
    traffic_road.add_argument("--level", type=int, default=5)

    traffic_circle = subparsers.add_parser("traffic-circle", help="Analyze advanced traffic status around a point.")
    traffic_circle.add_argument("location", help="Center lon,lat or a place name.")
    traffic_circle.add_argument("--city", default="")
    traffic_circle.add_argument("--radius", type=int, default=1000)
    traffic_circle.add_argument("--level", type=int, default=5)

    traffic_rectangle = subparsers.add_parser("traffic-rectangle", help="Analyze advanced traffic status inside a rectangle.")
    traffic_rectangle.add_argument("rectangle", help="left-bottom lon,lat;right-top lon,lat")
    traffic_rectangle.add_argument("--level", type=int, default=5)

    weather = subparsers.add_parser("weather", help="Query AMap live weather or forecast by city.")
    weather.add_argument("city", help="City name or adcode, e.g. 成都 or 510100.")
    weather.add_argument("--type", dest="weather_type", default="live", choices=["live", "forecast"])
    weather.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print normalized JSON instead of Chinese summary.",
    )

    travel = subparsers.add_parser("travel", help="Analyze travel route reasonableness.")
    travel.add_argument("text", help="Example: 北京：故宫、景山公园、南锣鼓巷")
    travel.add_argument("--city", default="")

    args = parser.parse_args()
    service = AmapService()

    try:
        result = _run_command(service, args)
    except MissingAmapKeyError:
        print("未配置高德 Web服务 Key。请设置 AMAP_WEBSERVICE_KEY。", file=sys.stderr)
        return 2
    except AmapApiError as exc:
        print(f"高德接口错误：{exc.safe_message()}", file=sys.stderr)
        return 3
    except AmapServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 4

    if args.json:
        print(json.dumps(public_dict(result["data"]), ensure_ascii=False, indent=2))
    else:
        print(result["summary"])
    return 0


def _run_command(service: AmapService, args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "set-home":
        point = service.set_profile_location("home", args.place, args.city)
        return {"summary": f"已设置家：{point.address or point.name}\n坐标：{point.location}", "data": point}
    if args.command == "set-company":
        point = service.set_profile_location("company", args.place, args.city)
        return {"summary": f"已设置公司：{point.address or point.name}\n坐标：{point.location}", "data": point}
    if args.command == "commute":
        result = service.get_commute_status(args.direction, origin=args.origin, destination=args.destination)
        return {"summary": format_commute_result(result), "data": result}
    if args.command == "route":
        route = service.route_plan(args.origin, args.destination, args.mode, city=args.city, include_alternatives=True)
        return {"summary": format_route(route, "推荐"), "data": route}
    if args.command == "traffic":
        route = service.traffic_status(args.origin, args.destination, city=args.city)
        return {"summary": format_route(route, "路况"), "data": route}
    if args.command == "traffic-road":
        result = service.traffic_status(
            args.name,
            city=args.city,
            query_type="road",
            adcode=args.adcode,
            level=args.level,
        )
        return {"summary": format_traffic_result(result), "data": result}
    if args.command == "traffic-circle":
        location = args.location
        if not LONLAT_RE.match(str(location or "")):
            location = service.resolve_point(location, args.city, prefer_poi=True).location
        result = service.traffic_status(
            location,
            query_type="circle",
            radius=args.radius,
            level=args.level,
        )
        return {"summary": format_traffic_result(result), "data": result}
    if args.command == "traffic-rectangle":
        result = service.traffic_status(
            args.rectangle,
            query_type="rectangle",
            level=args.level,
        )
        return {"summary": format_traffic_result(result), "data": result}
    if args.command == "weather":
        result = service.weather(args.city, args.weather_type)
        return {"summary": format_weather_result(result), "data": result}
    if args.command == "travel":
        city, points = parse_points_text(args.text)
        result = service.analyze_travel_route(points, args.city or city)
        return {"summary": format_travel_result(result), "data": result}
    raise AmapServiceError(f"不支持的命令：{args.command}")


if __name__ == "__main__":
    sys.exit(main())
