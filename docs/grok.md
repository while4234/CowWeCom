# Grok / xAI 使用文档

本文说明 CowWeCom 的 Grok/xAI 原生账号能力。Grok 相关协议能力继续复用 `integrations/hermes_xai/` 中前序 PR 迁移自 Hermes 的 OAuth、Responses、TTS、图片和视频实现；PR5 只补文档、配置、只读迁移、Web 状态和回归测试。

不要把 access token、refresh token、authorization code、code verifier、完整 callback URL、API key 写入日志、群聊、Issue、README 或提交记录。

## 功能概览

CowWeCom 当前支持：

- Grok OAuth 登录。
- Grok Chat，`bot_type=grok` 或 `bot_type=xai` 时启用。
- Grok TTS。
- 企业微信语音模式，覆盖 `wechatcom_app` 和 `wecom_bot`。
- Grok 图片生成，支持文生图和单张参考图图生图。
- Grok 视频生成，支持文生视频、单图生视频、多图生视频。

个人微信当前不新增语音发送能力；个人微信文字、图片、文件、视频等既有能力保持原行为。

## Grok OAuth 登录

推荐在 Web 管理页完成 Grok 登录：

1. 打开 CowWeCom Web 管理页，进入 Grok 登录页或 Grok 登录卡片。
2. 点击“登录 Grok”。
3. 默认使用 loopback 回调：`http://127.0.0.1:56121/callback`。
4. 本地部署通常可以自动完成：浏览器授权后，后端收到 loopback callback，Web 页面轮询到登录成功。
5. 远程部署或 loopback 端口不可用时，使用 manual paste。
6. token 保存到 CowWeCom auth store，默认是项目内 `data/auth/grok_auth.json`，也可用 `grok_auth_file` 指定。
7. CowWeCom 不写回 Hermes auth store。

运行时优先使用 CowWeCom OAuth token；没有可用 OAuth 时，才回退到 `grok_api_key` 或 `XAI_API_KEY`。OAuth bearer 只允许发往 xAI 域名。

## 远程部署 Manual Paste

远程服务器、容器或浏览器无法访问服务端 loopback 时，按下面流程完成：

1. 在 CowWeCom Web 管理页点击登录，复制返回的 `authorize_url` 到浏览器。
2. 在浏览器完成 xAI/Grok 登录授权。
3. 浏览器跳转到 `http://127.0.0.1:56121/callback?...`，页面可能打不开，这是远程部署的正常现象。
4. 复制浏览器地址栏完整 callback URL。
5. 粘贴到 CowWeCom Web 页面的 manual paste 输入框。
6. CowWeCom 后端校验 `state`，再用当前 PKCE 会话换 token。

也可以粘贴只包含 `code` 和 `state` 的 query string，例如：

```text
?code=...&state=...
```

默认不接受裸 authorization code。只有在明确需要兼容旧 Grok fallback 页面时，才把 `grok_oauth_accept_bare_code` 设为 `true`，且必须仍存在当前内存中的 PKCE 登录会话。

不要把 callback URL 发到日志、群聊或不可信渠道；其中包含短期授权 code 和 state。

## Hermes Auth 只读导入

PR5 支持只读导入 Hermes 的 xAI OAuth 登录态：

- 配置 `grok_import_hermes_auth=true` 时，如果 CowWeCom auth store 不存在，会尝试读取 `~/.hermes/auth.json` 或 `HERMES_HOME/auth.json`。
- 只复制 `providers.xai-oauth` 的必要字段。
- 不复制其他 provider。
- 不修改 `~/.hermes/auth.json`。
- 默认 `grok_import_hermes_auth_overwrite=false`，CowWeCom 已有 xai-oauth 时不会覆盖。
- Web/API 返回导入状态时不返回 token。

