#!/usr/bin/env python3
"""Query public 12306 ticket and train route endpoints."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


BASE_URL = "https://kyfw.12306.cn"
STATION_URL = f"{BASE_URL}/otn/resources/js/framework/station_name.js"
LEFT_TICKET_ENDPOINTS = (
    "/otn/leftTicket/queryG",
    "/otn/leftTicket/queryZ",
    "/otn/leftTicket/query",
)
DEFAULT_TIMEOUT = 25


@dataclass(frozen=True)
class Station:
    name: str
    code: str
    pinyin: str
    short: str
    city: str


@dataclass(frozen=True)
class Ticket:
    train_no: str
    train_code: str
    from_station: str
    to_station: str
    from_code: str
    to_code: str
    depart_time: str
    arrive_time: str
    duration: str
    start_date: str
    status: str
    can_buy: bool
    seats: dict


class RailwayError(RuntimeError):
    """Raised when the public 12306 query cannot be completed."""


class StationResolver:
    def __init__(self, cache_dir: Path, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.cache_dir = cache_dir
        self.timeout = timeout
        self._stations: list[Station] | None = None

    def resolve(self, query: str) -> Station:
        key = query.strip()
        if not key:
            raise RailwayError("Station name/code is empty.")

        stations = self._load_stations()
        upper = key.upper()
        lower = key.lower()

        for station in stations:
            if station.code.upper() == upper:
                return station

        for station in stations:
            if station.name == key:
                return station

        for station in stations:
            if station.pinyin == lower or station.short == lower:
                return station

        suggestions = self.search(key, limit=8)
        suffix = ""
        if suggestions:
            suffix = " Suggestions: " + ", ".join(
                f"{item.name}({item.code})" for item in suggestions
            )
        raise RailwayError(f"Unknown station: {query}.{suffix}")

    def search(self, query: str, limit: int = 10) -> list[Station]:
        key = query.strip().lower()
        if not key:
            return []

        matches = []
        for station in self._load_stations():
            fields = (
                station.name.lower(),
                station.code.lower(),
                station.pinyin,
                station.short,
                station.city.lower(),
            )
            if any(key in value for value in fields):
                matches.append(station)
        return matches[:limit]

    def _load_stations(self) -> list[Station]:
        if self._stations is not None:
            return self._stations

        text = self._read_station_js()
        match = re.search(r"station_names\s*=\s*'(.+?)';", text, re.DOTALL)
        if not match:
            raise RailwayError("Could not parse 12306 station list.")

        stations: list[Station] = []
        for item in match.group(1).split("@"):
            if not item:
                continue
            parts = item.split("|")
            if len(parts) < 7:
                continue
            stations.append(
                Station(
                    name=parts[1],
                    code=parts[2],
                    pinyin=parts[3],
                    short=parts[4],
                    city=parts[7] if len(parts) > 7 else "",
                )
            )

        if not stations:
            raise RailwayError("12306 station list is empty.")
        self._stations = stations
        return stations

    def _read_station_js(self) -> str:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = self.cache_dir / "station_name.js"
        max_age_seconds = 7 * 24 * 60 * 60

        if cache_file.exists() and time.time() - cache_file.stat().st_mtime < max_age_seconds:
            return cache_file.read_text(encoding="utf-8")

        text = fetch_text(STATION_URL, timeout=self.timeout)
        cache_file.write_text(text, encoding="utf-8")
        return text


class RailwayClient:
    def __init__(self, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self._initialized = False

    def query_tickets(self, from_station: Station, to_station: Station, date: str) -> list[Ticket]:
        validate_date(date)
        self._init_session()
        params = {
            "leftTicketDTO.train_date": date,
            "leftTicketDTO.from_station": from_station.code,
            "leftTicketDTO.to_station": to_station.code,
            "purpose_codes": "ADULT",
        }

        last_error: Exception | None = None
        for endpoint in LEFT_TICKET_ENDPOINTS:
            try:
                data = self._get_json(endpoint, params)
                result = data.get("data", {}).get("result", [])
                station_map = data.get("data", {}).get("map", {})
                return [parse_ticket(row, station_map) for row in result]
            except Exception as exc:  # Try known 12306 endpoint variants.
                last_error = exc

        raise RailwayError(f"12306 ticket query failed: {last_error}")

    def query_route(
        self,
        train_no: str,
        from_station: Station,
        to_station: Station,
        date: str,
    ) -> list[dict]:
        validate_date(date)
        params = {
            "train_no": train_no,
            "from_station_telecode": from_station.code,
            "to_station_telecode": to_station.code,
            "depart_date": date,
        }
        data = self._get_json("/otn/czxx/queryByTrainNo", params)
        return data.get("data", {}).get("data", [])

    def _init_session(self) -> None:
        if self._initialized:
            return
        self._open(f"{BASE_URL}/otn/leftTicket/init")
        self._initialized = True

    def _get_json(self, path: str, params: dict) -> dict:
        query = urlencode(params)
        text = self._open(f"{BASE_URL}{path}?{query}")
        if not text.lstrip().startswith("{"):
            raise RailwayError("12306 returned a non-JSON response.")
        data = json.loads(text)
        if data.get("status") is False:
            raise RailwayError(str(data.get("messages") or "12306 returned status=false."))
        return data

    def _open(self, url: str) -> str:
        request = Request(url, headers=default_headers())
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            raise RailwayError(f"HTTP {exc.code} from 12306: {url}") from exc
        except URLError as exc:
            raise RailwayError(f"Network error from 12306: {exc.reason}") from exc


def default_cache_dir() -> Path:
    base = os.environ.get("COW_12306_CACHE_DIR")
    if base:
        return Path(base)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "CowWechat" / "cache" / "12306"
    return Path.home() / ".cache" / "cowwechat" / "12306"


def default_headers() -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
        ),
        "Referer": f"{BASE_URL}/otn/leftTicket/init",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }


def fetch_text(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    request = Request(url, headers=default_headers())
    try:
        with build_opener().open(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RailwayError(f"HTTP {exc.code} from 12306: {url}") from exc
    except URLError as exc:
        raise RailwayError(f"Network error from 12306: {exc.reason}") from exc


def validate_date(value: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise RailwayError("Date must use YYYY-MM-DD format.") from exc


def normalize_seat(value: str) -> str:
    if value in ("", None):
        return "-"
    return value


def parse_ticket(row: str, station_map: dict) -> Ticket:
    parts = row.split("|")
    if len(parts) < 33:
        raise RailwayError("Unexpected 12306 ticket row format.")

    seats = {
        "business": normalize_seat(parts[32]),
        "first": normalize_seat(parts[31]),
        "second": normalize_seat(parts[30]),
        "soft_sleeper": normalize_seat(parts[23]),
        "hard_sleeper": normalize_seat(parts[28]),
        "hard_seat": normalize_seat(parts[29]),
        "no_seat": normalize_seat(parts[26]),
    }

    return Ticket(
        train_no=parts[2],
        train_code=parts[3],
        from_station=station_map.get(parts[6], parts[6]),
        to_station=station_map.get(parts[7], parts[7]),
        from_code=parts[6],
        to_code=parts[7],
        depart_time=parts[8],
        arrive_time=parts[9],
        duration=parts[10],
        start_date=parts[13],
        status=parts[1],
        can_buy=parts[11] == "Y",
        seats=seats,
    )


def filter_tickets(tickets: Iterable[Ticket], train_prefix: str | None) -> list[Ticket]:
    if not train_prefix:
        return list(tickets)
    prefixes = tuple(item.strip().upper() for item in train_prefix.split(",") if item.strip())
    if not prefixes:
        return list(tickets)
    return [ticket for ticket in tickets if ticket.train_code.upper().startswith(prefixes)]


def find_train_no(train: str, tickets: list[Ticket]) -> str:
    key = train.strip().upper()
    for ticket in tickets:
        if ticket.train_no.upper() == key or ticket.train_code.upper() == key:
            return ticket.train_no
    raise RailwayError(f"Train {train} was not found in the ticket query result.")


def emit_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def render_stations(stations: list[Station]) -> None:
    if not stations:
        print("No stations found.")
        return
    for station in stations:
        city = f" / {station.city}" if station.city else ""
        print(f"{station.name} ({station.code}) pinyin={station.pinyin} short={station.short}{city}")


def render_tickets(tickets: list[Ticket], date: str, from_station: Station, to_station: Station) -> None:
    print(f"{date} {from_station.name}({from_station.code}) -> {to_station.name}({to_station.code})")
    print(f"Found {len(tickets)} train(s). Availability can change quickly.")
    for ticket in tickets:
        seats = ticket.seats
        print(
            f"{ticket.train_code:<8} {ticket.depart_time}->{ticket.arrive_time} "
            f"{ticket.duration:<5} {ticket.from_station}->{ticket.to_station} "
            f"二等:{seats['second']} 一等:{seats['first']} 商务:{seats['business']} "
            f"软卧:{seats['soft_sleeper']} 硬卧:{seats['hard_sleeper']} "
            f"硬座:{seats['hard_seat']} 无座:{seats['no_seat']} "
            f"状态:{ticket.status} train_no:{ticket.train_no}"
        )


def render_route(stops: list[dict], train: str, date: str) -> None:
    print(f"{date} {train} stops: {len(stops)}")
    for stop in stops:
        print(
            f"{stop.get('station_no', ''):>2} {stop.get('station_name', ''):<10} "
            f"到达:{stop.get('arrive_time', ''):<5} "
            f"出发:{stop.get('start_time', ''):<5} "
            f"停留:{stop.get('stopover_time', '')}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query public 12306 ticket data without LINKAI_API_KEY.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--cache-dir", default=str(default_cache_dir()))
    subparsers = parser.add_subparsers(dest="command", required=True)

    stations = subparsers.add_parser("stations", help="Resolve or search stations.")
    stations.add_argument("query")
    stations.add_argument("--limit", type=int, default=10)
    stations.add_argument("--json", action="store_true")

    tickets = subparsers.add_parser("tickets", help="Query remaining tickets.")
    tickets.add_argument("from_station")
    tickets.add_argument("to_station")
    tickets.add_argument("date")
    tickets.add_argument("--limit", type=int, default=20)
    tickets.add_argument("--train-prefix", help="Comma-separated prefixes, e.g. G,D,K")
    tickets.add_argument("--json", action="store_true")

    route = subparsers.add_parser("route", help="Query train stops.")
    route.add_argument("train", help="Public train code such as G547, or internal train_no.")
    route.add_argument("from_station")
    route.add_argument("to_station")
    route.add_argument("date")
    route.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    resolver = StationResolver(Path(args.cache_dir), timeout=args.timeout)
    client = RailwayClient(timeout=args.timeout)

    try:
        if args.command == "stations":
            stations = resolver.search(args.query, limit=args.limit)
            if args.json:
                emit_json([asdict(station) for station in stations])
            else:
                render_stations(stations)
            return 0

        if args.command == "tickets":
            from_station = resolver.resolve(args.from_station)
            to_station = resolver.resolve(args.to_station)
            tickets = client.query_tickets(from_station, to_station, args.date)
            tickets = filter_tickets(tickets, args.train_prefix)[: args.limit]
            if args.json:
                emit_json([asdict(ticket) for ticket in tickets])
            else:
                render_tickets(tickets, args.date, from_station, to_station)
            return 0

        if args.command == "route":
            from_station = resolver.resolve(args.from_station)
            to_station = resolver.resolve(args.to_station)
            train_no = args.train
            if not re.match(r"^\d{6,}", train_no):
                tickets = client.query_tickets(from_station, to_station, args.date)
                train_no = find_train_no(args.train, tickets)
            stops = client.query_route(train_no, from_station, to_station, args.date)
            if args.json:
                emit_json(stops)
            else:
                render_route(stops, args.train, args.date)
            return 0

    except RailwayError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
