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

Not allowed:

- Do not claim that real nearby stores were found.
- Do not claim access to real-time Meituan ratings, menus, delivery fees, delivery times, discounts, or availability.
- Do not call unauthorized Meituan takeout merchant APIs.
- Do not scrape Meituan App or web pages.
- Do not place orders or click purchase/order links.
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
3. If the user mentions Meituan, coupons, red packets, saving money, discounts, or cheap options, prioritize using or suggesting `meituan-coupons` before choosing a final order.
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
