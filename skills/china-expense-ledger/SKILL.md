---
name: china-expense-ledger
description: Local-only China mainland expense ledger for user-provided text, vision-extracted payment/order screenshots, and Alipay/WeChat CSV bill exports. Use when the user asks to 记账, 记录消费, 导入支付宝账单, 导入微信支付账单, 统计支出, 查询本地账本, 修正分类, or learn local expense categories. Never scrape, reverse engineer, log in to, or automatically fetch bills from payment, shopping, takeout, or service apps.
metadata:
  requires:
    bins: ["python"]
  emoji: "🧾"
---

# China Expense Ledger

Use this skill for local bookkeeping in China mainland payment, shopping, takeout, grocery, supermarket, travel, and daily-service scenarios.

The helper is local-only. It stores user-provided or Agent-extracted transaction fields in SQLite and never logs in to Alipay, WeChat, Taobao, JD, Meituan, Douyin, Sam's Club, Hema, or any other platform.

## Safety Scope

Allowed:

- Record transactions from natural language provided by the user.
- Record transactions from screenshot content that the Agent has already read with vision.
- Import user-provided Alipay CSV exports and WeChat Pay CSV exports.
- Keep local learning rules for item, merchant, platform, and category corrections.
- Query, summarize, correct, and export the local SQLite ledger.

Not allowed:

- Do not crawl, scrape, reverse engineer, bypass login, use cookies, use tokens, or automate bill fetching from any App or website.
- Do not request passwords, SMS codes, cookies, auth tokens, payment credentials, or platform session files.
- Do not call official payment or e-commerce APIs by default. Adapter design is allowed only as disabled documentation; no real key logic belongs in this MVP.
- Do not store original screenshots. Extract fields first, then store only transaction data and a source hash.
- Do not invent unclear product names, merchants, categories, or platforms. Ask the user to clarify.

## Storage

Default database:

```bash
~/cow/data/china_expense_ledger/ledger.db
```

Override with:

```bash
CHINA_EXPENSE_LEDGER_DB=/path/to/ledger.db
```

Initialize:

```bash
python "<base_dir>/scripts/ledger.py" init
```

All commands print JSON.

## Key Fields

Keep these concepts separate:

- `payment_app`: payment tool, such as 支付宝, 微信支付, 银行卡, 云闪付, Apple Pay, 现金, 未知.
- `order_platform`: order or consumption platform, such as 淘宝, 天猫, 京东, 拼多多, 抖音商城, 美团, 美团外卖, 饿了么, 山姆, 盒马, 京东到家, 携程, 滴滴, 12306, 线下商户, 未知.
- `source_app`: screenshot source App or App mentioned by the user.
- `merchant`: merchant, shop, payee, or counterparty.
- `item_name`: product or service name.
- `category`: top-level ledger category.
- `subcategory`: optional second-level category.

Default categories:

餐饮, 外卖, 商超日用, 生鲜买菜, 服饰鞋包, 数码家电, 交通出行, 住房物业, 水电燃气, 通讯网络, 医疗健康, 教育学习, 娱乐休闲, 宠物, 母婴, 旅行住宿, 人情红包, 转账, 收入, 退款, 其他.

## Natural Language Workflow

1. Extract structured fields from the user's sentence.
   - If the user mentions a date in natural language, use the active model to resolve it before calling the helper.
   - Pass only a standard `occurred_at` value (`YYYY-MM-DD` or ISO 8601) to the helper.
   - Keep the original phrase in `occurred_at_text` and the model's explanation in `occurred_at_resolution` when useful.
   - If the user gives no date, omit `occurred_at`; the helper will default to today. Do not ask a date-only clarification.
2. Infer `direction`:
   - refunds are `refund`, not normal income;
   - clear consumption is `expense`;
   - clear salary/bonus/reimbursement is `income`;
   - unclear 红包 or 转账 should be `unknown` or `transfer` with `status=pending`.
