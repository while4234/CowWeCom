import datetime as dt
import importlib
import inspect
import json

import pytest


try:
    client_mod = importlib.import_module("agent.tools.amap.client")
    service_mod = importlib.import_module("agent.tools.amap.service")
    state_mod = importlib.import_module("agent.tools.amap.state")
    tool_mod = importlib.import_module("agent.tools.amap.tool")
except ImportError:
    client_mod = service_mod = state_mod = tool_mod = None

pytestmark = pytest.mark.skipif(
    client_mod is None,
    reason="AMap public API modules are planned but not present yet.",
)

if client_mod is not None:
    AmapClient = client_mod.AmapClient
    AmapApiError = client_mod.AmapApiError
    MissingAmapKeyError = client_mod.MissingAmapKeyError
    AmapService = service_mod.AmapService
    AmapStateStore = state_mod.AmapStateStore
    AmapTool = tool_mod.AmapTool


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, *items):
        self.items = list(items)
        self.calls = []

    def get(self, url, params=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})
        if not self.items:
            raise AssertionError(f"Unexpected AMap request to {url}")
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return FakeResponse(item)


class TimeoutErrorForTest(Exception):
    pass


def patch_http(monkeypatch, fake_http):
    if hasattr(client_mod, "requests"):
        monkeypatch.setattr(client_mod.requests, "get", fake_http.get, raising=False)

        class FakeSession:
            def get(self, url, params=None, timeout=None, **kwargs):
                return fake_http.get(url, params=params, timeout=timeout, **kwargs)

        monkeypatch.setattr(client_mod.requests, "Session", lambda: FakeSession(), raising=False)
        if hasattr(client_mod.requests, "Timeout"):
            monkeypatch.setattr(client_mod.requests, "Timeout", TimeoutErrorForTest, raising=False)


def make_client(monkeypatch, fake_http=None, api_key="unit-test-key", **overrides):
    monkeypatch.setenv("AMAP_API_KEY", api_key)
    monkeypatch.setenv("AMAP_KEY", api_key)
    if fake_http is not None:
        patch_http(monkeypatch, fake_http)

    attempts = [
        {"api_key": api_key, **overrides},
        {"key": api_key, **overrides},
        {"amap_key": api_key, **overrides},
        {"config": {"api_key": api_key, **overrides}},
        {**overrides},
    ]
    last_error = None
    for kwargs in attempts:
        try:
            return AmapClient(**kwargs)
        except TypeError as exc:
            last_error = exc
    raise last_error


def make_service(client=None, state_store=None, **overrides):
    attempts = [
        {"client": client, "state_store": state_store, **overrides},
        {"amap_client": client, "state_store": state_store, **overrides},
        {"client": client, "store": state_store, **overrides},
        {**overrides},
    ]
    last_error = None
    for kwargs in attempts:
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        try:
            return AmapService(**kwargs)
        except TypeError as exc:
            last_error = exc
    raise last_error


def call_public(obj, method_names, **kwargs):
    for name in method_names:
        method = getattr(obj, name, None)
        if method is None:
            continue
        signature = inspect.signature(method)
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
            return method(**kwargs)
        accepted = {key: value for key, value in kwargs.items() if key in signature.parameters}
        return method(**accepted)
    raise AssertionError(f"{type(obj).__name__} exposes none of {method_names}")


def value(result, *names, default=None):
    current = result
    for name in names:
        if current is None:
            return default
        if isinstance(current, dict):
            current = current.get(name, default)
        else:
            current = getattr(current, name, default)
    return current


def text_blob(result):
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    return json.dumps(getattr(result, "__dict__", str(result)), ensure_ascii=False, default=str)


def assert_contains_any(blob, expected_parts):
    assert any(part in blob for part in expected_parts), blob


def test_client_geocode_success_parses_first_location(monkeypatch):
    fake_http = FakeHttp(
        {
            "status": "1",
            "infocode": "10000",
            "geocodes": [
                {
                    "formatted_address": "北京市朝阳区望京SOHO",
                    "location": "116.480881,39.996236",
                    "adcode": "110105",
                    "city": "北京市",
                }
            ],
        }
    )
    client = make_client(monkeypatch, fake_http)

    result = call_public(client, ["geocode"], address="望京SOHO", city="北京")

    assert value(result, "formatted_address") == "北京市朝阳区望京SOHO"
    assert value(result, "location") in ("116.480881,39.996236", (116.480881, 39.996236))
    assert fake_http.calls[0]["params"]["key"] == "unit-test-key"


