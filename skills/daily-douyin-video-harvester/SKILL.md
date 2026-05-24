---
name: daily-douyin-video-harvester
description: Fetch Douyin hot topics, filter for meme-worthy and commentary-friendly trending videos, download the TOP videos, send them to Enterprise WeChat with sharp Chinese commentary, and queue local video cleanup after 24 hours. Use when the user asks to harvest, download, push, archive, schedule, or 锐评 Douyin 抖音热点视频, 有梗热点, 名场面, 热梗, 爆款视频, or daily social-video素材.
metadata:
  cow:
    emoji: "🎬"
    requires:
      bins: ["python3"]
---

# Daily Douyin Video Harvester

## 核心目标

Use this skill when the user wants Douyin hot videos that are actually worth sending, not a stiff list of generic hot news. The bundled script:

1. Opens a dedicated persistent Playwright Chrome profile, injects the user-provided `DOUYIN_COOKIE` when present, collects normal Douyin page responses from bounded search queries, and closes the browser when done.
2. Uses interest-seeded searches first by default, then optionally uses Douyin hot-board terms when `douyin.use_hot_terms=true`. This avoids sending duplicate hard-news hot-search items the user already sees.
3. Filters out stale known-timestamp videos outside the default 48-hour window, and penalizes candidates without a timestamp.
4. Filters out boring or unsuitable topics such as formal announcements, disasters, crime, pure finance, hard-news bulletins, generic movie recaps, shopping, travel, and old fixed memes.
5. Ranks candidates by a combined score: current heat, engagement, freshness, and 梗感分.
6. Downloads only the TOP videos, default `3`, with an 80 MB per-video ceiling.
7. Generates short sharp commentary before sending.
8. Sends markdown commentary plus video to Enterprise WeChat when WeCom bot config and receiver are available.
9. Queues downloaded files for deletion after 24 hours and schedules a Windows cleanup task when possible.
10. Suppresses topics/videos already downloaded earlier on the same day, so a second same-day run does not resend the first batch.

## 有梗搜索策略

Do not search only for `梗图` or blindly send raw hot-list results. The script uses these signals:

- Default to bounded interest seeds such as `轻擦边舞蹈`, `氛围感美女跳舞`, `甜妹变装`, `情侣搞笑日常`, `评论区笑死`, and `反转名场面`.
- Search current hot-board terms only when `douyin.use_hot_terms=true`, with patterns such as `{term}`, `{term} 名场面`, `{term} 反转`, `{term} 笑死`, `{term} 二创`.
- Keep known-timestamp videos within `since_hours`, default `48`; stale videos are skipped.
- Limit the final TOP list to one video per hot term by default, so a single topic cannot fill all three slots.
- Add score: `名场面`, `离谱`, `笑死`, `反转`, `破防`, `整活`, `抽象`, `社死`, `显眼包`, `魔性`, `吐槽`, `二创`, `模仿`, `挑战`, `瓜`, `热梗`, `绷不住`, `擦边`, `氛围感`, `美女`, `甜妹`, `辣妹`, `热舞`, `舞蹈`, `变装`.
- Add score: titles with conversational hooks such as `怎么`, `为什么`, `原来`, `竟然`, `不是`, `这也`, `谁懂`, `网友`, `全网`, `哈哈`.
- Down-rank or filter: `国防部`, `外交部`, `警方通报`, `事故`, `地震`, `火灾`, `死亡`, `战争`, `违法`, `犯罪`, `发布会`, `财报`, `股价`, `政策`, `会议`, `电影推荐`, `影视解说`, `旅行`, `探店`, `带货`, `投资`, `读书`, `比赛集锦`, plus explicit sexual or unsafe terms such as `裸露`, `露骨`, `色情`, `成人`, `约炮`, `成人视频`, `未成年`, `nsfw`.
- Do not pin stale named memes as default search seeds. Examples such as `安卓人` or `峰哥` are only kept as scoring vocabulary when they appear in today's hot-board terms or page content.

The default `commentary_style` is `sharp`, so the Enterprise WeChat message should read like a concise social-media editor, not a neutral file sender. Use `brief` for softer wording and `none` to disable commentary.

## 一次性运行

```bash
python3 skills/daily-douyin-video-harvester/scripts/harvest_douyin_videos.py --max-total 3 --out "/path/to/output"
```

Dry run without downloads:

```bash
python3 skills/daily-douyin-video-harvester/scripts/harvest_douyin_videos.py --dry-run --json --max-total 3
```

Freshness override for diagnosis:

```bash
python3 skills/daily-douyin-video-harvester/scripts/harvest_douyin_videos.py --dry-run --json --max-total 3 --since-hours 72
```

Download only, do not send:

```bash
python3 skills/daily-douyin-video-harvester/scripts/harvest_douyin_videos.py --max-total 3 --no-send
```

Cleanup due videos:

```bash
python3 skills/daily-douyin-video-harvester/scripts/harvest_douyin_videos.py --cleanup --json
```

## 配置

Output directory priority:

1. CLI `--out`
2. Environment variable `DOUYIN_VIDEO_OUTPUT_DIR`
3. `~/.cow-douyin-video-harvester/config.json`
4. Default `~/cow/douyin-videos`

Environment variables:

