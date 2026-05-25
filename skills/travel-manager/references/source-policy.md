# Source Policy

Travel Manager must separate planning suggestions from source-backed facts.

## Dynamic Facts

The following must come from a live skill result, a user-provided page, or an official/reliable source:

- Ticket availability
- Train seat classes and stops
- Flight numbers, prices, baggage rules, and schedules
- Hotel room inventory and prices
- Attraction ticket inventory and opening hours
- Weather forecasts
- Traffic and ETA
- Visa, entry, eTA, ETIAS, ESTA, K-ETA, eVisa, transit, and health rules
- Holiday traffic controls, closures, strikes, and event restrictions

If no source is available, write "待查询" or "待官方核验".

## Source Priority

1. User-provided official/OTA/airline/hotel/attraction/transport page
2. Local skill with live query result
3. Official government, consular, railway, airport, airline, attraction, or venue source
4. Reliable real-time search result
5. Model background knowledge for non-dynamic context only

## Prohibited Behavior

- Do not fabricate prices, inventory, availability, visa rules, schedules, or opening hours.
- Do not treat social posts, generic guides, or model memory as the only source for dynamic facts.
- Do not store or request full passport numbers, ID card numbers, bank card numbers, passwords, cookies, tokens, or API keys.
- Do not purchase, pay, submit passenger identity, submit visa applications, or bypass platform controls.

## FlyAI Policy

Use FlyAI for flights, hotels, attractions, trains, and mixed travel inventory only when installed and smoke-tested. China railway facts still prefer `plugin-12306-ticket`.

If FlyAI is unavailable, keep the itinerary useful and mark flight/hotel/attraction inventory as requiring live verification through OTA, airline, hotel, or official attraction pages.