## 配置项总表

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `grok_model` | `grok-4.3` | Grok Chat 模型。 |
| `grok_api_base` | `https://api.x.ai/v1` | xAI API base；OAuth token 不得发往非 xAI 域名。 |
| `grok_auth_file` | `""` | CowWeCom Grok auth store 路径；空值使用 `data/auth/grok_auth.json`。 |
| `grok_auth_prefer_oauth` | `true` | 优先使用 OAuth 登录态。 |
| `grok_import_hermes_auth` | `true` | CowWeCom auth store 缺失时只读导入 Hermes `providers.xai-oauth`。 |
| `grok_import_hermes_auth_overwrite` | `false` | 是否允许覆盖 CowWeCom 已有 xai-oauth。默认不覆盖。 |
| `grok_wire_api` | `responses` | Grok Chat 使用 Responses 兼容路径。 |
| `grok_api_key` | `""` | API key fallback；不要提交真实值。 |
| `text_to_voice` | `openai` | 设为 `grok` 或 `xai` 时使用 Grok TTS。 |
| `grok_tts_voice_id` | `eve` | Grok TTS voice。 |
| `grok_tts_language` | `zh` | Grok TTS 语言。 |
| `grok_tts_sample_rate` | `24000` | TTS 输出采样率。 |
| `grok_tts_bit_rate` | `128000` | TTS 输出码率。 |
| `grok_tts_auto_speech_tags` | `false` | 是否自动补充 speech tags。 |
| `grok_voice_reply_enabled` | `true` | 低延迟 low 语音回复开关；不影响语音会话模式单独开启。 |
| `grok_voice_mode_enabled` | `true` | 低延迟 low 语音模式兼容开关。 |
| `grok_voice_conversation_mode_enabled` | `true` | 语音会话模式：企业微信应用 / WeCom Bot 中“语音输入 -> 语音回复”。 |
| `grok_voice_reply_channels` | `["wechatcom_app", "wecom_bot"]` | 允许 Grok 语音回复的渠道；不要加入个人微信。 |
| `grok_voice_streaming_enabled` | `true` | 是否按模型增量流式切段 TTS。 |
| `grok_voice_require_low_reasoning` | `true` | 非会话模式下要求 low reasoning 才语音回复。 |
| `grok_voice_require_low_reasoning_when_not_conversation_mode` | `true` | 低延迟模式保持 low reasoning 门槛；语音会话模式不使用该门槛。 |
| `grok_voice_force_voice_for_voice_input_in_conversation_mode` | `true` | 语音会话模式中，允许语音输入优先语音回复。 |
| `grok_voice_force_reasoning_effort` | `low` | 语音会话模式强制低延迟 effort。 |
| `grok_voice_low_latency_backend` | `""` | 可选低延迟后端覆盖；空值沿用当前后端。 |
| `grok_voice_low_latency_model` | `""` | 可选低延迟模型覆盖；空值沿用当前模型。 |
| `grok_voice_max_output_tokens` | `220` | 语音模式短回复 token 上限。 |
| `grok_voice_short_answer_prompt_enabled` | `true` | 语音会话模式是否追加短回复提示。 |
| `grok_voice_max_segment_chars` | `180` | 单段 TTS 最大字符数。 |
| `grok_voice_min_segment_chars` | `18` | 单段 TTS 最小字符数。 |
| `grok_voice_flush_idle_ms` | `1500` | 增量空闲多久后 flush 一段。 |
| `grok_voice_tts_queue_size` | `4` | 单会话 TTS 队列大小。 |
| `wecom_voice_max_seconds` | `55` | 企业微信单条语音最大秒数。 |
| `wecom_voice_max_bytes` | `1900000` | 企业微信单条语音最大字节数。 |
| `wecom_voice_normalize_enabled` | `true` | 转 AMR 前启用响度归一化。 |
| `wecom_voice_normalize_target_dbfs` | `-18.0` | 响度归一化目标。 |
| `wecom_voice_normalize_headroom_db` | `1.0` | 归一化预留余量。 |
| `wecom_voice_amr_bitrate` | `12.2k` | 企业微信 AMR-NB 码率。 |
| `reasoning_effort_policy_low_effort` | `low` | 本地低推理策略命中时使用的 effort。 |
| `text_to_image` | `dall-e-2` | 设为 `grok` 或 `xai` 时使用 Grok 图片生成。 |
| `grok_image_model` | `grok-imagine-image` | Grok 图片生成模型。 |
| `grok_image_resolution` | `1k` | Grok 图片分辨率。 |
| `grok_image_aspect_ratio` | `square` | Grok 图片比例。 |
| `grok_image_timeout_seconds` | `120` | 图片生成超时。 |
| `grok_image_download_timeout_seconds` | `60` | 图片下载超时。 |
| `video_generation_provider` | `xai` | 视频生成 provider；当前只支持 Grok/xAI。 |
| `video_create_prefix` | `["生成视频", "视频生成", "画个视频"]` | 文生视频/图生视频触发前缀。 |
| `grok_video_model` | `grok-imagine-video` | Grok 视频模型。 |
| `grok_video_duration` | `8` | 默认视频时长，单位秒。 |
| `grok_video_aspect_ratio` | `16:9` | 默认视频比例。 |
| `grok_video_resolution` | `720p` | 默认视频分辨率。 |
| `grok_video_timeout_seconds` | `240` | 视频任务轮询总超时。 |
| `grok_video_poll_interval_seconds` | `5` | 视频任务轮询间隔。 |
| `grok_video_download_timeout_seconds` | `120` | MP4 下载超时。 |

