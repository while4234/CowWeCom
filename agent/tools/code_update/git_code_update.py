import os
from typing import Any, Dict

from agent.tools.base_tool import BaseTool, ToolResult
from common.git_code_updater import GitCodeUpdater


class GitCodeUpdateTool(BaseTool):
    name = "git_code_update"
    description = (
        "Safely update this CowWechat code checkout from the user's GitHub repository. "
        "Use when the admin asks in natural language to pull, sync, update, or refresh remote GitHub code. "
        "This tool only performs fast-forward code updates and refuses to modify local config, .env, token, "
        "cookie, session, credential, log, virtualenv, or Codex runtime files. It also refuses to run when "
        "there are uncommitted local code changes."
    )
    params = {
        "type": "object",
        "properties": {
            "remote": {
                "type": "string",
                "description": "Git remote to fetch from. Defaults to origin.",
            },
            "branch": {
                "type": "string",
                "description": "Remote branch to update from. Defaults to main.",
            },
        },
    }

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        params = params or {}
        remote = params.get("remote") or "origin"
        branch = params.get("branch") or "main"

        try:
            update = GitCodeUpdater(os.getcwd()).update(remote=remote, branch=branch)
        except ValueError as e:
            return ToolResult.fail(str(e))
        except Exception as e:
            return ToolResult.fail(f"Failed to update code: {e}")

        payload = {
            "status": update.status,
            "message": update.message,
            "remote_ref": update.remote_ref,
            "old_head": update.old_head,
            "new_head": update.new_head,
            "changed_files": update.changed_files,
            "protected_files": update.protected_files,
            "dirty_entries": update.dirty_entries,
            "restart_required": update.status == "updated",
        }

        if update.ok:
            return ToolResult.success(payload)
        return ToolResult.fail(payload)
