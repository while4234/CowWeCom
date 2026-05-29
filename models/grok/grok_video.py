# encoding:utf-8

"""Thin CowWeCom adapter for Hermes-derived Grok video generation."""

from __future__ import annotations

import re
from typing import Optional

from bridge.reply import Reply, ReplyType
from channel.image_recognition import (
    MAX_VIDEO_REFERENCE_IMAGES,
    explicit_text_to_video_requested,
    explicit_video_reference_image_count,
    get_image_recognition_manager,
    requested_video_reference_image_count,
)
from config import conf
from integrations.hermes_xai.video_gen import XaiVideoGenError


_GROK_VIDEO_PROVIDERS = {"xai", "grok"}
_IMAGE_REF_RE = re.compile(r"\[\s*(?:\u56fe\u7247|image)\s*:\s*([^\]]+?)\s*\]", re.IGNORECASE)
_REFERENCE_HINTS = (
    "\u53c2\u8003\u4e0a\u56fe",
    "\u53c2\u8003\u4e0a\u9762",
    "\u53c2\u8003\u521a\u624d",
    "\u53c2\u8003\u8fd9\u5f20",
    "\u4e0a\u56fe",
    "\u4e0a\u9762\u51e0\u5f20",
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


def generate_reply(prompt: str, context=None, provider: Optional[object] = None) -> Reply:
    if provider is not None:
        return _generate_reply_sync(prompt, context, provider)

    clean_prompt, image_refs, available_count, requested_count = _extract_prompt_and_image_refs(prompt, context)
    if requested_count and requested_count > available_count:
        if available_count == 0:
            return Reply(
                ReplyType.ERROR,
                "没有找到可用图片，请先发送/上传图片后重试，或明确说明“文生视频”。",
            )
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

    try:
        profile = _resolve_background_profile(context)
        params = {
            "prompt": clean_prompt,
            "duration": conf().get("grok_video_duration") or 8,
            "resolution": conf().get("grok_video_resolution") or "720p",
        }
        if image_refs:
            params["image_url"] = image_refs[0] if len(image_refs) == 1 else image_refs
        else:
            params["aspect_ratio"] = conf().get("grok_video_aspect_ratio") or "16:9"

        from agent.tools.video_generation.job_manager import get_grok_video_generation_job_manager
        from bridge.bridge import Bridge

        manager = get_grok_video_generation_job_manager(Bridge().get_agent_bridge())
        job = manager.submit(params, context, profile)
        position = manager.queue_position(job)
        state = "已启动" if position == 0 else f"已排队（队列位置 {position}）"
        return Reply(ReplyType.TEXT, f"Grok 视频生成任务{state}，任务 {job.job_id}。\n后台生成完成后会回发视频。")
    except XaiVideoGenError as exc:
        return Reply(ReplyType.ERROR, str(exc))
    except Exception as exc:
        return Reply(ReplyType.ERROR, f"Grok 视频任务提交失败：{exc}")


def _generate_reply_sync(prompt: str, context, provider) -> Reply:
    clean_prompt, image_refs, available_count, requested_count = _extract_prompt_and_image_refs(prompt, context)
    if requested_count and requested_count > available_count:
        return Reply(ReplyType.ERROR, "没有找到足够的可用图片。")
    image_url = image_refs[0] if len(image_refs) == 1 else None
    reference_image_urls = image_refs if len(image_refs) > 1 else None
    try:
        video_path = provider.generate(
            clean_prompt,
            image_url=image_url,
            reference_image_urls=reference_image_urls,
            model=conf().get("grok_video_model"),
            duration=conf().get("grok_video_duration"),
            aspect_ratio=conf().get("grok_video_aspect_ratio") if not image_refs else None,
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
    if not refs and context is not None and not explicit_text_to_video_requested(content):
        refs = _unique_refs([*refs, *_recent_image_refs_for_context(context, content)])
    requested_count = _requested_recent_count(content)
    selected_refs = _select_recent_refs(content, refs, requested_count, context=context)
    clean_prompt = _IMAGE_REF_RE.sub("", content).strip()
    return clean_prompt or content.strip(), selected_refs, len(refs), requested_count


def _resolve_background_profile(context):
    if context is None:
        raise ValueError("missing chat context for background video task")
    try:
        profile = context.get("_actor_profile")
    except Exception:
        profile = getattr(context, "_actor_profile", None)
    if profile is not None:
        return profile
    from agent.user_profiles import apply_profile_to_context, resolve_agent_user_profile

    profile = resolve_agent_user_profile(context)
    apply_profile_to_context(context, profile)
    try:
        context["_actor_profile"] = profile
    except Exception:
        pass
    return profile


def _recent_image_refs_for_context(context, prompt: str) -> list[str]:
    try:
        session_id = str(context.get("session_id") or "").strip()
    except Exception:
        session_id = str(getattr(context, "session_id", "") or "").strip()
    if not session_id:
        return []
    try:
        requested_refs = requested_video_reference_image_count(prompt, max_refs=MAX_VIDEO_REFERENCE_IMAGES)
        return get_image_recognition_manager().recent_image_refs_for_session(
            session_id,
            limit=requested_refs,
        )
    except Exception:
        return []


def _unique_refs(values) -> list[str]:
    refs: list[str] = []
    for value in values or []:
        ref = str(value or "").strip().strip("'\"")
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _requested_recent_count(prompt: str) -> Optional[int]:
    return explicit_video_reference_image_count(prompt, max_refs=MAX_VIDEO_REFERENCE_IMAGES)


def _select_recent_refs(prompt: str, refs: list[str], requested_count: Optional[int], context=None) -> list[str]:
    if not refs:
        return []
    if requested_count is None:
        requested_count = 1
    count = requested_count
    if count <= 0:
        return []
    return refs[-min(count, MAX_VIDEO_REFERENCE_IMAGES):]


def _looks_like_reference_video_request(prompt: str, context=None) -> bool:
    haystack = str(prompt or "")
    if context is not None:
        haystack += "\n" + str(getattr(context, "content", "") or "")
    if explicit_text_to_video_requested(haystack):
        return False
    lowered = haystack.lower()
    return any(hint.lower() in lowered for hint in _REFERENCE_HINTS)
