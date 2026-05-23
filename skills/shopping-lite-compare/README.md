# shopping-lite-compare

本 skill 提供网购只读比价工作流，只在用户明确提出“比价”“查淘宝/京东/抖音价格”“哪里买便宜”“查券”“全网比价”“同款比价”“价格对比”等需求时触发。

它不处理外卖推荐。外卖、今天吃什么、美团红包、点外卖等场景应使用 `takeout-lite-recommender`。

## 核心辅助 Skill

必须安装 ClawHub `taobao`：

```powershell
openclaw.cmd skills install taobao
openclaw.cmd skills list
```

如果 `taobao` 安装失败，本 skill 只能提供比价流程和安全提醒，无法完成真实商品查价、查券或跨平台比价。

## 允许范围

- 只读搜索商品。
- 只读查价、查券、对比平台结果。
- 平台范围：淘宝/天猫、京东、抖音；必要时可全平台搜索。
- 输出平台、商品标题、展示价格、优惠信息、销量/评价等辅助 skill 返回的非敏感信息。
- 建议用户回官方 App 核验价格、优惠、店铺、售后、运费和库存。

## 禁止范围

- 不自动打开购买链接、返利链接、推广链接、短链或跳转链接。
- 不自动下单，不自动加购。
- 不自动填写地址、手机号、验证码、cookie、token。
- 不自动登录。
- 不自动使用邀请码、返利码或分享身份完成购买。
- 默认不把返利、邀请码或推广身份作为推荐依据。

## 已检查的 taobao Skill 披露

当前 OpenClaw 安装的 `taobao@1.0.3` 实际 skill frontmatter 名称为 `maishou`。其脚本位于：

`C:\Users\RondleLiu\.openclaw\workspace\skills\taobao\scripts\main.py`

已发现：

- 脚本请求第三方站点 `maishou88.com` 的商品和价格数据。
- 脚本含默认邀请码 `6110440`，也可由环境变量 `MAISHOU_INVITE_CODE` 覆盖。
- 脚本含 hardcoded `openid`。
- 搜索结果包含 `commission` 字段，说明上游可能存在返利或联盟佣金信息。
- 脚本的 `detail` 命令会请求购买链接和复制口令。

因此本 CowWechat skill 默认只用于只读比价搜索，不自动调用购买链路，不自动打开或使用购买/返利/推广链接，不自动使用邀请码完成购买。
本 skill 不新增或隐藏任何邀请码、openid/user ID、affiliate ID、referral ID、rebate code、PID/adzone 或推广链接模板；如果上游返回佣金、返利、分享或推广链接相关信息，应明确披露并建议用户回官方 App 手动核验。

## 验收样例

用户：

```text
帮我比较淘宝、京东、抖音上苹果20W充电器哪里便宜
```

期望：

- 触发 `shopping-lite-compare`。
- 不触发 `takeout-lite-recommender`。
- 输出淘宝/天猫、京东、抖音或全平台搜索摘要。
- 输出平台、商品标题、价格、优惠信息。
- 不自动打开购买链接，不要求手机号、地址、验证码、cookie、token。

用户：

```text
帮我下单最便宜那个
```

期望：

- 拒绝自动下单。
- 提醒用户回官方 App 自行核验价格、店铺、售后、运费和优惠券。
- 可以保留比价建议，但不能进入购买自动化。