最小 Grok Chat 配置示例：

```json
{
  "bot_type": "grok",
  "grok_model": "grok-4.3",
  "grok_auth_prefer_oauth": true
}
```

## 语音模式规则

Grok 语音回复分为两种模式：

- 低延迟 low 语音模式：用户原始输入是语音，且本地思考深度策略直接命中 low 时，才优先语音回复；语音输入但复杂任务默认走文本回复。
- 语音会话模式：开启 `grok_voice_conversation_mode_enabled` 后，在 `wechatcom_app` 和 `wecom_bot` 中允许“用户发语音 -> 机器人语音回复”。语音模式下发送语音均回复语音，同时强制低延迟、短回复和 low reasoning。

两种模式都必须遵守以下边界：

- 只对 `input_is_voice=True` 生效，文字输入不会语音回复。
- 企业微信语音回复只在 `wechatcom_app` 和 `wecom_bot` 中启用，个人微信不支持新增语音发送。
- 个人微信当前不新增语音发送能力；即使收到个人微信语音，也不会因为 Grok 配置新增发语音。
- TTS、AMR 转码、上传或发送失败时必须回退文本。
- 已成功发送至少一段语音流后，才会抑制最终完整文本；没有语音段成功时最终文本必须发送。
- 临时 TTS / AMR 文件会在发送流程结束后清理。

旧配置 `voice_reply_voice` 默认保持关闭，避免文字输入被意外转成语音。

## 企业微信语音限制

企业微信原生语音消息使用 AMR。CowWeCom 会在发送前转换、切分并上传：

- 格式：AMR。
- 单条 <= 55 秒。
- 单条 <= 1.9 MB。
- 默认 AMR-NB 码率：`12.2k`。

如果 ffmpeg、pydub 或 AMR 编码不可用，语音发送会失败并回退文本。部署新机器时请确认 `ffmpeg` 和 `ffprobe` 在 `PATH` 中。

## 图片与视频生成

图片生成：

- 通过现有图片生成前缀触发。
- `text_to_image=grok` 或 `text_to_image=xai` 时使用 Grok 图片生成。
- 使用同一套 Grok OAuth / API key 凭据。
- 图生图 v1 只支持一张参考图，可使用本地路径、`file://`、HTTP/HTTPS URL 或 data URI；未显式指定比例/尺寸时会尽量按参考图尺寸推断比例和 1k/2k 分辨率。
- xAI 返回 b64 或 URL 后，CowWeCom 先保存成本地文件，再发送本地图片。
- 不直接向用户发送远端 URL。
- URL 下载只允许公开 HTTPS 地址，并会逐跳校验 redirect、DNS 解析结果和 Content-Type，拒绝 localhost、内网、link-local 与云 metadata 地址。
- 生成图片保存在 `tmp/grok_media/`，发送成功或失败后都会按 cleanup 标记清理。

视频生成：

