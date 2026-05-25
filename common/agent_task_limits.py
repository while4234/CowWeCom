# encoding:utf-8

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional


DEFAULT_AGENT_MAX_STEPS = 20
DEFAULT_DEVELOPMENT_MAX_STEPS = 40
DEFAULT_COMPLEX_PLANNING_MAX_STEPS = 40


_DEVELOPMENT_TASK_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(code|coding|programming|debug|fix|bug|refactor|unit test|pytest|typescript|javascript|python|backend|frontend|api|script|repo|repository|git|commit|push|pull request|pr)\b",
        r"(开发\s*代码|代码\s*开发|写\s*代码|改\s*代码|修改\s*代码|代码库|单元测试|测试失败|补测试|报错|异常|调试|修复|重构|实现.{0,12}(功能|代码|脚本|接口|页面|组件)|开发.{0,12}(代码|功能|工具|脚本|网页|网站|接口|项目|仓库)|前端|后端|脚本|接口|函数|类|仓库|提交|推送)",
    )
)

_COMPLEX_PLANNING_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(完整.{0,8}(旅行|旅游|行程|出行).{0,8}(方案|规划)|旅行.{0,8}方案|旅游.{0,8}方案|行程.{0,8}规划|每日.{0,4}行程|多城市|跨城|一日游|自驾)",
        r"(机票|航班|高铁|火车票|余票|酒店|景点票|签证|入境|护照|天气|预算|风险|备选|低强度|老人|儿童|孕妇|轮椅)",
        r"\b(travel|trip|itinerary|flight|hotel|visa|entry|budget|route|weather)\b",
    )
)

_COMPLEX_PLANNING_TOPIC_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(旅行|旅游|行程|出行|游玩|一日游|多城市|自驾)",
        r"(机票|航班|飞|高铁|火车票|余票|车次|铁路)",
        r"(酒店|住宿|民宿)",
        r"(景点票|门票|预约|景点)",
        r"(签证|入境|护照|证件|ETIAS|ESTA|K-ETA)",
        r"(天气|打包|雨|台风|降雪)",
        r"(预算|费用|价格|票价)",
        r"(风险|备选|低强度|老人|儿童|孕妇|轮椅|慢性病)",
        r"\b(travel|trip|itinerary|flight|train|hotel|visa|entry|weather|budget|risk)\b",
    )
)


@dataclass(frozen=True)
class AgentTaskBudget:
    max_steps: int
    kind: str


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


def is_complex_planning_task(content: Any) -> bool:
    text = str(content or "").strip()
    if not text:
        return False

    if any(pattern.search(text) for pattern in _COMPLEX_PLANNING_PATTERNS[:1]):
        return True

    topic_hits = sum(1 for pattern in _COMPLEX_PLANNING_TOPIC_PATTERNS if pattern.search(text))
    if topic_hits >= 3:
        return True

    has_plan_intent = any(
        keyword in text
        for keyword in ("方案", "规划", "安排", "路线", "每天", "每日", "完整", "帮我做", "帮我定")
    )
    has_travel_inventory = any(pattern.search(text) for pattern in _COMPLEX_PLANNING_TOPIC_PATTERNS[1:5])
    has_travel_context = any(pattern.search(text) for pattern in _COMPLEX_PLANNING_TOPIC_PATTERNS[:1])
    return has_plan_intent and has_travel_inventory and (has_travel_context or topic_hits >= 2)


def resolve_agent_task_budget(
    content: Any,
    settings: Mapping[str, Any],
    *,
    override: Optional[Any] = None,
) -> AgentTaskBudget:
    """Return the per-run Agent decision-step budget and its classification."""
    base_steps = _positive_int(
        settings.get("agent_max_steps", DEFAULT_AGENT_MAX_STEPS),
        DEFAULT_AGENT_MAX_STEPS,
    )
    if override is not None:
        return AgentTaskBudget(_positive_int(override, base_steps), "override")

    if is_development_task(content):
        development_steps = _positive_int(
            settings.get("agent_development_max_steps", DEFAULT_DEVELOPMENT_MAX_STEPS),
            DEFAULT_DEVELOPMENT_MAX_STEPS,
        )
        return AgentTaskBudget(max(base_steps, development_steps), "development")

    if is_complex_planning_task(content):
        fallback_steps = _positive_int(
            settings.get("agent_development_max_steps", DEFAULT_DEVELOPMENT_MAX_STEPS),
            DEFAULT_DEVELOPMENT_MAX_STEPS,
        )
        planning_steps = _positive_int(
            settings.get("agent_complex_planning_max_steps", fallback_steps),
            DEFAULT_COMPLEX_PLANNING_MAX_STEPS,
        )
        return AgentTaskBudget(max(base_steps, planning_steps), "complex_planning")

    return AgentTaskBudget(base_steps, "default")


def resolve_agent_max_steps(
    content: Any,
    settings: Mapping[str, Any],
    *,
    override: Optional[Any] = None,
) -> int:
    """Return the per-run Agent decision-step limit for this task."""
    return resolve_agent_task_budget(content, settings, override=override).max_steps
