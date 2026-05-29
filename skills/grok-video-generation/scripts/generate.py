from __future__ import annotations

import inspect
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict


MAX_IMAGE_REFERENCES = 7
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v"}
_DETACHED_COWWECOM_LOG_STREAMS = []


def main(argv: list[str]) -> int:
    _ensure_project_root_on_path()
    _route_cowwecom_console_logs_to_stderr()
    try:
        args = _parse_args(argv)
        output_dir = Path(str(args.get("output_dir") or os.getcwd())).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return _emit_error("prompt is required")

        video_path = GrokXAIVideoProvider().generate(
            prompt,
            image_url=_normalize_image_refs(args.get("image_url")),
            aspect_ratio=_optional_text(args.get("aspect_ratio")),
            duration=_optional_text(args.get("duration")),
            resolution=_optional_text(args.get("resolution")),
            quality=_optional_text(args.get("quality")),
            prompt_enhancement=_prompt_enhancement_enabled(args),
            output_dir=str(output_dir),
        )
        print(json.dumps({"videos": [{"url": video_path}]}, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        print(f"[grok-video-generation] {exc}", file=sys.stderr, flush=True)
        return _emit_error(str(exc))


class GrokXAIVideoProvider:
    """Small script adapter around the CowWeCom xAI video integration."""

    def generate(
        self,
        prompt: str,
        *,
        image_url: str | list[str] | None = None,
        aspect_ratio: str | None = None,
        duration: str | None = None,
        resolution: str | None = None,
        quality: str | None = None,
        prompt_enhancement: bool = True,
        output_dir: str,
    ) -> str:
        from integrations.hermes_xai import video_gen as xai_video_gen

        provider = xai_video_gen.XAIVideoGenProvider()
        refs = _refs_as_list(image_url)
        kwargs = {
            "image_url": refs[0] if len(refs) == 1 else None,
            "reference_image_urls": refs if len(refs) > 1 else None,
            "aspect_ratio": aspect_ratio,
            "duration": duration,
            "resolution": resolution,
            "quality": quality,
            "prompt_enhancement": prompt_enhancement,
            "output_dir": output_dir,
        }
        try:
            source_path = _call_provider_generate(provider, prompt, kwargs)
        except Exception:
            _write_prompt_metadata(provider, output_dir)
            raise
        _write_prompt_metadata(provider, output_dir)
        return _copy_video_to_output(source_path, output_dir)


def _parse_args(argv: list[str]) -> Dict[str, Any]:
    if len(argv) < 2:
        raise ValueError("missing JSON job argument")
    payload = json.loads(argv[1])
    if not isinstance(payload, dict):
        raise ValueError("job argument must be a JSON object")
    return payload


def _prompt_enhancement_enabled(args: Dict[str, Any]) -> bool:
    for key in ("prompt_enhancement", "enhance_prompt", "image_prompt_enhancement"):
        if key in args and _falsey(args.get(key)):
            return False
    return True


def _falsey(value: Any) -> bool:
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value == 0
    return str(value or "").strip().lower() in {"0", "false", "no", "off", "disabled"}


def _call_provider_generate(provider: Any, prompt: str, kwargs: Dict[str, Any]) -> str:
    generate = getattr(provider, "generate", None)
    if not callable(generate):
        raise ValueError("XAIVideoGenProvider.generate is not callable")

    accepted_kwargs = _filter_kwargs(generate, kwargs)
    try:
        result = generate(prompt, **accepted_kwargs)
    except TypeError:
        result = generate(prompt)
    return _extract_video_path(result)


def _filter_kwargs(func: Any, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    signature = inspect.signature(func)
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
        return {key: value for key, value in kwargs.items() if value not in (None, "", [])}
    accepted = set(signature.parameters)
    return {
        key: value
        for key, value in kwargs.items()
        if key in accepted and value not in (None, "", [])
    }


def _extract_video_path(result: Any) -> str:
    if isinstance(result, (str, os.PathLike)):
        return str(result)
    if isinstance(result, dict):
        for key in ("url", "path", "video_url", "video_path", "output_path"):
            value = result.get(key)
            if value:
                return str(value)
        videos = result.get("videos") or result.get("video")
        if isinstance(videos, list) and videos:
            return _extract_video_path(videos[0])
        if isinstance(videos, dict):
            return _extract_video_path(videos)
    if isinstance(result, list) and result:
        return _extract_video_path(result[0])
    raise ValueError("xAI video provider returned no video path")


def _write_prompt_metadata(provider: Any, output_dir: str) -> None:
    metadata = getattr(provider, "last_prompt_metadata", None)
    if not isinstance(metadata, dict):
        return
    try:
        from common.image_prompt_enhancer import write_prompt_metadata

        write_prompt_metadata(output_dir, metadata)
    except Exception as exc:
        print(f"[grok-video-generation] prompt metadata skipped: {exc}", file=sys.stderr, flush=True)


def _copy_video_to_output(source_path: str, output_dir: str) -> str:
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"xAI video file was not created: {source}")
    extension = source.suffix.lower() if source.suffix.lower() in VIDEO_EXTENSIONS else ".mp4"
    output = Path(output_dir).resolve() / f"result{extension}"
    if source != output:
        shutil.copyfile(source, output)
    if output.stat().st_size <= 0:
        raise ValueError("xAI video file is empty")
    return str(output)


def _normalize_image_refs(value: Any) -> str | list[str] | None:
    refs = _refs_as_list(value)
    if not refs:
        return None
    return refs[0] if len(refs) == 1 else refs


def _refs_as_list(value: Any) -> list[str]:
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
    return refs[:MAX_IMAGE_REFERENCES]


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _emit_error(message: str) -> int:
    print(json.dumps({"error": str(message or "unknown error")}, ensure_ascii=False), flush=True)
    return 1


def _route_cowwecom_console_logs_to_stderr() -> None:
    try:
        from common.log import logger

        for handler in getattr(logger, "handlers", []):
            stream = getattr(handler, "stream", None)
            if stream is not None and stream is not sys.__stderr__:
                _DETACHED_COWWECOM_LOG_STREAMS.append(stream)
                handler.stream = sys.__stderr__
    except Exception:
        pass
    logging.basicConfig(stream=sys.stderr)


def _ensure_project_root_on_path() -> None:
    candidates = [
        os.environ.get("COWWECHAT_ROOT"),
        str(Path(__file__).resolve().parents[3]) if len(Path(__file__).resolve().parents) > 3 else "",
        os.getcwd(),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        root = Path(candidate).expanduser().resolve()
        if (root / "integrations" / "hermes_xai").exists():
            root_str = str(root)
            if root_str not in sys.path:
                sys.path.insert(0, root_str)
            return


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
