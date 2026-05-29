# encoding:utf-8

"""Small local parser for user-visible video generation options."""

from __future__ import annotations

import re
from typing import Any, Dict


VALID_VIDEO_RESOLUTIONS = {"480p", "720p"}
VALID_VIDEO_ASPECT_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"}

_RESOLUTION_RE = re.compile(r"(?<!\d)(480|720)\s*p\b", re.IGNORECASE)
_ASPECT_RATIO_RE = re.compile(r"(?<!\d)(1:1|16:9|9:16|4:3|3:4|3:2|2:3)(?!\d)")
_DURATION_RE = re.compile(
    r"(?<!\d)(1[0-5]|[1-9])\s*(?:s|sec|secs|second|seconds|秒)(?![a-z0-9])",
    re.IGNORECASE,
)


def extract_video_generation_options(prompt: Any) -> Dict[str, str]:
    """Extract simple explicit video options from the user's original text."""
    text = str(prompt or "")
    options: Dict[str, str] = {}

    resolution = _extract_resolution(text)
    if resolution:
        options["resolution"] = resolution

    duration = _extract_duration(text)
    if duration:
        options["duration"] = duration

    aspect_ratio = _extract_aspect_ratio(text)
    if aspect_ratio:
        options["aspect_ratio"] = aspect_ratio

    return options


def _extract_resolution(text: str) -> str:
    match = _RESOLUTION_RE.search(text)
    if not match:
        return ""
    return f"{match.group(1)}p".lower()


def _extract_duration(text: str) -> str:
    match = _DURATION_RE.search(text)
    if not match:
        return ""
    return f"{int(match.group(1))}s"


def _extract_aspect_ratio(text: str) -> str:
    match = _ASPECT_RATIO_RE.search(text)
    return match.group(1) if match else ""
