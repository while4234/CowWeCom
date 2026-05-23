# Codex Project Rules For CowWechat

These rules are mandatory for Codex sessions working in `D:\CowWechat`.

## Git And GitHub Are Not Optional

- After any development task that changes project files, Codex must leave the work in Git before the final response.
- For every project, local Git applies: inspect `git status`, stage only intentional files, commit completed code/docs/test changes, and report the commit hash.
- For this project only (`D:\CowWechat` / `while4234/CowWeCom`), GitHub push also applies: after a successful local commit, push the current branch to `origin`.
- Do not wait for the user to ask for commit or push in this project. Treat commit and push as part of the definition of done.
- If validation fails, protected files are staged, the remote rejects the push, or credentials/network block publishing, stop before unsafe actions and report the blocker clearly.
- Never push to any remote other than `origin` unless the user explicitly asks.
- Do not force-push without an explicit user request for that exact action.

## Scope Boundaries

- The automatic GitHub push rule is limited to this CowWechat repository because this project has a configured GitHub remote.
- In other projects, local Git commit still applies after development work, but GitHub push does not apply unless that project has a confirmed remote and the user asks or project instructions say so.
- If a different project has no Git repository, initialize local Git when development work changes files, add a safe `.gitignore`, and make a local commit.

## Staging Safety

- Stage path-specifically. Do not use broad `git add .` when unrelated local changes exist.
- Never stage secrets, credentials, tokens, cookies, local auth files, logs, generated runtime state, `.env`, `config.json`, `.codex/`, `.playwright-mcp/`, virtual environments, or local databases.
- Respect `.gitignore` and the project `safe-github-upload` skill rules.
- For CowWechat skill work, keep both copies synchronized:
  - Repository copy: `D:\CowWechat\skills\<skill-name>\`
  - Runtime copy: `C:\Users\RondleLiu\cow\skills\<skill-name>\`

## Required CowWechat Publish Flow

1. Run the safe upload preflight before staging:
   ```powershell
   $env:PYTHONUTF8='1'
   .venv\Scripts\python.exe skills\safe-github-upload\scripts\preflight.py --root D:\CowWechat
   ```
2. Stage only intentional source, tests, docs, safe templates, and skill files.
3. Run preflight again after staging.
4. Inspect staged files with `git diff --cached --name-status` and `git diff --cached --check`.
5. Run focused validation for the changed area.
6. Update `GIT_NOTES.md` and `.codex/HANDOFF.md` when the task changed project state.
7. Commit with a concise message.
8. Push to `origin` on the current branch.
9. Final response must include commit hash, push result, validation, and any unrelated uncommitted files left behind.