3. Infer category conservatively:
   - 美团外卖, 饿了么, 外卖, 奶茶, 咖啡, 餐厅, 饭店, 黄焖鸡, 麻辣烫, 汉堡 -> 餐饮 or 外卖.
   - 盒马, 山姆, 沃尔玛, 永辉, 朴朴, 叮咚买菜, 菜, 肉, 蛋, 奶, 水果 -> 生鲜买菜 or 商超日用.
   - 淘宝, 天猫, 京东, 拼多多, 抖音商城: if the item is clear, categorize by item; otherwise use 其他.
   - 滴滴, 曹操出行, T3, 公交, 地铁, 12306, 机票, 打车 -> 交通出行.
   - 中转 API token, API 额度, 额度卡, API key -> AI工具.
   - If the user names an item but it does not match a stable built-in or curated major category, use 其他 and learn the user's item rule instead of inventing a new category.
4. Call `record-json`.

Example:

```bash
python "<base_dir>/scripts/ledger.py" record-json --json "{\"user_id\":\"local-user\",\"source_type\":\"text\",\"raw_text\":\"刚才在淘宝买了数据线 19.9，支付宝付的\",\"amount_cents\":1990,\"direction\":\"expense\",\"order_platform\":\"淘宝\",\"payment_app\":\"支付宝\",\"item_name\":\"数据线\",\"category\":\"数码家电\"}"
```

## Screenshot Workflow

For screenshots, first use CowWeCom vision or the active model's image-reading ability to extract visible text and fields. Then call `analyze-bill` or `record-json` with `source_type=image`.

Do not pass the raw image path to this helper for OCR. This helper does not parse images.

Date handling:

- Use the current CowWeCom vision/multimodal model to understand bill dates directly from the screenshot layout and visible fields.
- Do not rely on a local OCR string or `ledger.py` to infer dates from screenshots.
- Before calling the helper, convert visible or user-described dates such as 今天, 昨天, 前天, 上周三, 今年母亲节, 五一那天 to standard `occurred_at`.
- Never pass natural-language dates such as `今年母亲节` as `occurred_at`; if useful, store that phrase as `occurred_at_text` and add `occurred_at_resolution`.
- If the screenshot date is not clear, omit `occurred_at`, set `occurred_at_assumed=true`, and tell the user the bill was defaulted to today and can be corrected later.

Private-chat automation:

- In private chat, if the image is clearly a bill and has amount, app/platform, item/merchant, and category confidence, use `analyze-bill` and auto-record it. Include the booked date in the reply, for example: `已记账：2026-05-10 ¥99.88 AI工具。如果不需要记账，请回复“不记账”或“撤销记账”，我会撤销这笔。`
- If no clear date was provided, add: `已默认记为今天 <YYYY-MM-DD>，如需修改日期请告诉我。`
- If a fuzzy date was resolved by the model, add the interpretation, for example: `“上周”已按 2026-05-18 记录，如需修改日期请告诉我。`
- In group chat, never auto-record. Group images may be recognized for context, but ledger writes require private chat.
- If it looks like a bill but the app/platform, category, item, merchant, amount, or direction is unclear, ask only for the missing fields and do not invent them.
- Text follow-ups such as category/item clarification, duplicate decisions, date changes, and undo requests must be tied to a recent same-user/same-chat bill context. If the user is asking an unrelated task such as backend token/quota/status, do not treat it as ledger clarification.
- After the user answers a missing field, call `confirm-bill`. Partial answers are kept across multiple clarifications, and final confirmation stores screenshot UI rules plus item/merchant learning rules, so the same UI or same item can be recognized later without asking again.
- If the user replies `不记账`, `撤销记账`, `取消记账`, or similar after an auto-recorded bill, call `undo-bill` for the same user/chat.
- If a new bill may be the same order as an existing record, do not insert, undo, or modify first. Treat it as possible duplicate when amount, date/day, direction, category, and merchant/item/order/app evidence overlap. Ask whether to `仍要记账/新增一笔`, `撤销这笔`, or keep the existing record.
- Do not treat menus, product lists, coupons, or shopping-cart pages with visible prices as bills unless there are bill markers such as 支付成功, 交易成功, 账单详情, 订单编号, 交易单号, or 付款方式.

