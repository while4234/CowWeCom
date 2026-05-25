---
name: amap-cowwechat
description: 使用高德地图 / AMap Web Service 为 CowWechat 提供通勤 ETA、路线规划、路况分析、地理编码、POI 查询和旅游路线合理性分析。Use when the user asks 高德, AMap, 通勤, 上班, 下班, 家到公司, 路线, 路况, ETA, 出行时间, 景点路线, or travel route optimization in China.
metadata:
  openclaw:
    requires:
      anyEnv:
        - AMAP_WEBSERVICE_KEY
        - SKILL_AMAP_COWWECHAT_WEBSERVICE_KEY
        - AMAP_KEY
    primaryEnv: AMAP_WEBSERVICE_KEY
    emoji: "🗺️"
---

# 高德通勤与旅行路线 Skill

在用户询问高德地图、AMap、通勤、家到公司、公司到家、路线、路况、预计到达时间、POI、景点动线或旅游路线合理性时使用此 Skill。

优先调用内置 `amap` Agent 工具。需要排查或给定结构化命令时，可以运行本 Skill 的 CLI 包装脚本。

## Key 安全

- 只读取 Web Service Key，不使用 Android/iOS/小程序/JS SDK Key。
- 推荐环境变量：`AMAP_WEBSERVICE_KEY`。
- 兼容环境变量：`SKILL_AMAP_COWWECHAT_WEBSERVICE_KEY`、`AMAP_KEY`。
- 可选高级路况开关：`AMAP_ENABLE_ADVANCED_TRAFFIC=true`。只有在确认当前 Key 已开通高德高级交通态势相关能力时才开启。
- 不要把 Key 写入源码、日志、聊天回复、`config.json`、`SKILL.md`、脚本或 Git。
- 如果用户在聊天里发送了真实 Key，提醒其考虑在高德后台轮换 Key，并改用本机环境变量配置。

## 支持命令

```text
高德 设置家 北京市朝阳区望京SOHO
高德 设置公司 北京市海淀区中关村软件园
高德 上班
高德 通勤
高德 家到公司
高德 下班
高德 公司到家
高德 路线 北京南站 到 故宫
高德 驾车 北京南站 到 故宫
高德 公交 北京南站 到 故宫
高德 路况 家 公司
高德 路况 北京南站 到 故宫
高德 查 五环路 路况
高德 查 公司附近 3 公里路况
高德 查 北京站到国贸范围内路况
高德 规划旅游 北京：故宫、景山公园、南锣鼓巷、三里屯
高德 分析路线 上海：外滩 -> 豫园 -> 陆家嘴 -> 迪士尼
```

如果用户没有提供城市、起点、终点或交通方式，而当前上下文也无法推断，先问一个简短问题补齐必要信息。

## 工具调用

优先调用 `amap` 工具：

- `set_profile_location`：设置家或公司。
- `commute_status`：查询上班/下班通勤。
- `route_plan`：查询自定义路线。
- `traffic_status`：查询路况。默认基础版优先解析驾车路线返回的 `tmcs`；当 `AMAP_ENABLE_ADVANCED_TRAFFIC=true` 且 Key 有权限时，可按道路、圆形区域或矩形区域补充高级交通态势。
- `analyze_travel_route`：分析旅游路线合理性并推荐分段交通方式。
- `geocode`、`reverse_geocode`、`poi_search`：地点解析和 POI 查询。

CLI 包装器示例：

```powershell
python "<base_dir>\scripts\amap_cowwechat.py" set-home "北京市朝阳区望京SOHO"
python "<base_dir>\scripts\amap_cowwechat.py" commute --direction home_to_company
python "<base_dir>\scripts\amap_cowwechat.py" route "北京南站" "故宫" --mode driving --city 北京
python "<base_dir>\scripts\amap_cowwechat.py" traffic "北京南站" "故宫" --city 北京
python "<base_dir>\scripts\amap_cowwechat.py" traffic-road "东三环" --adcode 110000
python "<base_dir>\scripts\amap_cowwechat.py" traffic-circle "116.305776,39.986414" --radius 3000
python "<base_dir>\scripts\amap_cowwechat.py" traffic-rectangle "116.351147,39.966309;116.357134,39.968727"
python "<base_dir>\scripts\amap_cowwechat.py" travel "北京：故宫、景山公园、南锣鼓巷、三里屯"
```

启用高级交通态势的本机示例：

```powershell
$env:AMAP_ENABLE_ADVANCED_TRAFFIC='true'
python "<base_dir>\scripts\amap_cowwechat.py" traffic-road "东三环" --adcode 110000
```

## 高级交通态势

`AMAP_ENABLE_ADVANCED_TRAFFIC` 默认关闭。开启后，路况查询可以在基础路线 `tmcs` 之外尝试高德高级交通态势能力，用于三类问题：

- 道路路况：用户指定明确道路、高架、环路或快速路，例如“查五环路路况”。适合回答一条道路当前整体是否畅通，以及拥堵集中在哪些路段。
- 圆形区域路况：用户关注某个点位周边，例如“公司附近 3 公里路况”“机场周边路况”。适合出发前判断附近路网是否拥堵。
- 矩形区域路况：用户关注两个地点或一个城区范围内的路网，例如“北京站到国贸范围内路况”。适合区域拥堵概览、绕行建议和活动周边研判。

使用高级交通态势时必须先解析地点或道路语义，缺少城市、半径或范围边界时先追问一个简短问题。不要要求用户提供 Key，也不要在回复中输出 Key。

权限、额度或限流降级要求：

- 如果高级交通态势接口返回权限不足、配额不足、限流或不支持当前区域，必须降级到基础路线规划和 `tmcs` 路况摘要。
- 降级不应让通勤、路线规划、ETA 或旅游路线分析失败；回复中可简短说明“高级路况不可用，已使用路线实时路况估算”。
- 不要重试到刷额度；同一次用户请求内最多尝试必要的一次高级查询，然后使用基础结果。

## 高德 API 要求

- 地理编码：`/v3/geocode/geo`
- 逆地理编码：`/v3/geocode/regeo`
- POI 搜索：优先 `/v5/place/text`，权限不足时降级 `/v3/place/text`
- 路线规划：优先 v5 驾车、步行、骑行、电动车、公交换乘接口
- 驾车路线默认请求 `show_fields=cost,tmcs,navi,polyline,cities`
- 拥堵分析优先解析驾车路线返回的 `tmcs`
- 高级交通态势仅在 `AMAP_ENABLE_ADVANCED_TRAFFIC=true` 时尝试，按 road/circle/rectangle 场景选择合适查询方式；权限不足、额度不足或限流不能影响基础路线规划

## 输出要求

回复中文摘要，不直接输出大段 JSON。微信消息优先包含：

- 推荐路线或推荐顺序
- 预计耗时和 ETA
- 距离
- 当前路况
- 主要拥堵路段
- 备选路线
- 风险提示和调整建议

旅游路线必须说明每段交通方式、每段耗时、总交通耗时、是否合理，以及是否存在过密、折返、跨城、公交换乘过多或步行过长等风险。
