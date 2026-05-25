"""Chinese user-facing formatting for AMap results."""

from __future__ import annotations

from typing import Iterable, List

from agent.tools.amap.models import CommuteResult, RoutePlan, TrafficStatusResult, TravelRouteAnalysis, WeatherResult


MODE_LABELS = {
    "driving": "驾车",
    "transit": "公交/地铁",
    "walking": "步行",
    "bicycling": "骑行",
    "electrobike": "电动车",
}


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes // 60
    remain = minutes % 60
    if remain:
        return f"{hours} 小时 {remain} 分钟"
    return f"{hours} 小时"


def format_distance(meters: int) -> str:
    meters = max(0, int(meters or 0))
    if meters < 1000:
        return f"{meters} 米"
    return f"{meters / 1000:.1f} 公里"


def format_route(route: RoutePlan, title: str = "路线") -> str:
    lines = [
        f"{route.origin.name} → {route.destination.name}",
        "",
        f"{title}：{_strategy_text(route)}",
        f"预计耗时：{format_duration(route.duration_seconds)}",
    ]
    if route.eta:
        lines.append(f"预计到达：{route.eta}")
    lines.extend([
        f"距离：{format_distance(route.distance_meters)}",
        f"当前路况：{route.congestion_summary}",
    ])
    if route.tolls:
        lines.append(f"过路费：约 {route.tolls:g} 元")
    if route.traffic_lights:
        lines.append(f"红绿灯：约 {route.traffic_lights} 个")
    lines.extend(_format_segments(route))
    return "\n".join(lines)


def format_commute(result: CommuteResult) -> str:
    route = result.recommended_route
    lines = [format_route(route, title="推荐")]
    if result.alternatives:
        lines.append("")
        lines.append("备选：")
        for alt in result.alternatives[:3]:
            lines.append(f"- {_strategy_text(alt)}：{format_duration(alt.duration_seconds)}")
    if result.summary_text:
        lines.append("")
        lines.append(result.summary_text)
    return "\n".join(lines)


def format_travel_analysis(analysis: TravelRouteAnalysis) -> str:
    order = " → ".join(point.name for point in analysis.recommended_order)
    lines = [
        "旅游路线分析",
        "",
        f"推荐顺序：{order}",
        f"总交通耗时：{format_duration(analysis.total_duration_seconds)}",
        f"总距离：{format_distance(analysis.total_distance_meters)}",
        f"合理性评分：{analysis.reasonableness_score}/100",
        "",
        "分段建议：",
    ]
    for index, leg in enumerate(analysis.legs, 1):
        route = leg.route
        mode = MODE_LABELS.get(leg.recommended_mode, leg.recommended_mode)
        if route:
            lines.append(
                f"{index}. {leg.origin.name} → {leg.destination.name}：{mode}，"
                f"{format_duration(route.duration_seconds)}，{format_distance(route.distance_meters)}"
            )
        else:
            lines.append(f"{index}. {leg.origin.name} → {leg.destination.name}：{mode}")
        if leg.warning:
            lines.append(f"   提醒：{leg.warning}")

    if analysis.warnings:
        lines.append("")
        lines.append("风险提示：")
        for warning in analysis.warnings[:6]:
            lines.append(f"- {warning}")

    if analysis.suggestions:
        lines.append("")
        lines.append("调整建议：")
        for suggestion in analysis.suggestions[:6]:
            lines.append(f"- {suggestion}")
    return "\n".join(lines)


def format_traffic_status(result: TrafficStatusResult) -> str:
    lines = [
        "交通态势",
        "",
        f"查询：{_traffic_query_text(result)}",
        f"当前路况：{_status_label(result.status)}",
    ]
    if result.description:
        lines.append(f"综述：{result.description}")
    lines.extend([
        f"畅通占比：{result.expedite_percent:g}%",
        f"缓行占比：{result.slow_percent:g}%",
        f"拥堵占比：{result.congested_percent:g}%",
    ])
    if result.unknown_percent:
        lines.append(f"未知占比：{result.unknown_percent:g}%")
    if result.roads:
        lines.append("")
        lines.append("重点道路：")
        for index, road in enumerate(result.roads[:5], 1):
            suffix = f"，均速约 {road.speed_kmh} km/h" if road.speed_kmh else ""
            direction = f"（{road.direction}）" if road.direction else ""
            lines.append(f"{index}. {road.name or '未命名道路'}{direction}：{_status_label(road.status)}{suffix}")
    if result.warning:
        lines.append("")
        lines.append(f"提示：{result.warning}")
    return "\n".join(lines)


def format_weather(result: WeatherResult) -> str:
    if result.live:
        live = result.live
        lines = [
            f"{live.city or result.city}实时天气",
            "",
            f"天气：{live.weather or '未知'}",
        ]
        if live.temperature_c:
            lines.append(f"温度：{live.temperature_c}℃")
        if live.humidity_percent:
            lines.append(f"湿度：{live.humidity_percent}%")
        wind = _join_nonempty([live.wind_direction, live.wind_power])
        if wind:
            lines.append(f"风力：{wind}")
        if live.report_time:
            lines.append(f"发布时间：{live.report_time}")
        return "\n".join(lines)

    forecast = result.forecast
    if not forecast:
        return f"{result.city or result.adcode}天气：未返回可用数据"

    lines = [
        f"{forecast.city or result.city}天气预报",
        "",
    ]
    for item in forecast.casts[:4]:
        day_weather = item.day_weather or "未知"
        night_weather = item.night_weather or "未知"
        temp = _format_temperature_range(item.night_temp_c, item.day_temp_c)
        wind = _join_nonempty([item.day_wind, item.day_power])
        suffix = f"，{wind}" if wind else ""
        lines.append(f"{item.date}：白天{day_weather}，夜间{night_weather}{temp}{suffix}")
    if forecast.report_time:
        lines.append(f"发布时间：{forecast.report_time}")
    return "\n".join(lines)


def _format_segments(route: RoutePlan) -> List[str]:
    if not route.congestion_segments:
        return ["", "主要拥堵：未返回详细路况段"]
    lines = ["", "主要拥堵："]
    for index, segment in enumerate(route.congestion_segments[:3], 1):
        lines.append(
            f"{index}. {segment.road_name or '未命名道路'}："
            f"{_status_label(segment.status)}，约 {format_distance(segment.distance_meters)}"
        )
    return lines


def _strategy_text(route: RoutePlan) -> str:
    mode = MODE_LABELS.get(route.mode, route.mode)
    if route.strategy:
        return route.strategy
    return mode


def _status_label(status: str) -> str:
    labels = {
        "unknown": "未知",
        "smooth": "畅通",
        "slow": "缓行",
        "congested": "拥堵",
        "severe_congested": "严重拥堵",
    }
    return labels.get(status, status or "未知")


def _traffic_query_text(result: TrafficStatusResult) -> str:
    labels = {
        "road": "道路",
        "circle": "圆形区域",
        "rectangle": "矩形区域",
    }
    return f"{labels.get(result.query_type, result.query_type)} {result.query}".strip()


def _format_temperature_range(low: str, high: str) -> str:
    if low and high:
        return f"，{low}~{high}℃"
    if high:
        return f"，最高 {high}℃"
    if low:
        return f"，最低 {low}℃"
    return ""


def _join_nonempty(values: Iterable[str]) -> str:
    return " ".join(str(value).strip() for value in values if str(value).strip())


def join_warnings(warnings: Iterable[str]) -> str:
    items = [item for item in warnings if item]
    return "；".join(items) if items else "无"
