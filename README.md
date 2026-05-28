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
| 图像生成 | 使用本项目 `image-generation` Skill，经 Codex auth 调用图像生成工具，支持后台任务和结果回传 |
| 后端路由 | Codex、OpenAI-compatible/CAPI 等后端路由，支持额度查询、自动切换和推理强度策略 |
| 安全隔离 | 管理员/普通用户角色、普通用户文件访问边界、敏感路径保护、Web 管理接口认证 |

## 支持范围

当前 README 明确覆盖以下通道：

| 通道 | `channel_type` | 状态 | 说明 |
| --- | --- | --- | --- |
| 个人微信 | `weixin` 或 `weixin_*` | 重点维护 | 扫码登录，支持命名实例和多用户隔离 |
| 企业微信智能机器人 | `wecom_bot` | 默认推荐 | 使用 Bot ID 和 Secret 走长连接，适合企业微信单聊和群聊 |
| 企业微信自建应用 | `wechatcom_app` | 可用 | 需要公网回调 URL、企业可信 IP 和企业微信后台配置 |
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
| `agent_user_profiles` | 用户角色、展示名、记忆 ID 等覆盖配置；扫码选择的管理员/普通用户身份会写入这里 |
| `external_reply_inject_to_agent_context` | 是否把 CowCli 等非 Agent 快答的可见问答同步进后续 Agent 会话上下文，默认开启，便于“把这个转述给她”这类跟进指令引用最新回复 |
| `knowledge` | 是否启用本地知识库 |
| `knowledge_backend` | 本地文档知识库与公共协议/规范知识后端配置 |
| `agent_knowledge_max_steps` | 需要查询公共/个人知识库、上传文档、协议原文或 `knowledge_query/deep_query` 的问答步数预算；默认 40，并锁定高质量推理 |
| `skill` | Skills 的运行时配置，例如图像生成 Codex auth |
| `llm_backend` | Codex/CAPI 等后端路由与自动切换配置 |
| `project_optimizer_*` | 本地优化证据记录、原始输入缓存消费和临时脚本快照配置；默认写入 `agent_workspace/data/project-optimizer/`，不得进入 Git |
| `reasoning_effort_policy_runtime_auto_optimize_enabled` | 旧的 Agent 内后台思考深度自动调优开关；默认关闭，主力机器改用 Codex 每日 0 点 automation 先检查 300 次增量模型调用再运行项目优化 |

CAPI 额度卡/月卡查询依赖 `llm_backend.providers.capi` 和 `llm_backend.providers.capi_monthly` 下的专用 key。更新部署后请检查本机 `CAPI_API_KEY`、`CAPI_MONTHLY_API_KEY`，或在 ignored 的 `config.json` 中配置 `llm_backend.providers.capi.api_key`、`llm_backend.providers.capi_monthly.api_key`；不要把真实 key 写入 `config-template.json` 或提交到 Git。

Grok/xAI 目前作为灰度能力接入：管理员可访问 `/grok` 完成原生账号 OAuth 登录，但普通配置页默认不展示 Grok，也不会因为登录 token 改变当前真实聊天后端。需要灰度切换时再手动设置 `bot_type` 为 `grok` 或 `xai`；如需在普通模型配置面板展示 Grok，可显式开启 `grok_gray_enabled=true`。OAuth token 默认写入 `data/auth/grok_auth.json`，也可以用 `grok_auth_file` 指定；`grok_api_key` 和 `XAI_API_KEY` 只作为未登录时的 fallback。Web 状态接口只返回登录状态、邮箱、过期时间等安全字段，不返回 access token、refresh token 或 callback code。

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

## Web 控制台

Web 控制台默认随服务启动，提供以下管理能力：

