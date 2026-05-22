# Git Notes

## Repository Purpose

Personal CowWechat deployment and development workspace for Weixin bot integration, OpenAI-compatible model providers, image generation tooling, scheduler tools, and agent memory features.

## Ignore And Secret Policy

Runtime secrets and local state stay out of Git. The repository ignores `config.json`, env files, key/certificate files, Weixin credentials, cookies, tokens, session files, logs, virtual environments, generated build output, `.codex/`, `.playwright-mcp/`, and other local workspace artifacts.

Do not commit API keys, QR login material, credential JSON files, chat logs, cookies, tokens, or local auth material. Use committed templates and documentation only for placeholders and safe defaults.

## Current Baseline

- Latest local code work: social bridge active-send runtime channel fix
- Latest committed skill sync: `631f159` `feat: sync local cow skills`
- Latest merged code work: `852e909` `feat: add wechat quick progress lane`; `c309e61` `fix: make weixin onboarding greeting deterministic`
- GitHub upload target: private repository `while4234/CowWechat`
- Remote layout: `origin` points to `https://github.com/while4234/CowWechat.git`; `upstream` points to the original `https://github.com/zhayujie/CowAgent.git` and has push disabled.
- Working tree: expected clean after merging the CowWechat skill sync, multi-Weixin console/identity fix, real Weixin ID persistence follow-up, and token usage skill/runtime environment fix; runtime secrets, local state, chat logs, pycache files, and API keys remain ignored.
- Validation: social bridge active-send runtime-channel tests and related Weixin/multi-user/progress regressions passed; social bridge display-name fallback tests passed; skill script py_compile passed for `capi_usage.py`, image-generation scripts, reliable-search scripts, skill-creator scripts, and `token_usage.py`; `.venv312\Scripts\python.exe -m py_compile channel\weixin\weixin_identity.py channel\weixin\weixin_channel.py channel\web\web_channel.py agent\user_profiles.py tests\test_multi_weixin_instances.py` passed; `.venv312\Scripts\python.exe -m unittest tests.test_token_usage_tracker_skill tests.test_bash_tool tests.test_llm_usage_tracker` passed with 9 tests; earlier multi-Weixin/user isolation tests passed with 27 tests; `node --check channel\web\static\js\console.js` passed; `git diff --check` passed with Windows CRLF warnings only.

## Change Log

- `2026-05-22` `fix: route bridge active sends through running app`: Fixed social bridge proactive sends when CowAgent is launched as `python app.py` by keeping a canonical `app` module reference and falling back to the `__main__` ChannelManager. Pending bridge messages are now visible/retryable by both sender and target, with a sender/status index. Validation: `.venv312\Scripts\python.exe -m unittest tests.test_social_bridge_service tests.test_social_bridge_store tests.test_social_bridge_tools tests.test_multi_weixin_instances tests.test_multi_user_isolation tests.test_fast_lane_progress` OK (59 tests); `.venv312\Scripts\python.exe -m compileall app.py agent/social_bridge/service.py agent/social_bridge/store.py tests/test_social_bridge_service.py tests/test_social_bridge_store.py` OK; `git diff --check` OK with CRLF warnings only. Note: one unrelated image-generation concurrency timing test exceeded its local 0.65s threshold during a broad run, while the affected manager lookup test passed separately.
- `2026-05-22` `fix: show declared names in bridge directory`: Made the social bridge directory derive public display names from both `USER.md` and `MEMORY.md`, including Agent-written formats such as `用户希望被称为「小栀」` and `用户称呼：小栀`, while ignoring blank-template placeholders. Validation: `.venv312\Scripts\python.exe -m unittest tests.test_social_bridge_service tests.test_social_bridge_store tests.test_social_bridge_tools tests.test_multi_weixin_instances tests.test_multi_user_isolation tests.test_fast_lane_progress` OK (57 tests); `.venv312\Scripts\python.exe -m compileall agent/social_bridge/service.py tests/test_social_bridge_service.py` OK; `git diff --check` OK with CRLF warnings only.
- `2026-05-22` `feat: add social bridge messaging`: Added controlled cross-Weixin user directory, relationship memory, authorized bridge messaging, pending/retry handling, Weixin proactive text routing, social bridge tool registration, config defaults, and focused privacy/active-send tests. Validation: `.venv312\Scripts\python.exe -m unittest tests.test_social_bridge_store tests.test_social_bridge_tools tests.test_social_bridge_service tests.test_multi_weixin_instances tests.test_multi_user_isolation` OK (47 tests); `.venv312\Scripts\python.exe -m compileall agent\social_bridge agent\tools\social_bridge channel\weixin\weixin_channel.py config.py` OK; ToolManager bridge-tool loading smoke OK; `git diff --check` OK with CRLF warnings only.
- `2026-05-22` `fix: make token usage skill read CowAgent runtime logs`: Updated `token-usage-tracker` to fall back to `data/llm_cache_usage.jsonl`, surface cache/reasoning metrics, and fixed the Bash tool so skill subprocesses receive only string env vars and resolve the active Python interpreter.
- `2026-05-22` `fix: persist real Weixin id mappings`: Stopped the channels API from exposing iLink `@im.wechat` raw IDs as display WeChat IDs, added save support for real WeChat IDs on Weixin channel cards, and covered the mapping path with focused tests.
- `2026-05-22` `d4976d2` `fix: show multi-weixin users and real ids`: Added dynamic `weixin_*` channel cards, repeated QR login for new WeChat users, admin/member role display, startup credential backfill, and real WeChat ID mapping for prompt-cache/user labels.
- `2026-05-22` `631f159` `feat: sync local cow skills`: Synced the seven locally installed CowWechat skills into `skills/`, added per-skill `INSTALL.md` files, documented required user-provided API keys, omitted pycache/local state, and sanitized local-only paths.
- `2026-05-22` `852e909` `feat: add wechat quick progress lane`: Added `/q` progress and quick-reply fast lanes, `/状态`/`/取消`/`/跳过` local controls, per-session progress snapshots, cooperative Agent cancellation, and fast-lane unit tests.
- `2026-05-22` `c309e61` `fix: make weixin onboarding greeting deterministic`: Added a narrow pre-model onboarding greeting path so a clean workspace with `BOOTSTRAP.md` returns the full first-run welcome for pure greeting messages.
- `2026-05-22` `3fc82be` `docs: update latency telemetry notes`: Updated local handoff and Git notes after latency telemetry validation.
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
- To roll back the CowWechat skill sync, revert `631f159`; local installed skills under `~/cow/skills` are not changed by reverting the repository.
- To roll back latency telemetry only, revert `3d39398` after confirming no runtime-only config files are staged.
- To roll back the `/q` fast-lane behavior only, revert `852e909` after confirming no runtime-only config files are staged.
- Local runtime config is ignored; rolling back code does not change `config.json`, `.env*`, Weixin credentials, or local virtual environments.
- The active service can be checked with `PYTHONUTF8=1 .venv\Scripts\python.exe -m cli.cli status`.
