# Git Notes

## Repository Purpose

Local CowAgent deployment and development workspace for Weixin bot integration, OpenAI-compatible model providers, and agent tooling.

## Ignore And Secret Policy

Runtime secrets and local state stay out of Git. The repository ignores `config.json`, env files, key/certificate files, Weixin credentials, cookies, tokens, logs, virtual environments, generated build output, `.codex/`, and other local workspace artifacts.

Do not commit API keys, QR login links, credential JSON files, chat logs, or local auth material. Use committed templates and documentation only for placeholders and safe defaults.

## Current Baseline

- Latest feature commit: `b6d43bb` `feat: add OpenAI responses wire mode`
- Working tree: expected to contain only this notes file before the docs commit; local ignored files include runtime config, logs, `.venv`, and `.codex`.
- Validation: focused Responses adapter tests passed; modified Python files compiled; full test suite passed under `PYTHONUTF8=1`.

## Change Log

- `2026-05-21` `b6d43bb` `feat: add OpenAI responses wire mode`: Added Responses API wire-mode support across OpenAI-compatible chat, streaming, tools, vision, voice gateway handling, config, and focused tests.
- `2026-05-21` `ac69a90` `deploy: document windows weixin deepseek setup`: Initial local Windows Weixin deployment documentation and safe ignore policy.

## Rollback Notes

- To roll back the Responses API feature while keeping later docs, inspect `git show b6d43bb` first, then revert with `git revert b6d43bb`.
- Local runtime config is ignored; rolling back code does not change `config.json` or Weixin credentials.
- The active service can be checked with `PYTHONUTF8=1 .venv\Scripts\python.exe -m cli.cli status`.
