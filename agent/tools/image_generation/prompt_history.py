from __future__ import annotations

from typing import Any, Dict

from agent.tools.base_tool import BaseTool, ToolResult
from common.image_prompt_enhancer import load_prompt_history
from common.log import logger
from common.utils import expand_path
from config import conf


class ImageGenerationPromptHistoryTool(BaseTool):
    """Reveal hidden media-generation prompts only on explicit user request."""

    name = "image_generation_prompt_history"
    description = (
        "Return the hidden enhanced prompt used for a recent image or Grok video generation task. "
        "Use this only when the user explicitly asks to see, inspect, copy, or debug "
        "the prompt used for a generated image/video. Do not call it during normal media "
        "generation or ordinary follow-up chat."
    )
    params = {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "Optional image/video generation task id. Omit to get the most recent prompt in this chat.",
            },
            "limit": {
                "type": "integer",
                "description": "Optional number of recent prompts to return, default 1.",
            },
            "exact_only": {
                "type": "boolean",
                "description": "Return only the prompt text. By default this is translated to Chinese for display; set raw=true for the original stored prompt.",
            },
            "raw": {
                "type": "boolean",
                "description": "Return the original stored enhanced prompt without Chinese translation.",
            },
            "original": {
                "type": "boolean",
                "description": "Alias for raw=true.",
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
            return ToolResult.fail("No hidden media prompt was found for the recent generation tasks in this chat.")

        translate = not _truthy(params.get("raw") or params.get("original"))
        if _truthy(params.get("exact_only")):
            prompts = [
                _display_prompt_text(record, translate=translate, model=getattr(self, "model", None))
                for record in records
            ]
            return ToolResult.success("\n\n---\n\n".join(prompt for prompt in prompts if prompt))

        sections = []
        for record in records:
            prompt_text = _display_prompt_text(record, translate=translate, model=getattr(self, "model", None))
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
            section_lines = [
                f"Task ID: {record.get('job_id') or 'unknown'}",
                f"Generation status: {record.get('generation_status')}" if record.get("generation_status") else "",
                f"Model target: {record.get('target') or 'unknown'}",
                f"Media type: {record.get('media_type') or 'image'}",
                f"Use case: {record.get('use_case') or 'unknown'}",
                source_label,
                template_text,
                "Chinese prompt:" if translate else "Enhanced prompt:",
                "```text",
                prompt_text,
                "```",
            ]
            sections.append("\n".join(line for line in section_lines if line))
        return ToolResult.success("\n\n---\n\n".join(sections))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "raw", "exact"}


def _display_prompt_text(record: Dict[str, Any], *, translate: bool, model: Any = None) -> str:
    prompt = str(record.get("enhanced_prompt") or "")
    if not translate or not prompt:
        return prompt
    translated = _translate_prompt_to_chinese(prompt, record, model=model)
    return translated or prompt


def _translate_prompt_to_chinese(prompt: str, record: Dict[str, Any], *, model: Any = None) -> str:
    if _should_use_grok_translation(record):
        return (
            _translate_prompt_with_grok(prompt)
            or _translate_prompt_with_attached_model(prompt, model)
            or _translate_prompt_with_bridge(prompt)
            or ""
        )
    return _translate_prompt_with_attached_model(prompt, model) or _translate_prompt_with_bridge(prompt) or ""


def _should_use_grok_translation(record: Dict[str, Any]) -> bool:
    target = str(record.get("target") or "").strip().lower()
    version = str(record.get("version") or "").strip().lower()
    runtime = str(record.get("runtime") or "").strip().lower()
    return target == "grok" or version.startswith("grok-model-rewrite") or "grok" in runtime


def _translate_prompt_with_grok(prompt: str) -> str:
    try:
        from models.grok.grok_bot import GrokBot

        return _call_translation_model(GrokBot(), prompt, request_kind="grok_prompt_history_translation")
    except Exception as exc:
        logger.warning("[PromptHistory] Grok prompt translation failed: %s", exc)
        return ""


def _translate_prompt_with_attached_model(prompt: str, model: Any) -> str:
    if model is None or not hasattr(model, "call_with_tools"):
        return ""
    try:
        return _call_translation_model(model, prompt, request_kind="prompt_history_translation")
    except Exception as exc:
        logger.warning("[PromptHistory] prompt translation failed: %s", exc)
        return ""


def _translate_prompt_with_bridge(prompt: str) -> str:
    try:
        from bridge.bridge import Bridge

        response = Bridge().fetch_translate(prompt, from_lang="", to_lang="zh")
        return _clean_translated_prompt(getattr(response, "content", response))
    except Exception as exc:
        logger.warning("[PromptHistory] bridge prompt translation failed: %s", exc)
        return ""


def _call_translation_model(model: Any, prompt: str, *, request_kind: str) -> str:
    system = (
        "Translate hidden image/video generation prompts into Chinese for display. "
        "Preserve quoted text exactly, and keep model names, file paths, aspect ratios, "
        "numbers, parameter names, and code-like tokens unchanged. Return only the Chinese translation."
    )
    user_prompt = "Translate this prompt into Chinese:\n\n" + str(prompt or "")
    response = model.call_with_tools(
        messages=[{"role": "user", "content": user_prompt}],
        tools=None,
        stream=False,
        system=system,
        temperature=0,
        max_tokens=2000,
        max_output_tokens=2000,
        request_timeout=60,
        cache_shape_metadata={"request_kind": request_kind},
    )
    return _clean_translated_prompt(_extract_text_response(response))


def _extract_text_response(response: Any) -> str:
    if isinstance(response, str):
        return response
    if not isinstance(response, dict):
        return ""
    if response.get("error"):
        raise RuntimeError(str(response.get("message") or "prompt translation model call failed"))
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            return _extract_content_text(message.get("content"))
    for key in ("content", "text", "message", "result"):
        if response.get(key):
            return _extract_content_text(response.get(key))
    return ""


def _extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
        return "".join(parts)
    return str(content or "")


def _clean_translated_prompt(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            text = "\n".join(lines[1:-1]).strip()
    return text
