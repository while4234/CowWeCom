---
name: reliable-search
description: Reliable web search using Serper Google Search and Brave Search APIs with provider fallback and Google blocked-page diagnostics. Use when the user asks to search the web, look up current information, verify facts, find sources, diagnose Google search empty results, or perform Google/Brave-powered research. Trigger for phrases like 查一下, 搜索, Google 搜索, 最新, 新闻, 资料, 来源, weather, current, web search, search reliability, or when raw Google SERP returns empty/redirect/enablejs/sorry pages.
metadata:
  requires:
    bins: ["python"]
    anyEnv: ["SERPER_API_KEY", "BRAVE_API_KEY"]
  emoji: "🔎"
---

# Reliable Search

Use this skill to perform reliable web search through configured search APIs instead of scraping raw Google SERP pages.

## Provider Policy

Use providers in this order:

1. **Serper Google Search** (`SERPER_API_KEY`) — preferred Google-backed search path.
2. **Brave Search** (`BRAVE_API_KEY`) — independent fallback and cross-check source.
3. **Raw Google diagnostic probe** — only to identify blocked/JS-gated pages; do not rely on it for production results.

Never treat raw Google empty output as "no results" until blocked-page diagnostics are checked.

## Script

Run the bundled script from this skill's base directory:

```cmd
python "<base_dir>/scripts/reliable_search.py" "query text"
```

Useful options:

```cmd
python "<base_dir>/scripts/reliable_search.py" "query text" --num 10
python "<base_dir>/scripts/reliable_search.py" "query text" --provider serper
python "<base_dir>/scripts/reliable_search.py" "query text" --provider brave
python "<base_dir>/scripts/reliable_search.py" "query text" --diagnose-google
```

Output is JSON with:

- `query`
- `provider`
- `results[]`: title, url, snippet, source
- `diagnostics[]`: unavailable/failed/blocked providers and reasons

## Workflow

1. Use this skill whenever a user asks for current information or web search.
2. Run `reliable_search.py` with the user's query.
3. Prefer `serper_google` results when available.
4. Use `brave` fallback if Serper fails, returns no results, or needs cross-checking.
5. If diagnosing Google failures, run with `--diagnose-google` and report whether raw Google shows `/sorry/index`, `enablejs`, abnormal traffic, or redirect markers.
6. Summarize results for the user with source links. Mention provider failures only when relevant.

## Setup

This skill reads API keys from environment variables. Do not write secrets into skill files.

Configure one or both keys:

```text
SERPER_API_KEY=...
BRAVE_API_KEY=...
```

Use `env_config` to set them securely when needed. Values must be masked in replies.

## Notes

- Serper uses `https://google.serper.dev/search` and returns Google-style organic results.
- Brave uses `https://api.search.brave.com/res/v1/web/search` and returns independent web results.
- Raw Google SERP scraping is intentionally treated as diagnostic-only because it commonly returns JS redirect, consent, CAPTCHA, or `/sorry/index` abnormal-traffic pages.
