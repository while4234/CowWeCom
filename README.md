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
| `agent_admin_users` | 管理员用户 actor id 列表 |
| `agent_user_profiles` | 用户角色、展示名、记忆 ID 等覆盖配置 |
| `knowledge` | 是否启用本地知识库 |
| `knowledge_backend` | 协议/规范公共知识库后端配置 |
| `skill` | Skills 的运行时配置，例如图像生成 Codex auth |
| `llm_backend` | Codex/CAPI 等后端路由与自动切换配置 |
| `project_optimizer_*` | 本地优化证据记录、原始输入缓存消费和临时脚本快照配置；默认写入 `agent_workspace/data/project-optimizer/`，不得进入 Git |
| `reasoning_effort_policy_runtime_auto_optimize_enabled` | 旧的 Agent 内后台思考深度自动调优开关；默认关闭，主力机器改用 Codex 每日 0 点 automation 先检查 300 次增量模型调用再运行项目优化 |

## 微信接入

个人微信通道使用：

```json
{
  "channel_type": "weixin",
  "web_console": true,
  "agent": true
}
```

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

### 协议/规范公共知识后端

本项目维护了面向协议、规范、设计文档的公共知识后端。它支持上传 PDF、DOCX、TXT、Markdown，构建 SQLite 索引，生成可追溯的学习文档，并在回答中进行检索。

默认公共数据目录：

```text
public_protocol_knowledge/
```

可提交到仓库的公共知识数据仅限：

- `public_protocol_knowledge/originals/`
- `public_protocol_knowledge/derived/`
- `public_protocol_knowledge/reports/`
- `public_protocol_knowledge/indexes/kb.sqlite`
- `public_protocol_knowledge/manifest.json`

