# Playwright Browser Control Skill

This skill was copied from the local Codex skill:

```text
C:\Users\RondleLiu\.codex\skills\playwright-browser-control
```

It documents the preferred browser automation route on this machine: Playwright MCP with a dedicated persistent Chrome profile. The persistent profile keeps normal browser session state across Codex sessions while avoiding direct takeover of the real Chrome Default profile for routine tasks.

## Runtime Requirements

- Node.js available at the path configured in the local Codex MCP config.
- `@playwright/mcp` installed under the local pinned package directory.
- A Codex MCP server named `playwright_chrome` configured with `--browser=chrome` and `--user-data-dir` pointing to the persistent profile.
- One-time user login in the persistent profile for sites that need authentication.

Do not commit the MCP package directory, browser profile, cookies, tokens, local storage dumps, or Codex config files. Only this instruction skill belongs in Git.

## Validation

For skill packaging validation:

```powershell
.venv\Scripts\python.exe skills\skill-creator\scripts\quick_validate.py skills\playwright-browser-control
```

For runtime browser validation, open a non-sensitive page through the mounted `playwright_chrome` MCP tools and verify navigation works from the persistent profile.
