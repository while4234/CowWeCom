# Output Template

Use this template for full itinerary planning. Keep factual status labels visible.

## 0. 规划前确认

Use this section only when missing plan-shaping details would materially change the itinerary and the user has not asked you to proceed with assumptions.

- Ask at most 3 concise questions.
- Ask only for facts that can prevent a conflicting plan, such as origin/destination, dates or duration, traveler constraints, budget class, mobility/health needs, or nationality/passport when entry rules matter.
- For vague opening prompts, ask before using hotel, flight, ticket, weather, visa, or route tools. Do not spend tool calls building a full plan that may be invalidated by basic missing facts.
- If the runtime deterministic clarification gate already sent this section, stop the turn there and wait for the user's answers.
- For international trips with dates/destination but missing origin, budget, traveler constraints, or passport/nationality, ask the highest-impact missing items first unless the user explicitly requests a rough draft.
- If the user says to proceed, asks for a rough draft, or enough information already exists, skip this section and state assumptions in "关键假设".
- Do not place major plan-conflicting facts here and then also draft as if they were optional.

## 1. 关键假设

- 出发地:
- 目的地:
- 日期/天数:
- 人数与同行人限制:
- 预算:
- 已核验来源:
- 待查询/待官方核验:

## 2. 行程总览

- 推荐节奏:
- 推荐城市/景点顺序:
- 主要交通方式:
- 适合/不适合的原因:

## 3. 每日安排

For each day include:

- 上午:
- 下午:
- 晚上:
- 交通方式:
- 预计耗时:
- 餐饮和休息:
- 预约/门票/证件:
- 备选方案:

## 4. 交通矩阵

Compare realistic options only:

- Route or segment
- Source or skill used
- Mode
- Duration
- Cost status
- Comfort/accessibility
- Risk and fallback

## 5. 住宿建议/酒店候选

Include this section by default for every itinerary with at least one overnight stay.

- Night split by city:
- Preferred area or neighborhood:
- FlyAI/search source used:
- Hotel candidates or products:
- Price/inventory status:
- Commute to main sights, stations, or airports:
- Cancellation, deposit, preauthorization, and check-in document notes:
- Accessibility/luggage/elderly-child suitability:
- If live hotel data is unavailable: list recommended areas and mark specific hotels, rooms, and prices as "待实时查询".

## 6. 预算拆分

Mark every live price as source-backed or unverified:

- Intercity transport
- Local transport
- Hotel
- Attraction tickets
- Food
- Insurance/document costs
- Buffer

## 7. 证件/入境/签证核验清单

- Passport/nationality needed:
- Visa/electronic authorization status:
- Transit rules:
- Entry documents:
- Official source/date:
- User action:

## 8. 天气与打包

- Weather source:
- City/country disambiguation:
- Rain, wind, temperature:
- Clothing:
- Outdoor backup:

## 9. 当地特点

Include payment, apps, etiquette, safety, closure days, local transport habits, and booking culture.

## 10. 风险与备选

- Crowds/holidays:
- Weather:
- Route density:
- Ticket/inventory volatility:
- Accessibility/health:
- Plan B:

## 11. 待确认事项

List volatile live-verification items, official checks, or optional preferences that refine the plan without overturning it. Use "待查询" or "待官方核验" where live data is missing.

Do not use this section for major plan-shaping facts that should have been handled upfront in "规划前确认", such as unknown origin/destination, missing dates or duration, budget class, traveler constraints, mobility/health requirements, or nationality/passport when entry rules matter.
