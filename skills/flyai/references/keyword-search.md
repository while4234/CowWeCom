
# keyword-search ref

## Keyword Search (keyword-search)

### Parameters

- **--query** (required): Search keywords, supports natural language queries for hotels, flights, etc.
  - Choose the best-matching query pattern from below:
  - **Location / Nearby**: e.g. "nearby attractions", "restaurants near {POI}"
  - **POI general**: e.g. "{POI} free travel", "{POI} routes"
  - **Attractions / Activities**: e.g. "attraction tickets", "photo tours near {POI}"
  - **Destination**: e.g. "destination guide", "destination hotels"
  - **Entertainment / Experiences**: e.g. "hot spring spa", "skiing"
  - **Tours / Packages**: e.g. "group tour", "custom tour"
  - **Food & Dining**: e.g. "food guide", "buffet"
  - **Visa / Documents**: e.g. "visa", "travel insurance"
  - **Telecom / Connectivity**: e.g. "wifi rental", "SIM card"
  - **Cruise**: e.g. "cruise", "ocean sightseeing"
  - **Other**: e.g. "time-based queries", "shopping district {POI}"

### Examples

```powershell
python skills\flyai\scripts\flyai_wrapper.py keyword-search --query "France visa"
python skills\flyai\scripts\flyai_wrapper.py keyword-search --query "Hangzhou group tour"
python skills\flyai\scripts\flyai_wrapper.py keyword-search --query "Hangzhou 3-day trip"
python skills\flyai\scripts\flyai_wrapper.py keyword-search --query "Shanghai cruise"
python skills\flyai\scripts\flyai_wrapper.py keyword-search --query "Hong Kong SIM card"
```

### Output Example

```json
{
    "data": {
      "itemList": [
        {
          "info": {
              "jumpUrl": "...",
              "picUrl": "...",
              "price": "...",
              "scoreDesc": "",
              "star": "...",
              "tags": ["..."],
              "title": "..."
          }
        }
      ]
    },
    "message": "success",
    "systemMessage": "...",
    "status": 0
}
```
