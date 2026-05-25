---
name: amap-cowwechat
description: Use 高德地图 / AMap Web Service for CowWechat commute, route planning, traffic status, travel time, driving, transit, walking, cycling, geocoding, distance, and trip advice.
metadata:
  openclaw:
    requires:
      anyEnv:
        - AMAP_WEBSERVICE_KEY
        - SKILL_AMAP_COWWECHAT_WEBSERVICE_KEY
        - AMAP_KEY
    primaryEnv: AMAP_WEBSERVICE_KEY
---

# AMap CowWechat

Use this built-in skill when the user asks for 高德地图 / AMap commute, route, traffic, ETA, distance, or local travel guidance, especially Chinese requests such as:

- "查一下从公司到家现在开车要多久"
- "高德看下去虹桥机场的路线"
- "现在到深圳北站堵不堵"
- "明早 8 点通勤路线怎么走"
- "帮我规划步行/骑行/公交/驾车路线"

## Key Selection

Read the Web Service key from the first available environment variable in this order:

1. `AMAP_WEBSERVICE_KEY`
2. `SKILL_AMAP_COWWECHAT_WEBSERVICE_KEY`
3. `AMAP_KEY`

Prefer `AMAP_WEBSERVICE_KEY` in documentation and setup instructions. `AMAP_KEY` is accepted only as a compatibility alias.

## Command Patterns

Use natural Chinese command recognition. Treat these as equivalent trigger styles:

```text
高德 查路线 北京南站 到 首都机场
高德 通勤 从 公司 到 家
高德 路况 中关村 到 望京
高德 计算距离 上海虹桥站 到 人民广场
高德 旅游路线 杭州西湖 半天
```

When origin or destination is missing, ask one concise follow-up question. If a place name is ambiguous, ask for city or district before calling AMap.

## Workflow

1. Identify intent: commute, route, traffic, travel planning, geocoding, distance, or ETA.
2. Extract origin, destination, city, travel mode, departure time, and constraints.
3. Use AMap Web Service only after confirming enough location detail.
4. Return a concise Chinese answer with route summary, expected time, distance, traffic condition, and practical caveats.
5. Mention that traffic and ETA are time-sensitive and may change.

## Safety

- Never print, log, paste, or include the raw AMap key in replies, command output, docs, screenshots, or handoff notes.
- Store keys in environment variables or the secure `env_config` flow, not in prompts or committed files.
- If a user sends a key in chat, tell them it should be rotated if exposed and configure only the redacted operational fact.
- Do not commit real keys to `config-template.json`, docs, examples, or skill files.
