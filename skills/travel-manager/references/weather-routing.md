# Weather Routing

Weather is dynamic and must be source-backed.

## Default Routing

- China mainland: prefer AMap weather when `amap-cowwechat` weather support is available and a Web Service key is configured.
- China mainland fallback: use `quick-weather` through Open-Meteo when AMap is unavailable, has no key, or returns permission/quota errors.
- International destinations: use `quick-weather` or another reliable global weather source. Disambiguate city and country before querying.

## AMap Weather

AMap weather uses `/v3/weather/weatherInfo` and is best for China mainland city-level live and forecast weather. It requires a Web Service key and an adcode.

Expected command shape:

```powershell
python skills/amap-cowwechat/scripts/amap_cowwechat.py weather 成都 --type live
python skills/amap-cowwechat/scripts/amap_cowwechat.py weather 成都 --type forecast
```

If key, permission, quota, or API errors occur, do not expose the key. Fall back to `quick-weather`.

## quick-weather

`quick-weather` uses Open-Meteo and does not need an API key. Use it for no-key fallback and international weather.

For international Chinese city names, prefer explicit country disambiguation:

```powershell
python skills/quick-weather/scripts/quick_weather.py --place 东京 --country-code JP --days 3
python skills/quick-weather/scripts/quick_weather.py --place 纽约 --country-code US --days 3
```

## Output Rules

Weather output should include:

- Source
- City and country/region interpreted
- Forecast date range
- Temperature
- Rain probability or precipitation
- Wind
- Clothing and outdoor backup

Do not promise long-range forecast precision.
