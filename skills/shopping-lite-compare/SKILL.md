---
name: shopping-lite-compare
description: Read-only Chinese e-commerce comparison skill for 比价, 淘宝, 天猫, 京东, 拼多多, 抖音, 抖音商城, 哪里买便宜, 查券, 网购推荐, 全网比价, 同款比价, 价格对比, 商品链接, 购买链接, and similar shopping price research requests. Use only when the user explicitly asks for online shopping comparison; never use for takeout or food recommendation.
---

# Shopping Lite Compare

Use this skill for read-only e-commerce price comparison. The core auxiliary skill is the installed ClawHub `taobao` skill, which installs as a `maishou` skill under OpenClaw.

## Scope

Allowed:

- Search and compare product prices when the user explicitly asks for price comparison, coupons, where to buy cheaper, or platform-specific shopping research.
- Use platforms returned by the auxiliary skill, especially Taobao/Tmall, JD, Pinduoduo, Douyin, and all-platform search when no platform is specified.
- Output non-sensitive product information returned by the auxiliary skill, such as platform, product title, displayed price, coupon or discount information, shop/sales/review-like fields when available, and a recommendation reason.
- When the user explicitly asks for 商品链接, 购买链接, or link output, display upstream returned links as Markdown links for manual user tapping only.
- Suggest that the user returns to the official platform App to verify price, shop, after-sales policy, shipping, coupons, and availability.
- Use the bundled `scripts/shopping_compare_helper.py` or the installed `taobao`/`maishou` local script for read-only API search.

Not allowed:

- Do not use this skill for takeout, food recommendation, Meituan takeout, or today-what-to-eat prompts. Use `takeout-lite-recommender` for those.
- Do not automatically open purchase links, rebate links, promotion links, short links, or redirect links. Showing links is allowed only for manual user choice.
- Do not open browser pages, Baidu, Taobao web, JD web, Douyin web, or public search pages as a fallback for price comparison. If the local script cannot return results, report that comparison is incomplete instead of triggering human verification pages.
- Do not automatically place orders, add to cart, fill delivery address, fill phone number, enter verification code, use cookies/tokens, log in, or complete a purchase.
- Do not use rebate links, invite codes, referral identity, or sharing identity as the default basis for recommendation.
- Do not ask the user to provide phone numbers, addresses, verification codes, cookies, tokens, or account credentials.

## Auxiliary Skill Disclosure

The installed OpenClaw `taobao` package currently contains a `maishou` skill. Its bundled script:

- Requests product and price data from `maishou88.com`.
- Contains a default invite code fallback `6110440`, overridable by the `MAISHOU_INVITE_CODE` environment variable.
- Contains a hardcoded `openid` used in search requests.
- Search results include a `commission` field, so rebate or affiliate economics may be present upstream.
- Has a `detail` command that can request target purchase URLs and copy commands. Use it only when the user explicitly asks for links.

Default policy for CowWechat:

- Use this auxiliary skill for read-only search and comparison only.
- Prefer search/list results over detail-link generation unless the user explicitly asks for links.
- Do not open or follow purchase links, promotion links, short links, or app schema links.
- Do not hardcode additional invite codes, openid/user IDs, affiliate IDs, referral IDs, rebate codes, PID/adzone values, or promotion-link templates in this CowWechat skill.
- If purchase links, copy commands, invite codes, openid/user IDs, commission fields, rebate identity, or sharing identity appear in upstream output, disclose that possibility, do not use them for automated purchase, and advise the user to verify manually in official Apps.

## Workflow

1. Extract:
   - Product keyword.
   - Budget or price ceiling if provided.
   - Preferred platform if provided.
   - Whether the user asks to compare only Taobao/Tmall, JD, Douyin, or all platforms.
2. Choose platform scope:
   - No platform specified: use all-platform search.
   - Taobao or Tmall specified: use Taobao/Tmall search.
   - JD specified: use JD search.
   - Douyin or Douyin Mall specified: use Douyin search.
3. Use or guide use of the installed `taobao`/`maishou` auxiliary skill for read-only search and comparison.
   - Prefer: `python "<base_dir>/scripts/shopping_compare_helper.py" "<keyword>" --platform all`
   - For Taobao/Tmall only: use `--platform taobao`
   - For JD only: use `--platform jd`
   - For Pinduoduo only: use `--platform pdd`
   - For Douyin only: use `--platform douyin`
   - If the user explicitly asks for 商品链接 or 购买链接, add `--include-links` and show returned links as manual links.
   - Do not use browser automation or public shopping webpages as fallback.
4. Produce a comparison table with at least:
   - Platform.
   - Product title.
   - Displayed price.
   - Coupon or discount information if returned.
   - Recommendation reason.
   - Risk note.
5. End with a safety reminder:
   - Prices, coupons, shipping, after-sales policy, stock, and seller quality must be verified in the official platform App.
   - No purchase link was opened and no order was placed.

## Refusal For Purchase Automation

If the user asks to buy, order, add to cart, use the cheapest link, auto-open a purchase link, or complete checkout:

1. Refuse the automated purchase action.
2. Keep any existing comparison advice if useful.
3. Tell the user to manually verify in the official App before buying.
4. Do not request or process phone numbers, addresses, verification codes, cookies, tokens, or login credentials.

Example response:

```markdown
我不能替你自动下单、打开购买链接或填写收货/登录信息。可以保留当前比价建议：请回淘宝/天猫、京东或抖音官方 App 手动核验价格、店铺、售后、运费和优惠券后再自行购买。
```

## Local Helper

Run:

```bash
python "<base_dir>/scripts/shopping_compare_helper.py" "苹果20W充电器" --platform all
```

The helper:

- Locates the installed OpenClaw `taobao` package under `%USERPROFILE%\.openclaw\workspace\skills\taobao`.
- Calls its `scripts/main.py search` command through `uv` in read-only mode.
- Searches the requested platform scope without opening a browser.
- Calls only `search` by default.
- Calls upstream `detail` only when `--include-links` is used, then displays returned links/口令 without opening them.

If the helper cannot find the installed script, `uv`, or usable results, state that `shopping-lite-compare` is incomplete and ask the user to install or fix the `taobao` skill. Do not switch to browser search.

## Output Template

```markdown
一、比价结论

二、只读比价表

| 平台 | 商品标题 | 展示价格 | 优惠/券信息 | 推荐理由 | 风险提示 |
|---|---|---:|---|---|---|
| 淘宝/天猫 | ... | ... | ... | ... | 以官方 App 实际页面为准 |

三、安全提醒

未自动打开购买链接，未下单，未加购，未填写地址/手机号/验证码/cookie/token。请回官方 App 核验价格、优惠、店铺、售后、运费和库存。
```
