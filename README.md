# CowWeCom

<p align="center">
  <img src="docs/images/readme-banner.png" alt="CowWeCom 项目横幅" width="920" />
</p>

<p align="center">
  <a href="https://github.com/while4234/CowWeCom"><img src="https://img.shields.io/badge/Project-CowWeCom-29b36a" alt="CowWeCom"></a>
  <a href="https://github.com/zhayujie/CowAgent"><img src="https://img.shields.io/badge/Upstream-CowAgent-555555" alt="上游 CowAgent"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/Python-3.7--3.13-3776ab" alt="Python 3.7-3.13">
  <img src="https://img.shields.io/badge/Channels-Weixin%20%7C%20WeCom-2fb26b" alt="微信与企业微信">
</p>

CowWeCom 是一个面向微信和企业微信场景的本地 AI Agent 项目。它保留了 [zhayujie/CowAgent](https://github.com/zhayujie/CowAgent) 的核心框架能力，并在本仓库中围绕微信、企业微信、长期记忆、知识库、Skills、定时任务、图像生成、后端路由和多用户隔离做了持续开发。

本文档只描述本项目当前重点开发和验证的能力。上游项目中存在但本项目未重点测试的飞书、钉钉、QQ、公众号等通道不在本 README 中作为已支持能力展开说明。

## 目录

- [项目定位](#项目定位)
- [能力概览](#能力概览)
- [支持范围](#支持范围)
- [快速开始](#快速开始)
- [核心配置](#核心配置)
- [微信接入](#微信接入)
- [企业微信接入](#企业微信接入)
- [Web 控制台](#web-控制台)
- [Agent、记忆与知识库](#agent记忆与知识库)
- [Skills 与工具](#skills-与工具)
- [常用命令](#常用命令)
- [项目结构](#项目结构)
- [更新日志](#更新日志)
- [安全说明](#安全说明)
- [项目来源与许可证](#项目来源与许可证)

## 项目定位

CowWeCom 的目标不是做一个“所有平台都写在 README 里的通用机器人”，而是把微信和企业微信里的日常 AI 助手体验做得更稳：

- 面向个人微信和企业微信会话，提供可长期运行的本地 Agent。
- 让 Agent 可以使用本地文件、终端、浏览器、知识库、定时任务和 Skills 完成复杂任务。
- 对多用户、多会话、群聊和企业微信场景做隔离，避免私有记忆互相污染。
- 保留上游 CowAgent 的可扩展架构，但文档聚焦本仓库实际维护的方向。

## 能力概览

| 模块 | 当前能力 |
| --- | --- |
| 微信通道 | 个人微信扫码登录、文本/图片/文件/视频收发、图片识别上下文、主动发送与跨用户转述能力 |
| 企业微信智能机器人 | WebSocket 长连接、单聊/群聊、文本/图片/文件、Markdown/流式回复、群成员别名、群聊记忆隔离 |
| 企业微信自建应用 | 通过回调服务接入企业微信应用，适合有公网服务和企业后台配置的场景 |
| Web 控制台 | 本地对话、通道管理、Skills 管理、记忆/知识库浏览、调度任务、日志、缓存和后端状态查看 |
| Agent 模式 | 多轮任务规划、本地工具调用、长任务进度反馈、取消/跳过/状态查询、上下文压缩 |
| 长期记忆 | 按用户和会话隔离的记忆文件、每日深度整理、记忆检索和管理 |
| 知识库 | 本地知识库、协议/规范公共知识后端、上传构建索引、LLM 学习文档生成、可追溯检索 |
| Skills | 项目内置 Skills 启动同步到运行工作区，可按需启用、禁用、校验和扩展 |
| 图像/视频生成 | 使用本项目 `image-generation` Skill，支持按当前模型后端选择 GPT/Codex 或 Grok 生图、Grok 单图图生图、后台任务和结果回传；切到 Grok 后默认生图走 Grok，显式说 GPT/OpenAI/Codex 生图才回到 GPT/Codex；`image-prompt-optimization` Skill 集中保存 YouMind/Nano Banana Pro 仓库、Grok 重写模板和随机片段仓库；GPT/Codex 图片继续按默认筛选规则润色，Grok 图片/视频默认用 Grok 文本模型按模板和仓库片段重写；管理员可用 `/grok-direct image|video` 直出命令绕过 Agent 提示词润色和隐藏增强/重写，图片和视频都可复用可解析图片或最近图片作为参考图 |
| 后端路由 | Codex、OpenAI-compatible/CAPI 等 GPT 后端路由，支持额度查询、自动切换、推理强度策略，以及管理员/白名单独立 Grok 后端 |
| 安全隔离 | 管理员/普通用户角色、普通用户文件访问边界、敏感路径保护、Web 管理接口认证 |

## 支持范围

当前 README 明确覆盖以下通道：

| 通道 | `channel_type` | 状态 | 说明 |
| --- | --- | --- | --- |
| 个人微信 | `weixin` 或 `weixin_*` | 重点维护 | 扫码登录，支持命名实例和多用户隔离 |
| 企业微信智能机器人 | `wecom_bot` | 默认推荐 | 使用 Bot ID 和 Secret 走长连接，适合企业微信单聊和群聊 |
| 企业微信自建应用 | `wechatcom_app` | 可用 | 需要公网回调 URL、企业可信 IP 和企业微信后台配置 |
| Discord | `discord` | 可用 | 独立于微信和企业微信的管理员通道，支持原生 Slash Commands 和 Grok 生图/视频直出 |
| Web 控制台 | 自动附加 `web` | 默认开启 | 本地管理入口，不替代微信/企业微信业务通道 |
| 终端调试 | `terminal` | 开发调试 | 适合本地排查，不作为主要使用入口 |

未在本项目重点验证的其他通道，代码中可能仍然保留上游实现，但不在此处承诺可用性。

## 快速开始

### 1. 准备环境

- Python 3.7 到 3.13。
- Windows、Linux、macOS 均可运行；本仓库当前主要按 Windows 本地运行场景维护。
- 需要至少一个可用的大模型后端密钥或 Codex auth 配置。
- 微信接入需要扫码登录。
- 企业微信智能机器人接入需要 Bot ID 和 Secret。

### 2. 安装依赖

Windows PowerShell：

```powershell
git clone https://github.com/while4234/CowWeCom.git
cd CowWeCom

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
```

Grok/xAI TTS 和企业微信语音发送需要 `pydub` 与系统 `ffmpeg`。`pydub` 已纳入 `requirements.txt`；部署新机器时还需要确认 `ffmpeg`/`ffprobe` 在 `PATH` 中，否则 TTS 可以生成音频文件但无法转换并上传为企业微信语音。

Linux / macOS：

```bash
git clone https://github.com/while4234/CowWeCom.git
cd CowWeCom

python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip setuptools wheel
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m pip install -e .
```

### 3. 创建本地配置

```powershell
Copy-Item .\config-template.json .\config.json
```

`config.json` 是本地私有配置，已经被 Git 忽略。请只在这里保存 API Key、企业微信 Secret、Web 密码等本机凭证。

### 4. 启动

```powershell
.\.venv\Scripts\python.exe app.py
```

安装 CLI 后也可以使用：

```powershell
.\.venv\Scripts\cow.exe start
.\.venv\Scripts\cow.exe status
.\.venv\Scripts\cow.exe logs
```

启动后默认会同时运行 Web 控制台。默认访问地址：

```text
http://127.0.0.1:9899
```

## 核心配置

默认模板面向企业微信智能机器人：

```json
{
  "channel_type": "wecom_bot",
  "model": "deepseek-v4-flash",
  "deepseek_api_key": "",
  "deepseek_api_base": "https://api.deepseek.com/v1",
  "wecom_bot_id": "",
  "wecom_bot_secret": "",
  "wecom_bot_auth_source": "cowagent",
  "web_console": true,
  "web_host": "",
  "web_password": "",
  "agent": true,
  "agent_workspace": "~/cow"
}
```

常用字段说明：

| 字段 | 说明 |
| --- | --- |
| `channel_type` | 主通道类型。推荐 `wecom_bot` 或 `weixin`。多个通道可用逗号分隔，例如 `wecom_bot,weixin` |
| `model` | 默认模型名称。模板使用 `deepseek-v4-flash` |
| `agent` | 是否启用 Agent 模式。推荐保持 `true` |
| `agent_workspace` | 运行工作区，存放记忆、Skills、MCP 配置、用户文件等 |
| `web_console` | 是否自动启动 Web 控制台 |
| `web_host` | Web 控制台监听地址。留空时，本地无密码默认只监听 `127.0.0.1` |
| `web_password` | Web 控制台访问密码。公网或局域网暴露时必须设置 |
| `agent_admin_users` | 管理员用户 actor id 列表；Web 微信扫码接入遵守微信管理员约束，企业微信智能机器人按 `wecom_bot` 单独判断首个管理员 |
| `discord_bot_token` / `discord_admin_user_id` / `discord_allowed_channel_ids` / `discord_proxy` / `discord_prune_global_commands_on_startup` | Discord 通道配置；Discord 只允许一个独立管理员，可选限定 Guild 和频道；国内网络环境可为 Discord 单独设置 HTTP 代理；配置 Guild 时默认清理历史全局 Slash Commands |
| `agent_user_profiles` | 用户角色、展示名、记忆 ID 等覆盖配置；扫码选择的管理员/普通用户身份会写入这里 |
| `external_reply_inject_to_agent_context` | 是否把 CowCli 等非 Agent 快答的可见问答同步进后续 Agent 会话上下文，默认开启，便于“把这个转述给她”这类跟进指令引用最新回复 |
| `short_contextual_reply_keep_turns` | “没有/不用/好的”等含糊短回复请求只保留最近上下文轮数，默认 2，避免旧主题串扰 |
| `image_create_prefix` | 图片生成显式触发词；默认只保留“画图/生图/生成图片”等明确生图说法，引用图片后问“看这张图是什么”会走识图问答 |
| `single_chat_image_recognition_auto_reply` | 私聊图片识别完成后是否主动回复非账单图片，默认 `false`；账单自动记账和后续文本追问不受影响 |
| `image_send_max_width` / `image_send_max_height` | 微信、企业微信发送图片前的最大宽高，默认 2048x2048 |
| `weixin_image_send_max_bytes` / `wecom_image_send_max_bytes` / `wechatcom_image_send_max_bytes` | 微信、企业微信智能机器人、企业微信自建应用发送图片前的字节上限 |
| `knowledge` | 是否启用本地知识库 |
| `knowledge_backend` | 本地文档知识库与公共协议/规范知识后端配置 |
| `agent_knowledge_max_steps` | 需要查询公共/个人知识库、上传文档、协议原文或 `knowledge_query/deep_query` 的问答步数预算；默认 40，并锁定高质量推理 |
| `skill` | Skills 的运行时配置，例如图像生成 Codex auth |
| `llm_backend` | Codex/CAPI/Grok 等后端路由、受限用户后端和 GPT 自动切换配置 |
| `project_optimizer_*` | 本地优化证据记录、原始输入缓存消费和临时脚本快照配置；默认写入 `agent_workspace/data/project-optimizer/`，不得进入 Git |
| `reasoning_effort_policy_runtime_auto_optimize_enabled` | 旧的 Agent 内后台思考深度自动调优开关；默认关闭，主力机器改用 Codex 每日 0 点 automation 先检查 300 次增量模型调用再运行项目优化 |

CAPI 额度卡/月卡查询依赖 `llm_backend.providers.capi` 和 `llm_backend.providers.capi_monthly` 下的专用 key。更新部署后请检查本机 `CAPI_API_KEY`、`CAPI_MONTHLY_API_KEY`，或在 ignored 的 `config.json` 中配置 `llm_backend.providers.capi.api_key`、`llm_backend.providers.capi_monthly.api_key`；不要把真实 key 写入 `config-template.json` 或提交到 Git。

Grok 现在作为独立、受限的模型后端接入 `llm_backend.providers.grok`：Web 管理端可以添加或保存模型后端 profile，并把 Grok 或其他已保存后端只分配给管理员和白名单用户。默认白名单只有 `山海入梦来`；普通用户仍共用同一个 GPT 后端池，只按现有额度和规则在 CAPI/CAPI 月卡/Codex 之间切换，不能切换到受限 Grok 后端。每日 00:00 自动切换只处理全局 GPT 后端，不会自动切到 Grok；管理员和白名单用户的个人后端选择也不会改写普通用户的全局后端。

管理员和白名单用户可以在聊天中直接用自然语言快速切换个人模型后端，例如“切换后端到 Grok / xAI”会在 CowCli 本地命令层写入个人 Grok override，并在 Agent 执行前完成；“切回 GPT 后端”会清除个人 override，回到共享 GPT 后端池。这个本地切换不触发 Agent、不修改普通用户的全局 GPT 后端；查询“当前后端”或“当前后端额度”时会按发起人的有效后端展示，普通用户看到共享 GPT 后端，管理员/白名单用户看到自己的个人后端，普通用户发送同类后端切换话术仍会被拒绝。

Grok/xAI 原生账号登录现在在 Web 控制台的模型配置页完成，`/grok` 旧灰度页会回到控制台；控制台可新增多个命名 Grok 账号、轮询/粘贴 callback 完成 OAuth、切换当前账号、检查凭据并退出指定账号。文字聊天模型由受限后端 profile 控制，不再要求把普通用户的全局 `bot_type` 改成 Grok。Grok 图片生成复用当前选中的 Grok OAuth 凭据：当管理员或白名单用户把个人模型后端切到 Grok 后，普通生图默认走 Grok；如果此时要走 GPT/OpenAI/Codex 生图，需要在请求里明确说明。仅说质量或速度偏好不会切换生图提供方。管理员还可以直接发送 `/grok-direct image -- <prompt>` 或 `/grok-direct video -- <prompt>`，绕过 Agent/LLM 的提示词分析和润色，把原始 prompt 提交到 Grok 后台任务；默认生图为 speed，默认视频为 `480p / 16:9 / 10s`。`/grok-direct image` 会同时跳过 Grok 模型提示词重写；`/grok-direct video` 会使用消息中可解析的上传、回复或引用图片作为视频参考图，并在企业微信智能机器人里支持先发图片、随后说“生成视频 ...”或“参考上图/上面几张生成视频”时自动补齐同会话最近图片。若企业微信引用旧图事件只携带纯文本、不携带图片内容或可追溯 msgid，机器人无法知道引用的是哪张旧图。

国内网络环境中如果 Web Grok 登录报 `xAI OIDC discovery failed` 或直连 `auth.x.ai` 超时，请在 Web 配置或 ignored 的 `config.json` 中设置 `grok_proxy`，例如 `http://127.0.0.1:7897`。该代理会覆盖 Grok OAuth discovery/token 交换、Grok Chat、TTS、图片/视频生成、生成结果下载和视频后台子进程；为空时会复用全局 `proxy` 或 `discord_proxy`。

Grok 生图支持 `grok-imagine-image` 速度模型和 `grok-imagine-image-quality` 质量模型；只有用户明确说 Grok 高质量、quality mode、高清、高质量、精细等类似要求时才使用质量模型，否则 Grok 默认使用快速模型，并把 xAI 返回的 URL 或 b64 图片先落成本地文件再发送。Grok 图生图 v1 支持一张参考图，可接收本地路径、`file://`、HTTP/HTTPS URL 或 data URI；有参考图时走 xAI image edit `/images/edits`，纯文生图保持 `/images/generations`；未显式指定比例/尺寸时会尽量按参考图尺寸推断比例和 1k/2k 分辨率，明确写了 `16:9`、`1K`、`1024x1024` 等参数时不会被参考图覆盖。图生图和图生视频在润色、非润色直出路径都会追加参考图身份锁，尽量保持参考人物面容、脸型、发型、肤色/肤质、显著特征和整体体型；有参考图时润色不会额外加入国籍、眼睛颜色、发色、年龄、体型、面部特征等外貌描述，除非用户明确要求修改。先上传一张图片后，管理员可用 `/grok-direct image -- 换成电影海报风格`、`/grok-direct image --quality quality -- 换成高质量摄影棚风格` 或 `/grok-direct image --ar 16:9 -- 改成横版封面` 做原始 prompt 图生图；`/grok-direct image` 不做自动提示词润色，而自然语言“参考上图生成图片 / 按照这张图改成 ...”会走普通 Grok 生图链路并启用 Grok 专属提示词重写。隐藏提示词处理现在集中在 `skills/image-prompt-optimization/`：GPT/Codex 继续隐式检索 `references/nano-banana-pro/` 中的 YouMind 全量提示词库并按默认分类筛选规则适配；Grok 图片和普通 Grok 视频不再走 GPT，而是用 Grok 文本模型读取 `templates/grok_image_system_prompt.txt` / `templates/grok_video_system_prompt.txt` 和随机仓库片段后重写最终 prompt。普通 Grok 润色默认按 90% `repositories/grok/`、10% 其他仓库选择随机补全；若 prompt 包含 `NSFW`/`nsfw`，会把该词作为内部选择信号从润色源 prompt 移除，优先使用 `repositories/grok/NSFW/` 片段，并在可用时只混入 1 条背景、风格、色彩、材质等安全上下文补充片段；若 prompt 指明 `Korean`、`Korea`、`韩国` 等国籍/族裔，会加入稳定人物约束并过滤冲突身份外貌片段；显式写入仓库关键词 `grok` 时也会从最终视觉请求中移除该关键词。`repositories/grok/` 内置 YetAnotherWildcardCollection 的完整 `.txt` wildcard/prompt 快照，后续可继续追加 UTF-8 `.txt`，每行一条片段。隐藏提示词只写入用户工作区历史，普通前台消息不展示；用户明确要求查看刚才润色后的提示词时，Agent 会读取已存储的最终 prompt，不重新润色；如果下游审核或 API 在润色完成后失败，也会尽量记录这次最终 prompt 供排查。Grok 视频生成使用当前选中的 Grok OAuth 凭据和 `grok-imagine-video`，配置项为 `video_generation_provider=xai`、`video_create_prefix`、`grok_video_model`、`grok_video_duration`、`grok_video_aspect_ratio`、`grok_video_resolution`、`grok_video_timeout_seconds`、`grok_video_poll_interval_seconds`、`grok_video_download_timeout_seconds`、`grok_video_generation_global_workers` 和 `grok_video_generation_actor_workers`；微信/企业微信/Web 里可直接说“生成视频 720p 10s ...”，本地会识别 `720p`、`480p`、`10s` 等明确视频参数，普通 Grok 视频会先做 Grok 专属 prompt 重写，`/grok-direct video` 继续使用原始 prompt，视频重写结果也会写入同一套 prompt 历史。默认会向上使用同会话最近 1 张已发送或已上传图片作为参考图，也可以先发多张图片后说“参考上面发的 3 张图片生成 ... 视频”。明确说“文生视频”或“不参考图”时不会自动带图。带参考图且未显式指定比例时，视频比例会按参考图尺寸推断；普通 Grok 视频和 `/grok-direct video` 都提交后台任务，完成后回发 MP4，视频后台支持同一用户并行生成。生成结果总是先下载为本地 MP4，再用 `ReplyType.VIDEO` 发送，不直接把 xAI 远端 URL 发给用户；Web 端会把后台完成的本地图片/视频注册为可打开或下载的临时链接。OAuth token 默认写入 CowWeCom 的 `data/auth/grok_auth.json`，默认账号沿用 `providers.xai-oauth`，命名账号写入 `providers.xai-oauth:<account_id>`；也可以用 `grok_auth_file` 指定 auth store。`grok_import_hermes_auth=true` 时可在 CowWeCom auth store 缺失时只读导入 Hermes 的默认 `providers.xai-oauth`，不会写回 Hermes auth store；`grok_api_key` 和 `XAI_API_KEY` 只作为未登录时的 fallback。手动粘贴登录默认要求完整 callback URL 或同时包含 `code` 和 `state` 的查询字符串；裸授权码兼容需显式开启 `grok_oauth_accept_bare_code=true`，且必须存在当前 PKCE 登录会话。Web 状态/测试接口只返回登录状态、账号名称、邮箱、过期时间等安全字段，不返回 access token、refresh token、authorization code 或 code_verifier。完整配置和排障见 [docs/grok.md](docs/grok.md)。

图片生成现在只在用户明确说“画图 / 生图 / 生成图片 / 绘图 / 出图”或明确要求编辑、融合图片时触发；引用或发送图片后问“看这张图是什么”“找一下来源”等普通识图问题不会进入生图工具。微信和企业微信发送图片前会按配置的最大宽高和字节上限自动规整，避免超大生成图或引用图在上传阶段失败。

当前 Grok 提示词资源已经拆分：GPT/Codex 图片仍使用 `skills/image-prompt-optimization/references/nano-banana-pro/`；Grok 图片使用 `skills/grok-image-prompt-optimization/` 下的模板、脚本和 `repositories/grok/` 片段；Grok 视频使用 `skills/grok-video-generation/templates/grok_video_system_prompt.txt`，不再依赖图片片段仓库。“随机…提示词”类请求只返回英文 prompt 和中文翻译，不会提交生图任务；未明确 `文生图` 时默认按图生图提示词约束处理。

企业微信原生语音气泡仍受平台 AMR 窄带格式限制。为改善听感，默认会减少 Grok 流式 TTS 的切段频率（`grok_voice_max_segment_chars=180`、`grok_voice_flush_idle_ms=1500`），并在转换企业微信语音前启用响度归一化与最高 AMR-NB 码率（`wecom_voice_normalize_enabled=true`、`wecom_voice_normalize_target_dbfs=-18.0`、`wecom_voice_normalize_headroom_db=1.0`、`wecom_voice_amr_bitrate=12.2k`）。这些设置会保留原生语音气泡，但不会突破企业微信 AMR 本身的电话音质上限。

## 微信接入

个人微信通道使用：

```json
{
  "channel_type": "weixin",
  "web_console": true,
  "agent": true
}
```

部署完成后可在 Web 控制台「通道」页面扫码接入微信，并在扫码前选择「管理员」或「普通用户」。系统只允许存在一个管理员；如果已配置或已扫码产生管理员，后续扫码只能选择普通用户。

首次启动时会出现二维码，使用微信扫码并确认后即可登录。登录凭证默认保存到：

```text
~/.weixin_cow_credentials.json
```

如需重新登录，停止服务后删除该凭证文件，再重新启动。

### 多微信实例

本项目支持命名微信实例，例如管理员和普通用户分别扫码：

```json
{
  "channel_type": "weixin,weixin_user",
  "weixin_credentials_path": "~/.weixin_cow_credentials.json",
  "weixin_instances": {
    "weixin_user": {
      "credentials_path": "~/.weixin_cow_credentials_user.json"
    }
  }
}
```

命名实例会形成独立的 actor id 和记忆空间，便于做用户隔离、社交桥和权限控制。

## 企业微信接入

### 企业微信智能机器人

推荐使用 `wecom_bot`。它通过企业微信智能机器人长连接模式接入，不需要公网回调。

```json
{
  "channel_type": "wecom_bot",
  "wecom_bot_id": "YOUR_BOT_ID",
  "wecom_bot_secret": "YOUR_BOT_SECRET",
  "wecom_bot_auth_source": "cowagent"
}
```

连接成功后日志会出现类似信息：

```text
[WecomBot] Subscribe success
```

本项目在企业微信智能机器人方向额外强化了：

- 单聊和群聊身份区分。
- 群聊按 `chatid` 建立独立会话与记忆空间。
- 单聊首个接入的企业微信用户会自动登记为 `wecom_bot` 管理员；之后接入的企业微信联系人默认为普通用户。这个判断与个人微信管理员分离，即使微信已经有管理员，企微第一个单聊用户仍会成为企微管理员。
- 群成员别名配置。
- 图片、文件缓存后与后续文本合并处理。
- 定时任务和社交桥消息的主动发送。
- 流式响应结束后的最终内容合并。

群成员别名示例：

```json
{
  "wecom_bot_member_aliases": {
    "USER_ID": "张三"
  },
  "wecom_bot_group_member_aliases": {
    "GROUP_CHAT_ID": {
      "USER_ID": "张三"
    }
  }
}
```

### 企业微信自建应用

企业微信自建应用使用 `wechatcom_app`，适合有公网服务器、企业后台权限和可信 IP 配置的场景。

```json
{
  "channel_type": "wechatcom_app",
  "single_chat_prefix": [""],
  "wechatcom_corp_id": "YOUR_CORP_ID",
  "wechatcomapp_token": "YOUR_TOKEN",
  "wechatcomapp_secret": "YOUR_SECRET",
  "wechatcomapp_agent_id": "YOUR_AGENT_ID",
  "wechatcomapp_aes_key": "YOUR_AES_KEY",
  "wechatcomapp_port": 9898
}
```

回调地址格式：

```text
http://YOUR_HOST:9898/wxcomapp/
```

请在企业微信后台确认 URL、Token、EncodingAESKey、可信 IP 和端口安全组配置一致。

提示词查看说明：隐藏提示词历史只在用户明确要求时读取，默认返回中文展示文案；Grok 图片和视频记录优先使用 Grok 文本模型翻译，普通 GPT/Codex 记录使用当前 Agent 模型或翻译桥兜底。需要排查原始 prompt 时，可明确要求返回原文。

## Discord 接入

Discord 通道独立于微信和企业微信，适合把 CowCli 管理命令和 Grok 生图、文生视频、图生视频直出放到 Discord 使用。当前实现只允许一个 Discord 管理员，管理员配置不会复用微信/企业微信管理员身份。

```json
{
  "channel_type": "discord",
  "discord_bot_token": "YOUR_DISCORD_BOT_TOKEN",
  "discord_guild_id": "YOUR_GUILD_ID",
  "discord_admin_user_id": "YOUR_DISCORD_USER_ID",
  "discord_allowed_channel_ids": ["YOUR_CHANNEL_ID"],
  "discord_proxy": "http://127.0.0.1:7897"
}
```

也可以在 Web 控制台的「通道」页选择 Discord，填写 Bot Token、Guild ID、Admin User ID 和允许的频道 ID 后连接。Bot 启动时会同步原生 Slash Commands：保留 `help`、`status`、`backend`、`config`、`skill`、`memory`、`knowledge`、`voice`、`updates`、`tokens`、`ledger` 等 CowCli 常用项目命令，并过滤无关历史命令。Grok 媒体入口统一为 `/grok-gen-image`、`/grok-gen-video`、`/grok-direct-gen-image` 和 `/grok-direct-gen-video`；四个命令的图片附件都是可选项，不上传就是文生图/文生视频，上传就是图生图/图生视频。图片质量可选 `speed` 或 `quality`，视频时长默认 `10s`，可选 `6s` 或 `10s`，分辨率可选 `480p` 或 `720p`。配置 `discord_guild_id` 时，`discord_prune_global_commands_on_startup=true` 会在启动同步前清理历史全局 Slash Commands，避免旧的 `codex-app`、`imagine`、`image-to-image` 等残留。

如果本机 Chrome 通过 Clash、V2Ray 等代理访问 Discord，而 CowAgent 后台显示 `Cannot connect to host discord.com:443`，请设置 `discord_proxy` 或环境变量 `DISCORD_PROXY`，例如 `http://127.0.0.1:7897`。Discord 现在会直接处理普通文本消息和图片附件，不再只靠原生 Slash Commands；仍然需要在 Discord Developer Portal 为 Bot 开启 Message Content Intent。

## Web 控制台

Web 控制台默认随服务启动，提供以下管理能力：

- 本地聊天和会话历史。
- 通道状态与微信扫码入口。
- Skills 启用、禁用和展示。
- 记忆、知识库和知识图谱浏览。
- 定时任务管理。
- 缓存、日志、后端状态、用量查看和管理员模型后端配置。
- 协议知识后端的上传、构建和文档导出。

本地运行建议：

```json
{
  "web_console": true,
  "web_host": "127.0.0.1",
  "web_port": 9899,
  "web_password": "LOCAL_PASSWORD"
}
```

如果把 `web_host` 设置为 `0.0.0.0`，请务必配置 `web_password`，并在防火墙或反向代理层限制访问来源。

## Agent、记忆与知识库

### Agent 模式

`agent=true` 时，CowWeCom 会使用 Agent 流程处理消息。Agent 可以：

- 自主规划多步任务。
- 调用本地工具、MCP 工具和 Skills。
- 在长任务中持续反馈进度。
- 长任务成功后可追加短完成回执；当兜底进度提醒实际出现两次或达到配置轮数时触发，方便手机端确认任务已结束。
- 在任务过长时做上下文压缩。
- 根据任务类型调整最大步数和推理强度。
- 普通文本回复中的远程酒店、OTA、搜索图片链接保留在正文中，不再自动拆成企业微信图片消息，避免远程 CDN 下载失败时向用户暴露 `image failed`。

聊天中的常用控制指令：

| 指令 | 作用 |
| --- | --- |
| `/q` 或 `/状态` | 查看当前长任务进度 |
| `/取消` | 请求取消当前正在执行的任务，保留排队消息 |
| `/跳过` | 清空当前会话的排队消息 |

### 长期记忆

记忆数据默认位于 `agent_workspace` 下，按用户、会话和群聊隔离。项目包含每日记忆整理能力，会把活跃会话沉淀为更稳定的用户记忆。

相关能力：

- 用户级记忆隔离。
- 群聊级共享记忆。
- 群成员入场信息记录。
- 每日深度整理和启动补偿。
- 记忆检索工具。
- Web 控制台记忆浏览。

### 本地知识库

普通知识库用于存放可复用 Markdown 知识，由 Agent 检索并按需注入上下文。

```json
{
  "knowledge": true
}
```

### 本地文档知识后端

本项目维护了面向协议、规范、设计文档、教材、代码规范和验证方法资料的本地文档知识后端。它支持上传 PDF、DOCX、TXT、Markdown，构建 SQLite 索引，生成可追溯的学习文档，并在回答中进行检索。协议/规范类问答默认优先使用本地脚本生成 deep evidence bundle：先命中检索片段，再展开相邻 chunk、source span、页码和 section，标记覆盖词与证据不足，尽量在本地完成证据整理而不额外调用 LLM。

默认本地文档知识库数据目录：

```text
public_protocol_knowledge/
```

视觉图表补全会记录 `pipeline_version`，视觉提取、裁剪或提示词版本变化时会自动清理旧视觉缓存，管理员也可通过 `visual/reset` 按文档或知识库手动重建。当前默认视觉管线为 `visual-pipeline-v2`；如果本机 ignored 的 `config.json` 仍显式设置 `visual_analysis.pipeline_version="visual-pipeline-v1"`，请改成 v2，或先调用 `visual/reset` 后再补全。视觉管线同时保留 page-level artifact 与 group-level artifact：跨页表格、大图、时序图会优先生成多页视觉 chunk，低置信结果只留分析记录不参与检索；高密度单页图表会按需高分辨率重试或 tile 分块合并，Web 进度会显示 artifact、group 和 tile 状态。旧公共协议 SQLite 如果已经有 PDF 图内乱码普通文本 chunk，可用 `scripts/repair_knowledge_text_chunks.py` 先 dry-run 检查，再用 `--apply` 备份并替换普通文本 chunk；脚本会保留高置信 `visual_analysis` chunk、视觉 artifact 表与映射，并重建 FTS。

可提交到仓库的公共协议/规范知识数据仍仅限：

- `public_protocol_knowledge/originals/`
- `public_protocol_knowledge/derived/`
- `public_protocol_knowledge/reports/`
- `public_protocol_knowledge/indexes/kb.sqlite`
- `public_protocol_knowledge/manifest.json`

个人知识、会话总结和自动生成的用户记忆不要放入 Git。

当前已提交的公共协议库包括 AMBA AXI v2.0、AXI4-Stream 和 UCIe 1.1；每个协议库都包含原始规范副本、可检索 SQLite 索引、manifest、报告以及必要的派生学习/分析文档。

## Skills 与工具

启动时，项目会把仓库内 `skills/` 下的内置技能同步到运行工作区：

```text
~/cow/skills/
```

当前项目重点使用和维护的能力包括：

| 类型 | 示例 |
| --- | --- |
| 图像/视频生成 | `image-generation` 默认跟随当前模型后端：普通 GPT 后端用户走 Codex/GPT 生图，已切到 Grok 的管理员/白名单用户走 Grok 生图；显式说 GPT/OpenAI/Codex 生图才在 Grok 后端下回到 GPT/Codex；`image-prompt-optimization` 统一保存 YouMind 库、Grok 模板和随机片段仓库，GPT/Codex 图片按默认筛选规则润色，Grok 图片/普通视频用 Grok 文本模型重写；`grok-video-generation` 通过已登录 Grok 账号后台生成 MP4 并回传视频；管理员可用 `/grok-direct` 直出图片/视频 |
| 企业微信能力 | `wecom-cli`，用于企业微信相关资料和操作辅助 |
| Git 与发布安全 | `github`、`safe-github-upload`、`code-update` |
| 项目运维 | `project-restart`，管理员说“重启/重启项目/重启服务”时默认触发，安全重启当前 CowWechat 服务 |
| 文档处理 | `docx`、`pptx`、`xlsx`、`pdf` |
| 检索与生活工具 | `reliable-search`、`quick-weather`、`fast-market-price` |
| 本地生活记账 | `china-expense-ledger`，本地记录用户主动提供的文字、截图视觉提取结果和支付宝/微信 CSV 账单；私聊清晰账单截图可自动记账并支持撤销，模糊账单会追问并学习，不自动抓取 App 账单 |
| 用量与额度 | `token-usage-tracker`、`codex-quota-query`、`capi-usage-monitor` |
| 工作进度与周报 | `work-progress-reporter`，私聊记录个人工作进度、临时任务和收获，并在周五生成中文周报；不同用户数据互相隔离 |
| 旅行与本地助手 | `travel-manager`、`amap-cowwechat`、`takeout-lite-recommender`、`shopping-lite-compare` |

Agent 可用的内置工具包括文件读写、编辑、目录查看、终端执行、定时任务、发送消息、网页搜索、网页抓取、浏览器、视觉识别、知识库查询、图像生成任务、Grok 视频生成任务、社交桥和 MCP。

图像生成说明：README 主图由本项目 `image-generation` Skill 使用 `codex_auth` 运行时生成，未使用上游 README 图片、社区入口或外部宣传素材。

## 聊天内 CowCli 权限

微信和企业微信会话里的 `/...` 与 `cow ...` 命令按风险分级：

- 普通用户可用：`/help`、`/status`、`/version`、本地账本查询、Skill 查询/搜索/用法、知识库统计/列表、后端状态和额度查询。
- 管理员可用：后端切换、配置修改、日志查看、浏览器依赖安装、Skill 安装/卸载/启用/禁用、知识库开关、记忆蒸馏和索引重建等会影响全局运行、隐私或本地文件状态的动作。
- `/help` 会根据当前用户角色只显示可用命令；新增 CowCli 命令如果没有显式归类，默认按管理员命令处理。

## 常用命令

```powershell
# 启动
.\.venv\Scripts\cow.exe start

# 查看状态
.\.venv\Scripts\cow.exe status

# 查看最近日志
.\.venv\Scripts\cow.exe logs

# 跟随日志
.\.venv\Scripts\cow.exe logs -f

# 重启
.\.venv\Scripts\cow.exe restart

# 通过 Agent 自然语言重启
# 管理员直接说“重启”或“重启项目”会触发 project-restart Skill

# 停止
.\.venv\Scripts\cow.exe stop

# 查看知识库概览
.\.venv\Scripts\cow.exe knowledge

# 安装浏览器工具依赖
.\.venv\Scripts\cow.exe install-browser
```

如果当前 shell 没有 `cow` 可执行文件，也可以使用：

```powershell
.\.venv\Scripts\python.exe -m cli.cli status
```

## 项目结构

```text
CowWeCom/
├─ agent/                      Agent、工具、记忆、知识库、Skills 加载
├─ bridge/                     Chat 与 Agent 的桥接层
├─ channel/
│  ├─ weixin/                  个人微信通道
│  ├─ wecom_bot/               企业微信智能机器人通道
│  ├─ wechatcom/               企业微信自建应用通道
│  ├─ discord/                 Discord 通道
│  └─ web/                     Web 控制台
├─ cli/                        cow 命令行
├─ common/                     配置、日志、后端路由、用量、运行时工具
├─ docs/                       项目文档与图片资源
├─ models/                     模型后端适配
├─ plugins/                    传统插件系统
├─ public_protocol_knowledge/  默认本地协议知识库数据目录，可提交的公共协议知识库
├─ skills/                     项目内置 Skills
├─ tests/                      单元测试和回归测试
├─ config-template.json        安全配置模板
└─ app.py                      主入口
```

## 更新日志

这里记录本仓库当前维护方向的核心变化。详细提交、验证命令和回滚线索请看 `GIT_NOTES.md`；README 只保留面向使用者和部署者的摘要。

### 2026-05-31

- Web 知识库后台会轮询 `/api/knowledge/admin/visual/status`，即使视觉补全是 Codex 在本机后台脚本里跑，也会在知识库页显示准备页数、已发现图表、剩余分析项、合并组和实际视觉后端。
- 本地文档导出修复单文档导出重写全局索引时产生缺失链接的问题；索引只引用已经写出的 Markdown 页面，旧协议库不会再出现“表里有但页面链接打不开”的状态。
- 视觉运行记录按源文档真实 `kb_id` 归档，并在状态接口返回最近一次运行后端，便于区分 UCIe、AXI4-Stream 等公共协议知识库的 Codex 补图进展。

### 2026-05-30

- A2E 自动签到统一使用内置 PowerShell helper `scripts\a2e_checkin.ps1`，同步仓库与运行时 Skill 副本；签到成功后会按 A2E 返回的下一次可领取时间自动刷新对应账号的 CowAgent scheduler 任务时间。
- Web 控制台自定义后端新增“测试连接”流程：填写后端 ID、模型、API Base、Key 与 Chat Completions/Responses 协议后必须先测试成功，才允许保存或加入后端列表；自定义 OpenAI-compatible 后端的直填 key/base 会优先于同名环境变量，`config.json` 带 UTF-8 BOM 时 Web 保存也能正常处理。
- Web 控制台 Grok 手动登录输入框明确支持 Callback URL / 授权码；提交授权码后如果后端登录态已经完成，会复查状态并显示“登录完成”，避免实际成功但前台误报失败。
- Grok 受限后端默认 `reasoning_effort` 调整为 `xhigh`，Agent 工具调用和普通 Grok Chat 都会把该默认思考深度传给支持 reasoning 的 Grok 模型；Grok 请求仍不会携带非 xAI 的 `thinking` 字段。
- ????????????????Web/API ????? `kb_id`????????????????? `kb_id` ??? SQLite??????????? limit ????
- ???????????? `--rebuild-text-chunks`???? PDF ?? ordinary chunks?source spans?entities?relations???????? `visual_analysis` chunks?????????????????? readiness validation?visual rows ? 0 ???????
- ??????????? `public_protocol_knowledge/`???????????? `knowledge_backend.visual_analysis`?Web ?????? `kb_id` ???
- Grok 图片/视频隐藏提示词仓库从 `grokSfw` 更名为 `grok`，并内置 YetAnotherWildcardCollection 的完整 wildcard/prompt `.txt` 快照。
- 普通 Grok 生图/视频润色默认 90% 使用 `grok` 仓库、10% 使用其他仓库；prompt 含 `NSFW`/`nsfw` 时把该词作为内部选择信号移除，优先使用 `grok/NSFW`，并只混入 1 条背景、风格、色彩、材质等安全上下文补充片段。
- “随机…提示词”现在固定走提示词文本路径，不会误触发生图/图生图；未明确 `文生图` 时默认生成图生图提示词，仓库片段筛选由本地脚本完成，Grok 只做最终润色和删除错误外貌/表情/画质词。
- prompt 指明 `Korean`、`Korea`、`韩国` 等国籍/族裔时会生成稳定人物约束，过滤会引入冲突身份外貌的随机片段，避免“韩国人”被补成偏欧美特征。
- 图生图和图生视频在润色与 direct 直出路径都会追加参考图身份锁；有参考图时不再额外补入国籍、眼睛颜色、发色、年龄、体型或面部特征描述，除非用户明确要求修改。
- 图片后台任务恢复逻辑把 `delivery_failed` 当作终态处理，服务重启后只恢复真正未完成的 `queued/running` 任务，避免已完成或已投递失败的图片在启动恢复时重复发送；图片发送成功后会在 5 分钟后自动清理本地生成图和本次任务使用的工作区内参考图。
- 查看“刚才润色后的提示词”会读取最近一次已存储的最终 prompt，不重新润色；默认以中文展示，Grok 图片/视频记录优先用 Grok 翻译，需要原文时可显式要求原文；直出和未产生 rewrite metadata 的 Grok 图片/视频 prompt 也会记录到同一套历史里；下游审核/API 失败后也会尽量保留已润色 prompt 供排查。
- 管理员或白名单用户把个人后端切到 Grok 后，再通过 `/backend codex`、自然语言“switch backend to codex”或 Web 控制台切回 Codex，会更新同一个个人后端 override，不再被之前的 Grok override 卡住；Web 控制台也会按实际 actor 后端回填 Codex/Grok 选择。
- 生图脚本修复 `Invalid JSON` 入参解析：后台任务传入的合法 JSON 会先原样解析，prompt 内包含弯引号时不再被误替换破坏；手工 CLI 使用弯引号包 JSON 字段时仍保留兼容恢复。
- Discord Grok 图片/视频 Slash Commands 的附件语义固定为：不上传附件就是文生图/文生视频，上传附件才进入图生图/图生视频；视频默认时长改为 `10s`；direct 图片/视频不再绕 CowCli 的最近图片启发式；Discord 普通消息和微信个人号现在按 Grok 后端识别文生图、图生图、文生视频、图生视频，视频意图优先于图片意图。
- 本地知识库旧协议视觉补全支持 Codex 多图跨页 group merge；Codex 多图失败会依次降级到 Codex 文本合并和 deterministic fallback，并在 Web 进度、结果 JSON 与 group chunk metadata 中显示 `group_merge_strategy` 和 fallback 原因；Web“补全图表/视觉知识”改为逐文档、`limit=1` 的 `/visual/build` 增量续跑，不再一次性调用长耗时 `/visual/complete`，并新增静态回归覆盖按钮入口、请求体、source document 队列、generated document 排除与 group merge 进度输出。
- 私聊图片自动记账扩展到微信转账账单截图和更口语化的美团外卖账单/订单截图；转账类截图会先追问消费、退款、收入或个人转账，并支持直接回复“消费/退款/收入/个人转账”完成确认；企业微信图片下载遇到临时超时会快速重试。

### 2026-05-29

- Web 控制台重新收拢模型后端管理：普通厂商仍在模型配置区保存；新增/保存自定义 OpenAI-compatible 后端 profile 时可选择 Chat Completions 或 Responses 协议、填写 key/base/env/timeout，并可显式加入自动切换候选；自定义后端不会再继承 CAPI/OpenAI 全局凭据。
- Grok 登录从旧 `/grok` 灰度页迁入 Web 控制台模型配置页，支持多个命名 Grok OAuth 账号、当前账号切换、轮询/manual callback、凭据检查和指定账号退出；`/grok` 旧入口会回到控制台。
- Grok 继续作为管理员/白名单可用的受限后端，普通用户仍共用全局 GPT 后端池；“当前后端”和额度状态按发起人的有效后端展示，Grok 不参与全局自动切换，CAPI 月卡低额和运行时失败可落到已显式加入的自定义 fallback 后端。
- Grok 图片/视频能力补齐：支持单图图生图、显式 `/grok-direct image|video` 直出、视频 720p/10s 等参数解析、同会话最近图片参考、后台并行任务、代理统一走 `grok_proxy`；新增 `image-prompt-optimization` Skill 集中保存 YouMind 库、Grok 模板和 Grok 随机片段仓库，普通 Grok 生图/视频默认用 Grok 文本模型重写提示词，不再复用 GPT 或 YouMind 本地库，并避免重启后重复发送已终态失败的视频完成通知。
- Discord 和多通道运行继续加固：Discord 支持普通文本/图片附件进入正常聊天流，Slash Commands 收敛为项目命令与 Grok 媒体入口，多通道同名启动和 Web 重复保存会在核心生命周期层去重。
- Agent 上下文、识图和知识触发更稳：进度快照和短确认不会误触发协议知识问答，私聊图片识别默认只缓存非账单信息，账单自动记账仍保留回执。

### 2026-05-28

- Grok/xAI 原生账号能力继续扩展：早期 OAuth 登录、文字对话、TTS、图片生成和视频生成复用同一登录态；PR 5 补齐 [docs/grok.md](docs/grok.md)、Hermes auth 只读导入、Web 状态脱敏和核心回归测试；后续登录入口已迁入 Web 控制台。
- Grok/xAI 图片/视频生成加固：xAI 返回 URL 只允许公开 HTTPS 下载，逐跳校验 redirect、DNS、Content-Type 和大小上限，生成文件统一落到 `tmp/grok_media/`，发送成功、失败或 fallback 后清理本次生成文件。
- 图像生成在 2026-05-28 新增隐藏式提示词增强：内置 YouMind 全量 Nano Banana Pro 提示词库，GPT/Codex 按海报、人物、产品、流程图等用途检索润色；Grok 已在 2026-05-29 改为单独模型重写分支，普通回复不展示隐藏提示词，用户明确要求时可查看最近一次生成提示词。
- Grok/xAI 视频生成接入 PR 4：新增文生视频、单图生视频、多图参考视频、`VIDEO_CREATE` 前缀和 `grok-video-generation` 后台 Skill；WeCom Bot 现在优先识别 `video_create_prefix`，并会在刚发图片后直接“生成视频 ...”时自动带入最近参考图。
- Grok/xAI 企业微信语音回复改为“双模式”规则：低延迟 low 模式与语音会话模式分开说明；语音会话模式允许企业微信应用/WeCom Bot 中语音输入优先语音回复，文字输入和个人微信仍不新增语音发送。
- Agent 和本地工具体验继续收口：同轮重复工具调用结果改成短引用，本机 token 用量查询支持用户别名合并，语音模式支持 `/voice on|off` 热切换，账单截图识别优先保留截图中的精确金额，账单补充说明不再误走本地查询/额度查询快路径。
- 本地文档与公共协议知识库继续加固：视觉 chunk/source span、PDF caption、公式/大表候选、prepare checkpoint 和旧 ordinary chunk 去污染闭环收紧；UCIe、AMBA AXI、AXI4-Stream 公共知识库 SQLite 已刷新，Web Markdown 库拉取后需重新导出。

### 2026-05-27

- 本地文档知识库从“协议资料”泛化为通用文档知识库，默认数据目录调整为 `public_document_knowledge/`，上传 PDF、DOCX、TXT、Markdown 后可导出到 `knowledge/documents/<kb_id>/`。
- 图表/视觉知识补全改为页级增量准备和 artifact 级断点续跑，支持选择视觉分析后端、手动 reset、低置信结果隔离、Web 进度展示和 fake analyzer 单测注入。
- 跨页图表进入 group-level 分析并继续加固：`index_low_confidence` 不再放行低置信 page/group 入检索，member retry/force 会让旧 group chunk 失效，显式空 `source_pages`、实际多页但 parts 不足、陈旧 membership 都会被拦截。
- 高密度图表处理继续加固：小字图表高分辨率重试会使用独立长边上限，超大单页图表支持 tile 分块；force 重跑会重算 tile，tile 复用会校验模型、提示词和 image hash，任一 tile 低置信都会阻止整页入库。
- PDF 文本清洗、caption 识别和导出链路继续收紧：过滤图内噪声，避免正文引用误判为图表，导出和 deep query 不再重复展示视觉 chunk；旧索引维护脚本支持 dry-run 报告、严格按 `<data_dir>/indexes/kb.sqlite` 推断数据目录、保留视觉结果、修复共享/孤儿/跨文档 source span 冲突，并在修复或清理旧版本视觉 chunk 后同步清理失效图谱引用。
- 修复每日记忆文件缺失误报、后台任务异常无提示、安全上传预检误拦源码目录等运行体验问题；CAPI 流式读上游中断时会重试并切换可用后端继续执行，同时在企微/Web 流式回复中明确提示后端切换。

### 2026-05-26

- 知识库问答新增本地 deep evidence bundle、邻近 chunk 展开、表格证据块和缺证状态，并发布 AMBA AXI v2.0 公共协议知识库。
- 知识类任务统一提高推理预算，个人知识写入增加来源分层和证据守卫，避免把 AI 推导结论误沉淀为用户知识。
- CowCli、Web 管理、微信/企微权限和 `/help` 做了角色化收敛，普通用户与管理员可见能力更清晰。
- 本地账本按当前用户查询，短句账单查询进入本地快路径，账单澄清、撤销和重复记录保护更稳。
- Codex 额度查询改为官方 app-server 直连，token 使用统计按北京时间自然日/月计算，社交转述上下文也做了修正。

### 2026-05-25

- README 与项目规则重写，明确代码、运行行为和配置变化需要同步文档。
- 旅行规划增加出发前确认、复杂规划预算和住宿判断，减少直接生成不完整行程。
- 企业微信长任务分段回复、完成回执、远程图链处理和订阅重连更稳。
- CAPI/Codex 后端接力、额度路由、自然语言后端切换和自我进化缓存继续加固。

### 2026-05-24

- 图像生成后台任务支持服务重启后的恢复、失败通知和发送结果处理。
- 定时任务服务启动时自动恢复，并支持错过运行、失败通知、立即运行和跳过待执行任务。
- 企业微信群聊按 `chatid` 隔离记忆并记录群成员上下文。
- LLM 后端路由、额度查询、自动切换、推理强度策略和审计数据继续增强。

### 2026-05-23

- 企业微信智能机器人配置改为优先手动填写 Bot ID/Secret，并补充扫码创建的权限说明。
- 同步多种本地 Skills，覆盖安全上传、GitHub、图像生成、企业微信 CLI、文档处理、搜索、天气、行情和旅行等能力。

### 2026-05-22

- 加入微信多实例、真实微信 ID 映射、跨用户社交桥和主动发送能力。
- 强化多用户记忆隔离、普通用户访问边界和本地 Skill/工具集成。

### 2026-05-21

- 建立 Windows 本地微信与 DeepSeek 部署基线。
- 加入浏览器、视觉、Responses API 和多模态相关适配。
## 开发与验证

本仓库要求代码与文档一起前进。修改项目代码、运行行为、通道、配置、Skills、安全策略、部署流程或用户可见能力时，需要同步更新根目录 `README.md`。更新日志只保留按日期合并的核心摘要；详细开发记录、验证命令和回滚线索请写入 `GIT_NOTES.md`。给后续 Codex 的强制 README 维护规则见 `AGENTS.md`。

常用验证命令：

```powershell
# 安装常用验证依赖
.\.venv\Scripts\python.exe -m pip install -r requirements-optional.txt

# Python 语法检查示例
.\.venv\Scripts\python.exe -m py_compile app.py config.py

# 重点通道回归
.\.venv\Scripts\python.exe -m pytest tests/test_wecom_social_bridge.py tests/test_multi_weixin_instances.py -q

# Web 控制台脚本检查
node --check channel\web\static\js\console.js
```

提交前建议运行安全预检：

```powershell
$root = git rev-parse --show-toplevel
$env:PYTHONUTF8='1'
py -3 "$root\skills\safe-github-upload\scripts\preflight.py" --root $root
```

## 安全说明

- 不要提交 `config.json`、`.env`、真实密钥、登录凭证、日志、运行数据库或本地工作区文件。
- `agent_workspace` 下包含用户记忆、文件、Skills 配置和运行状态，默认不应进入 Git。
- `memory/`、`data/project-optimizer/`、`tmp/`、`workspace/` 均属于本地隐私/运行证据范围；临时脚本快照和原始模型输入只用于本机优化分析，不允许上传到 GitHub。
- 用户记忆必须继续按 `memory/users/<memory_user_id>/` 隔离；优化报告只能使用哈希、计数和脱敏摘要，不能跨用户展示记忆原文。
- Web 控制台对外暴露时必须配置 `web_password`，并配合防火墙或反向代理访问控制。
- Agent 具备读写本地文件、执行命令和调用外部服务的能力。请谨慎设置管理员用户、普通用户读写根目录和敏感路径。
- 企业微信和微信账号的使用应遵守平台规则、企业制度和所在地法律法规。
- 公共协议知识库只提交可公开分享的规范、协议、报告和索引，不提交个人聊天知识或隐私内容。

## 项目来源与许可证

本项目基于 [zhayujie/CowAgent](https://github.com/zhayujie/CowAgent) 修改和扩展。感谢原项目提供的 Agent 框架、通道架构和工具体系。

本仓库继续遵循 [MIT License](./LICENSE)。使用、部署或二次开发时，请同时遵守相关模型服务、微信、企业微信以及运行环境的使用规则。
