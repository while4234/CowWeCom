# Git Notes

## Repository Purpose

Personal CowWechat deployment and development workspace for Weixin bot integration, OpenAI-compatible model providers, image generation tooling, scheduler tools, and agent memory features.

## Ignore And Secret Policy

Runtime secrets and local state stay out of Git. The repository ignores `config.json`, env files, key/certificate files, Weixin credentials, cookies, tokens, session files, logs, virtual environments, generated build output, `.codex/`, `.playwright-mcp/`, and other local workspace artifacts.

Do not commit API keys, QR login material, credential JSON files, chat logs, cookies, tokens, or local auth material. Use committed templates and documentation only for placeholders and safe defaults.

## Current Baseline

- Latest code commit: `40ee608` `chore: include latest web console state`
- GitHub upload target: private repository `while4234/CowWechat`
- Working tree: expected to contain only this notes update before the docs commit; ignored local runtime files include `config.json`, `.venv/`, `.codex/`, logs, and browser automation snapshots.
- Validation: high-confidence secret scan over commit-eligible files passed; ignore checks confirmed local secret/config patterns are excluded; `git diff --check` passed. Full test suite was not run for this upload-only task.

## Change Log

- `2026-05-22` `40ee608` `chore: include latest web console state`: Captured the final current web console cache-view state before GitHub upload.
- `2026-05-22` `af5f161` `chore: snapshot current CowWechat workspace`: Captured the current local workspace, including image generation tooling, access control/user isolation helpers, scheduler tests, and GitHub-upload ignore hardening.
- `2026-05-21` `3d47ee5` `docs: note browser dependency installation`: Documented browser dependency installation.
- `2026-05-21` `c135c23` `chore: document browser and vision optional deps`: Documented Playwright/browser-use/agentmesh and image/QR/HTML helper dependencies in optional requirements after installing them locally.
- `2026-05-21` `f44c612` `feat: cover OpenAI multimodal capability config`: Added OpenAI multimodal capability configuration and focused tests.
- `2026-05-21` `b6d43bb` `feat: add OpenAI responses wire mode`: Added Responses API wire-mode support across OpenAI-compatible chat, streaming, tools, vision, voice gateway handling, config, and focused tests.
- `2026-05-21` `ac69a90` `deploy: document windows weixin deepseek setup`: Initial local Windows Weixin deployment documentation and safe ignore policy.

## Rollback Notes

- To roll back the GitHub upload snapshot, inspect `git show af5f161` and `git show 40ee608`, then revert in reverse order if needed.
- Local runtime config is ignored; rolling back code does not change `config.json`, `.env*`, Weixin credentials, or local virtual environments.
- The active service can be checked with `PYTHONUTF8=1 .venv\Scripts\python.exe -m cli.cli status`.
