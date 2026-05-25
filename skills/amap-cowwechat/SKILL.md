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
高德 规划旅游 北京：故宫、景山公园、南锣鼓巷、三里屯
高德 分析路线 上海：外滩 -> 豫园 -> 陆家嘴 -> 迪士尼
```

如果用户没有提供城市、起点、终点或交通方式，而当前上下文也无法推断，先问一个简短问题补齐必要信息。

## 工具调用

优先调用 `amap` 工具：

- `set_profile_location`：设置家或公司。
- `commute_status`：查询上班/下班通勤。
- `route_plan`：查询自定义路线。
- `traffic_status`：查询路况，基础版优先解析驾车路线返回的 `tmcs`。
- `analyze_travel_route`：分析旅游路线合理性并推荐分段交通方式。
- `geocode`、`reverse_geocode`、`poi_search`：地点解析和 POI 查询。

CLI 包装器示例：

```powershell
python "<base_dir>\scripts\amap_cowwechat.py" set-home "北京市朝阳区望京SOHO"
python "<base_dir>\scripts\amap_cowwechat.py" commute --direction home_to_company
python "<base_dir>\scripts\amap_cowwechat.py" route "北京南站" "故宫" --mode driving --city 北京
python "<base_dir>\scripts\amap_cowwechat.py" travel "北京：故宫、景山公园、南锣鼓巷、三里屯"
```

## 高德 API 要求

- 地理编码：`/v3/geocode/geo`
- 逆地理编码：`/v3/geocode/regeo`
- POI 搜索：优先 `/v5/place/text`，权限不足时降级 `/v3/place/text`
- 路线规划：优先 v5 驾车、步行、骑行、电动车、公交换乘接口
- 驾车路线默认请求 `show_fields=cost,tmcs,navi,polyline,cities`
- 拥堵分析优先解析驾车路线返回的 `tmcs`
- 高级交通态势仅在 `AMAP_ENABLE_ADVANCED_TRAFFIC=true` 时尝试，权限不足不能影响基础路线规划

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
