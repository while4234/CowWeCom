# Git Notes

## Repository Purpose

Personal CowWechat deployment and development workspace for Weixin bot integration, OpenAI-compatible model providers, image generation tooling, scheduler tools, and agent memory features.

## Ignore And Secret Policy

Runtime secrets and local state stay out of Git. The repository ignores `config.json`, env files, key/certificate files, Weixin credentials, cookies, tokens, session files, logs, virtual environments, generated build output, `.codex/`, `.playwright-mcp/`, and other local workspace artifacts.

Do not commit API keys, QR login material, credential JSON files, chat logs, cookies, tokens, or local auth material. Use committed templates and documentation only for placeholders and safe defaults.

## Current Baseline

- Latest code commit: `3d39398` `feat: add latency telemetry for weixin replies`
- GitHub upload target: private repository `while4234/CowWechat`
- Remote layout: `origin` points to `https://github.com/while4234/CowWechat.git`; `upstream` points to the original `https://github.com/zhayujie/CowAgent.git` and has push disabled.
- Working tree: clean after the latency telemetry commit; runtime secrets and chat logs remain ignored.
- Validation: `.venv\Scripts\python.exe -m py_compile common\latency.py channel\chat_channel.py bridge\agent_bridge.py agent\protocol\agent_stream.py` passed; `.venv\Scripts\python.exe -m pytest tests\test_llm_usage_tracker.py tests\test_multi_user_isolation.py` passed; `git diff --check` passed with Windows CRLF warnings only; service restarted with `.venv\Scripts\python.exe -m cli.cli restart --no-logs` and status shows PID 8168 running `weixin,weixin_user` on `gpt-5.5`.

## Change Log

- `2026-05-22` `3d39398` `feat: add latency telemetry for weixin replies`: Added hashed request latency logs for session queue wait, channel handling, AgentBridge execution, and each LLM stream turn so slow Weixin replies can be attributed to queueing, model latency, cache misses, tools, persistence, or send time.
- `2026-05-22` `7598c2d` `feat: improve prompt cache telemetry`: Added prompt cache usage visibility and persisted cache hit metrics for the current deployment.
- `2026-05-22` `4ed1edf` `feat: add safe github code update skill`: Added natural-language `code-update` skill plus guarded `git_code_update` tool for fast-forward-only updates that refuse dirty worktrees and protected config/secret paths.
- `2026-05-22` `51e861b` `docs: document github remote layout`: Documented that Codex/UI pushes should use the user's `origin` remote while the original project is kept as fetch-only `upstream`.
- `2026-05-22` `cd29392` `fix: preserve private memory isolation in shared scope`: Prevented shared memory access from exposing other users' private memory entries and added isolation tests.
- `2026-05-22` `40ee608` `chore: include latest web console state`: Captured the final current web console cache-view state before GitHub upload.
- `2026-05-22` `af5f161` `chore: snapshot current CowWechat workspace`: Captured the current local workspace, including image generation tooling, access control/user isolation helpers, scheduler tests, and GitHub-upload ignore hardening.
- `2026-05-21` `3d47ee5` `docs: note browser dependency installation`: Documented browser dependency installation.
- `2026-05-21` `c135c23` `chore: document browser and vision optional deps`: Documented Playwright/browser-use/agentmesh and image/QR/HTML helper dependencies in optional requirements after installing them locally.
- `2026-05-21` `f44c612` `feat: cover OpenAI multimodal capability config`: Added OpenAI multimodal capability configuration and focused tests.
- `2026-05-21` `b6d43bb` `feat: add OpenAI responses wire mode`: Added Responses API wire-mode support across OpenAI-compatible chat, streaming, tools, vision, voice gateway handling, config, and focused tests.
- `2026-05-21` `ac69a90` `deploy: document windows weixin deepseek setup`: Initial local Windows Weixin deployment documentation and safe ignore policy.

## Rollback Notes

- To roll back the GitHub upload snapshot, inspect `git show af5f161` and `git show 40ee608`, then revert in reverse order if needed.
- To roll back latency telemetry only, revert `3d39398` after confirming no runtime-only config files are staged.
- Local runtime config is ignored; rolling back code does not change `config.json`, `.env*`, Weixin credentials, or local virtual environments.
- The active service can be checked with `PYTHONUTF8=1 .venv\Scripts\python.exe -m cli.cli status`.
