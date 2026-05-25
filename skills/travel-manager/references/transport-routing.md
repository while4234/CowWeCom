# Transport Routing

Use this file to choose the source and output shape for transport questions.

## China Mainland Local Travel

Use `amap-cowwechat` for:

- City route planning
- POI lookup
- Segment ETA and distance
- Current traffic
- Driving, transit, walking, cycling, and electrobike comparison
- Parking and walking-distance risk
- Attraction order and route density

Output should include route order, transport mode, estimated duration, risk, and backup.

## China Railway

Use `plugin-12306-ticket` for:

- Stations and telecodes
- High-speed rail, bullet trains, and regular trains
- Remaining tickets and seat classes
- Train numbers and stops
- Earliest or cheapest train comparison

Always state that 12306 availability can change quickly. Do not book tickets or submit passenger documents.

## Flights

Use FlyAI if installed and smoke-tested. Otherwise mark as "待实时查询".

For flight planning include:

- Origin and destination airports or cities
- Date and flexibility
- Direct vs connecting
- Baggage policy status
- Price source and timestamp if available
- Booking action left to user

## Hotels And Attraction Tickets

Use FlyAI or user-provided official/OTA pages. Do not invent room or ticket inventory.

Include cancellation policy, deposit/preauthorization, check-in requirements, and timed-entry risk when source-backed.

## Cross-Border And International Transport

Use region profiles before choosing a mode:

- Hong Kong/Macau/Taiwan: MTR, ferries, ports, HZMB, Taiwan HSR/TRA/MRT
- Japan: JR, private railways, metro, Shinkansen, IC cards
- Korea: subway, KTX, airport rail
- Southeast Asia: country-specific metro, Grab, domestic flights, ferries
- Europe: cross-border trains, low-cost airlines, seat reservations, strikes
- United States: self-driving, parking, rental car insurance, domestic flights
