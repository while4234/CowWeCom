# encoding:utf-8

"""Thin CowWeCom adapter for Hermes-derived Grok image generation."""

from __future__ import annotations

from typing import Optional

from bridge.reply import Reply, ReplyType
from config import conf
from integrations.hermes_xai.image_gen import XAIImageGenProvider, XaiImageGenError


_GROK_IMAGE_PROVIDERS = {"xai", "grok"}


def is_grok_image_provider(provider: Optional[str] = None) -> bool:
    value = provider if provider is not None else conf().get("text_to_image")
    return str(value or "").strip().lower() in _GROK_IMAGE_PROVIDERS


def generate_reply(prompt: str, context=None, provider: Optional[XAIImageGenProvider] = None) -> Reply:
    try:
        image_path = (provider or XAIImageGenProvider()).generate(prompt)
    except XaiImageGenError as exc:
        return Reply(ReplyType.ERROR, str(exc))
    reply = Reply(ReplyType.IMAGE, image_path)
    reply.cleanup_after_send = True
    reply.generated_media_path = image_path
    if context is not None:
        context["cleanup_after_send"] = True
        context["generated_media_paths"] = [image_path]
    return reply
