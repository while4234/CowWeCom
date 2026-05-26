---
name: github
description: GitHub core operations for repository management, authenticated REST API calls, issues, pull requests, releases, forks, and GitHub-backed Git push workflows. Use when the user asks to operate on GitHub repositories or fix/use local GitHub credentials.
metadata:
  cowagent:
    requires:
      bins: ["git", "curl"]
    primaryEnv: "GITHUB_TOKEN"
---

# GitHub Operations Skill

Use this skill to operate GitHub from the local machine without requiring `jq` or a shell-exported `GITHUB_TOKEN`. The bundled helper uses Python for credential resolution and JSON formatting, then uses `curl.exe` for the HTTPS request so it works with the machine's configured proxy.

## Authentication

Prefer the local helper:

```powershell
python skills\github\scripts\github_api.py --endpoint /user
```

To check whether a usable local token is discoverable without calling GitHub:

```powershell
python skills\github\scripts\github_api.py --check-auth
```

The helper resolves credentials in this order:

1. `GITHUB_TOKEN`
2. `GH_TOKEN`
3. Git Credential Manager via `git credential fill` for `https://github.com`

Do not print tokens. Do not write tokens into Git remotes, config files, docs, handoffs, or commits.

For Git operations, prefer normal credential-manager-backed commands:

```powershell
git push origin <branch>
```

Do not rewrite a remote URL to include a token.

## REST API Helper

Run GitHub REST API calls with:

```powershell
python skills\github\scripts\github_api.py --method GET --endpoint /repos/OWNER/REPO
```

Use `--data-json` for request bodies:

```powershell
python skills\github\scripts\github_api.py `
  --method POST `
  --endpoint /repos/OWNER/REPO/issues `
  --data-json '{\"title\":\"Issue title\",\"body\":\"Description\"}'
```

The endpoint may be either `/path` or a full `https://api.github.com/path` URL. Output is formatted JSON when possible.

## Common Operations

### Current User

```powershell
python skills\github\scripts\github_api.py --endpoint /user
```

### List Visible Repositories

Use the built-in convenience action instead of writing a temporary wrapper script:

```powershell
python skills\github\scripts\github_api.py --list-repos
```

### Recent Repository Updates

Use this for questions like "今天 GitHub 有哪些更新" or "我最近推了什么代码":

```powershell
python skills\github\scripts\github_api.py --recent-updates --days 1
python skills\github\scripts\github_api.py --recent-updates --days 7 --owner while4234
```

The output is JSON with repository names, recent commit samples, timestamps, and links. Summarize only repository names and commit messages needed for the user's answer; do not print tokens or credential details.

### Create Repository

```powershell
python skills\github\scripts\github_api.py `
  --method POST `
  --endpoint /user/repos `
  --data-json '{\"name\":\"REPO\",\"description\":\"desc\",\"private\":false}'
```

### Fork Repository

```powershell
python skills\github\scripts\github_api.py `
  --method POST `
  --endpoint /repos/OWNER/REPO/forks `
  --data-json '{\"default_branch_only\":true}'
```

### Create Pull Request

```powershell
python skills\github\scripts\github_api.py `
  --method POST `
  --endpoint /repos/OWNER/REPO/pulls `
  --data-json '{\"title\":\"PR title\",\"head\":\"feature-branch\",\"base\":\"main\",\"body\":\"Description\"}'
```

For pull requests from a fork, use `username:branch-name` as `head`.

### Comment On Issue Or Pull Request

Issues and pull requests share the issue comments API:

```powershell
python skills\github\scripts\github_api.py `
  --method POST `
  --endpoint /repos/OWNER/REPO/issues/NUMBER/comments `
  --data-json '{\"body\":\"Comment content\"}'
```

### Create Release

```powershell
python skills\github\scripts\github_api.py `
  --method POST `
  --endpoint /repos/OWNER/REPO/releases `
  --data-json '{\"tag_name\":\"v1.0.0\",\"name\":\"v1.0.0\",\"body\":\"Release notes\",\"generate_release_notes\":true}'
```

## Setup Fallback

If the helper reports that no GitHub token is available, sign in with Git Credential Manager by running a normal authenticated Git operation such as:

```powershell
git ls-remote https://github.com/OWNER/REPO.git
```

Alternatively set `GITHUB_TOKEN` or `GH_TOKEN` in the process environment.
