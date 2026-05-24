---
name: daily-meme-harvester
description: Fetch, rank, deduplicate, and download the latest high-engagement meme and hot-topic images from Chinese social web sources such as Weibo and Xiaohongshu. Use when the user asks to collect, download, save, archive, or schedule daily hot memes, 梗图, 表情包, meme images, reaction images, hot-topic images, or social-media image素材到本地.
metadata:
  cow:
    emoji: "🖼️"
    requires:
      bins: ["python3"]
---

# Daily Meme Harvester

## 功能说明

This skill collects high-engagement images that are likely to become memes or reaction material. The default strategy is interest-seeded, not raw hot-search driven: it searches recent platform content for funny, relationship, comment-section, light-suggestive but non-explicit, and meme-friendly themes. Raw hot-search terms are optional because Weibo/Douyin top lists often duplicate hard news the user already sees.

- Weibo: uses a skill-owned persistent Chrome profile by default, searches bounded interest seeds through normal logged-in browser traffic, ranks by engagement and query score, then downloads public images. Public HTTP search is disabled by default and only used when explicitly enabled as fallback.
- Xiaohongshu: uses a skill-owned persistent Chrome profile by default, searches bounded interest seeds such as 抽象名场面, 评论区笑死, 擦边穿搭, 氛围感美女, applies the default search filters `sort_type=time_descending`, `note_type=普通笔记`, and `time_filter=一天内`, then downloads image URLs exposed in normal web responses. A short bounded HTTP path remains as fallback when the browser path produces no candidates.
- Optional Reddit support remains available when explicitly requested, but the default deployment for this project is Weibo and Xiaohongshu.
- Each provider is isolated: if one platform returns 403, 429, captcha, risk-control shells, or empty data, the script records a warning and continues with the other providers.
- The script ranks candidates, filters obvious sensitive content, deduplicates by URL and SHA-256, downloads images atomically, and writes `index.md`, `manifest.jsonl`, and state files.

The implementation intentionally does not generate anti-bot signatures, bypass captcha, automate login, repost content, or perform high-frequency scraping.

## Agent routing guard

- For Xiaohongshu and Weibo diagnostics, do not call the generic `browser` tool and do not open Bing/Baidu/search-engine fallbacks. CowAgent's generic browser uses `~/.cow/browser_profile`, which is separate from this skill's platform profiles and can falsely appear logged out.
- To test collection, run this skill's `harvest_memes.py` commands only. If a human login or risk check is needed, open the matching dedicated profile with `--open-xiaohongshu-profile --json` or `--open-weibo-profile --json`.
- A login wall seen in the generic browser is not evidence that the skill-owned profile lost login state. Verify the active path by checking the script warning line that includes `persistent browser profile: ...`.
- If dry-run returns too few candidates, report the thin result and warnings from the script. Do not broaden the task into web search unless the user explicitly asks for public-web research.

## 一次性运行命令

```bash
python3 <base_dir>/scripts/harvest_memes.py --providers weibo,xiaohongshu --out "/path/to/output" --max-total 6 --max-per-provider 12
```

In this repository:

```bash
python3 skills/daily-meme-harvester/scripts/harvest_memes.py --providers weibo,xiaohongshu --out "/path/to/output" --max-total 6 --max-per-provider 12
```

Default TOP3 behavior:

- `max_downloads_per_provider` defaults to `3`.
- With providers `weibo,xiaohongshu`, the default `max_total` is `6`.
- Keep `--max-total 6` for the default behavior: TOP3 from Weibo plus TOP3 from Xiaohongshu.
- Dedupe happens after each platform's TOP3 is selected. If Weibo and Xiaohongshu hit the same hot topic, the duplicate is removed and the script does not backfill another item.
- Same-day repeat runs automatically suppress hot topics, source URLs, and image URLs already downloaded earlier that day. The second run therefore looks for a fresh batch from the same one-pass candidate pool.
- Serious accident/disaster keywords are filtered by default so daily meme runs do not select tragedy or emergency-news images as reaction素材.

Dry run without downloads:

```bash
python3 skills/daily-meme-harvester/scripts/harvest_memes.py --providers weibo,xiaohongshu --dry-run --json
```

Open the dedicated Xiaohongshu profile for one-time manual login or risk verification:

```bash
python3 skills/daily-meme-harvester/scripts/harvest_memes.py --open-xiaohongshu-profile --json
```

Open the dedicated Weibo profile for one-time manual login or risk verification:

```bash
python3 skills/daily-meme-harvester/scripts/harvest_memes.py --open-weibo-profile --json
```

