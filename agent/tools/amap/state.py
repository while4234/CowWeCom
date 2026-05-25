"""Runtime state and cache storage for the AMap tool."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from common.utils import expand_path
from agent.tools.amap.models import GeoPoint


class AmapStateStore:
    """Stores non-secret AMap profile and geocode cache data."""

    def __init__(self, base_dir: str = "", path: str = ""):
        if path:
            state_path = Path(expand_path(str(path)))
            self.base_dir = state_path.parent
            self.profile_path = state_path
            self.cache_path = state_path
        else:
            root = Path(expand_path(base_dir)) if base_dir else Path(expand_path("~/cow")) / "data" / "amap-cowwechat"
            self.base_dir = root
            self.profile_path = self.base_dir / "profile.json"
            self.cache_path = self.base_dir / "geocode_cache.json"

    def get_profile_location(self, kind: str) -> Optional[GeoPoint]:
        normalized = self._normalize_kind(kind)
        profile = self._read_json(self.profile_path, {})
        data = profile.get(normalized)
        if isinstance(data, dict) and data.get("location"):
            return GeoPoint(
                name=data.get("name") or self._display_kind(normalized),
                location=data["location"],
                address=data.get("address", ""),
                city=data.get("city", ""),
                adcode=data.get("adcode", ""),
            )

        env_point = self._location_from_env(normalized)
        if env_point:
            return env_point
        return None

    def set_profile_location(self, kind: str, point: GeoPoint) -> None:
        normalized = self._normalize_kind(kind)
        profile = self._read_json(self.profile_path, {})
        profile[normalized] = {
            "name": point.name or self._display_kind(normalized),
            "location": point.location,
            "address": point.address,
            "city": point.city,
            "adcode": point.adcode,
        }
        self._write_json(self.profile_path, profile)

    def get_cached_geocode(self, address: str, city: str = "") -> Optional[GeoPoint]:
        key = self._cache_key(address, city)
        cache = self._read_json(self.cache_path, {})
        data = cache.get(key)
        if not isinstance(data, dict) or not data.get("location"):
            return None
        return GeoPoint(
            name=data.get("name") or address,
            location=data["location"],
            address=data.get("address") or address,
            city=data.get("city", ""),
            adcode=data.get("adcode", ""),
        )

    def set_cached_geocode(self, address: str, city: str, point: GeoPoint) -> None:
        cache = self._read_json(self.cache_path, {})
        cache[self._cache_key(address, city)] = {
            "name": point.name,
            "location": point.location,
            "address": point.address,
            "city": point.city,
            "adcode": point.adcode,
        }
        self._write_json(self.cache_path, cache)

    def write_cache(self, key: str, value: Any) -> None:
        cache = self._read_json(self.cache_path, {})
        cache[str(key)] = value
        self._write_json(self.cache_path, cache)

    set_cache = write_cache
    cache_set = write_cache

    def read_cache(self, key: str, default: Any = None) -> Any:
        cache = self._read_json(self.cache_path, {})
        return cache.get(str(key), default)

    get_cache = read_cache
    cache_get = read_cache

    def clear_cached_geocode(self, address: str = "", city: str = "") -> None:
        if not self.cache_path.exists():
            return
        if not address:
            self._write_json(self.cache_path, {})
            return
        cache = self._read_json(self.cache_path, {})
        cache.pop(self._cache_key(address, city), None)
        self._write_json(self.cache_path, cache)

    def set_home(self, address: str, location: str, **kwargs) -> None:
        self.set_profile_location("home", GeoPoint(name="家", location=location, address=address))

    update_home = set_home

    def set_company(self, address: str, location: str, **kwargs) -> None:
        self.set_profile_location("company", GeoPoint(name="公司", location=location, address=address))

    update_company = set_company

    def get_home(self) -> Optional[Dict[str, Any]]:
        point = self.get_profile_location("home")
        return None if point is None else point.__dict__

    home = get_home

    def get_company(self) -> Optional[Dict[str, Any]]:
        point = self.get_profile_location("company")
        return None if point is None else point.__dict__

    company = get_company

    def _location_from_env(self, kind: str) -> Optional[GeoPoint]:
        prefix = "AMAP_HOME" if kind == "home" else "AMAP_COMPANY"
        lonlat = os.environ.get(f"{prefix}_LONLAT", "").strip()
        address = os.environ.get(f"{prefix}_ADDRESS", "").strip()
        if lonlat:
            return GeoPoint(
                name=self._display_kind(kind),
                location=lonlat,
                address=address,
                city=os.environ.get("AMAP_DEFAULT_CITY", "").strip(),
                adcode=os.environ.get("AMAP_DEFAULT_ADCODE", "").strip(),
            )
        return None

    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            if not path.exists():
                return default
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if data is not None else default
        except Exception:
            return default

    def _write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp_path.replace(path)

    @staticmethod
    def _cache_key(address: str, city: str = "") -> str:
        return f"{city.strip()}|{address.strip()}"

    @staticmethod
    def _normalize_kind(kind: str) -> str:
        raw = str(kind or "").strip().lower()
        if raw in ("company", "work", "office", "公司", "单位"):
            return "company"
        return "home"

    @staticmethod
    def _display_kind(kind: str) -> str:
        return "公司" if kind == "company" else "家"
