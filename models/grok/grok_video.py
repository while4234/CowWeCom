# encoding:utf-8

"""Thin CowWeCom adapter for Hermes-derived Grok video generation."""

from __future__ import annotations

import re
from typing import Optional

from bridge.reply import Reply, ReplyType
from config import conf
from integrations.hermes_xai.video_gen import XAIVideoGenProvider, XaiVideoGenError


_GROK_VIDEO_PROVIDERS = {"xai", "grok"}
_IMAGE_REF_RE = re.compile(r"\[\s*(?:\u56fe\u7247|image)\s*:\s*([^\]]+?)\s*\]", re.IGNORECASE)
_RECENT_IMAGE_COUNT_RE = re.compile(r"(?:\u4e0a\u9762\u53d1\u7684|\u6700\u8fd1|\u4e0a\u9762|last)\s*(\d+)\s*(?:\u5f20|images?|pics?|pictures?)", re.IGNORECASE)
_REFERENCE_HINTS = (
    "\u53c2\u8003\u4e0a\u9762",
    "\u53c2\u8003\u8fd9\u5f20",
    "\u8fd9\u5f20\u56fe",
    "\u8fd9\u5f20\u56fe\u7247",
    "\u4e0a\u9762\u53d1\u7684",
    "\u56fe\u751f\u89c6\u9891",
    "\u628a\u56fe\u52a8\u8d77\u6765",
    "\u8ba9\u56fe\u52a8\u8d77\u6765",
    "this image",
    "this picture",
    "reference image",
    "image to video",
)


def is_grok_video_provider(provider: Optional[str] = None) -> bool:
    value = provider if provider is not None else conf().get("video_generation_provider")
    return str(value or "").strip().lower() in _GROK_VIDEO_PROVIDERS


def generate_reply(prompt: str, context=None, provider: Optional[XAIVideoGenProvider] = None) -> Reply:
    clean_prompt, image_refs, available_count, requested_count = _extract_prompt_and_image_refs(prompt, context)
    if requested_count and requested_count > available_count:
        return Reply(
            ReplyType.ERROR,
            f"只找到 {available_count} 张可参考图片，无法按请求使用 {requested_count} 张。请补发图片，或改成参考已有图片数量。",
        )
    if len(image_refs) > 7:
        return Reply(ReplyType.ERROR, "Grok 视频最多支持 7 张参考图片。请减少图片数量，或说明只参考最近 7 张以内。")
    if _looks_like_reference_video_request(prompt, context) and not image_refs:
        return Reply(
            ReplyType.ERROR,
            "这像是图生视频请求，但我没有找到可用图片。请先发送、引用或回复一张图片，再发送视频描述。",
        )

    image_url = image_refs[0] if len(image_refs) == 1 else None
    reference_image_urls = image_refs if len(image_refs) > 1 else None
    try:
        video_path = (provider or XAIVideoGenProvider()).generate(
            clean_prompt,
            image_url=image_url,
            reference_image_urls=reference_image_urls,
            model=conf().get("grok_video_model"),
            duration=conf().get("grok_video_duration"),
            aspect_ratio=conf().get("grok_video_aspect_ratio"),
            resolution=conf().get("grok_video_resolution"),
            timeout_seconds=conf().get("grok_video_timeout_seconds"),
            poll_interval_seconds=conf().get("grok_video_poll_interval_seconds"),
        )
    except XaiVideoGenError as exc:
        return Reply(ReplyType.ERROR, str(exc))
    reply = Reply(ReplyType.VIDEO, video_path)
    reply.cleanup_after_send = True
    reply.generated_media_path = video_path
    if context is not None:
        context["cleanup_after_send"] = True
        context["generated_media_paths"] = [video_path]
    return reply


def _extract_prompt_and_image_refs(prompt: str, context=None) -> tuple[str, list[str], int, Optional[int]]:
    content = str(prompt or "")
    refs = _unique_refs(_IMAGE_REF_RE.findall(content))
    if context is not None:
        context_content = str(getattr(context, "content", "") or "")
        refs = _unique_refs([*refs, *_IMAGE_REF_RE.findall(context_content)])
    requested_count = _requested_recent_count(content)
    selected_refs = _select_recent_refs(content, refs, requested_count)
    clean_prompt = _IMAGE_REF_RE.sub("", content).strip()
    return clean_prompt or content.strip(), selected_refs, len(refs), requested_count


def _unique_refs(values) -> list[str]:
    refs: list[str] = []
    for value in values or []:
        ref = str(value or "").strip().strip("'\"")
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _requested_recent_count(prompt: str) -> Optional[int]:
    match = _RECENT_IMAGE_COUNT_RE.search(str(prompt or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _select_recent_refs(prompt: str, refs: list[str], requested_count: Optional[int]) -> list[str]:
    if requested_count is None:
        return refs
    count = requested_count
    if count <= 0:
        return []
    return refs[-min(count, 7):]


def _looks_like_reference_video_request(prompt: str, context=None) -> bool:
    haystack = str(prompt or "")
    if context is not None:
        haystack += "\n" + str(getattr(context, "content", "") or "")
    lowered = haystack.lower()
    return any(hint.lower() in lowered for hint in _REFERENCE_HINTS)