Download and send to Enterprise WeChat:

```bash
python3 skills/daily-meme-harvester/scripts/harvest_memes.py --providers weibo,xiaohongshu --max-total 6 --send-wecom
```

## 配置方式

Output directory priority:

1. CLI `--out`
2. Environment variable `MEME_OUTPUT_DIR`
3. `~/.cow-meme-harvester/config.json`
4. Default `~/cow/memes`

Supported environment variables:

- `MEME_OUTPUT_DIR`: default local output directory when `--out` is not supplied.
- `WEIBO_COOKIE`: optional; used only for normal authenticated Weibo requests when the user explicitly provides it.
- `WEIBO_BROWSER_USER_DATA_DIR`: optional persistent Chrome profile override. By default the script uses `~/.cow-meme-harvester/weibo-browser-profile`.
- `XHS_COOKIE`: optional fallback cookie header for the bounded Xiaohongshu HTTP path. The default persistent-browser path can rely on the skill-owned browser profile instead.
- `XHS_BROWSER_USER_DATA_DIR`: optional persistent Chrome profile override. By default the script uses `~/.cow-meme-harvester/xiaohongshu-browser-profile`.
- `HTTP_PROXY` / `HTTPS_PROXY`: optional proxy settings honored by Python networking unless a provider disables proxy use in config.
- `WECOM_BOT_ID` / `WECOM_BOT_SECRET`: optional override for sending; if missing, the script reads `D:/CowWechat/config.json`.
- `WECOM_BOT_RECEIVER`: optional receiver userid/chatid for `--send-wecom`; if missing, the script tries to resolve the first WeCom admin profile in CowWechat config.
- `WECOM_BOT_IS_GROUP`: set `1` or `true` when the receiver is a group chatid.
- `~/.cow-meme-harvester/config.json`: created on first run if missing and never overwritten.

Important config keys:

```json
{
  "output_dir": "~/cow/memes",
  "providers": ["weibo", "xiaohongshu"],
  "max_total": 6,
  "max_per_provider": 30,
  "max_downloads_per_provider": 3,
  "dedupe_same_content": true,
  "dedupe_cross_provider_topics": true,
  "exclude_same_day_topics": true,
  "dedupe_days": 90,
  "skip_sensitive": true,
  "send_after_download": false
}
```

Provider notes:

- Weibo has its own dedicated persistent browser profile at `~/.cow-meme-harvester/weibo-browser-profile`. The default Weibo collector opens this profile, warms up `weibo.com` and `m.weibo.cn`, then searches the bounded interest-seeded query list through the browser profile. Closing Chrome does not delete this profile; login state persists until the directory is deleted or the platform expires the session.
- Weibo candidates with parseable timestamps older than `since_hours` (default 24) are skipped, so interest-seeded search does not backfill old celebrity or meme archive posts just because they rank high.
- Weibo HTTP fallback is off by default (`weibo.http_fallback_enabled=false`) so the skill does not silently behave like a background HTTP scraper. Turn it on only for diagnostics or controlled fallback; it remains bounded by `max_search_terms`, `max_search_suffixes`, `request_timeout_seconds`, and `search_time_budget_seconds`.
- Xiaohongshu defaults to a dedicated persistent browser profile at `~/.cow-meme-harvester/xiaohongshu-browser-profile`. This keeps the skill independent from Codex profiles and portable to another machine. Closing Chrome does not delete this profile; login state persists until the directory is deleted or the platform expires the session.
- If the dedicated Xiaohongshu profile is not logged in or shows risk verification, run `--open-xiaohongshu-profile --json`, complete login or verification in the opened normal Chrome window, close it, then run the normal command again.
- Collection uses normal system Chrome with this profile and connects briefly through local CDP to read page responses. The older Playwright-managed launch path is still available by setting `xiaohongshu.browser.launch_mode` away from `system_chrome_cdp`, but the default avoids creating a fresh automation-looking profile.
- Xiaohongshu search defaults to recent image notes: `sort_type=time_descending`, `note_type=普通笔记`, `time_filter=一天内`. Set `xiaohongshu.search_filters.enabled` to `false` or change those values in config if you want broader results.
- Xiaohongshu HTTP fallback is intentionally bounded: only a small number of queries run, each with short per-request timeouts and an overall fallback time budget.
- Xiaohongshu HTTP fallback defaults to `disable_proxy: true` because some local proxies break its TLS handshake.
- When Xiaohongshu HTTP fallback appears unreachable because subscription updates broke local Clash Verge rules, the script runs `D:\CowWechat\scripts\clash_verge_rule_guard.py --json` once and retries once. This only repairs local DIRECT rules; it does not bypass platform login, captcha, or risk controls. Dry-run mode skips this repair guard.
- Cookies are read from environment variables or the process environment. Do not paste cookies into chat logs or commit them to the repository.

