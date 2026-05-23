---
name: daily-meme-harvester
description: Fetch, rank, deduplicate, and download the latest high-engagement meme images from Chinese and global social web sources such as Weibo, X/Twitter, and optional Reddit. Use when the user asks to collect, download, save, archive, or schedule daily hot memes, 梗图, 表情包, meme images, reaction images, or social-media image素材到本地.
metadata:
  cow:
    emoji: "🖼️"
    requires:
      bins: ["python3"]
---

# Daily Meme Harvester

## 功能说明

Use this skill to collect current high-engagement meme and reaction images, save the public images locally, and keep attribution metadata for later review. The bundled script:

- Collects meme-like images related to Weibo hot-search terms, including 梗图、表情包、搞笑图, and meme keyword searches.
- Collects popular X/Twitter meme or reaction-image posts through the official X API when `X_BEARER_TOKEN` is configured.
- Conservatively tries Xiaohongshu public search pages for 梗图/表情包 images; if public access fails, use a user-provided `XHS_COOKIE` or skip with a warning.
- Optionally collects Reddit meme subreddit images when `reddit` is enabled or requested.
- Ranks candidates by engagement, filters obvious sensitive content, deduplicates by URL and SHA-256, downloads images, and writes `index.md`, `manifest.jsonl`, and state files.

## 一次性运行

```bash
python3 <base_dir>/scripts/harvest_memes.py --providers weibo,x,reddit --out "/path/to/output" --max-total 50
```

In this repository, the script path is:

```bash
python3 skills/daily-meme-harvester/scripts/harvest_memes.py --providers weibo,x,reddit --out "/path/to/output" --max-total 50
```

Low-volume TOP3 smoke run for Weibo, Xiaohongshu, and X/Twitter:

```bash
python3 skills/daily-meme-harvester/scripts/harvest_memes.py --providers weibo,xiaohongshu,x --out "/path/to/output" --max-total 3 --max-per-provider 3
```

## 配置方式

Output directory priority:

1. CLI `--out`
2. Environment variable `MEME_OUTPUT_DIR`
3. `~/.cow-meme-harvester/config.json`
4. Default `~/cow/memes`

Supported environment variables:

- `MEME_OUTPUT_DIR`: default local output directory when `--out` is not supplied.
- `X_BEARER_TOKEN`: required for X/Twitter collection; X is skipped with a warning when absent.
- `WEIBO_COOKIE`: optional; used only for normal authenticated Weibo requests when the user explicitly provides it.
- `XHS_COOKIE`: optional; used only for normal Xiaohongshu web requests when public access is unavailable and the user explicitly provides it.
- `HTTP_PROXY` / `HTTPS_PROXY`: optional proxy settings honored by Python networking.
- `~/.cow-meme-harvester/config.json`: created on first run if missing and never overwritten.

## 每日定时运行

CowAgent scheduler:

- Time: every day at 09:00
- Cron: `0 9 * * *`
- Task: 运行 daily-meme-harvester，把今天的热门梗图下载到 {输出目录}，来源 weibo,x,reddit，最多 50 张，生成 index.md 和 manifest.jsonl

System cron fallback:

```cron
0 9 * * * cd /path/to/CowWechat && python3 skills/daily-meme-harvester/scripts/harvest_memes.py --providers weibo,x,reddit --out "/path/to/output" --max-total 50 >> ~/.cow-meme-harvester/cron.log 2>&1
```

## 合规说明

- Only save publicly accessible images.
- Preserve attribution and `source_url` in `manifest.jsonl` and `index.md`.
- Do not automatically forward, repost, or publish harvested images.
- Do not bypass CAPTCHA, login limits, platform risk controls, or other access restrictions.
- Use the official X API for X/Twitter collection.
- For Weibo, use only public endpoints or a user-provided `WEIBO_COOKIE`; skip and record a warning when access fails.
- For Xiaohongshu, use only public search pages or a user-provided `XHS_COOKIE`; do not generate anti-bot signatures or automate login.
- Default to daily collection; high-frequency scraping can trigger platform risk controls.
- Default sensitive-content filtering skips X `possibly_sensitive=true`, Reddit `over_18=true`, and applies a keyword blacklist for Weibo where reliable NSFW metadata is unavailable.

## Output

The script writes one dated harvest folder plus shared state:

```text
{output_dir}/
  YYYY-MM-DD/
    manifest.jsonl
    index.md
    state_delta.json
    weibo/
    x/
    reddit/
  state/
    seen_urls.json
    seen_hashes.json
```
