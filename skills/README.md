# Skills

Skills are reusable instruction sets that extend the agent's capabilities. Each skill is a `SKILL.md` file in its own directory, providing specialized knowledge, workflows, and tool integrations for specific tasks.

## Skill Hub

Browse, search, and install skills from [Cow Skill Hub](https://skills.cowagent.ai/).

Open source: [github.com/zhayujie/cow-skill-hub](https://github.com/zhayujie/cow-skill-hub)

## Install Skills

Install skills from multiple sources via chat (`/skill`) or terminal (`cow skill`):

```bash
/skill install <name>                   # From Skill Hub
/skill install <owner>/<repo>           # From GitHub
/skill install clawhub:<name>           # From ClawHub
/skill install linkai:<code>            # From LinkAI
/skill install <url>                    # From URL (zip or SKILL.md)
```

List all available remote skills:

```bash
/skill list --remote
```

## Manage Skills

```bash
/skill list                  # List installed skills
/skill info <name>           # View skill details
/skill enable <name>         # Enable a skill
/skill disable <name>        # Disable a skill
/skill uninstall <name>      # Uninstall a skill
```

> In terminal, replace `/skill` with `cow skill`.

## Included Local Skills

This repository currently includes the CowWechat skills that were installed on
the local deployment machine. Each skill directory contains an `INSTALL.md`
with machine setup notes and any required user-provided keys.

| Skill | Purpose | External key/setup needed |
|---|---|---|
| `capi-usage-monitor` | Query and snapshot CAPI/Codex quota and usage. | `OPENAI_API_KEY` by default, or a local CAPI key override. |
| `code-update` | Safely fast-forward CowWechat from GitHub while protecting local state. | No API key; Git credentials stay local. |
| `cowagent-self-evolution` | Record and reuse local execution lessons such as Windows shell dialect failures without showing them in chat. | No external API key; needs Python. |
| `docx` | Create, read, edit, and format Word `.docx` documents. | No external API key. |
| `github` | Perform GitHub repository, fork, PR, release, issue, and comment operations. | `GITHUB_TOKEN`; also needs `git`, `curl`, and `jq`. |
| `image-generation` | Generate/edit images through background jobs. | Local Codex login, or one configured image provider API key. |
| `knowledge-wiki` | Maintain a local structured knowledge wiki. | No API key. |
| `markdown-converter` | Convert PDF, Office, HTML, CSV/JSON/XML, images, audio, archives, YouTube URLs, and EPUBs to Markdown. | No external API key; converter dependencies may be installed locally. |
| `pdf` | Read, extract, split, merge, rotate, watermark, create, and secure PDF files. | No external API key. |
| `plugin-12306-ticket` | Query China Railway 12306 tickets, stations, and train route information through local public 12306 endpoints. | No external API key; needs Python network access. |
| `pptx` | Create, read, edit, and extract content from PowerPoint `.pptx` decks. | No external API key. |
| `reliable-search` | Search with Serper/Brave provider fallback. | `SERPER_API_KEY` or `BRAVE_API_KEY` supplied by the user. |
| `skill-creator` | Create, validate, and package skills. | No API key unless the new skill needs one. |
| `stock-analysis` | Analyze stocks and cryptocurrencies, portfolios, watchlists, dividends, and trend signals. | No external API key; requires `uv`. |
| `token-usage-tracker` | Track local per-user token usage. | No API key. |
| `travel-manager` | Plan multi-destination trips, family travel logistics, costs, and itineraries. | No external API key. |
| `wechat-article-search` | Search WeChat public-account articles and return titles, summaries, dates, sources, and links. | No external API key. |
| `xlsx` | Create, read, edit, clean, format, chart, and analyze spreadsheet files. | No external API key. |

## Development Sync Rule

When adding or fixing a skill in this project, keep the repository copy and the
deployed workspace copy synchronized:

1. Edit and validate the builtin skill under `<project_root>/skills/<skill>/`.
2. If `~/cow/skills/<skill>/` exists, copy the same updated skill directory
   there, because custom workspace skills override builtin skills at runtime.
3. Commit the repository copy and push it when publishing a fix, so later users
   who deploy from GitHub do not receive stale skill behavior.
4. Never copy local secrets, pycache files, snapshots, or generated runtime
   state into either skill directory.

## Skill Structure

```
skills/
  my-skill/
    SKILL.md          # Required: skill definition
    scripts/          # Optional: bundled scripts
    resources/        # Optional: reference files
```

`SKILL.md` uses YAML frontmatter:

```markdown
---
name: my-skill
description: Brief description of what the skill does
metadata: {"cow":{"emoji":"🔧","requires":{"bins":["tool"],"env":["API_KEY"]}}}
---

# My Skill

Instructions, examples, and usage patterns...
```

### Frontmatter Fields

| Field | Description |
|---|---|
| `name` | Skill name (must match directory name) |
| `description` | Brief description (required) |
| `metadata.cow.emoji` | Display emoji |
| `metadata.cow.always` | Always include this skill (default: false) |
| `metadata.cow.requires.bins` | Required binaries |
| `metadata.cow.requires.env` | Required environment variables |
| `metadata.cow.requires.config` | Required config paths |
| `metadata.cow.os` | Supported OS (e.g., `["darwin", "linux"]`) |

## Skill Loading Order

Skills are loaded from two locations (higher precedence overrides lower):

1. **Builtin skills** (lower): `<project_root>/skills/` — shipped with the codebase
2. **Custom skills** (higher): `~/cow/skills/` — installed via `cow skill install` or skill creator

Skills with the same name in the custom directory override builtin ones.

## Create & Contribute

See the [Skill Creation docs](https://docs.cowagent.ai/skills/create) for details, or submit your skill to [Skill Hub](https://skills.cowagent.ai/submit).
