"""
Tool access control and shared-resource leases for multi-user agent sessions.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Dict, Iterable, Optional, Tuple

from agent.tools.base_tool import BaseTool, ToolResult
from agent.user_profiles import AgentUserProfile
from common.utils import expand_path


class ResourceLeaseManager:
    """Small in-process lease table for scarce shared resources."""

    def __init__(self):
        self._lock = threading.RLock()
        self._leases: Dict[str, Tuple[str, float]] = {}

    def acquire(self, resource: str, owner: str, ttl_seconds: int) -> bool:
        now = time.time()
        expires_at = now + max(int(ttl_seconds or 0), 1)
        with self._lock:
            current = self._leases.get(resource)
            if current:
                current_owner, current_expires = current
                if current_expires > now and current_owner != owner:
                    return False
            self._leases[resource] = (owner, expires_at)
            return True

    def release_owner(self, owner: str) -> None:
        with self._lock:
            for resource, (lease_owner, _) in list(self._leases.items()):
                if lease_owner == owner:
                    self._leases.pop(resource, None)

    def owner(self, resource: str) -> Optional[str]:
        now = time.time()
        with self._lock:
            current = self._leases.get(resource)
            if not current:
                return None
            owner, expires_at = current
            if expires_at <= now:
                self._leases.pop(resource, None)
                return None
            return owner


_lease_manager = ResourceLeaseManager()


def get_resource_leases() -> ResourceLeaseManager:
    return _lease_manager


def _security_project_root() -> str:
    return os.path.realpath(os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))


def _norm_path(path: Any, cwd: str) -> str:
    value = expand_path(str(path))
    if os.path.isabs(value):
        return os.path.realpath(os.path.abspath(value))
    return os.path.realpath(os.path.abspath(os.path.join(cwd, value)))


def _case_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _is_within(path: str, root: str) -> bool:
    path = _case_path(path)
    root = _case_path(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _matches_any_root(path: str, roots: Iterable[str]) -> bool:
    return any(_is_within(path, root) for root in roots if root)


def _matches_any_file(path: str, files: Iterable[str]) -> bool:
    path = _case_path(path)
    return any(path == _case_path(file_path) for file_path in files if file_path)


class ToolAccessPolicy:
    """Per-profile access rules applied immediately before tool execution."""

    FILE_READ_TOOLS = {"read", "ls", "grep", "find", "send", "vision"}
    FILE_WRITE_TOOLS = {"write", "edit"}
    BLOCKED_WITHOUT_OPT_IN = {"bash", "terminal", "env_config"}
    ADMIN_ONLY_TOOLS = {"git_code_update"}
    PATH_KEYS = ("path", "location", "file_path", "image", "input_path", "output_path")
    SECURITY_CRITICAL_FILES = (
        "agent/access_control.py",
        "agent/user_profiles.py",
        "agent/tools/memory/memory_get.py",
        "agent/tools/memory/memory_search.py",
        "agent/tools/scheduler/scheduler_tool.py",
        "agent/tools/scheduler/task_store.py",
        "bridge/agent_initializer.py",
        "channel/web/web_channel.py",
        "config.py",
        "config-template.json",
    )

    def __init__(self, profile: AgentUserProfile):
        self.profile = profile

    def authorize(self, tool: BaseTool, args: dict) -> Tuple[bool, str]:
        name = getattr(tool, "name", "")
        args = args or {}

        if name == "browser":
            return self._authorize_browser()

        if not self.profile.is_admin and name in self.ADMIN_ONLY_TOOLS:
            return False, self._deny(f"普通用户不能使用 {name} 工具。")

        if not self.profile.is_admin and name in self.BLOCKED_WITHOUT_OPT_IN:
            if name == "bash" and self.profile.can_use_bash:
                return self._authorize_bash(args)
            if name == "env_config" and self.profile.can_use_env_config:
                return True, ""
            return False, self._deny(f"普通用户不能使用 {name} 工具。请改用受控文件、浏览器或搜索工具。")

        if not self.profile.is_admin and name in self.FILE_READ_TOOLS | self.FILE_WRITE_TOOLS:
            return self._authorize_paths(name, tool, args)

        return True, ""

    def prepare_args(self, tool: BaseTool, args: dict) -> dict:
        name = getattr(tool, "name", "")
        if self.profile.is_admin or name not in self.FILE_READ_TOOLS | self.FILE_WRITE_TOOLS:
            return args or {}

        prepared = dict(args or {})
        for key in self.PATH_KEYS:
            value = prepared.get(key)
            if isinstance(value, str):
                prepared[key] = self._rewrite_shared_relative_path(value)
            elif isinstance(value, list):
                prepared[key] = [
                    self._rewrite_shared_relative_path(item) if isinstance(item, str) else item
                    for item in value
                ]
        return prepared

    def snapshot_security_files(self, tool_name: str) -> Optional[Dict[str, Optional[bytes]]]:
        if self.profile.is_admin or tool_name != "bash":
            return None

        root = _security_project_root()
        snapshot: Dict[str, Optional[bytes]] = {}
        for rel_path in self.SECURITY_CRITICAL_FILES:
            path = os.path.join(root, *rel_path.split("/"))
            try:
                with open(path, "rb") as f:
                    snapshot[path] = f.read()
            except FileNotFoundError:
                snapshot[path] = None
            except OSError:
                snapshot[path] = None
        return snapshot

    def restore_changed_security_files(
        self,
        snapshot: Optional[Dict[str, Optional[bytes]]],
    ) -> Tuple[str, ...]:
        if not snapshot:
            return tuple()

        changed = []
        for path, before in snapshot.items():
            try:
                with open(path, "rb") as f:
                    after = f.read()
            except FileNotFoundError:
                after = None
            except OSError:
                after = None

            if after == before:
                continue

            if before is None:
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass
            else:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(before)
            changed.append(path)
        return tuple(changed)

    def _authorize_browser(self) -> Tuple[bool, str]:
        from config import conf

        ttl = int(conf().get("agent_browser_lock_timeout_seconds", 900) or 900)
        owner = self.profile.conversation_id
        leases = get_resource_leases()
        if leases.acquire("browser", owner, ttl):
            return True, ""
        return False, self._deny("浏览器工具正在被另一个用户使用，请稍后再试。")

    def _authorize_bash(self, args: dict) -> Tuple[bool, str]:
        command = str((args or {}).get("command", "") or "")
        if not command:
            return True, ""

        if not self.profile.can_delete_files and self._contains_delete_operation(command):
            return False, self._deny("普通用户不能通过 bash 删除文件或目录。")

        lowered = command.lower()
        sensitive_tokens = (
            ".env",
            "config.json",
            "config-template.json",
            ".cow",
            ".weixin_cow_credentials",
        )
        if any(token in lowered for token in sensitive_tokens):
            return False, self._deny("普通用户不能通过 bash 访问配置、凭据或环境变量文件")

        if self._contains_project_write_operation(command):
            return False, self._deny("普通用户不能通过 bash 修改项目代码或配置。")

        normalized_command = os.path.normcase(command)
        skills_root = os.path.join(self.profile.shared_workspace, "skills")
        protected_roots = [
            root for root in self.profile.denied_roots
            if root and not _is_within(skills_root, root)
        ]
        for protected in tuple(protected_roots) + tuple(self.profile.denied_files):
            if protected and os.path.normcase(str(protected)) in normalized_command:
                return False, self._deny(f"拒绝通过 bash 访问受保护路径: {protected}")

        return True, ""

    @staticmethod
    def _contains_delete_operation(command: str) -> bool:
        lowered = command.lower()
        patterns = (
            r"(^|[&|;\s])rm(\.exe)?\s+",
            r"(^|[&|;\s])del(\.exe)?\s+",
            r"(^|[&|;\s])erase(\.exe)?\s+",
            r"(^|[&|;\s])rd(\.exe)?\s+",
            r"(^|[&|;\s])rmdir(\.exe)?\s+",
            r"\bremove-item\b",
            r"\brm\s+-",
            r"\bos\.remove\s*\(",
            r"\bos\.unlink\s*\(",
            r"\bshutil\.rmtree\s*\(",
            r"\.unlink\s*\(",
        )
        return any(re.search(pattern, lowered) for pattern in patterns)

    def _contains_project_write_operation(self, command: str) -> bool:
        lowered = command.lower()
        write_verbs = (
            ">",
            ">>",
            "copy ",
            "xcopy ",
            "move ",
            "ren ",
            "rename ",
            "set-content",
            "add-content",
            "out-file",
            "new-item",
            "write_text",
            "write_bytes",
            "open(",
        )
        if not any(verb in lowered for verb in write_verbs):
            return False

        normalized_command = os.path.normcase(command)
        for denied_root in self.profile.denied_roots:
            normalized_root = os.path.normcase(os.path.realpath(os.path.abspath(str(denied_root))))
            if normalized_root and normalized_root in normalized_command:
                return True

        project_markers = (
            "agent\\",
            "agent/",
            "bridge\\",
            "bridge/",
            "channel\\",
            "channel/",
            "common\\",
            "common/",
            "models\\",
            "models/",
            "plugins\\",
            "plugins/",
            "tests\\",
            "tests/",
            "config.py",
            "config-template.json",
            "pyproject.toml",
        )
        return any(marker in lowered for marker in project_markers)

    def _authorize_paths(self, tool_name: str, tool: BaseTool, args: dict) -> Tuple[bool, str]:
        paths = self._extract_paths(args)
        if not paths:
            return True, ""

        cwd = getattr(tool, "cwd", None) or getattr(tool, "config", {}).get("cwd") or self.profile.tool_workspace
        for raw_path in paths:
            if not raw_path or self._is_url(raw_path):
                continue
            resolved = _norm_path(raw_path, cwd)
            if self._is_denied(resolved):
                return False, self._deny(f"拒绝访问受保护路径: {raw_path}")
            roots = self.profile.writable_roots if tool_name in self.FILE_WRITE_TOOLS else self.profile.readable_roots
            if roots and not _matches_any_root(resolved, roots):
                return False, self._deny(
                    f"普通用户只能在自己的工作区内访问文件: {self.profile.tool_workspace}"
                )
        return True, ""

    def _extract_paths(self, args: dict) -> Tuple[str, ...]:
        paths = []
        for key in self.PATH_KEYS:
            value = args.get(key)
            if isinstance(value, str):
                paths.append(value)
            elif isinstance(value, list):
                paths.extend(item for item in value if isinstance(item, str))
        return tuple(paths)

    def _rewrite_shared_relative_path(self, raw_path: str) -> str:
        if not raw_path or self._is_url(raw_path) or os.path.isabs(expand_path(raw_path)):
            return raw_path

        rel = raw_path.replace("\\", "/").lstrip("/")
        if rel.startswith("knowledge/"):
            return os.path.join(self.profile.shared_workspace, *rel.split("/"))

        own_prefix = f"memory/users/{self.profile.memory_user_id}/"
        if rel == "MEMORY.md":
            return os.path.join(
                self.profile.shared_workspace,
                "memory",
                "users",
                self.profile.memory_user_id,
                "MEMORY.md",
            )
        if rel.startswith(own_prefix):
            return os.path.join(self.profile.shared_workspace, *rel.split("/"))
        if rel.startswith("memory/users/"):
            return os.path.join(self.profile.shared_workspace, *rel.split("/"))
        if rel.startswith("memory/"):
            private_rel = rel[len("memory/"):]
            return os.path.join(
                self.profile.shared_workspace,
                "memory",
                "users",
                self.profile.memory_user_id,
                *private_rel.split("/"),
            )
        return raw_path

    def _is_denied(self, path: str) -> bool:
        return (
            _matches_any_file(path, self.profile.denied_files)
            or _matches_any_root(path, self.profile.denied_roots)
        )

    @staticmethod
    def _is_url(value: str) -> bool:
        lowered = value.lower()
        return lowered.startswith("http://") or lowered.startswith("https://")

    @staticmethod
    def _deny(message: str) -> str:
        return f"权限拒绝: {message}"


class GuardedTool(BaseTool):
    """Transparent wrapper that keeps a tool schema unchanged and gates calls."""

    def __init__(self, inner: BaseTool, policy: ToolAccessPolicy):
        super().__init__()
        self.inner = inner
        self.policy = policy
        self.name = inner.name
        self.description = inner.description
        self.params = inner.params
        self.stage = getattr(inner, "stage", self.stage)
        self.model = getattr(inner, "model", None)

    def execute(self, params: dict) -> ToolResult:
        prepared = self.policy.prepare_args(self.inner, params or {})
        allowed, message = self.policy.authorize(self.inner, prepared)
        if not allowed:
            return ToolResult.fail(message)
        self.inner.model = getattr(self, "model", getattr(self.inner, "model", None))
        if hasattr(self, "context"):
            self.inner.context = self.context
        snapshot = self.policy.snapshot_security_files(self.name)
        try:
            result = self.inner.execute_tool(prepared)
        finally:
            changed = self.policy.restore_changed_security_files(snapshot)
        if changed:
            return ToolResult.fail(self.policy._deny("normal-user bash cannot modify security policy files"))
        return result

    def should_auto_execute(self, context) -> bool:
        return self.inner.should_auto_execute(context)

    def close(self):
        return self.inner.close()

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)
