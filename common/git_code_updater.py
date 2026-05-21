import fnmatch
import os
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional


PROTECTED_PATH_PATTERNS = (
    ".env",
    ".env.*",
    "config.json",
    "plugins.json",
    "plugins/config.json",
    "plugins/*/config.json",
    "credentials*.json",
    "token*.json",
    "cookies*.json",
    "session*.json",
    "service-account*.json",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "secrets/*",
    ".codex/*",
    ".playwright-mcp/*",
    ".venv/*",
    "venv*/*",
    "logs/*",
    "*.log",
)


@dataclass
class GitUpdateResult:
    status: str
    message: str
    old_head: str = ""
    new_head: str = ""
    remote_ref: str = ""
    changed_files: List[str] = field(default_factory=list)
    protected_files: List[str] = field(default_factory=list)
    dirty_entries: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in {"updated", "up_to_date", "local_ahead"}


class GitCodeUpdater:
    """Safely fast-forward this checkout from a configured Git remote."""

    def __init__(self, repo_path: Optional[str] = None, git_executable: str = "git"):
        self.repo_path = os.path.abspath(repo_path or os.getcwd())
        self.git_executable = git_executable

    def update(self, remote: str = "origin", branch: str = "main") -> GitUpdateResult:
        remote = self._validate_ref_part(remote, "remote")
        branch = self._validate_ref_part(branch, "branch")

        repo_root = self._repo_root()
        if not repo_root:
            return GitUpdateResult(
                status="error",
                message="Current directory is not inside a Git repository.",
            )
        self.repo_path = repo_root

        dirty_entries = self._dirty_entries()
        if dirty_entries:
            return GitUpdateResult(
                status="dirty",
                message="Local worktree has uncommitted files; refusing to update code.",
                dirty_entries=dirty_entries,
            )

        fetch = self._run(["fetch", "--prune", remote, branch], timeout=120)
        if fetch.returncode != 0:
            return GitUpdateResult(
                status="error",
                message=self._format_git_error("git fetch failed", fetch),
            )

        local_head = self._git_output(["rev-parse", "HEAD"])
        remote_ref = f"{remote}/{branch}"
        remote_head = self._git_output(["rev-parse", "--verify", f"{remote_ref}^{{commit}}"])
        if not remote_head:
            return GitUpdateResult(
                status="error",
                message=f"Remote ref {remote_ref} was not found after fetch.",
                old_head=local_head,
                remote_ref=remote_ref,
            )

        if local_head == remote_head:
            return GitUpdateResult(
                status="up_to_date",
                message="Already up to date.",
                old_head=local_head,
                new_head=remote_head,
                remote_ref=remote_ref,
            )

        changed_files = self._changed_files(local_head, remote_head)
        protected_files = [path for path in changed_files if self._is_protected_path(path)]
        if protected_files:
            return GitUpdateResult(
                status="protected_path",
                message="Remote update contains protected config or secret paths; refusing to modify local config.",
                old_head=local_head,
                new_head=remote_head,
                remote_ref=remote_ref,
                changed_files=changed_files,
                protected_files=protected_files,
            )

        if self._is_ancestor(local_head, remote_head):
            merge = self._run(["merge", "--ff-only", remote_ref], timeout=120)
            if merge.returncode != 0:
                return GitUpdateResult(
                    status="error",
                    message=self._format_git_error("git merge --ff-only failed", merge),
                    old_head=local_head,
                    remote_ref=remote_ref,
                    changed_files=changed_files,
                )
            new_head = self._git_output(["rev-parse", "HEAD"])
            return GitUpdateResult(
                status="updated",
                message="Fast-forward update applied.",
                old_head=local_head,
                new_head=new_head,
                remote_ref=remote_ref,
                changed_files=changed_files,
            )

        if self._is_ancestor(remote_head, local_head):
            return GitUpdateResult(
                status="local_ahead",
                message="Local branch is ahead of the remote; no update was applied.",
                old_head=local_head,
                new_head=local_head,
                remote_ref=remote_ref,
            )

        return GitUpdateResult(
            status="diverged",
            message="Local branch and remote branch have diverged; manual merge is required.",
            old_head=local_head,
            new_head=remote_head,
            remote_ref=remote_ref,
            changed_files=changed_files,
        )

    def _repo_root(self) -> str:
        result = self._run(["rev-parse", "--show-toplevel"])
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    def _dirty_entries(self) -> List[str]:
        result = self._run(["status", "--porcelain"])
        if result.returncode != 0:
            return ["<unable to read git status>"]
        return [line for line in result.stdout.splitlines() if line.strip()]

    def _changed_files(self, old_head: str, new_head: str) -> List[str]:
        result = self._run(["diff", "--name-only", old_head, new_head])
        if result.returncode != 0:
            return []
        return [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self._run(["merge-base", "--is-ancestor", ancestor, descendant])
        return result.returncode == 0

    def _git_output(self, args: List[str]) -> str:
        result = self._run(args)
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    def _run(self, args: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.git_executable, *args],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    @staticmethod
    def _validate_ref_part(value: str, label: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError(f"{label} must not be empty")
        if value.startswith("-") or any(ch.isspace() for ch in value):
            raise ValueError(f"{label} contains unsafe characters: {value!r}")
        return value

    @staticmethod
    def _is_protected_path(path: str) -> bool:
        normalized = path.replace("\\", "/").lstrip("/")
        return any(fnmatch.fnmatch(normalized, pattern) for pattern in PROTECTED_PATH_PATTERNS)

    @staticmethod
    def _format_git_error(prefix: str, result: subprocess.CompletedProcess) -> str:
        detail = (result.stderr or result.stdout or "").strip()
        return f"{prefix}: {detail}" if detail else prefix
