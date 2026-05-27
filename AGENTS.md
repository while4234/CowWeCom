# Codex Project Rules For CowWeCom

These rules are mandatory for Codex sessions working in a Git checkout whose
`origin` remote is the user's writable `github.com/while4234/CowWeCom` repository
(HTTPS or SSH, with or without `.git`). Do not key this policy to one machine's
checkout path.

## Git And GitHub Are Not Optional

- After any development task that changes project files, Codex must leave the work in Git before the final response.
- For every project, local Git applies: inspect `git status`, stage only intentional files, commit completed code/docs/test changes, and report the commit hash.
- For this project only (`while4234/CowWeCom`, identified by a writable `origin` remote for this user's GitHub account), GitHub push also applies: after a successful local commit, push the current branch to `origin`.
- Do not wait for the user to ask for commit or push in this project. Treat commit and push as part of the definition of done.
- If validation fails, protected files are staged, the remote rejects the push, or credentials/network block publishing, stop before unsafe actions and report the blocker clearly.
- Never push to any remote other than `origin` unless the user explicitly asks.
- Do not force-push without an explicit user request for that exact action.

## Scope Boundaries

- The automatic GitHub push rule is limited to checkouts whose `origin` remote is the user's writable `while4234/CowWeCom` remote.
- If this repository is a fork, a read-only clone, a checkout owned by another GitHub user, or `origin` is not writable for the current local Git credentials, ignore the automatic push rule: make the local commit only and report that remote publishing is not active for this checkout.
- In other projects, local Git commit still applies after development work, but GitHub push does not apply unless that project has a confirmed remote and the user asks or project instructions say so.
- If a different project has no Git repository, initialize local Git when development work changes files, add a safe `.gitignore`, and make a local commit.

## Staging Safety

- Stage path-specifically. Do not use broad `git add .` when unrelated local changes exist.
- Never stage secrets, credentials, tokens, cookies, local auth files, logs, generated runtime state, `.env`, `config.json`, `.codex/`, `.playwright-mcp/`, virtual environments, or local databases.
- Exception: only administrator Web-backend uploaded protocol/specification knowledge is public project data. Those portable artifacts must live under `public_protocol_knowledge/` and be committed after validation: `indexes/kb.sqlite`, `originals/`, `derived/`, `reports/`, and `manifest.json`. Personal knowledge, conversation-generated summaries, and `knowledge-wiki` or other automatic knowledge outputs must stay out of Git in ignored runtime locations such as `knowledge_backend/`, `knowledge/`, `workspace/knowledge/`, or the Agent workspace.
- Respect `.gitignore` and the project `safe-github-upload` skill rules.
- For CowWeCom/CowWechat skill work, keep both copies synchronized:
  - Repository copy: `<project-root>\skills\<skill-name>\`
  - Runtime copy: `C:\Users\RondleLiu\cow\skills\<skill-name>\`

## README Must Track Code

- Any task that changes project code, runtime behavior, channels, configuration, tests, skills, safety policy, deployment flow, or user-visible capabilities must update the root `README.md` in the same commit set. At minimum, add or refresh the current Chinese update-log entry; when behavior/config/support scope changes, update the relevant README sections too.
- The root `README.md` update log must keep one consolidated date entry per calendar date. If an entry for the current date already exists, merge the new change into that entry and summarize the day's updates instead of adding another same-date row or section.
- Keep each README update-log date entry concise and scannable: prefer 3-6 short bullets focused on user-visible capability changes, usage/config changes, migration notes, and important fixes.
- Do not put schema field lists, test names, internal function names, full validation logs, temporary debugging notes, or commit-by-commit details into the README update log.
- Put detailed development records, validation commands, failure causes, rollback clues, and file-level changes in `GIT_NOTES.md` instead of expanding the README update log.
- When `git fetch`, `git pull`, or `git rebase` shows that `origin/main` has new code commits and those commits did not update the root `README.md`, Codex must automatically update `README.md` to cover those remote code changes before finishing the task, committing, or pushing.
- Keep the root README Chinese, CowWeCom-focused, and aligned with actually developed/tested Weixin and WeCom scope. Do not reintroduce upstream promotional/contact material or unverified channel setup instructions.

## CowCli Permission Policy

- Chat-visible CowCli commands must be explicitly classified by risk. Low-risk read-only/self-scoped commands may be public; high-risk commands that mutate global runtime state, install/uninstall/enable/disable skills, change backend/config/knowledge/memory state, expose logs, install dependencies, run restart/process actions, or touch sensitive local state must require admin.
- New CowCli commands must default to admin-only until deliberately reviewed and added to the access table.
- `/help` must be role-aware: ordinary users should only see commands they can run, while administrators can see the full command surface.
- Personal ledger commands should use the current chat context's `memory_user_id`; never fall back to placeholder IDs such as `local-user`. In group contexts, admin checks must consider the actual sender id when available, not only the group actor id.

## Required CowWeCom Publish Flow

1. Run the safe upload preflight before staging:
   ```powershell
   $root = git rev-parse --show-toplevel
   $env:PYTHONUTF8='1'
   py -3 "$root\skills\safe-github-upload\scripts\preflight.py" --root $root
   ```
2. Update root `README.md` for every code change or newly discovered remote code change that lacks a matching README update.
3. Stage only intentional source, tests, docs, safe templates, and skill files.
4. Run preflight again after staging.
5. Inspect staged files with `git diff --cached --name-status` and `git diff --cached --check`.
6. Run focused validation for the changed area.
7. Update `GIT_NOTES.md` and `.codex/HANDOFF.md` when the task changed project state.
8. Commit with a concise message.
9. Push to `origin` on the current branch.
10. Final response must include commit hash, push result, validation, and any unrelated uncommitted files left behind.