def test_client_geocode_not_found_is_api_error(monkeypatch):
    fake_http = FakeHttp({"status": "1", "infocode": "10000", "count": "0", "geocodes": []})
    client = make_client(monkeypatch, fake_http)

    with pytest.raises(AmapApiError):
        call_public(client, ["geocode"], address="不存在的位置", city="北京")


def test_client_requires_api_key(monkeypatch):
    monkeypatch.delenv("AMAP_API_KEY", raising=False)
    monkeypatch.delenv("AMAP_KEY", raising=False)

    try:
        client = AmapClient()
    except MissingAmapKeyError:
        return

    with pytest.raises(MissingAmapKeyError):
        call_public(client, ["geocode"], address="望京SOHO", city="北京")


def test_client_infocode_error_raises_contextual_api_error(monkeypatch):
    fake_http = FakeHttp(
        {
            "status": "0",
            "infocode": "10001",
            "info": "INVALID_USER_KEY",
        }
    )
    client = make_client(monkeypatch, fake_http)

    with pytest.raises(AmapApiError) as exc_info:
        call_public(client, ["geocode"], address="望京SOHO", city="北京")

    error_text = str(exc_info.value)
    assert "10001" in error_text
    assert "INVALID_USER_KEY" in error_text


def test_client_retries_once_after_timeout(monkeypatch):
    fake_http = FakeHttp(
        TimeoutErrorForTest("socket timed out"),
        {
            "status": "1",
            "infocode": "10000",
            "geocodes": [{"formatted_address": "北京市", "location": "116.4074,39.9042"}],
        },
    )
    client = make_client(monkeypatch, fake_http, max_retries=1)

    result = call_public(client, ["geocode"], address="北京", city="北京")

    assert value(result, "location") in ("116.4074,39.9042", (116.4074, 39.9042))
    assert len(fake_http.calls) == 2


def test_driving_route_parses_duration_eta_and_tmcs(monkeypatch):
    fake_http = FakeHttp(
        {
            "status": "1",
            "infocode": "10000",
            "route": {
                "paths": [
                    {
                        "distance": "23800",
                        "duration": "2700",
                        "strategy": "速度优先",
                        "steps": [
                            {
                                "instruction": "沿京密路行驶",
                                "tmcs": [
                                    {
                                        "distance": "1200",
                                        "status": "畅通",
                                        "polyline": "116.1,39.1;116.2,39.2",
                                    },
                                    {"distance": "800", "status": "拥堵"},
                                ],
                            }
                        ],
                    }
                ]
            },
        }
    )
    client = make_client(monkeypatch, fake_http)

    result = call_public(
        client,
        ["driving_route", "route_driving", "driving"],
        origin="116.480881,39.996236",
        destination="116.397428,39.90923",
        strategy=0,
        departure_time=dt.datetime(2026, 5, 25, 8, 0, 0),
    )
    blob = text_blob(result)

    assert_contains_any(blob, ["2700", "45", "45分钟"])
    assert_contains_any(blob, ["ETA", "eta", "到达"])
    assert_contains_any(blob, ["畅通", "free_flow"])
    assert_contains_any(blob, ["拥堵", "congested"])


def test_congestion_status_normalization():
    service = make_service()

    normalized = call_public(
        service,
        ["normalize_congestion_status", "_normalize_congestion_status"],
        status="严重拥堵",
    )

    assert normalized in {"severe", "severe_congestion", "heavy", "严重拥堵"}