个人知识、会话总结和自动生成的用户记忆不要放入 Git。

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
├─ public_protocol_knowledge/  可提交的公共协议知识库
├─ skills/                     项目内置 Skills
├─ tests/                      单元测试和回归测试
├─ config-template.json        安全配置模板
└─ app.py                      主入口
```

## 更新日志

这里记录的是本仓库当前维护方向的更新，不再保留上游 CowAgent 的版本发布日志。

| 日期 | 更新 |
| --- | --- |
| 2026-05-26 | 调整项目优化触发方式：关闭旧的 CowAgent 运行时按阈值自动调优，新增 `query_incremental_calls.py` 快速统计本地 `llm_cache_usage.jsonl` 增量调用数；本机 Codex automation 每天 0 点先判断是否新增 300 次模型调用，满足后再运行 `cowwechat-project-optimizer` 并删除已消费原始输入缓存 |
| 2026-05-26 | 新增 `cowwechat-project-optimizer` Skill 和本地优化证据记录：Agent 会把任务开始/结束、模型请求形状、最终 provider payload 形状、工具步骤摘要、临时脚本快照写入 `agent_workspace/data/project-optimizer/`；原始用户/模型输入只保存在本机 ignored raw cache，优化 skill 成功生成脱敏报告后再删除已消费 raw cache；同时加固 safe GitHub preflight，禁止上传临时脚本归档、用户记忆和优化证据 |
| 2026-05-26 | 新增 `cowagent-workflow-auditor` Skill：用于脱敏审计 CowAgent/CowWechat 运行日志、工具调用链路、`tmp`/`workspace` 临时产物、已安装 Skills/Plugins，识别可沉淀为 Skill 的重复工作流；同时扩展 `github` Skill，内置仓库列表和近期更新查询，减少反复创建 GitHub 临时包装脚本 |
| 2026-05-26 | 增强 `china-expense-ledger` 私聊账单截图流程：清晰账单默认自动记账并提示可撤销，模糊账单只追问缺失的 App/平台、分类、商品或方向；用户回答后会按用户学习相同截图 UI、商品和商户规则；群聊仍不自动记账，菜单/价目表等非账单图片不会误入账；新增日/周/月/上月 per-user 汇总缓存，便于快速回答今天、本周、本月和上月消费 |
| 2026-05-26 | 新增 `china-expense-ledger` 本地记账 Skill：支持自然语言记账、截图经 Agent 视觉提取后的结构化入库、支付宝/微信 CSV 导入、SQLite 本地学习纠错和分类汇总；明确禁止爬虫、逆向、绕过登录、自动抓取 App 账单或默认启用官方支付/电商接口 |
| 2026-05-26 | 统一本地验证环境到 `.venv`：补齐当前 `.venv` 的 `PyYAML` 和 `pytest`，并在可选依赖中声明 `pytest`，后续 skill 校验和 focused pytest 默认使用 `.venv` |
| 2026-05-26 | 新增 `work-progress-reporter` Skill：所有用户都可在私聊中独立管理本周/下周工作计划、每日进度、临时任务、收获和周末加班安排；真实状态写入各自 `memory_user_id` 私有目录，群聊触发只做隐私引导，周五可生成中文 Markdown 周报 |
| 2026-05-26 | 修复 LLM backend status 的当前后端额度展示：CAPI 额度卡、CAPI 月卡和 Codex 的手动额度查询都会回写统一的 `backend_quota` 状态缓存，`/backend status` 始终展示当前后端已记录的最新值；默认每 50 次非静默用户模型调用会在后台刷新当前后端额度 |
| 2026-05-26 | 新增 `project-restart` Skill：管理员对 Agent 说“重启/重启项目/重启服务”时默认走该技能，先派生延迟 worker 再复用 `cli.cli restart` 关闭当前项目所有匹配 `app.py` 的进程并拉起新服务，避免运行中的 Agent 直接杀掉自己的工具子进程 |
| 2026-05-26 | 修复 CAPI 月卡日重置后的后端状态展示：午夜自动切回 `capi_monthly` 时会同步重置月卡状态缓存，避免 `/backend status` 把今天的 `daily_monthly_card_reset` 和昨天的 `monthly_quota_low`/`remaining=0` 拼在一起；本次也用真实月卡查询确认服务端剩余额度已恢复 |
| 2026-05-25 | 优化自我进化与模型思考深度策略：简单 medium 请求默认不再注入动态 self-evolution 规则，减少提示前缀变化以提升缓存命中；reasoning-effort 优化器只有在有成功、无工具错误且一轮完成的 outcome 证据时，才允许把不确定任务学习为 `medium` |
| 2026-05-25 | 修复企业微信长任务结果过长导致客户端看不到或截断：WeCom Bot 对超长 stream 终包和主动 markdown 文本按安全长度分段发送，首段保持当前消息回复，后续段自动补发为同会话消息 |
| 2026-05-25 | 继续修复企业微信里“帮我总结下今天更新的功能适合推送给我老婆使用的有哪些”这类问题：现在先读取本项目当天 Git/README 更新记录，再交给模型按用户原话里的推荐对象筛选，不再用本地模板泛化成“日常使用者” |
| 2026-05-25 | 修复自然语言切换后端到 CAPI 额度卡：`帮我切换后端到capi额度卡`、`切换到额度卡后端` 现在优先识别为后端切换到 `capi`，不再因为包含“额度卡”而误走 CAPI 额度查询路径 |
| 2026-05-25 | 加固企业微信智能机器人长连接：当 WebSocket 已连接但订阅 ACK 因节点维护或链路抖动迟迟不返回时，自动关闭半连接并进入既有重连流程，避免企业微信消息长时间无回复；本次也验证 Bot ID/Secret 可正常订阅，国内直连与当前代理链路均可拿到 `errcode=0` |
| 2026-05-25 | 修复长任务结束体验：兜底进度提醒出现两次后，成功完成会追加短完成回执；文本中的远程酒店/OTA 图片链接不再自动拆发为企业微信图片，避免 `image failed` 干扰最终回复 |
| 2026-05-25 | 加固 CAPI 运行时接力：月卡和额度卡遇到 `ConnectError`、DNS、远端断连、读超时、流中断等网络错误时，会重试后切换到 Codex 并重放当前 Agent 请求，避免直接落成错误回复 |
| 2026-05-25 | 新增项目规则：后续任何代码、通道、配置、Skills、安全策略或用户可见能力变更都必须同步更新根目录 README；拉取远端后若发现代码已更新但 README 未跟进，需要自动补写后再提交 |
| 2026-05-25 | 重写 README，聚焦 CowWeCom 当前项目范围；删除上游宣传素材和未验证通道展开说明；新增由本项目图像生成 Skill 生成的主视觉 |
| 2026-05-25 | 补充远端最新代码更新：旅行规划新增前置“规划前确认” gate，信息不足时先询问关键问题并停止工具调用；信息完整的旅行需求继续进入复杂规划 Agent 流程 |
| 2026-05-25 | 补充远端最新代码更新：CAPI 月卡额度耗尽时先查询 Codex 额度并自动选择可用后端；普通旅行规划提示会进入复杂规划预算；FlyAI Skill 示例改为运行时可用的 wrapper 路径 |
| 2026-05-25 | 优化旅行规划技能，夜间和过夜行程默认提示酒店/住宿安排；改进长任务规划反馈；补充 CowWeCom 远端识别规则 |
| 2026-05-24 | 修复图像生成后台任务在服务重启后的恢复与失败通知，避免用户只看到任务沉默；完善图片发送结果和恢复重试 |
| 2026-05-24 | 启动时自动恢复定时任务服务，支持错过运行通知、失败通知、`run_now` 和 `skip_pending` 等调度管理动作 |
| 2026-05-24 | 强化企业微信群聊记忆隔离：私聊使用用户维度，群聊使用 `chatid` 维度，并记录群成员称呼和上下文 |
| 2026-05-24 | 增强 LLM 后端路由、Codex/CAPI 额度查询、自动切换和推理强度策略；补充用量、缓存和策略审计数据 |
| 2026-05-23 | 企业微信智能机器人配置改为优先手动填写 Bot ID/Secret，降低扫码创建的权限误解；补充 `wecom_bot_auth_source` |
| 2026-05-23 | 增加和同步多种本地 Skills，包含安全上传、GitHub、图像生成、企业微信 CLI、文档处理、搜索、天气、行情、旅行等能力 |
| 2026-05-22 | 加入微信多实例、真实微信 ID 映射、跨用户社交桥、主动发送、记忆隔离和普通用户访问边界保护 |
| 2026-05-21 | 建立 Windows 本地微信与 DeepSeek 部署基线，加入浏览器、视觉、Responses API 和多模态相关适配 |

## 开发与验证

本仓库要求代码与文档一起前进。凡是修改项目代码、运行行为、通道、配置、Skills、安全策略、部署流程或用户可见能力，都需要同步更新根目录 `README.md`。如果拉取或 rebase 时发现远端已有代码更新但 README 没有跟进，应在本地自动补写 README 后再提交和推送。

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
