---
name: playwright-browser-control
description: Browser automation policy for this machine using Playwright MCP with a dedicated persistent Chrome profile by default. Use when Codex needs to open, navigate, inspect, click, type, screenshot, or test websites while preserving login state across sessions. Use extension mode only when the task explicitly needs the user's real Chrome Default profile, existing tabs, or installed browser extensions.
---

# Playwright Browser Control

## Overview

Use Playwright MCP with a dedicated persistent Chrome profile as the default browser control path on this machine. This route preserves login state, cookies, and localStorage across Codex sessions without repeatedly asking the user to authorize takeover of their real Chrome tabs.

Use Playwright MCP extension mode only when a task specifically needs the user's real Chrome Default profile, an already-open tab, or installed browser extensions that are not present in the dedicated profile.

## Default Routing

- Start with the MCP server named `playwright_chrome`, configured to run `@playwright/mcp` with Chrome and `--user-data-dir=C:\Users\RondleLiu\.codex\playwright-profile`.
- Use this skill before `$codex-browser-control` for browser automation, authenticated websites, Discord workflows, SSO/2FA pages, and web validation.
- Prefer the dedicated persistent profile for repeated automation and remote-control workflows. If a site is not logged in yet, open it in this profile and let the user complete login once; reuse that saved state afterward.
- Use Playwright MCP extension mode only when the user explicitly asks to control their real Chrome Default profile, reuse an existing tab, or use installed Chrome extensions unavailable in the dedicated profile.
- Use the Codex Chrome Extension only when the user explicitly requests it or when Playwright MCP extension tools are unavailable.
- Use the in-app browser only for localhost, file-backed previews, or public pages that do not need login state.
- Do not use Chrome default-profile CDP or `--remote-debugging-port` takeover as the normal path.

## Workflow

1. Confirm Playwright MCP tools are mounted in the current Codex thread. If they are missing, tell the user Codex Desktop must be restarted or a fresh thread opened after the MCP config change.
2. Use the dedicated persistent profile for normal browser work. Do not ask the user to authorize browser takeover unless extension mode is actually needed.
3. If the site requires authentication and the persistent profile is not logged in, navigate to the login page and let the user complete login once. Continue after the visible page shows the authenticated state.
4. Navigate and interact only through visible page UI. Do not inspect cookies, tokens, local storage secrets, password stores, profile databases, `auth.json`, `.env`, or raw credentials.
5. If extension mode is needed, confirm the Playwright Extension is installed and enabled in the user's real Chrome Default profile. If it is missing, open the official Chrome Web Store listing at `https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm` for the user to install; do not sideload random CRX files.
6. Extension authorization: the user explicitly authorized Codex to handle Playwright browser takeover permission prompts automatically. When the Playwright Extension or browser takeover UI asks to open, select, or allow control of a new or existing page, choose the allowing/confirming option and the most relevant existing tab by default without asking again. Do not require the user to manually approve ordinary Playwright control prompts.
7. Automatic authorization does not cover destructive account changes, purchases, payments, exposing credentials, or security-sensitive permission changes unless the user explicitly requests that exact action.
8. Keep browser work narrow: verify the page, perform the requested action, and report the visible result.

## Discord Acceptance

For Discord browser validation:

- Use the dedicated persistent profile and open `https://discord.com/app` or a known Discord channel URL.
- Verify Discord is already logged in by checking for the main app UI, not by reading tokens or browser storage.
- If no channel URL is known, locate the visible exact channel name `codex-normal-chat`.
- Send only the user-approved validation message:
  `Codex Playwright persistent profile test`
- Execute the slash command `/openai-quota` in the same channel and wait for Discord to accept it or show the bot response.
- Report whether the message and slash command were visibly accepted; do not expose private Discord account details.

## Local Configuration

Expected MCP server in `C:\Users\RondleLiu\.codex\config.toml`:

```toml
[mcp_servers.playwright_chrome]
command = 'C:\Users\RondleLiu\AppData\Local\Microsoft\WinGet\Packages\OpenJS.NodeJS.LTS_Microsoft.Winget.Source_8wekyb3d8bbwe\node-v24.15.0-win-x64\node.exe'
args = ['C:\Users\RondleLiu\.codex\playwright-mcp\node_modules\@playwright\mcp\cli.js', '--browser=chrome', '--user-data-dir=C:\Users\RondleLiu\.codex\playwright-profile']
startup_timeout_sec = 45
tool_timeout_sec = 180
```

The local package is pinned under `C:\Users\RondleLiu\.codex\playwright-mcp`.

Dedicated persistent profile path: `C:\Users\RondleLiu\.codex\playwright-profile`.

Extension fallback configuration, only when real Chrome Default profile takeover is required:

```toml
[mcp_servers.playwright_chrome]
command = 'C:\Users\RondleLiu\AppData\Local\Microsoft\WinGet\Packages\OpenJS.NodeJS.LTS_Microsoft.Winget.Source_8wekyb3d8bbwe\node-v24.15.0-win-x64\node.exe'
args = ['C:\Users\RondleLiu\.codex\playwright-mcp\node_modules\@playwright\mcp\cli.js', '--extension']
startup_timeout_sec = 45
tool_timeout_sec = 180
```

Official extension ID: `mmlmfjhmonkocbjadbfplnigmagldckm`.