- 通过 `video_create_prefix` 触发，例如 `生成视频 夕阳下的城市航拍，电影感`。
- 引用单图：引用图片后发 `生成视频 让这张图里的车驶过雨夜街道`。
- 上文单图：先发图，再发 `参考上面发的图片生成一个镜头推进的视频`。
- 上文多图：连续发图，再发 `参考上面发的3张图片生成一个产品展示视频`。
- 最多 7 张参考图，超过会提示只支持最近 7 张。
- 生成可能耗时，任务有 timeout，不会无限轮询。
- 成功后下载成本地 MP4，再发送 `ReplyType.VIDEO`。
- 渠道不支持视频时会 fallback 为文件或明确文本提示，不会静默失败。
- 视频下载同样只允许公开 HTTPS 地址；`application/octet-stream` 只有通过 MP4 magic bytes 校验时才会接受。
- 生成视频保存在 `tmp/grok_media/`，WeCom Bot / 企业微信应用发送成功、上传失败或 fallback 为文件后都会清理本次生成的 MP4。

引用图片但上下文无图时，系统会提示先发送或引用图片。

## Web 状态与安全

Web Grok API 只返回登录状态、provider、base URL、邮箱、过期时间、是否需要重新登录、测试连接结果等安全字段。Web 状态、测试连接、手动登录和错误响应不得包含：

- access token。
- refresh token。
- authorization code。
- code verifier。
- Authorization header。
- 完整 callback URL。
- API key。

前端输出面板也会做一层防御性脱敏；后端才是安全边界。

## 常见错误

| 错误 | 可能原因 | 处理方式 |
| --- | --- | --- |
| 未登录 Grok | CowWeCom auth store 无 xai-oauth，且无 API key fallback。 | 在 Web 管理页登录 Grok，或配置 `grok_api_key`。 |
| token refresh 失败 | refresh token 失效、账号撤权、auth store 损坏。 | 退出 Grok 后重新登录。 |
| loopback 端口被占用 | `127.0.0.1:56121` 无法 bind。 | 使用 manual paste，或释放端口后重新登录。 |
| manual paste state mismatch | 粘贴的 callback 不属于当前登录会话。 | 重新点击登录，并粘贴最新 callback URL。 |
| ffmpeg 不支持 AMR | 本机 ffmpeg 缺少 AMR 编码能力。 | 安装支持 AMR 的 ffmpeg，或让语音失败回退文本。 |
| 企业微信语音上传超限 | 音频过长、过大或转换失败。 | 使用默认切分配置；仍失败时缩短回复或回退文本。 |
| 图片 URL 下载失败 | xAI CDN 临时不可达、代理或超时。 | 稍后重试，检查网络和 `grok_image_download_timeout_seconds`。 |
| 视频生成超时 | xAI 视频任务排队或运行超过 timeout。 | 稍后重试，或增大 `grok_video_timeout_seconds`。 |
| xAI 429 rate limit | xAI 额度或频率限制。 | 降低并发，等待额度恢复。 |
| xAI 401 auth error | access token/API key 无效。 | 触发 refresh；仍失败时重新登录或检查 API key。 |

## 回归矩阵

PR5 覆盖并建议人工确认：

| 场景 | 期望 |
| --- | --- |
| 普通文本输入 | 文本回复正常。 |
| 文字“你好” | 不语音回复。 |
| WeCom Bot 语音简单问题 | low，语音分段回复。 |
| WeCom Bot 语音复杂问题，未开语音会话模式 | 文本回复。 |
| WeCom Bot / 企业微信应用语音复杂问题，已开语音会话模式 | 语音分段回复，失败时回退文本。 |
| 企业微信应用语音简单问题 | low，语音分段回复。 |
| 个人微信文字 | 原行为不变。 |
| 个人微信语音 | 不新增发语音。 |
| Grok 未登录 | 清晰错误。 |
| Grok token 过期 | refresh 或提示重新登录。 |
| 图片生成 b64 | 本地文件发送。 |
| 图片生成 URL | 下载本地后发送。 |
| 视频生成 done | 本地 MP4 发送。 |
| 视频生成 timeout | 清晰超时提示。 |
| TTS 失败 | 回退文本。 |
| 上传语音超限 | 自动切分或回退文本。 |
