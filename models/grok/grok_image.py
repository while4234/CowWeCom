# encoding:utf-8

"""Thin CowWeCom adapter for Hermes-derived Grok image generation."""

from __future__ import annotations

from typing import Optional

from bridge.reply import Reply, ReplyType
from common.image_prompt_enhancer import record_prompt_history
from common.utils import expand_path
from config import conf
from integrations.hermes_xai.image_gen import XAIImageGenProvider, XaiImageGenError


_GROK_IMAGE_PROVIDERS = {"xai", "grok"}


def is_grok_image_provider(provider: Optional[str] = None) -> bool:
    value = provider if provider is not None else conf().get("text_to_image")
    return str(value or "").strip().lower() in _GROK_IMAGE_PROVIDERS


def generate_reply(prompt: str, context=None, provider: Optional[XAIImageGenProvider] = None) -> Reply:
    image_provider = provider or XAIImageGenProvider()
    try:
        image_path = image_provider.generate(prompt)
    except XaiImageGenError as exc:
        return Reply(ReplyType.ERROR, str(exc))
    reply = Reply(ReplyType.IMAGE, image_path)
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
        return
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
