from __future__ import annotations

from typing import Any, Dict

from agent.tools.base_tool import BaseTool, ToolResult
from common.image_prompt_enhancer import load_prompt_history
from common.utils import expand_path
from config import conf


class ImageGenerationPromptHistoryTool(BaseTool):
    """Reveal hidden image-generation prompts only on explicit user request."""

    name = "image_generation_prompt_history"
    description = (
        "Return the hidden enhanced prompt used for a recent image generation task. "
        "Use this only when the user explicitly asks to see, inspect, copy, or debug "
        "the prompt used for a generated image. Do not call it during normal image "
        "generation or ordinary follow-up chat."
    )
    params = {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "Optional image generation task id. Omit to get the most recent prompt in this chat.",
            },
            "limit": {
                "type": "integer",
                "description": "Optional number of recent prompts to return, default 1.",
            },
        },
    }

    def __init__(self, config: dict | None = None):
        super().__init__()
        self.config = config or {}
        self.current_context = None
        self.profile = None

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        params = dict(params or {})
        profile = self.profile
        if profile is None:
            return ToolResult.fail("Missing user profile; cannot read image prompt history.")

        workspace_root = str(
            getattr(profile, "shared_workspace", "")
            or self.config.get("workspace_root")
            or expand_path(conf().get("agent_workspace", "~/cow"))
        )
        memory_user_id = str(getattr(profile, "memory_user_id", "") or "")
        if not memory_user_id:
            return ToolResult.fail("Missing user identity; cannot read image prompt history.")

        session_id = ""
        if self.current_context is not None:
            try:
                session_id = str(self.current_context.get("session_id", "") or "")
            except Exception:
                session_id = ""

        records = load_prompt_history(
            workspace_root=workspace_root,
            memory_user_id=memory_user_id,
            session_id=session_id,
            job_id=str(params.get("job_id") or "").strip(),
            limit=int(params.get("limit") or 1),
        )
        if not records:
            return ToolResult.fail("No hidden image prompt was found for the recent image generation tasks in this chat.")

        sections = []
        for record in records:
            templates = record.get("templates") or []
            template_lines = []
            for item in templates[:3]:
                title = item.get("title") or ""
                category = item.get("category_slug") or ""
                template_id = item.get("id") or ""
                template_lines.append(f"- {category} #{template_id}: {title}".strip())
            if str(record.get("version") or "").startswith("grok-model-rewrite"):
                source_label = "Prompt rewrite source:"
                template_text = "- Grok text model with image-prompt-optimization rewrite templates"
            else:
                source_label = "Matched library templates:"
                template_text = "\n".join(template_lines) if template_lines else "- none"
            sections.append(
                "\n".join(
                    [
                        f"Task ID: {record.get('job_id') or 'unknown'}",
                        f"Model target: {record.get('target') or 'unknown'}",
                        f"Use case: {record.get('use_case') or 'unknown'}",
                        source_label,
                        template_text,
                        "Enhanced prompt:",
                        "```text",
                        str(record.get("enhanced_prompt") or ""),
                        "```",
                    ]
                )
            )
        return ToolResult.success("\n\n---\n\n".join(sections))
