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

_DATE_OR_DURATION_PATTERN = re.compile(
    r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|\d{1,2}月\d{1,2}日|"
    r"明天|后天|大后天|下周|周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"[一二三四五六七八九十\d]+天|\b\d+\s*days?\b)",
    re.IGNORECASE,
)
_TRAVEL_MOVEMENT_PATTERN = re.compile(
    r"(从.{1,24}(去|到|飞|出发).{1,24}|去.{1,24}|到.{1,24}|"
    r"\b(from|to|fly|depart|return)\b)",
    re.IGNORECASE,
)
_ROUND_TRIP_OR_TRAVEL_CONTEXT_PATTERN = re.compile(
    r"(回来|返回|返程|回程|往返|来回|回国|返航|\breturn\b|\bround\s*trip\b|"
    r"预算|费用|价格|两个人|[一二三四五六七八九十\d]+个人|"
    r"首尔|东京|大阪|京都|曼谷|新加坡|香港|澳门|台湾|欧洲|美国|韩国|日本)",
    re.IGNORECASE,
)
_COMMUTE_CONTEXT_PATTERN = re.compile(r"(通勤|上班|下班|家.{0,6}公司|公司.{0,6}家)")

_TRAVEL_PLAN_INTENT_KEYWORDS = (
    "规划",
    "计划",
    "安排",
    "方案",
    "行程",
    "攻略",
    "路线",
    "怎么玩",
    "玩几天",
    "帮我规划",
    "帮我做",
    "帮我安排",
    "做完整",
)
_TRAVEL_CONTEXT_KEYWORDS = (
    "旅行",
    "旅游",
    "出行",
    "游玩",
    "一日游",
    "多城市",
    "自驾",
    "机票",
    "航班",
    "高铁",
    "火车票",
    "余票",
    "酒店",
    "住宿",
    "景点票",
    "签证",
    "入境",
    "护照",
    "天气",
    "预算",
    "风险",
    "老人",
    "儿童",
    "轮椅",
    "孕妇",
)
_TRAVEL_DESTINATION_KEYWORDS = (
    "香港",
    "澳门",
    "台湾",
    "日本",
    "韩国",
    "釜山",
    "首尔",
    "济州",
    "东京",
    "大阪",
    "京都",
    "曼谷",
    "清迈",
    "新加坡",
    "吉隆坡",
    "越南",
    "巴厘岛",
    "欧洲",
    "申根",
    "美国",
    "洛杉矶",
    "旧金山",
    "纽约",
    "深圳",
    "广州",
    "北京",
    "上海",
    "成都",
    "重庆",
    "西安",
    "杭州",
)
_NATURAL_TRAVEL_DATE_PATTERN = re.compile(
    r"(\d{1,2}\s*月\s*(上旬|中旬|下旬|\d{1,2}\s*[日号]?)|"
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}|"
    r"明天|后天|大后天|下周|周末|春节|五一|十一|暑假|寒假|"
    r"\d+\s*(天|晚|日)|[一二三四五六七八九十两]+天|几天|回来|返回|出发)",
    re.IGNORECASE,
)
_NATURAL_TRAVEL_MOVEMENT_PATTERN = re.compile(
    r"(从.{1,24}(去|到|飞|出发).{1,32}|"
    r"(去|到|飞).{1,24}(玩|旅行|旅游|出差|住|几天|行程|攻略|规划)|"
    r"\b(from|to|fly|depart|return|trip|travel|itinerary)\b)",
    re.IGNORECASE,
)
_NATURAL_COMMUTE_KEYWORDS = ("通勤", "上班", "下班", "公司", "回家")


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


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def is_complex_planning_task(content: Any) -> bool:
    text = str(content or "").strip()
    if not text:
        return False

    if _is_natural_language_travel_plan(text):
        return True

    if any(pattern.search(text) for pattern in _COMPLEX_PLANNING_PATTERNS[:1]):
        return True

    topic_hits = sum(1 for pattern in _COMPLEX_PLANNING_TOPIC_PATTERNS if pattern.search(text))
    if topic_hits >= 3:
        return True

    has_plan_intent = any(
        keyword in text
        for keyword in ("方案", "规划", "安排", "路线", "每天", "每日", "完整", "帮我做", "帮我定")
    )
    if _is_plain_language_travel_plan(text, has_plan_intent):
        return True

    has_travel_inventory = any(pattern.search(text) for pattern in _COMPLEX_PLANNING_TOPIC_PATTERNS[1:5])
    has_travel_context = any(pattern.search(text) for pattern in _COMPLEX_PLANNING_TOPIC_PATTERNS[:1])
    return has_plan_intent and has_travel_inventory and (has_travel_context or topic_hits >= 2)


def _is_plain_language_travel_plan(text: str, has_plan_intent: bool) -> bool:
    if not has_plan_intent:
        return False
    if _COMMUTE_CONTEXT_PATTERN.search(text):
        return False
    return bool(
        _DATE_OR_DURATION_PATTERN.search(text)
        and _TRAVEL_MOVEMENT_PATTERN.search(text)
        and _ROUND_TRIP_OR_TRAVEL_CONTEXT_PATTERN.search(text)
    )


def _is_natural_language_travel_plan(text: str) -> bool:
    lowered = text.casefold()
    if (
        _contains_any(text, _NATURAL_COMMUTE_KEYWORDS)
        and not _contains_any(text, _TRAVEL_CONTEXT_KEYWORDS)
        and not _contains_any(text, _TRAVEL_DESTINATION_KEYWORDS)
    ):
        return False

    has_plan_intent = _contains_any(text, _TRAVEL_PLAN_INTENT_KEYWORDS)
    if not has_plan_intent:
        return False

    has_travel_context = (
        _contains_any(text, _TRAVEL_CONTEXT_KEYWORDS)
        or _contains_any(text, _TRAVEL_DESTINATION_KEYWORDS)
        or bool(_NATURAL_TRAVEL_MOVEMENT_PATTERN.search(text))
        or bool(re.search(r"\b(trip|travel|itinerary|vacation)\b", lowered))
    )
    if not has_travel_context:
        return False

    has_movement_or_destination = (
        bool(_NATURAL_TRAVEL_MOVEMENT_PATTERN.search(text))
        or _contains_any(text, _TRAVEL_DESTINATION_KEYWORDS)
    )
    has_date_or_duration = bool(_NATURAL_TRAVEL_DATE_PATTERN.search(text))
    has_multi_tool_topic = sum(
        1 for keyword in _TRAVEL_CONTEXT_KEYWORDS if keyword in text
    ) >= 2

    return has_movement_or_destination and (has_date_or_duration or has_multi_tool_topic)


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