Analyze a vision result:

```bash
python "<base_dir>/scripts/ledger.py" analyze-bill --json "{\"user_id\":\"local-user\",\"chat_id\":\"private-chat\",\"record_id\":\"image-id\",\"raw_text\":\"微信支付 支付成功 美团外卖 商品: 黄焖鸡 支付金额 ¥28.50 交易单号 123456789012\"}"
```

Confirm a pending bill after the user answers:

```bash
python "<base_dir>/scripts/ledger.py" confirm-bill --json "{\"context_id\":\"<context_id>\",\"item_name\":\"二手键盘\",\"category\":\"数码家电\"}"
```

Undo the latest auto-recorded bill for the same private chat:

```bash
python "<base_dir>/scripts/ledger.py" undo-bill --user-id "local-user" --chat-id "private-chat"
```

If the screenshot says 微信支付 and shows 美团外卖 ¥28.50:

```json
{
  "user_id": "local-user",
  "source_type": "image",
  "source_app": "微信支付",
  "payment_app": "微信支付",
  "order_platform": "美团外卖",
  "merchant": "美团外卖",
  "item_name": null,
  "amount_cents": 2850,
  "direction": "expense",
  "category": "外卖",
  "raw_text": "微信支付 支付成功 美团外卖 ¥28.50"
}
```

If item or merchant is unreadable, ask the user to clarify. Do not fabricate it.

## CSV Import

Import user-provided Alipay or WeChat Pay CSV exports:

```bash
python "<base_dir>/scripts/ledger.py" import-file --path "/path/to/alipay.csv" --source alipay --user-id "local-user"
python "<base_dir>/scripts/ledger.py" import-file --path "/path/to/wechat.csv" --source wechat --user-id "local-user"
```

Auto-detection is available:

```bash
python "<base_dir>/scripts/ledger.py" import-file --path "/path/to/bill.csv" --source auto --user-id "local-user"
```

XLS/XLSX are adapter placeholders in this MVP. The helper returns `unsupported_format`; ask the user to export or convert to CSV.

## Query And Correction

Live chat identity rule:

- In Weixin/WeCom runtime, query and summarize with the current chat context `memory_user_id`.
- Never use the example `local-user` for a real chat query, correction, undo, import, or export unless the user explicitly says they are working with that demo user id.
- If the current `memory_user_id` is unavailable, ask the user to retry from a supported chat context instead of querying another user id.

Query recent records:

```bash
python "<base_dir>/scripts/ledger.py" query --user-id "local-user" --period month --limit 20
```

Summarize. The helper keeps per-user day/week/month summary caches for fast today, week, month, and last-month answers:

```bash
python "<base_dir>/scripts/ledger.py" summary --user-id "local-user" --period month
python "<base_dir>/scripts/ledger.py" summary --user-id "local-user" --period week
python "<base_dir>/scripts/ledger.py" summary --user-id "local-user" --period today
python "<base_dir>/scripts/ledger.py" summary --user-id "local-user" --period last_month
```

Correct and learn:

```bash
python "<base_dir>/scripts/ledger.py" correct --transaction-id "<id>" --field category --new-value "生鲜买菜"
```

Export a local JSON backup:

```bash
python "<base_dir>/scripts/ledger.py" export-json --user-id "local-user" --period all --output "/path/to/ledger-export.json"
```

Check setup:

```bash
python "<base_dir>/scripts/ledger.py" doctor
```

## Output Handling

- If command output has `ok: false`, explain the error and ask for the missing user action.
- If output includes `needs_clarification`, ask exactly for the missing information before confirming the ledger entry.
- If `duplicate: true`, tell the user this looks already recorded and do not create a second record.
- If `possible_duplicate: true`, ask the user before doing anything. Only call `confirm-bill` with an explicit `force_new_transaction` decision when the user says it is a separate bill.
- Keep all ledger data local. Do not paste large exports back into chat unless the user asks for a summary.
