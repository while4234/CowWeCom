from __future__ import annotations

import re
from typing import Any, Dict

from agent.tools.base_tool import BaseTool, ToolResult
from bridge.context import ContextType
from common.image_generation_routing import explicit_image_generation_requested, looks_like_media_generation_status_question


class ImageGenerationTaskTool(BaseTool):
    """Create a background image generation job and return immediately."""

    name = "image_generation_task"
    description = (
        "Start an AI image generation/editing task in the background. Use this for all image "
        "generation requests in CowAgent runtime, but only when the user explicitly asks to "
        "generate, draw, create, edit, or fuse images. Do not use this tool for quoted/sent "
        "image questions such as identifying, explaining, reading, searching, or analyzing an "
        "image. It immediately returns a job id and does not wait for the image. The background "
        "worker will send the generated image back to the current chat when finished. Do not run "
        "scripts/generate.py manually in the chat turn."
    )
    params = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Image generation or editing prompt. Include style, composition, constraints, and text requirements.",
            },
            "size": {
                "type": "string",
                "description": "Optional size tier or pixel value, e.g. 1K, 2K, 4K, 1024x1024.",
            },
            "aspect_ratio": {
                "type": "string",
                "description": "Optional aspect ratio, e.g. 1:1, 3:2, 2:3, 16:9, 9:16.",
            },
            "quality": {
                "type": "string",
                "description": (
                    "Optional quality hint: low, medium, high, speed, quality, or auto. "
                    "This never selects Grok/xAI by itself; it only tunes the selected runtime."
                ),
            },
            "runtime": {
                "type": "string",
                "description": (
                    "Optional provider runtime. Leave omitted by default; the background worker follows the active "
                    "model backend, so Grok backend users generate images with Grok and GPT backend users use the "
                    "default Codex/GPT image runtime. Pass codex_auth only when a Grok-backend user explicitly asks "
                    "for GPT/OpenAI/Codex image generation. Pass grok when the user explicitly asks for Grok, xAI, "
                    "X.ai, or Grok account image generation. Do not switch runtime just because the user asks for "
                    "quality, speed, high quality, or a quick draft."
                ),
            },
            "image_url": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
                "description": "Optional input image URL(s) or local path(s) for image editing. Pass a list for multi-image fusion.",
            },
        },
        "required": ["prompt"],
    }

    IMAGE_REF_RE = re.compile(r"\[\s*(?:\u56fe\u7247|image)\s*:\s*([^\]]+?)\s*\]", re.IGNORECASE)
    IMAGE_REF_HINTS = (
        "\u8fd9\u5f20\u56fe",
        "\u8fd9\u5f20\u56fe\u7247",
        "\u539f\u56fe",
        "\u53c2\u8003\u56fe",
        "this image",
        "this picture",
        "input image",
        "reference image",
    )
    IMAGE_EDIT_ACTIONS = (
        "\u56fe\u751f\u56fe",
        "\u4ee5\u56fe\u751f\u56fe",
        "\u4fee\u56fe",
        "\u6539\u56fe",
        "\u7f16\u8f91\u56fe",
        "\u6362\u80cc\u666f",
        "\u53bb\u80cc\u666f",
        "\u62a0\u56fe",
        "\u878d\u5408",
        "\u5408\u6210",
        "\u6539\u6210",
        "\u53d8\u6210",
        "\u6362\u6210",
        "\u66ff\u6362",
        "\u4fdd\u7559",
        "edit the image",
        "modify the image",
        "replace",
        "preserve",
    )

    def __init__(self, config: dict | None = None):
        super().__init__()
        self.config = config or {}
        self.job_manager = None
        self.current_context = None
        self.profile = None

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        params = dict(params or {})
        prompt = str(params.get("prompt", "")).strip()
        if not prompt:
            return ToolResult.fail("Missing prompt; cannot create image generation task.")
        if self.job_manager is None:
            return ToolResult.fail("Image generation background task system is not initialized.")
        if self.current_context is None or self.profile is None:
            return ToolResult.fail("Missing chat context; cannot send image generation results back.")
        if not self._has_explicit_generation_intent(prompt):
            return ToolResult.fail(
                "Image generation was not started because the current request does not explicitly ask "
                "to generate, draw, create, edit, or fuse an image."
            )

        image_refs = self._extract_context_image_refs()
        if not params.get("image_url") and image_refs:
            params["image_url"] = image_refs[0] if len(image_refs) == 1 else image_refs
        if not params.get("image_url") and self._looks_like_image_edit_request(prompt):
            return ToolResult.fail(
                "This looks like an image editing request, but no input image was found. "
                "Please send an image first or reply to/quote an image, then send the edit instruction."
            )

        try:
            job = self.job_manager.submit(params, self.current_context, self.profile)
            position = self.job_manager.queue_position(job)
            state = "started" if position == 0 else f"queued at position {position}"
            return ToolResult.success(
                f"Image generation task {state}. Task ID: {job.job_id}. "
                "I will send the image to the current chat when it finishes."
            )
        except Exception as e:
            return ToolResult.fail(f"Failed to create image generation task: {e}")

    def _extract_context_image_refs(self) -> list[str]:
        content = getattr(self.current_context, "content", "") if self.current_context is not None else ""
        refs: list[str] = []
        for match in self.IMAGE_REF_RE.finditer(str(content or "")):
            ref = match.group(1).strip()
            if ref and ref not in refs:
                refs.append(ref)
        return refs

    def _looks_like_image_edit_request(self, prompt: str) -> bool:
        haystack_parts = [prompt]
        if self.current_context is not None:
            haystack_parts.append(str(getattr(self.current_context, "content", "") or ""))
        haystack = "\n".join(haystack_parts).lower()
        if any(action.lower() in haystack for action in self.IMAGE_EDIT_ACTIONS):
            return True
        has_ref = any(hint.lower() in haystack for hint in self.IMAGE_REF_HINTS)
        if has_ref and re.search(r"(改|换|变|替换|去掉|去除|保留|融合|合成|edit|modify|change)", haystack):
            return True
        explicit_image = re.search(r"\b(image|picture|photo)\b", haystack)
        edit_action = re.search(r"\b(edit|modify|change|replace|keep|preserve)\b", haystack)
        return bool(explicit_image and edit_action)

    def _has_explicit_generation_intent(self, prompt: str) -> bool:
        if looks_like_media_generation_status_question(prompt):
            return False
        if getattr(self.current_context, "type", None) == ContextType.IMAGE_CREATE:
            return True
        if explicit_image_generation_requested(prompt):
            return True
        return self._looks_like_image_edit_request(prompt)