- `DOUYIN_COOKIE`: optional fallback cookie header. The default persistent-browser path can rely on the skill-owned browser profile instead; if you set this, keep it in the user environment or local config, not in chat or Git.
- `DOUYIN_BROWSER_USER_DATA_DIR`: optional persistent Playwright Chrome profile override. By default the script uses its own project-local browser profile at `~/.cow-douyin-video-harvester/browser-profile`.
- `WECOM_BOT_ID` / `WECOM_BOT_SECRET`: optional override. If missing, the script reads `D:/CowWechat/config.json`.
- `WECOM_BOT_RECEIVER`: optional receiver userid/chatid. If missing, the script tries to resolve the first WeCom admin profile in CowWechat config.
- `WECOM_BOT_IS_GROUP`: set `1` or `true` for group chat receivers.
- `HTTP_PROXY` / `HTTPS_PROXY`: optional; Douyin requests default to direct mode in config because local proxy rules may break the site.

Important config keys:

```json
{
  "output_dir": "~/cow/douyin-videos",
  "collection_mode": "browser",
  "max_total": 3,
  "max_candidates": 30,
  "since_hours": 48,
  "max_per_hot_term": 1,
  "max_video_bytes": 80000000,
  "delete_after_hours": 24,
  "commentary_style": "sharp",
  "send_after_download": true,
  "exclude_same_day_topics": true,
  "douyin": {
    "cookie_env": "DOUYIN_COOKIE",
    "use_hot_terms": false,
    "fallback_keywords": ["轻擦边舞蹈", "氛围感美女跳舞", "甜妹变装", "情侣搞笑日常", "评论区笑死", "反转名场面"],
    "search_patterns": ["{term}", "{term} 名场面", "{term} 反转", "{term} 笑死", "{term} 二创"]
  },
  "browser_fallback": {
    "enabled": true,
    "use_persistent_profile": true,
    "user_data_dir": "~/.cow-douyin-video-harvester/browser-profile",
    "close_locked_profile_processes": true,
    "channel": "chrome",
    "headless": false,
    "max_queries": 6
  },
  "wecom": {
    "enabled": true,
    "receiver": "",
    "is_group": false,
    "project_config": "D:/CowWechat/config.json"
  }
}
```

If Douyin appears unreachable because Clash Verge subscription updates broke local direct rules, the script runs `D:\CowWechat\scripts\clash_verge_rule_guard.py --json` once and retries once. This repairs local proxy rules only; it does not bypass captcha, login, signatures, or platform risk control.

Default collection uses the skill-owned persistent Playwright Chrome profile under `~/.cow-douyin-video-harvester/browser-profile`. This keeps the skill portable across machines and independent from Codex-specific paths. The script still injects `DOUYIN_COOKIE` at startup when provided and closes the browser when done. If the skill profile is locked by a stale browser process, the script may close only Chrome processes whose command line explicitly references that profile path, then retry. It does not close the user's normal Chrome profile.

The script extracts candidates from ordinary page responses and DOM content. It does not read/export browser-stored cookies or localStorage, generate `a_bogus` or other signatures, or solve captcha. If a login or security verification page appears, it records a warning and stops; the user should complete the verification in the opened dedicated profile window, then run the skill again.

If a selected video is too large or unavailable, the script still sends the sharp commentary. When a cover image is available, it downloads and sends the cover instead of the video; otherwise it sends text only.

If no fresh, meme-worthy video passes the filters, the script exits normally with `candidate_count: 0`. This is intentional: it should not send stale or generic videos just to fill TOP3.

## 企业微信发送

The default send mode is the CowWechat WeCom bot websocket protocol:

- Send markdown first: title, score, source URL, author, and sharp commentary.
- Upload and send the video file second.
- If credentials or receiver cannot be resolved, the script keeps the files and records a warning instead of failing the whole run.

For use inside a live CowAgent chat, the agent may also run the script with `--no-send`, then send the downloaded local files through CowWechat's existing file-send tool. The script still writes `manifest.jsonl`, so the agent can read exact local video paths and commentary.

## 每日定时

CowAgent scheduler:

- Time: every day at 09:00
- Cron: `0 9 * * *`
- Task: 运行 daily-douyin-video-harvester，抓取今天 TOP3 有梗抖音热点视频，下载到 {输出目录}，以 sharp 风格生成锐评，发送到企业微信，并在 24 小时后清理下载视频。

System cron fallback:

```cron
0 9 * * * cd /path/to/CowWechat && python3 skills/daily-douyin-video-harvester/scripts/harvest_douyin_videos.py --max-total 3 --out "/path/to/output" >> ~/.cow-douyin-video-harvester/cron.log 2>&1
```

## 输出

```text
{output_dir}/
  YYYY-MM-DD/
    manifest.jsonl
    index.md
    state_delta.json
    douyin/
      001_douyin_...mp4
      001_douyin_cover_...jpg
  state/
    seen_urls.json
    seen_hashes.json
    daily_seen.json
    cleanup_queue.json
```

`manifest.jsonl` keeps source attribution, source URL, metrics, score, 梗感分, commentary, downloaded path, SHA-256, send status, and cleanup time.

## 合规边界

- Use normal public web responses or the user's explicit `DOUYIN_COOKIE`.
- Browser collection uses the skill-owned persistent Playwright profile by default; it must close the browser after each run and must not read/export browser-stored cookies or generate platform signatures.
- Do not bypass captcha, signature checks, login limits, or platform risk controls.
- Do not repost or publish to Douyin.
- Keep source attribution and local manifest records.
- Default frequency is daily and TOP3 only to avoid high-frequency scraping.
- Avoid sending sensitive tragedy/crime/disaster topics as "funny" commentary.
