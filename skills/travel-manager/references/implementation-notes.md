# Implementation Notes

## Modified Files

- `skills/travel-manager/SKILL.md`
- `skills/travel-manager/references/output-template.md`
- `skills/travel-manager/references/source-policy.md`
- `skills/travel-manager/references/transport-routing.md`
- `skills/travel-manager/references/weather-routing.md`
- `skills/travel-manager/references/region-mainland-china.md`
- `skills/travel-manager/references/region-global-profiles.md`
- `skills/travel-manager/references/travel-documents.md`
- `skills/travel-manager/references/family-travel-checklist.md`
- `agent/tools/amap/*`
- `skills/amap-cowwechat/*`
- `skills/quick-weather/*`
- `skills/flyai/*`

## Skill Integration

- `amap-cowwechat`: added city-level weather routing through AMap `/v3/weather/weatherInfo`; route, POI, ETA, traffic, and route-density planning remain the main AMap responsibilities.
- `plugin-12306-ticket`: confirmed stations, tickets, and train-stop queries work for China railway planning. Availability remains volatile and user booking/payment/document submission is out of scope.
- `flyai`: installed skill files through `clawhub` as optional travel inventory guidance. Use `skills/flyai/scripts/flyai_wrapper.py` for data-returning commands so valid stdout JSON remains usable when the Windows CLI exits nonzero with the known libuv assertion.
- `quick-weather`: added international city alias and `--country-code` support for Open-Meteo geocoding. It remains the no-key weather fallback and default international weather source.

## Local Path Portability

- Runtime examples should be run from the CowWechat repository root and use relative script paths such as `python skills\flyai\scripts\flyai_wrapper.py ...`.
- When a skill CLI is launched from a runtime skill copy on another machine, set `COWWECHAT_ROOT` to the repository root so shared imports resolve without hard-coded drive letters.
- `amap-cowwechat` checks the current directory, script parents, `COWWECHAT_ROOT`, `COWWECHAT_REPO_ROOT`, and `COWAGENT_ROOT` before legacy local fallbacks.

## Weather Decision

- AMap weather available: code path and CLI are present, but no AMap Web Service key is configured in this shell, so only the missing-key path was validated.
- quick-weather available: yes. Open-Meteo smoke tests passed for Chengdu, Tokyo/JP, and New York/US.
- Recommended source: China mainland AMap when a Web Service key is available; quick-weather fallback when AMap key/permission/quota is unavailable; international weather through quick-weather with city/country disambiguation.
- Fallback: mark weather as "待查询" or ask for city/country disambiguation when neither source can resolve the location.

## Smoke Test Results

### Static

Command:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_amap_tool.py tests\test_skill_travel_prompt.py tests\test_skill_catalog_cache.py tests\test_skill_display_names.py
```

Result: 40 passed.

Command:

```powershell
.venv\Scripts\python.exe skills\skill-creator\scripts\quick_validate.py skills\travel-manager
.venv\Scripts\python.exe skills\skill-creator\scripts\quick_validate.py skills\amap-cowwechat
.venv\Scripts\python.exe skills\skill-creator\scripts\quick_validate.py skills\quick-weather
.venv\Scripts\python.exe skills\skill-creator\scripts\quick_validate.py skills\flyai
```

Result: all four skills are valid.

Command:

```powershell
.venv\Scripts\python.exe -m py_compile skills\quick-weather\scripts\quick_weather.py skills\amap-cowwechat\scripts\amap_cowwechat.py
```

Result: passed.

### 12306

Command:

```powershell
.venv\Scripts\python.exe skills\plugin-12306-ticket\scripts\railway_12306.py stations 北京 --limit 5
.venv\Scripts\python.exe skills\plugin-12306-ticket\scripts\railway_12306.py tickets 北京南 上海虹桥 2026-06-02 --limit 5
.venv\Scripts\python.exe skills\plugin-12306-ticket\scripts\railway_12306.py route G547 北京南 上海虹桥 2026-06-02
```

Result: station resolution succeeded; ticket query returned five bookable Beijing South to Shanghai Hongqiao/Shanghai trains; G547 route returned 13 stops. Availability can change quickly.

### AMap

Command:

```powershell
.venv\Scripts\python.exe skills\amap-cowwechat\scripts\amap_cowwechat.py --help
.venv\Scripts\python.exe skills\amap-cowwechat\scripts\amap_cowwechat.py weather 成都 --type forecast
```

Result: help shows the new `weather` command. Weather query returned exit code 2 with a clear missing-key message because `AMAP_WEBSERVICE_KEY`, `SKILL_AMAP_COWWECHAT_WEBSERVICE_KEY`, and `AMAP_KEY` were absent in this shell.

### quick-weather

Command:

```powershell
.venv\Scripts\python.exe skills\quick-weather\scripts\quick_weather.py --place 成都 --days 1
.venv\Scripts\python.exe skills\quick-weather\scripts\quick_weather.py --place 东京 --country-code JP --days 3
.venv\Scripts\python.exe skills\quick-weather\scripts\quick_weather.py --place 纽约 --country-code US --days 3
```

Result: Chengdu, Tokyo, and New York returned Open-Meteo weather summaries with temperature, wind, precipitation, and travel advice.

### FlyAI

Command:

```powershell
$env:COWWECHAT_ROOT = (Get-Location).Path
clawhub inspect flyai --workdir $env:COWWECHAT_ROOT --dir skills --no-input
clawhub install flyai --workdir $env:COWWECHAT_ROOT --dir skills --no-input
flyai --help
python skills\flyai\scripts\flyai_wrapper.py keyword-search --query "东京酒店"
```

Result: inspect and install succeeded for `flyai` version 1.0.15. Later wrapper smoke returned `ok=true` and preserved FlyAI JSON results while warning that the raw Windows CLI exited nonzero after producing valid JSON.

## Known Limitations

- Real-time ticket availability may change.
- 12306 public endpoints may throttle or return non-JSON/anti-bot responses.
- FlyAI skill files are installed; use the local wrapper for real-time data commands and treat wrapper warnings as provenance notes rather than automatic failure.
- Visa and entry rules require official verification.
- Weather forecasts degrade with longer lead time.
- AMap weather requires a configured Web Service key and may fail on permission, quota, or service errors.

## Rollback

- Backup files are stored under `.codex/skill-backups/travel-manager/` and are intentionally not committed.
- If `travel-manager` triggers too broadly, narrow the frontmatter description but keep the body routing policy.
- If AMap weather causes regressions, revert the AMap weather code path and keep quick-weather as the weather source.
- If quick-weather international disambiguation causes regressions, remove the alias/country-code path and require explicit city/country clarification.
- If FlyAI is unusable, keep it optional and use official/OTA/user-provided pages for live flight, hotel, and attraction inventory.
