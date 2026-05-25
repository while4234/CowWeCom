#!/usr/bin/env python3
"""Fast Open-Meteo weather lookup for CowWechat."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Any


CITY_COORDS: dict[str, dict[str, Any]] = {
    "成都": {"name": "成都", "admin1": "四川", "latitude": 30.5728, "longitude": 104.0668, "timezone": "Asia/Shanghai"},
    "深圳": {"name": "深圳", "admin1": "广东", "latitude": 22.5431, "longitude": 114.0579, "timezone": "Asia/Shanghai"},
    "北京": {"name": "北京", "admin1": "北京", "latitude": 39.9042, "longitude": 116.4074, "timezone": "Asia/Shanghai"},
    "上海": {"name": "上海", "admin1": "上海", "latitude": 31.2304, "longitude": 121.4737, "timezone": "Asia/Shanghai"},
    "广州": {"name": "广州", "admin1": "广东", "latitude": 23.1291, "longitude": 113.2644, "timezone": "Asia/Shanghai"},
    "杭州": {"name": "杭州", "admin1": "浙江", "latitude": 30.2741, "longitude": 120.1551, "timezone": "Asia/Shanghai"},
    "重庆": {"name": "重庆", "admin1": "重庆", "latitude": 29.5630, "longitude": 106.5516, "timezone": "Asia/Shanghai"},
    "武汉": {"name": "武汉", "admin1": "湖北", "latitude": 30.5928, "longitude": 114.3055, "timezone": "Asia/Shanghai"},
    "南京": {"name": "南京", "admin1": "江苏", "latitude": 32.0603, "longitude": 118.7969, "timezone": "Asia/Shanghai"},
    "西安": {"name": "西安", "admin1": "陕西", "latitude": 34.3416, "longitude": 108.9398, "timezone": "Asia/Shanghai"},
}

INTERNATIONAL_CITY_ALIASES: dict[str, dict[str, str]] = {
    "东京": {"name": "Tokyo", "country_code": "JP"},
    "東京": {"name": "Tokyo", "country_code": "JP"},
    "纽约": {"name": "New York", "country_code": "US"},
    "紐約": {"name": "New York", "country_code": "US"},
    "首尔": {"name": "Seoul", "country_code": "KR"},
    "首爾": {"name": "Seoul", "country_code": "KR"},
    "巴黎": {"name": "Paris", "country_code": "FR"},
}

WEATHER_WORDS = ("天气", "气温", "温度", "降雨", "降水", "下雨", "风力", "风速", "冷不冷", "热不热")
CN_WEEKDAYS = "一二三四五六日"
CODE_TEXT = {
    0: "晴",
    1: "大体晴朗",
    2: "多云",
    3: "阴",
    45: "有雾",
    48: "有雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "较强毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "阵雨",
    81: "较强阵雨",
    82: "强阵雨",
    95: "雷阵雨",
}


def fetch_json(url: str, timeout: int = 12) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CowWechat quick-weather",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            last_error = exc
            if attempt == 3:
                break
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"天气接口暂时不可用：{last_error}") from last_error


def normalize_place(text: str) -> str:
    raw = (text or "").strip()
    for city in CITY_COORDS:
        if city in raw:
            return city
    for alias in INTERNATIONAL_CITY_ALIASES:
        if alias in raw:
            return alias
    cleaned = re.sub(r"(今天|今日|明天|后天|大后天|未来\d+天|现在|当前|最新|请|帮我|查一下|查下|查询|看看|的)", "", raw)
    for word in WEATHER_WORDS:
        cleaned = cleaned.replace(word, "")
    cleaned = cleaned.strip(" ，。！？;；")
    if 1 <= len(cleaned) <= 20:
        return cleaned
    return ""


def parse_dates(text: str, days_arg: int | None) -> list[dt.date]:
    today = dt.date.today()
    if days_arg:
        return [today + dt.timedelta(days=i) for i in range(max(1, min(days_arg, 16)))]
    raw = text or ""
    match = re.search(r"未来\s*(\d{1,2})\s*天", raw)
    if match:
        count = max(1, min(int(match.group(1)), 16))
        return [today + dt.timedelta(days=i) for i in range(count)]
    if "大后天" in raw:
        return [today + dt.timedelta(days=3)]
    if "后天" in raw:
        return [today + dt.timedelta(days=2)]
    if "明天" in raw:
        return [today + dt.timedelta(days=1)]
    return [today]


def normalize_country_code(country_code: str | None) -> str | None:
    if not country_code:
        return None
    normalized = country_code.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", normalized):
        raise ValueError("country code must be a two-letter ISO code, such as JP, US, KR, or FR")
    return normalized


def country_code_arg(value: str) -> str:
    try:
        return normalize_country_code(value) or ""
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def alias_for(place: str) -> dict[str, str] | None:
    return INTERNATIONAL_CITY_ALIASES.get(place.strip())


def geocode_language(place: str) -> str:
    return "en" if re.search(r"[A-Za-z]", place) else "zh"


def geocode(place: str, country_code: str | None = None) -> dict[str, Any]:
    normalized_country_code = normalize_country_code(country_code)
    if place in CITY_COORDS and normalized_country_code in (None, "CN"):
        return CITY_COORDS[place]
    alias = alias_for(place)
    query = alias["name"] if alias else place
    effective_country_code = normalized_country_code or (alias["country_code"] if alias else None)
    params = {"name": query, "count": 5, "language": geocode_language(query), "format": "json"}
    if effective_country_code:
        params["countryCode"] = effective_country_code
    url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
        params
    )
    data = fetch_json(url, timeout=10)
    results = data.get("results") or []
    if not results:
        country_hint = f"（{effective_country_code}）" if effective_country_code else ""
        raise RuntimeError(f"没有查到 {place}{country_hint} 的天气定位结果")
    first = next(
        (
            item
            for item in results
            if effective_country_code and str(item.get("country_code") or "").upper() == effective_country_code
        ),
        results[0],
    )
    return {
        "name": first.get("name") or place,
        "admin1": first.get("admin1") or "",
        "country": first.get("country") or "",
        "country_code": first.get("country_code") or effective_country_code or "",
        "latitude": first.get("latitude"),
        "longitude": first.get("longitude"),
        "timezone": first.get("timezone") or "Asia/Shanghai",
    }


def day_label(day: dt.date) -> str:
    return f"{day.month}月{day.day}日（周{CN_WEEKDAYS[day.weekday()]}）"


def weather_text(code: Any) -> str:
    try:
        return CODE_TEXT.get(int(code), f"天气代码 {code}")
    except Exception:
        return "天气待确认"


def advice(item: dict[str, Any]) -> str:
    tips: list[str] = []
    rain = float(item.get("rain") or 0)
    rain_prob = float(item.get("rain_prob") or 0)
    high = item.get("high")
    low = item.get("low")
    wind = float(item.get("wind") or 0)
    if rain >= 5 or rain_prob >= 80:
        tips.append("降雨明显，出门带伞，注意路滑")
    elif rain >= 1 or rain_prob >= 50:
        tips.append("有下雨可能，建议随身带伞")
    else:
        tips.append("降雨不明显，出行压力较小")
    if high is not None and float(high) >= 32:
        tips.append("偏热，注意防晒和补水")
    if low is not None and float(low) <= 8:
        tips.append("偏凉，建议加外套")
    if high is not None and low is not None and float(high) - float(low) >= 8:
        tips.append("温差较大，建议分层穿衣")
    if wind >= 25:
        tips.append("风较大，注意防风")
    return "；".join(tips[:3])


def lookup(place: str, dates: list[dt.date], country_code: str | None = None) -> dict[str, Any]:
    location = geocode(place, country_code=country_code)
    start, end = min(dates), max(dates)
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(
        {
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "current_weather": "true",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max",
            "timezone": location["timezone"],
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
    )
    data = fetch_json(url, timeout=15)
    daily = data.get("daily") or {}
    current = data.get("current_weather") or {}
    by_day: dict[str, dict[str, Any]] = {}
    for idx, value in enumerate(daily.get("time") or []):
        by_day[value] = {
            "date": dt.date.fromisoformat(value),
            "weather": weather_text((daily.get("weather_code") or [None])[idx]),
            "high": (daily.get("temperature_2m_max") or [None])[idx],
            "low": (daily.get("temperature_2m_min") or [None])[idx],
            "rain": (daily.get("precipitation_sum") or [None])[idx],
            "rain_prob": (daily.get("precipitation_probability_max") or [None])[idx],
            "wind": (daily.get("wind_speed_10m_max") or [None])[idx],
        }
    selected = [by_day[d.isoformat()] for d in sorted(set(dates)) if d.isoformat() in by_day]
    return {"location": location, "current": current, "days": selected, "source": "Open-Meteo"}


def format_reply(bundle: dict[str, Any]) -> str:
    loc = bundle["location"]
    subject = loc["name"] if not loc.get("admin1") or loc["admin1"] == loc["name"] else f"{loc['name']}（{loc['admin1']}）"
    current = bundle.get("current") or {}
    lines = [f"{subject}天气"]
    if current.get("temperature") is not None:
        lines.append(f"当前：约 {current.get('temperature')}°C，风速 {current.get('windspeed')} km/h，时间 {str(current.get('time') or '').replace('T', ' ')}")
    for item in bundle.get("days") or []:
        lines.append(
            f"{day_label(item['date'])}：{item['weather']}，{item['low']}~{item['high']}°C，降水 {item['rain']} mm，概率 {item['rain_prob']}%，最大风速 {item['wind']} km/h"
        )
        if len(bundle.get("days") or []) == 1:
            lines.append(f"建议：{advice(item)}")
    if len(bundle.get("days") or []) > 1:
        rainy = [day_label(item["date"]) for item in bundle["days"] if float(item.get("rain") or 0) >= 1 or float(item.get("rain_prob") or 0) >= 50]
        lines.append("建议：" + (f"{'、'.join(rainy[:3])} 更可能下雨，出门带伞。" if rainy else "这几天天气整体较平稳。"))
    lines.append("来源：Open-Meteo")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast weather lookup using Open-Meteo.")
    parser.add_argument("text", nargs="*", help="Weather query text")
    parser.add_argument("--place", help="Explicit place/city")
    parser.add_argument("--country-code", type=country_code_arg, help="Optional ISO country code to bias geocoding, such as JP or US")
    parser.add_argument("--days", type=int, help="Number of days from today, max 16")
    parser.add_argument("--json", action="store_true", help="Return raw JSON")
    args = parser.parse_args()
    text = " ".join(args.text).strip()
    place = args.place or normalize_place(text)
    if not place:
        raise SystemExit("请提供城市，例如：python quick_weather.py \"明天成都天气\"")
    bundle = lookup(place, parse_dates(text, args.days), country_code=args.country_code)
    if args.json:
        print(json.dumps(bundle, ensure_ascii=False, default=str, indent=2))
    else:
        print(format_reply(bundle))
    return 0


if __name__ == "__main__":
    sys.exit(main())