def test_multi_strategy_scoring_prefers_balanced_route():
    class FakeClient:
        def driving_route(self, origin, destination, strategy=None, **kwargs):
            routes = {
                0: {"duration": 2400, "distance": 46000, "congestion_score": 10, "strategy": "fast"},
                2: {"duration": 2700, "distance": 31000, "congestion_score": 1, "strategy": "balanced"},
                5: {"duration": 3600, "distance": 29000, "congestion_score": 3, "strategy": "avoid"},
            }
            return routes[strategy]

    service = make_service(client=FakeClient())

    result = call_public(
        service,
        ["score_routes", "rank_driving_strategies", "compare_driving_strategies"],
        origin="116.1,39.1",
        destination="116.9,39.9",
        strategies=[0, 2, 5],
    )
    blob = text_blob(result)

    assert "balanced" in blob
    assert blob.find("balanced") <= max(blob.find("fast"), 0) or value(result, "best", "strategy") == "balanced"


def test_state_store_cache_and_home_company_update(tmp_path):
    state_path = tmp_path / "amap-state.json"
    store = AmapStateStore(path=state_path)

    call_public(store, ["write_cache", "set_cache", "cache_set"], key="geocode:home", value={"location": "1,2"})
    cached = call_public(store, ["read_cache", "get_cache", "cache_get"], key="geocode:home")
    call_public(store, ["update_home", "set_home"], address="望京SOHO", location="116.480881,39.996236")
    call_public(store, ["update_company", "set_company"], address="国贸", location="116.457,39.908")

    reloaded = AmapStateStore(path=state_path)
    home = call_public(reloaded, ["get_home", "home"])
    company = call_public(reloaded, ["get_company", "company"])

    assert cached == {"location": "1,2"}
    assert value(home, "address") == "望京SOHO"
    assert value(company, "address") == "国贸"


def test_set_profile_location_prefers_poi_search(tmp_path):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def request(self, endpoint, params=None):
            self.calls.append((endpoint, dict(params or {})))
            if endpoint == "/v5/place/text":
                return {
                    "pois": [
                        {
                            "name": "Wangjing SOHO",
                            "location": "116.480881,39.996236",
                            "address": "Wangjing Street",
                            "cityname": "Beijing",
                            "adcode": "110105",
                        }
                    ]
                }
            raise AssertionError(f"Unexpected endpoint: {endpoint}")

    client = FakeClient()
    service = AmapService(client=client, state=AmapStateStore(path=tmp_path / "amap-state.json"))

    point = service.set_profile_location("home", "Wangjing SOHO", city="Beijing")

    assert point.location == "116.480881,39.996236"
    assert client.calls[0][0] == "/v5/place/text"
    assert all(call[0] != "/v3/geocode/geo" for call in client.calls)


def test_poi_v5_fallback_to_v3(monkeypatch):
    fake_http = FakeHttp(
        {"status": "0", "infocode": "40000", "info": "SERVICE_NOT_AVAILABLE"},
        {
            "status": "1",
            "infocode": "10000",
            "pois": [
                {
                    "name": "望京SOHO",
                    "location": "116.480881,39.996236",
                    "address": "望京街",
                }
            ],
        },
    )
    client = make_client(monkeypatch, fake_http)

    result = call_public(client, ["search_poi", "poi_search", "search_pois"], keywords="望京SOHO", city="北京")
    blob = text_blob(result)

    assert "望京SOHO" in blob
    assert len(fake_http.calls) == 2
    assert any("v5" in call["url"] or "place/text" in call["url"] for call in fake_http.calls)


def test_service_selects_travel_mode_and_warns_on_cross_city():
    class FakeClient:
        def geocode(self, address, city=None, **kwargs):
            if city == "北京":
                return {"location": "116.480881,39.996236", "city": "北京市"}
            return {"location": "121.4737,31.2304", "city": "上海市"}

        def driving_route(self, **kwargs):
            return {"mode": "driving", "duration": 18000, "distance": 1200000}

        def transit_route(self, **kwargs):
            return {"mode": "transit", "duration": 21000, "distance": 1200000}

    service = make_service(client=FakeClient())

    result = call_public(
        service,
        ["plan_trip", "route", "plan_route"],
        origin="望京SOHO",
        destination="上海虹桥站",
        origin_city="北京",
        destination_city="上海",
        mode="auto",
    )
    blob = text_blob(result)

    assert_contains_any(blob, ["cross_city", "跨城", "异地"])
    assert_contains_any(blob, ["driving", "transit", "train", "rail"])


