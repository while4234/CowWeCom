---
name: takeout-lite-recommender
description: Local lightweight takeout recommendation skill for 今天吃什么, 点外卖, 帮我推荐外卖, 美团外卖有啥券, 想省钱领券, 夜宵, 午饭, 晚饭, 加班, budget, taste, dietary limits, and other meal-planning needs. Use only for food and takeout recommendations, Meituan coupon reminders, and Meituan App search keyword suggestions; do not use for shopping price comparison.
metadata:
  requires:
    bins: ["python"]
---

# Takeout Lite Recommender

Use this skill for lightweight, compliant takeout recommendations. It does not query real nearby stores, menus, ratings, delivery fees, delivery times, or availability.

## Scope

Allowed:

- Recommend what to eat today.
- Generate three takeout dish or category candidates.
- Suggest Meituan App search keywords.
- Remind the user to claim or check Meituan coupons when the user mentions Meituan, takeout coupons, red packets, saving money, discounts, or cheap options.
- Display Meituan coupon usage links returned by `meituan-coupons` as normal Markdown links, such as `[立即使用](jump_url)`, when the coupon script returns `jump_url`.

Not allowed:

- Do not claim that real nearby stores were found.
- Do not claim access to real-time Meituan ratings, menus, delivery fees, delivery times, discounts, or availability.
- Do not call unauthorized Meituan takeout merchant APIs.
- Do not scrape Meituan App or web pages.
- Do not place orders or click purchase/order links.
- Do not click or open Meituan coupon, purchase, or order links automatically. The user must tap links manually in WeCom or the official App.
- Do not upload or request phone numbers, verification codes, tokens, cookies, delivery addresses, or other sensitive data.
- Do not trigger shopping price comparison. Use `shopping-lite-compare` only when the user explicitly asks for e-commerce price comparison.

## Workflow

1. Extract food context from the user request:
   - Budget.
   - Taste and texture preferences.
   - Dietary restrictions or dislikes.
   - Time period, such as lunch, dinner, overtime meal, or late-night snack.
   - Dining mode, especially takeout.
   - City or business district if the user provides it.
2. Generate three dish or category candidates. Reference `eat-what-today-skill` if available, but keep this skill usable without network access or external store data.
3. If the user mentions Meituan, coupons, red packets, saving money, discounts, or cheap options, prioritize `meituan-coupons`:
   - If `meituan-coupons` can be executed and returns coupons, preserve returned coupon fields and show any returned `jump_url` as a clickable Markdown link.
   - If login, verification, or user action is required, follow `meituan-coupons` authentication rules and never invent coupon links.
   - Never open or click the link for the user.
4. For each candidate, provide one or two Meituan App search keywords, such as:
   - `黄焖鸡 高评分 30分钟内`
   - `麻辣烫 满减 配送快`
   - `轻食 月售高 低脂`
5. End with a compliance reminder: store rating, delivery time, delivery fee, discounts, and delivery availability are subject to the actual Meituan App page.

## Script

Run the local helper when a deterministic recommendation is useful:

```bash
python "<base_dir>/scripts/takeout_lite.py" "今晚加班，预算35，想吃热的，点外卖" --coupon true
```

Supported options:

- `--budget`
- `--mode`
- `--spicy`
- `--city`
- `--coupon`

The script is local-only. It does not use the network and does not call Meituan APIs.

## Coupon Link Output

When `meituan-coupons` returns coupon data, include a coupon section before the final reminder:

```markdown
三、美团红包/券

| 券名称 | 面额 | 使用条件 | 有效期 | 去使用 |
|---|---:|---|---|---|
| ... | ... | ... | ... | [立即使用](jump_url) |
```

Rules:

- Only use `jump_url` values returned by `meituan-coupons`.
- Do not create, guess, shorten, rewrite, open, or click links.
- If no `jump_url` is returned, show `-` and tell the user to check the Meituan App.
- Links are for manual user tapping in WeCom or the official App only.

## Output Format

Use this structure:

```markdown
一、直接结论

二、3 个推荐选项

### 1. 菜品/品类
- 适合理由：
- 预算区间：
- 美团搜索关键词：
- 是否建议先领券：

三、提醒
店铺评分、配送时间、配送费、满减和可配送状态以美团 App 实际页面为准。
```
