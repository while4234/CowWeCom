# Codex Quota Query

This skill is bundled with CowWechat and can be synced to the runtime skill directory:

```powershell
New-Item -ItemType Directory -Force $HOME\cow\skills | Out-Null
Copy-Item -Recurse -Force .\skills\codex-quota-query $HOME\cow\skills\codex-quota-query
```

No secrets are stored in this skill. Runtime auth remains in the user's existing Codex login state. Configure `CODEX_AUTH_FILE`, top-level `codex_auth_file`, or `llm_backend.providers.codex.auth_file` on each deployment, and make sure the official `codex` executable is available on `PATH` or through `CODEX_CLI_BINARY`.
