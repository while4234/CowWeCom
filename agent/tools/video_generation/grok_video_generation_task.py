from __future__ import annotations

import re
from typing import Any, Dict

from agent.tools.base_tool import BaseTool, ToolResult
from channel.image_recognition import (
    MAX_VIDEO_REFERENCE_IMAGES,
    explicit_text_to_video_requested,
    explicit_video_reference_image_count,
    get_image_recognition_manager,
    requested_video_reference_image_count,
)
from common.image_generation_routing import looks_like_media_generation_status_question


MAX_IMAGE_REFERENCES = MAX_VIDEO_REFERENCE_IMAGES


class GrokVideoGenerationTaskTool(BaseTool):
    """Create a background Grok video generation job and return immediately."""

    name = "grok_video_generation_task"
    description = (
        "Start a Grok/xAI video generation task in the background. Use this for video "
        "generation, text-to-video, image-to-video, or multi-image reference video "
        "requests in CowAgent. The tool returns a job id immediately; the background "
        "worker sends the final video back to the current chat when finished."
    )
    params = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Video generation prompt. Include subject, action, camera motion, style, duration, and constraints.",
            },
            "image_url": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
                "description": "Optional input image reference path(s) or URL(s) for image-to-video. At most 7 references are used.",
            },
            "aspect_ratio": {
                "type": "string",
                "description": "Optional aspect ratio hint, e.g. 16:9, 9:16, 1:1.",
            },
            "duration": {
                "type": "string",
                "description": "Optional duration hint, e.g. 5s, 10s, short.",
            },
            "resolution": {
                "type": "string",
                "description": "Optional resolution hint, e.g. 480p or 720p.",
            },
            "quality": {
                "type": "string",
                "description": "Optional quality hint, e.g. speed, fast, quality, high, auto.",
            },
        },
        "required": ["prompt"],
    }

    IMAGE_REF_RE = re.compile(r"\[\s*(?:\u56fe\u7247|image)\s*:\s*([^\]]+?)\s*\]", re.IGNORECASE)
    IMAGE_TO_VIDEO_HINTS = (
        "this image",
        "this picture",
        "input image",
        "reference image",
        "image to video",
        "animate this",
        "animate the image",
        "\u53c2\u8003\u4e0a\u56fe",
        "\u53c2\u8003\u4e0a\u9762",
        "\u53c2\u8003\u521a\u624d",
        "\u4e0a\u9762\u51e0\u5f20",
        "\u4e0a\u9762\u53d1\u7684",
        "\u521a\u624d\u53d1\u7684",
        "\u6700\u8fd1\u51e0\u5f20",
        "\u8fd9\u5f20\u56fe",
        "\u8fd9\u5f20\u56fe\u7247",
        "\u53c2\u8003\u56fe",
        "\u56fe\u751f\u89c6\u9891",
        "\u52a8\u8d77\u6765",
        "\u8ba9\u5b83\u52a8",
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
            return ToolResult.fail("Missing prompt; cannot create Grok video generation task.")
        if looks_like_media_generation_status_question(prompt):
            return ToolResult.fail(
                "Grok video generation was not started because the current request looks like a status or failure question, not a new video request."
            )
        if self.job_manager is None:
            return ToolResult.fail("Grok video background task system is not initialized.")
        if self.current_context is None or self.profile is None:
            return ToolResult.fail("Missing chat context; cannot send Grok video generation results back.")

        image_refs = self._select_image_refs_for_prompt(prompt, self._extract_available_image_refs(prompt))
        if not params.get("image_url") and image_refs:
            params["image_url"] = image_refs[0] if len(image_refs) == 1 else image_refs
        params["image_url"] = self._normalize_image_refs(params.get("image_url"), prompt=prompt)
        if not params.get("image_url"):
            params.pop("image_url", None)
            if self._looks_like_image_to_video_request(prompt):
                return ToolResult.fail(
                    "This looks like an image-to-video request, but no input image was found. "
                    "Please send an image first or reply to/quote an image, then send the video instruction."
                )

        try:
            job = self.job_manager.submit(params, self.current_context, self.profile)
            position = self.job_manager.queue_position(job)
            state = "started" if position == 0 else f"queued at position {position}"
            return ToolResult.success(
                f"Grok video generation task {state}. Task ID: {job.job_id}. "
                "I will send the video to the current chat when it finishes."
            )
        except Exception as e:
            return ToolResult.fail(f"Failed to create Grok video generation task: {e}")

    def _extract_context_image_refs(self) -> list[str]:
        content = getattr(self.current_context, "content", "") if self.current_context is not None else ""
        refs: list[str] = []
        for match in self.IMAGE_REF_RE.finditer(str(content or "")):
            ref = match.group(1).strip()
            if ref and ref not in refs:
                refs.append(ref)
        return refs

    def _extract_available_image_refs(self, prompt: str) -> list[str]:
        refs = self._extract_context_image_refs()
        if refs or explicit_text_to_video_requested(prompt):
            return refs
        session_id = ""
        if self.current_context is not None:
            try:
                session_id = str(self.current_context.get("session_id") or "").strip()
            except Exception:
                session_id = str(getattr(self.current_context, "session_id", "") or "").strip()
        if not session_id:
            return refs
        try:
            requested_refs = requested_video_reference_image_count(prompt, max_refs=MAX_IMAGE_REFERENCES)
            recent = get_image_recognition_manager().recent_image_refs_for_session(
                session_id,
                limit=requested_refs,
            )
        except Exception:
            recent = []
        for ref in recent:
            if ref and ref not in refs:
                refs.append(ref)
        return refs

    def _normalize_image_refs(self, value: Any, prompt: str = "") -> str | list[str] | None:
        if isinstance(value, str):
            refs = [value.strip()] if value.strip() else []
        elif isinstance(value, (list, tuple)):
            refs = []
            for item in value:
                ref = str(item or "").strip()
                if ref and ref not in refs:
                    refs.append(ref)
                if len(refs) >= MAX_IMAGE_REFERENCES:
                    break
        else:
            refs = []
        refs = self._select_image_refs_for_prompt(prompt, refs)
        if not refs:
            return None
        return refs[0] if len(refs) == 1 else refs

    def _select_image_refs_for_prompt(self, prompt: str, refs: list[str]) -> list[str]:
        if not refs:
            return []
        requested_count = explicit_video_reference_image_count(prompt, max_refs=MAX_IMAGE_REFERENCES)
        if requested_count is None:
            requested_count = 1
        return refs[-min(requested_count, MAX_IMAGE_REFERENCES):]

    def _looks_like_image_to_video_request(self, prompt: str) -> bool:
        haystack_parts = [prompt]
        if self.current_context is not None:
            haystack_parts.append(str(getattr(self.current_context, "content", "") or ""))
        haystack = "\n".join(haystack_parts).lower()
        return any(hint.lower() in haystack for hint in self.IMAGE_TO_VIDEO_HINTS)
