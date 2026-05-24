---
name: find-skills-skill
description: Search and evaluate community OpenClaw or ClawHub skills before installing them into CowWechat. Use when the user asks to find skills, search ClawHub, discover available agent skills, compare popular skills, or install a community skill after local safety review and CowWechat runtime sync.
metadata:
  requires:
    bins: ["python"]
---

# Find Skills Skill

Use this skill to discover community skills and turn them into safe CowWechat-local skills.

## Local Policy

Prefer review-and-localize over blind installation. Community skill marketplaces can contain low-quality or unsafe instructions, so inspect the listing, `SKILL.md`, scripts, required keys, and audit status before installing.

For CowWechat, install into both locations:

- Repository copy: `D:\CowWechat\skills\<skill-name>\`
- Runtime copy: `C:\Users\RondleLiu\cow\skills\<skill-name>\`

After changing repo skill files, validate, update project notes, commit, and push according to `safe-github-upload`.

## Known Community Reference

As of 2026-05-23, ClawHub lists `fangkelvin/find-skills-skill` as:

- Downloads: `47.6k`
- Stars: `123`
- Audit status: `Pending`
- Install command shown by ClawHub: `openclaw skills install find-skills-skill`
- Purpose: search and discover OpenClaw skills from ClawHub, OpenClaw directories, GitHub, and community sources.

Because the audit is pending, use it as a reference for workflow and wording, then keep the CowWechat-local copy minimal and deterministic.

## Search Workflow

1. Search current sources:

```powershell
python "<base_dir>\scripts\find_skills.py" "weather" --sort installs
python "<base_dir>\scripts\find_skills.py" "skill search" --sort stars
```

The helper first tries a direct `clawhub` CLI, then `npx clawhub`, then common Windows Node install locations such as WinGet's `OpenJS.NodeJS` package directory. If the CLI still cannot run, continue with web lookup plus local review.

2. Prefer candidates with high downloads or stars, clear docs, small readable source, no secrets, no unexpected network endpoints, no destructive shell commands, and no hidden credential collection.
3. Read the candidate `SKILL.md` and bundled scripts before installation.
4. Localize the skill for CowWechat:
   - Keep only the useful instructions or safe scripts.
   - Replace external install assumptions with CowWechat paths.
   - Remove unrelated marketing, telemetry, credential capture, or broad shell execution.
   - Add no secrets and no real tokens.
5. Validate with:

```powershell
$env:PYTHONUTF8='1'
.venv\Scripts\python.exe skills\skill-creator\scripts\quick_validate.py skills\<skill-name>
```

6. Sync to runtime:

```powershell
New-Item -ItemType Directory -Force $HOME\cow\skills | Out-Null
Copy-Item -Recurse -Force .\skills\<skill-name> $HOME\cow\skills\<skill-name>
```

## Installation Decision

Use this skill to find and vet candidates. Use `skill-creator` to create or adapt a CowWechat-local skill. Use `safe-github-upload` before committing and pushing changes.
