# encoding:utf-8

"""Thin CowWeCom adapter for Hermes-derived Grok image generation."""

from __future__ import annotations

import time
from typing import Optional

from bridge.reply import Reply, ReplyType
from common.image_prompt_enhancer import record_prompt_history
from common.utils import expand_path
from config import conf
from integrations.hermes_xai.image_gen import XAIImageGenProvider, XaiImageGenError
from models.grok.grok_image_options import (
    extract_image_references,
    looks_like_grok_image_to_image_request,
    resolve_grok_image_options,
    strip_image_references,
)


_GROK_IMAGE_PROVIDERS = {"xai", "grok"}


def is_grok_image_provider(provider: Optional[str] = None) -> bool:
    value = provider if provider is not None else conf().get("text_to_image")
    return str(value or "").strip().lower() in _GROK_IMAGE_PROVIDERS


def generate_reply(prompt: str, context=None, provider: Optional[XAIImageGenProvider] = None) -> Reply:
    image_provider = provider or XAIImageGenProvider()
    prompt_text = str(prompt or "").strip()
    image_refs = extract_image_references(prompt_text)
    image_url = image_refs[0] if image_refs else None
    generation_prompt = strip_image_references(prompt_text) if image_url else prompt_text
    if not image_url and looks_like_grok_image_to_image_request(prompt_text):
        return Reply(ReplyType.ERROR, "这是图生图/修图请求，请先上传一张图片，或引用一张图片后再发送修改说明。")

    try:
        options = resolve_grok_image_options(
            prompt=generation_prompt,
            image_url=image_url,
        )
        image_path = image_provider.generate(
            generation_prompt,
            image_url=options.image_url,
            aspect_ratio=options.aspect_ratio,
            resolution=options.resolution,
            model=options.model,
            prompt_enhancement=True,
        )
    except XaiImageGenError as exc:
        return Reply(ReplyType.ERROR, str(exc))
    except ValueError as exc:
        return Reply(ReplyType.ERROR, str(exc))
    reply = Reply(ReplyType.IMAGE_URL, f"file://{image_path}")
    reply.cleanup_after_send = True
    reply.generated_media_path = image_path
    if context is not None:
        context["cleanup_after_send"] = True
        context["generated_media_paths"] = [image_path]
        _record_direct_prompt_history(context, image_path, image_provider)
    return reply


def _record_direct_prompt_history(context, image_path: str, provider: XAIImageGenProvider) -> None:
    metadata = getattr(provider, "last_prompt_metadata", None)
    if not metadata:
        metadata = _fallback_prompt_metadata(context, image_path)
    try:
        workspace_root = expand_path(conf().get("agent_workspace", "~/cow"))
        memory_user_id = str(context.get("memory_user_id") or context.get("session_id") or "direct-grok")
        record_prompt_history(
            workspace_root=workspace_root,
            memory_user_id=memory_user_id,
            session_id=str(context.get("session_id") or ""),
            job_id="direct-grok",
            output_path=image_path,
            metadata=metadata,
        )
    except Exception:
        return


def _fallback_prompt_metadata(context, image_path: str) -> dict:
    prompt = str(context.get("content") or "").strip() if context is not None else ""
    return {
        "version": "grok-direct-fallback-v1",
        "enhanced": False,
        "disabled_reason": "provider_metadata_missing",
        "target": "grok",
        "media_type": "image",
        "runtime": "grok_direct",
        "original_prompt": prompt,
        "enhanced_prompt": prompt,
        "output_path": image_path,
        "created_at": time.time(),
    }
