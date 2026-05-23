# takeout-lite-recommender

轻量版本地外卖推荐 skill。它只做“今天吃什么”推荐、美团红包提醒和美团 App 搜索关键词建议，不查询真实附近店铺、真实菜单、实时评分、配送费、配送时间或可配送状态。

## 依赖与辅助能力

建议安装以下 ClawHub skills：

```powershell
openclaw.cmd skills install eat-what-today-skill
openclaw.cmd skills install meituan-coupons
openclaw.cmd skills install taobao
```

其中 `eat-what-today-skill` 和 `meituan-coupons` 是本 skill 的辅助能力。`taobao` 属于 `shopping-lite-compare`，不会被本外卖推荐 skill 触发。

## 本地脚本

```powershell
python skills/takeout-lite-recommender/scripts/takeout_lite.py "今天中午点外卖，预算30，不想吃太油"
python skills/takeout-lite-recommender/scripts/takeout_lite.py "今晚加班，想吃热的，预算35" --coupon true
```

脚本不联网，不调用美团接口，不读取或上传手机号、验证码、token、cookie、收货地址等敏感信息。

## 合规边界

- 不调用未授权美团外卖商家 API。
- 不爬取美团 App 或网页。
- 不伪造真实附近店铺、评分、配送费、配送时间、菜单或可配送状态。
- 可以展示 `meituan-coupons` 返回的 `jump_url`，格式为 `[立即使用](jump_url)`，方便用户在企业微信里手动点击。
- 不自动下单，不点击购买、领券或外卖下单链接。
- 不自行拼装、猜测、缩短或改写红包链接。
- 最终店铺评分、配送时间、配送费、满减和可配送状态以美团 App 实际页面为准。

## Future

未来如果获得合规的美团外卖 API 权限，可以新增 Merchant API Layer。当前版本禁止伪造真实店铺数据，也不把任何模拟数据包装成实时结果。
