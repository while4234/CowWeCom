---
name: plugin-12306-ticket
description: 12306 - 本地 12306 火车票查询技能，支持余票查询、车站编码解析、列车经停路线查询；不需要 LINKAI_API_KEY。
metadata:
  requires:
    bins: ["python"]
    env: []
---

# 12306 Local Ticket Search

Use this skill when the user asks to query China Railway 12306 train tickets, station codes, train availability, remaining seats, or train stops/routes.

This version runs locally and calls public 12306 web endpoints directly. It does not require `LINKAI_API_KEY` or any paid plugin provider.

## Capabilities

- Resolve station names, telecodes, pinyin, and abbreviations.
- Query remaining tickets between two stations on a travel date, with optional official price fields.
- Filter by train prefix, such as `G`, `D`, `K`, `Z`, `T`.
- Query stops for a specific train when date/from/to are known.

## Usage

Run the bundled script from this skill directory:

```bash
python scripts/railway_12306.py stations 北京
python scripts/railway_12306.py tickets 北京南 上海虹桥 2026-05-23 --limit 10
python scripts/railway_12306.py tickets 北京南 上海虹桥 2026-05-23 --train-prefix G --include-prices --json
python scripts/railway_12306.py route G547 北京南 上海虹桥 2026-05-23
```

When answering users:

- Prefer concise summaries with train number, departure/arrival time, duration, and key seat availability.
- Use `tickets ... --include-prices --json` when users ask for fare comparison; `prices` contains raw official 12306 price fields.
- Mention that availability is live query data from public 12306 web endpoints and can change quickly.
- If the query fails with a non-JSON/HTML 12306 response, explain that 12306 may be rate-limiting or temporarily blocking public web queries and suggest retrying later.
- Do not claim this skill can book or purchase tickets. It only queries public availability and route data.

## Examples

### Query Tickets

```bash
python scripts/railway_12306.py tickets 北京南 上海虹桥 2026-05-23 --limit 5
```

### Query Stops

```bash
python scripts/railway_12306.py route G547 北京南 上海虹桥 2026-05-23
```

The route command accepts either the public train code, such as `G547`, or the internal `train_no` returned by the ticket query.