def test_skill_discovery_reads_frontmatter(tmp_path):
    skill_dir = tmp_path / "skills" / "amap"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: amap\n"
        "description: AMap routing and POI assistant.\n"
        "---\n\n"
        "# AMap\n",
        encoding="utf-8",
    )

    result = call_public(
        service_mod,
        ["discover_amap_skill", "load_skill_frontmatter", "read_skill_frontmatter"],
        skills_root=tmp_path / "skills",
    )

    assert value(result, "name") == "amap"
    assert "routing" in value(result, "description", default="")


def test_tool_schema_loads_with_expected_actions():
    schema = AmapTool.get_json_schema()
    params = schema.get("parameters", {})
    serialized = json.dumps(schema, ensure_ascii=False)

    assert schema["name"] in {"amap", "amap_tool", "amap_route"}
    assert params.get("type") == "object"
    assert "properties" in params
    assert_contains_any(serialized, ["geocode", "route", "poi", "home", "company"])
    assert "traffic_query_type" in serialized
    assert "road_name" in serialized
    assert "rectangle" in serialized
    assert "weather" in serialized
    assert "weather_type" in serialized


def test_weather_live_resolves_city_adcode_and_parses_result(tmp_path):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def request(self, endpoint, params=None):
            self.calls.append((endpoint, dict(params or {})))
            if endpoint == "/v3/geocode/geo":
                return {
                    "status": "1",
                    "infocode": "10000",
                    "geocodes": [
                        {
                            "formatted_address": "四川省成都市",
                            "location": "104.066541,30.572269",
                            "adcode": "510100",
                            "city": "成都市",
                        }
                    ],
                }
            if endpoint == "/v3/weather/weatherInfo":
                return {
                    "status": "1",
                    "infocode": "10000",
                    "lives": [
                        {
                            "province": "四川",
                            "city": "成都市",
                            "adcode": "510100",
                            "weather": "阴",
                            "temperature": "26",
                            "winddirection": "东南",
                            "windpower": "≤3",
                            "humidity": "66",
                            "reporttime": "2026-05-25 17:00:00",
                        }
                    ],
                }
            raise AssertionError(endpoint)

    service = AmapService(client=FakeClient(), state=AmapStateStore(tmp_path))

    result = service.weather("成都", "live")

    assert value(result, "weather_type") == "live"
    assert value(result, "adcode") == "510100"
    assert value(result, "live", "weather") == "阴"
    assert value(result, "live", "temperature_c") == "26"
    assert service.client.calls[0][0] == "/v3/geocode/geo"
    assert service.client.calls[1] == (
        "/v3/weather/weatherInfo",
        {"city": "510100", "extensions": "base"},
    )


def test_weather_forecast_accepts_adcode_without_geocoding():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def request(self, endpoint, params=None):
            self.calls.append((endpoint, dict(params or {})))
            assert endpoint == "/v3/weather/weatherInfo"
            return {
                "status": "1",
                "infocode": "10000",
                "forecasts": [
                    {
                        "province": "四川",
                        "city": "成都市",
                        "adcode": "510100",
                        "reporttime": "2026-05-25 17:00:00",
                        "casts": [
                            {
                                "date": "2026-05-26",
                                "week": "2",
                                "dayweather": "小雨",
                                "nightweather": "阴",
                                "daytemp": "28",
                                "nighttemp": "21",
                                "daywind": "东南",
                                "daypower": "≤3",
                            }
                        ],
                    }
                ],
            }

    service = AmapService(client=FakeClient())

    result = service.weather("510100", "forecast")

    assert value(result, "weather_type") == "forecast"
    assert value(result, "forecast", "casts", default=[])[0].day_weather == "小雨"
    assert service.client.calls == [
        ("/v3/weather/weatherInfo", {"city": "510100", "extensions": "all"})
    ]


