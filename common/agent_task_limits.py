# encoding:utf-8

import re
from typing import Any, Mapping, Optional


DEFAULT_AGENT_MAX_STEPS = 20
DEFAULT_DEVELOPMENT_MAX_STEPS = 40


_DEVELOPMENT_TASK_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(code|coding|programming|debug|fix|bug|refactor|unit test|pytest|typescript|javascript|python|backend|frontend|api|script|repo|repository|git|commit|push|pull request|pr)\b",
        r"(开发\s*代码|代码\s*开发|写\s*代码|改\s*代码|修改\s*代码|代码库|单元测试|测试失败|补测试|报错|异常|调试|修复|重构|实现.{0,12}(功能|代码|脚本|接口|页面|组件)|开发.{0,12}(代码|功能|工具|脚本|网页|网站|接口|项目|仓库)|前端|后端|脚本|接口|函数|类|仓库|提交|推送)",
    )
)


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def is_development_task(content: Any) -> bool:
    text = str(content or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _DEVELOPMENT_TASK_PATTERNS)


def resolve_agent_max_steps(
    content: Any,
    settings: Mapping[str, Any],
    *,
    override: Optional[Any] = None,
) -> int:
    """Return the per-run Agent decision-step limit for this task."""
    base_steps = _positive_int(
        settings.get("agent_max_steps", DEFAULT_AGENT_MAX_STEPS),
        DEFAULT_AGENT_MAX_STEPS,
    )
    if override is not None:
        return _positive_int(override, base_steps)
    if not is_development_task(content):
        return base_steps
    development_steps = _positive_int(
        settings.get("agent_development_max_steps", DEFAULT_DEVELOPMENT_MAX_STEPS),
        DEFAULT_DEVELOPMENT_MAX_STEPS,
    )
    return max(base_steps, development_steps)
