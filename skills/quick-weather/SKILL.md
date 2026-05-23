---
name: quick-weather
description: Quickly fetch current and forecast weather through Open-Meteo without an API key. Use when the user asks for today's weather, tomorrow's weather, rain, temperature, wind, forecast, 出门建议, 成都天气, 深圳天气, or fast weather lookup in CowWechat.
metadata:
  requires:
    bins: ["python"]
---

# Quick Weather

Use this skill for fast weather answers without invoking a general web search. It is based on the `D:\qq_openclaw` direct Open-Meteo pattern: extract a place, geocode when needed, fetch forecast data, then return a compact Chinese answer with practical advice.

## Quick Command

```powershell
python "<base_dir>\scripts\quick_weather.py" "明天成都天气"
python "<base_dir>\scripts\quick_weather.py" --place 深圳 --days 3
python "<base_dir>\scripts\quick_weather.py" "上海今天会下雨吗" --json
```

## Workflow

1. Use `quick_weather.py` when the request is mainly weather, temperature, rain, wind, or travel-outfit advice.
2. Prefer `--place` if the user names the city clearly; otherwise pass the original text so the script can extract a city.
3. For date requests:
   - `今天` uses `--days 1`.
   - `明天` starts from tomorrow.
   - `未来3天` or `三天天气` uses the requested day count, capped at 16 days.
4. Return the script's Chinese summary. Mention `Open-Meteo` as the source.
5. If the city is ambiguous or not found, ask the user for a more specific place.

## Notes

- No API key is required.
- Built-in city coordinates cover common Chinese cities for speed; other places use Open-Meteo geocoding.
- Open-Meteo forecast range is limited; do not promise long-range precision.