Xiaohongshu browser config:

```json
{
  "xiaohongshu": {
    "search_filters": {
      "enabled": true,
      "sort_type": "time_descending",
      "note_type": "普通笔记",
      "time_filter": "一天内"
    },
    "browser": {
      "enabled": true,
      "use_persistent_profile": true,
      "user_data_dir": "~/.cow-meme-harvester/xiaohongshu-browser-profile",
      "launch_mode": "system_chrome_cdp",
      "remote_debugging_port": 0,
      "channel": "chrome",
      "headless": false,
      "timeout_seconds": 18,
      "wait_seconds": 3,
      "manual_login_wait_seconds": 0,
      "max_queries": 5,
      "time_budget_seconds": 70
    },
    "http_fallback_enabled": true,
    "http_fallback_max_queries": 2,
    "http_time_budget_seconds": 25
  }
}
```

Weibo profile and search budget config:

```json
{
  "weibo": {
    "use_hot_terms": false,
    "max_search_terms": 5,
    "max_search_queries": 8,
    "max_search_suffixes": 1,
    "request_timeout_seconds": 6,
    "search_time_budget_seconds": 45,
    "http_fallback_enabled": false,
    "fallback_keywords": ["抽象名场面", "离谱名场面", "搞笑聊天截图", "情侣日常搞笑", "擦边但不低俗", "氛围感美女"],
    "browser": {
      "enabled": true,
      "use_persistent_profile": true,
      "user_data_dir": "~/.cow-meme-harvester/weibo-browser-profile",
      "launch_mode": "system_chrome_cdp",
      "warmup_urls": ["https://weibo.com/", "https://m.weibo.cn/"],
      "max_queries": 5,
      "time_budget_seconds": 60
    }
  }
}
```

Proxy guard config:

```json
{
  "proxy_guard": {
    "enabled": true,
    "script": "scripts/clash_verge_rule_guard.py",
    "providers": ["xiaohongshu"],
    "timeout_seconds": 20
  }
}
```

## 每日定时运行

CowAgent scheduler:

- Time: every day at 09:00
- Cron: `0 9 * * *`
- Task: 运行 daily-meme-harvester，把今天的热门梗图和热点图片下载到 {输出目录}，来源 weibo,xiaohongshu，每个平台最多 3 张，总计最多 6 张，生成 index.md 和 manifest.jsonl。

System cron fallback:

```cron
0 9 * * * cd /path/to/CowWechat && python3 skills/daily-meme-harvester/scripts/harvest_memes.py --providers weibo,xiaohongshu --out "/path/to/output" --max-total 6 --max-per-provider 12 >> ~/.cow-meme-harvester/cron.log 2>&1
```

## 合规说明

- Only save publicly accessible images.
- Preserve attribution, `source_url`, author, engagement metrics, download time, and local path.
- Do not automatically forward, repost, or publish harvested images.
- Do not bypass captcha, login limits, platform risk controls, or other access restrictions.
- Weibo uses public endpoints or a user-provided `WEIBO_COOKIE`; access failures are warnings, not fatal errors.
- Xiaohongshu uses the dedicated persistent browser profile first, then bounded public web/`XHS_COOKIE` fallback; no signature generation, captcha bypass, or automated login.
- Default sensitive-content filtering skips obvious NSFW/sensitive keywords. Platforms without reliable NSFW metadata use keyword filtering only.
- Default frequency is daily. High-frequency collection can trigger platform risk controls.

## Output

The script writes one dated harvest folder plus shared dedupe state:

```text
{output_dir}/
  YYYY-MM-DD/
    manifest.jsonl
    index.md
    state_delta.json
    weibo/
    xiaohongshu/
  state/
    seen_urls.json
    seen_hashes.json
    daily_seen.json
```

`manifest.jsonl` stores one JSON object per downloaded image, including:

```json
{
  "provider": "weibo",
  "source_id": "...",
  "source_url": "...",
  "image_url": "...",
  "local_path": "...",
  "title": "...",
  "author": "...",
  "created_at": "...",
  "score": 1234.0,
  "metrics": {},
  "sha256": "...",
  "content_type": "image/jpeg",
  "size_bytes": 123456,
  "downloaded_at": "2026-05-23T09:00:00+08:00"
}
```

`index.md` uses local relative image paths only, never remote image URLs.
