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

This skill collects high-engagement images that are likely to become memes or reaction material. It is hot-topic driven: it first gathers current trend terms, then searches each platform for images around those topics instead of only searching the literal keyword "梗图".

- Weibo: fetches hot-search terms from the public hotSearch endpoint, searches related image posts, ranks by hot-term score plus engagement, then downloads public images.
- Xiaohongshu: uses public search pages or a user-provided `XHS_COOKIE`, searches around shared hot topics plus fallback terms such as 今日热梗 and 名场面, then downloads image URLs exposed in normal web responses.
- Optional Reddit support remains available when explicitly requested, but the default deployment for this project is Weibo and Xiaohongshu.
- Each provider is isolated: if one platform returns 403, 429, captcha, risk-control shells, or empty data, the script records a warning and continues with the other providers.
- The script ranks candidates, filters obvious sensitive content, deduplicates by URL and SHA-256, downloads images atomically, and writes `index.md`, `manifest.jsonl`, and state files.

The implementation intentionally does not generate anti-bot signatures, bypass captcha, automate login, repost content, or perform high-frequency scraping.

## 一次性运行命令

```bash
python3 <base_dir>/scripts/harvest_memes.py --providers weibo,xiaohongshu --out "/path/to/output" --max-total 6 --max-per-provider 30
```

In this repository:

```bash
python3 skills/daily-meme-harvester/scripts/harvest_memes.py --providers weibo,xiaohongshu --out "/path/to/output" --max-total 6 --max-per-provider 30
```

Default TOP3 behavior:

- `max_downloads_per_provider` defaults to `3`.
- With providers `weibo,xiaohongshu`, the default `max_total` is `6`.
- Keep `--max-total 6` for the default behavior: TOP3 from Weibo plus TOP3 from Xiaohongshu.
- Dedupe happens after each platform's TOP3 is selected. If Weibo and Xiaohongshu hit the same hot topic, the duplicate is removed and the script does not backfill another item.
- Same-day repeat runs automatically suppress hot topics, source URLs, and image URLs already downloaded earlier that day. The second run therefore looks for a fresh batch from the same one-pass candidate pool.

Dry run without downloads:

```bash
python3 skills/daily-meme-harvester/scripts/harvest_memes.py --providers weibo,xiaohongshu --dry-run --json
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
- `XHS_COOKIE`: optional; used only for normal Xiaohongshu web requests when public access is unavailable and the user explicitly provides it.
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

- Xiaohongshu defaults to `disable_proxy: true` because some local proxies break its TLS handshake.
- When Xiaohongshu appears unreachable because subscription updates broke local Clash Verge rules, the script runs `D:\CowWechat\scripts\clash_verge_rule_guard.py --json` once and retries once. This only repairs local DIRECT rules; it does not bypass platform login, captcha, or risk controls.
- Cookies are read from environment variables or the process environment. Do not paste cookies into chat logs or commit them to the repository.

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
0 9 * * * cd /path/to/CowWechat && python3 skills/daily-meme-harvester/scripts/harvest_memes.py --providers weibo,xiaohongshu --out "/path/to/output" --max-total 6 --max-per-provider 30 >> ~/.cow-meme-harvester/cron.log 2>&1
```

## 合规说明

- Only save publicly accessible images.
- Preserve attribution, `source_url`, author, engagement metrics, download time, and local path.
- Do not automatically forward, repost, or publish harvested images.
- Do not bypass captcha, login limits, platform risk controls, or other access restrictions.
- Weibo uses public endpoints or a user-provided `WEIBO_COOKIE`; access failures are warnings, not fatal errors.
- Xiaohongshu uses public search pages or a user-provided `XHS_COOKIE`; no signature generation or automated login.
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