def test_advanced_traffic_road_parses_evaluation_and_roads():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def request(self, endpoint, params=None):
            self.calls.append((endpoint, dict(params or {})))
            assert endpoint == "/v3/traffic/status/road"
            return {
                "status": "1",
                "infocode": "10000",
                "trafficinfo": {
                    "description": "东三环当前局部拥堵",
                    "evaluation": {
                        "status": "3",
                        "expedite": "20",
                        "congested": "40",
                        "blocked": "35",
                        "unknown": "5",
                    },
                    "roads": [
                        {
                            "name": "东三环",
                            "status": "3",
                            "direction": "由北向南",
                            "speed": "18",
                            "polyline": "116.1,39.1;116.2,39.2",
                        }
                    ],
                },
            }

    service = AmapService(
        client=FakeClient(),
        enable_advanced_traffic=True,
        default_adcode="110000",
    )

    result = service.traffic_status("东三环", query_type="road")

    assert value(result, "query_type") == "road"
    assert value(result, "status") == "congested"
    assert value(result, "congested_percent") == 35
    assert value(result, "roads", default=[])[0].name == "东三环"
    endpoint, params = service.client.calls[0]
    assert endpoint == "/v3/traffic/status/road"
    assert params["name"] == "东三环"
    assert params["adcode"] == "110000"
    assert params["extensions"] == "all"


def test_advanced_traffic_disabled_requires_explicit_enable():
    service = AmapService(client=object(), enable_advanced_traffic=False, default_adcode="110000")

    with pytest.raises(service_mod.AmapServiceError):
        service.traffic_status("东三环", query_type="road")


def test_advanced_traffic_permission_error_returns_degraded_summary():
    class FakeClient:
        def request(self, endpoint, params=None):
            raise AmapApiError("高德接口返回失败: SERVICE_NOT_AVAILABLE", info="SERVICE_NOT_AVAILABLE", infocode="40000")

    service = AmapService(client=FakeClient(), enable_advanced_traffic=True, default_adcode="110000")

    result = service.traffic_status("东三环", query_type="road")
    blob = text_blob(result)

    assert value(result, "status") == "unknown"
    assert "高级交通态势不可用" in blob
    assert "40000" in blob


def test_traffic_status_route_still_uses_basic_tmcs_when_destination_present():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def request(self, endpoint, params=None):
            self.calls.append((endpoint, dict(params or {})))
            return {
                "status": "1",
                "infocode": "10000",
                "route": {
                    "paths": [
                        {
                            "distance": "1000",
                            "duration": "600",
                            "steps": [{"tmcs": [{"road": "测试路", "status": "缓行", "distance": "300"}]}],
                        }
                    ]
                },
            }

    service = AmapService(client=FakeClient(), enable_advanced_traffic=False)

    result = service.traffic_status("116.100000,39.100000", "116.200000,39.200000")

    assert value(result, "mode") == "driving"
    assert service.client.calls[0][0] == "/v5/direction/driving"
    assert value(result, "congestion_summary") == "整体缓行"


def test_advanced_traffic_circle_bounds_radius_and_parses_roads():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def request(self, endpoint, params=None):
            self.calls.append((endpoint, dict(params or {})))
            return {
                "status": "1",
                "infocode": "10000",
                "trafficinfo": {
                    "evaluation": {"status": "1", "expedite": "90", "congested": "5", "blocked": "0"},
                    "roads": [{"name": "彩和坊路", "status": "1", "speed": "35"}],
                },
            }

    service = AmapService(client=FakeClient(), enable_advanced_traffic=True)

    result = service.advanced_traffic_circle("116.3057764,39.98641364", radius=9000)

    assert value(result, "query_type") == "circle"
    assert value(result, "status") == "smooth"
    assert service.client.calls[0][0] == "/v3/traffic/status/circle"
    assert service.client.calls[0][1]["radius"] == 4999


def test_advanced_traffic_rectangle_validates_size():
    service = AmapService(client=object(), enable_advanced_traffic=True)

    with pytest.raises(service_mod.AmapServiceError):
        service.advanced_traffic_rectangle("116.000000,39.000000;117.000000,40.000000")


def test_tool_manager_can_load_amap_tool(monkeypatch):
    tools_package = importlib.import_module("agent.tools")
    exported = set(getattr(tools_package, "__all__", []))
    assert "AmapTool" in exported

    from agent.tools.tool_manager import ToolManager

    manager = ToolManager()
    manager.tool_classes.clear()
    monkeypatch.setattr(manager, "_load_mcp_tools", lambda: None)
    manager.load_tools(config_dict={})

    assert any(cls is AmapTool for cls in manager.tool_classes.values())
