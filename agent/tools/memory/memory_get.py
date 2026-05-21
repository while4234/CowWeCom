"""
Memory get tool

Allows agents to read specific sections from memory files
"""

from pathlib import Path
from typing import Optional

from agent.tools.base_tool import BaseTool


class MemoryGetTool(BaseTool):
    """Tool for reading memory file contents"""
    
    name: str = "memory_get"
    description: str = (
        "Read specific content from memory files. "
        "Use this to get full context from a memory file or specific line range."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the memory file (e.g. 'MEMORY.md', 'memory/2026-01-01.md')"
            },
            "start_line": {
                "type": "integer",
                "description": "Starting line number (optional, default: 1)",
                "default": 1
            },
            "num_lines": {
                "type": "integer",
                "description": "Number of lines to read (optional, reads all if not specified)"
            }
        },
        "required": ["path"]
    }
    
    def __init__(
        self,
        memory_manager,
        user_id: Optional[str] = None,
        allow_shared_memory: bool = True,
    ):
        """
        Initialize memory get tool
        
        Args:
            memory_manager: MemoryManager instance
        """
        super().__init__()
        self.memory_manager = memory_manager
        self.user_id = user_id
        self.allow_shared_memory = allow_shared_memory

        from config import conf
        if conf().get("knowledge", True):
            self.description = (
                "Read specific content from memory or knowledge files. "
                "Use this to get full context from a memory file, knowledge page, or specific line range."
            )
            self.params = {**self.params}
            self.params["properties"] = {**self.params["properties"]}
            self.params["properties"]["path"] = {
                "type": "string",
                "description": "Relative path to the memory or knowledge file (e.g. 'MEMORY.md', 'memory/2026-01-01.md', 'knowledge/concepts/moe.md')"
            }
    
    def execute(self, args: dict):
        """
        Execute memory file read
        
        Args:
            args: Dictionary with path, start_line, num_lines
            
        Returns:
            ToolResult with file content
        """
        from agent.tools.base_tool import ToolResult
        
        path = args.get("path")
        start_line = args.get("start_line", 1)
        num_lines = args.get("num_lines")
        
        if not path:
            return ToolResult.fail("Error: path parameter is required")
        
        try:
            workspace_dir = self.memory_manager.config.get_workspace()
            
            path = self._normalize_visible_path(path)
            if path is None:
                return ToolResult.fail("Error: Access denied: memory belongs to another user or shared chat memory")
            
            file_path = (workspace_dir / path).resolve()
            workspace_resolved = workspace_dir.resolve()
            
            if not self._is_relative_to(file_path, workspace_resolved):
                return ToolResult.fail(f"Error: Access denied: path outside workspace")
            
            if not file_path.exists():
                return ToolResult.fail(f"Error: File not found: {path}")
            
            content = file_path.read_text(encoding='utf-8')
            lines = content.split('\n')
            
            # Handle line range
            if start_line < 1:
                start_line = 1
            
            start_idx = start_line - 1
            
            if num_lines:
                end_idx = start_idx + num_lines
                selected_lines = lines[start_idx:end_idx]
            else:
                selected_lines = lines[start_idx:]
            
            result = '\n'.join(selected_lines)
            
            # Add metadata
            total_lines = len(lines)
            shown_lines = len(selected_lines)
            
            output = [
                f"File: {path}",
                f"Lines: {start_line}-{start_line + shown_lines - 1} (total: {total_lines})",
                "",
                result
            ]
            
            return ToolResult.success('\n'.join(output))
            
        except Exception as e:
            return ToolResult.fail(f"Error reading memory file: {str(e)}")

    def _normalize_visible_path(self, path: str) -> Optional[str]:
        normalized = str(path).replace("\\", "/").lstrip("/")

        if self.allow_shared_memory:
            if self.user_id and normalized.startswith("memory/users/"):
                private_owner = normalized[len("memory/users/"):].split("/", 1)[0]
                if private_owner != self.user_id:
                    return None
            if (
                not normalized.startswith("memory/")
                and not normalized.startswith("knowledge/")
                and normalized != "MEMORY.md"
            ):
                return f"memory/{normalized}"
            return normalized

        if normalized.startswith("knowledge/"):
            return normalized

        if not self.user_id:
            return None

        own_prefix = f"memory/users/{self.user_id}/"
        if normalized.startswith(own_prefix):
            return normalized
        if normalized == "MEMORY.md":
            return f"{own_prefix}MEMORY.md"
        if normalized.startswith("memory/users/"):
            return None
        if normalized.startswith("memory/"):
            candidate = normalized[len("memory/"):]
        else:
            candidate = normalized
        if "/" in candidate:
            return None
        return f"{own_prefix}{candidate}"

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False