- 本地聊天和会话历史。
- 通道状态与微信扫码入口。
- Skills 启用、禁用和展示。
- 记忆、知识库和知识图谱浏览。
- 定时任务管理。
- 缓存、日志、后端状态和用量查看。
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
public_document_knowledge/
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
| 图像生成 | `image-generation`，通过 Codex auth 后台生成并回传图片 |
| 企业微信能力 | `wecom-cli`，用于企业微信相关资料和操作辅助 |
| Git 与发布安全 | `github`、`safe-github-upload`、`code-update` |
| 项目运维 | `project-restart`，管理员说“重启/重启项目/重启服务”时默认触发，安全重启当前 CowWechat 服务 |
| 文档处理 | `docx`、`pptx`、`xlsx`、`pdf` |
| 检索与生活工具 | `reliable-search`、`quick-weather`、`fast-market-price` |
| 本地生活记账 | `china-expense-ledger`，本地记录用户主动提供的文字、截图视觉提取结果和支付宝/微信 CSV 账单；私聊清晰账单截图可自动记账并支持撤销，模糊账单会追问并学习，不自动抓取 App 账单 |
| 用量与额度 | `token-usage-tracker`、`codex-quota-query`、`capi-usage-monitor` |
| 工作进度与周报 | `work-progress-reporter`，私聊记录个人工作进度、临时任务和收获，并在周五生成中文周报；不同用户数据互相隔离 |
| 旅行与本地助手 | `travel-manager`、`amap-cowwechat`、`takeout-lite-recommender`、`shopping-lite-compare` |

Agent 可用的内置工具包括文件读写、编辑、目录查看、终端执行、定时任务、发送消息、网页搜索、网页抓取、浏览器、视觉识别、知识库查询、图像生成任务、社交桥和 MCP。

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
│  └─ web/                     Web 控制台
├─ cli/                        cow 命令行
├─ common/                     配置、日志、后端路由、用量、运行时工具
├─ docs/                       项目文档与图片资源
├─ models/                     模型后端适配
├─ plugins/                    传统插件系统
├─ public_document_knowledge/  默认本地文档知识库数据目录
├─ public_protocol_knowledge/  可提交的公共协议知识库
├─ skills/                     项目内置 Skills
├─ tests/                      单元测试和回归测试
├─ config-template.json        安全配置模板
└─ app.py                      主入口
```

## 更新日志

这里记录本仓库当前维护方向的核心变化。详细提交、验证命令和回滚线索请看 `GIT_NOTES.md`；README 只保留面向使用者和部署者的摘要。

### 2026-05-28

- 新增 Grok/xAI 原生账号 OAuth 登录与文字对话灰度接入：管理员可通过隐藏 `/grok` 页面登录账号并检查凭据，无法连接 loopback 时可粘贴 Grok Build 显示的授权码；普通配置页默认不展示 Grok，不会影响当前真实后端，只有手动设置 `bot_type=grok`/`xai` 或开启 `grok_gray_enabled` 后才进入灰度切换。
- Windows Python 3.13 可选语音依赖补充 `audioop-lts`，修复 `pydub` 因标准库 `audioop` 移除而无法加载的问题，语音转换能力在重启后可正常初始化。
- Agent 同轮重复工具调用结果进一步压缩：相同参数的重复 read/bash/edit 等工具仍保留首次完整结果，重复结果改为短引用，减少上下文膨胀和缓存扰动。
- KnowledgeStorage 视觉 chunk/source span 完整性继续收口：视觉结果追加、删除和 reset 避免覆盖普通 chunk 与共享 span，并清理无人引用的图谱证据引用。
- 本地文档视觉补全链路继续加固：AMBA/ARM 常见无点号 Figure/Table caption 可被重新发现，默认使用当前后端视觉模型；Web 未选中文档时可一键补全全部 source documents，并新增脚本/API 级完整补全入口。
- 已刷新公共协议知识库 SQLite：UCIe 1.1、AMBA AXI v2.0 和 AXI4-Stream 的 PDF 普通文本 chunk 已用当前 sanitizer 重建，Figure/Table 周边图内信号、时序和表格碎片乱码抽检清零，三个协议验证报告均通过；远端拉取后实际检索使用随仓库更新的 `public_protocol_knowledge/indexes/kb.sqlite`，网页文档库需重新导出到 `knowledge/documents/<kb_id>/`，并清理旧 `~/cow/knowledge/protocols/` 残留，避免误点旧 Markdown。

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
