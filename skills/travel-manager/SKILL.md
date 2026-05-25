---
name: travel-manager
description: Comprehensive travel planning, booking, and itinerary management skill. Use when planning domestic or international trips, city one-day trips, multi-destination itineraries, family travel logistics, travel costs, booking coordination, or travel documents. Pair with amap-cowwechat for China route, POI, ETA, traffic, and transport-mode decisions.
---

# Travel Manager Skill

## Core Capabilities
- Domestic and international trip planning
- City one-day and weekend itinerary design
- Multi-destination itinerary creation
- Family travel logistics
- Cost optimization
- Travel document management
- Attraction sequencing, pace control, meal/rest windows, and contingency planning

## Workflow Steps
1. Destination Analysis
2. Route Optimization
3. Cost Calculation
4. Document Preparation
5. Booking Coordination

For local city trips, adapt the workflow:

1. Clarify date, city, start/end point, people, pace, interests, budget, and must-see or must-avoid places.
2. Draft a realistic itinerary with time blocks, meal/rest buffers, and weather or crowd risks.
3. If the trip is in China and involves routes, traffic, ETA, POI lookup, or transport choice, also read `amap-cowwechat/SKILL.md`.
4. Use `travel-manager` for itinerary structure and traveler constraints; use `amap-cowwechat` / the `amap` tool for factual route, traffic, ETA, and transport-mode checks.
5. Reconcile the route evidence with the itinerary. If the route is too dense, loops back, parking is likely painful, or public transit is better, adjust the plan before replying.

## Cooperation With AMap / 与高德协同

When the user asks for a China city itinerary such as "规划成都一日游", "明天在成都玩怎么安排", "判断交通方式", "堵不堵", or "景点路线怎么走", this skill should work together with `amap-cowwechat`.

- `travel-manager` owns: day structure, attraction selection, pacing, meal/rest windows, budget/logistics, family constraints, and final itinerary wording.
- `amap-cowwechat` owns: location resolution, route order validation, segment ETA, distance, current traffic, and transport-mode comparison.
- Do not treat AMap's fastest route as the whole travel plan. Use it as evidence to improve the itinerary.
- If AMap cannot provide a mode or traffic detail, keep the itinerary useful and clearly mark the uncertainty.

## Key Considerations for Family Travel
- Child-friendly routes
- Stopover comfort
- Baggage requirements
- Age-specific travel needs

## References
- [Family Travel Checklist](references/family-travel-checklist.md)
- [International Travel Documents](references/travel-documents.md)
- [Airline Comparison Matrix](references/airline-matrix.md)

## Usage Examples
- "规划成都一日游，帮我判断路线和交通方式"
- "明天在成都玩一天，从家出发晚上回家，堵不堵？"
- "Plan a family trip to Korea and Japan"
- "Find the most cost-effective international travel route"
- "Prepare travel documents for a multi-country trip"
