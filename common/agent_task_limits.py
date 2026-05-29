# encoding:utf-8

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional


DEFAULT_AGENT_MAX_STEPS = 20
DEFAULT_DEVELOPMENT_MAX_STEPS = 40
DEFAULT_COMPLEX_PLANNING_MAX_STEPS = 40
DEFAULT_KNOWLEDGE_MAX_STEPS = 40


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

_KNOWLEDGE_TASK_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(knowledge|knowledge[-_\s]*base|knowledge_query|deep_query|read\s+knowledge|source[-_\s]*backed)\b",
        r"\b(spec|specification|protocol|standard|pdf|document|source|evidence|citation|chapter|section|clause)\b",
        r"\b(table|field|encoding|register|state\s*machine|timing|sequence|mapping|step|figure|diagram)\b",
        r"\b(ucie|pcie|pcie6|pcie\s*6|cxl|amba|axi|axi4|tlp|dllp|mbinit|mbtrain|phyretrain)\b",
        (
            "\u77e5\u8bc6\u5e93|\u4e2a\u4eba\u77e5\u8bc6\u5e93|\u516c\u5171\u77e5\u8bc6\u5e93|"
            "\u539f\u6587|\u6e90\u6587|\u4e0a\u4f20\u6587\u6863|\u4e0a\u4f20\u6587\u4ef6|"
            "\u6587\u6863|\u8d44\u6599|\u8bc1\u636e|\u4f9d\u636e|\u5f15\u7528"
        ),
        (
            "\u534f\u8bae|\u89c4\u8303|\u6807\u51c6|\u72b6\u6001\u673a|\u6b65\u9aa4|"
            "\u65f6\u5e8f|\u5bc4\u5b58\u5668|\u6620\u5c04|\u8868\u683c|\u5b57\u6bb5|"
            "\u7ae0\u8282|\u6761\u6b3e|\u7f16\u7801|\u5bf9\u6bd4|\u786e\u8ba4"
        ),
    )
)

_PERCENT_PROGRESS_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:100|[1-9]?\d)\s*%|(?:100|[1-9]?\d)\s*(?:percent|pct)\b",
    re.IGNORECASE,
)
_PROGRESS_STATUS_PATTERN = re.compile(
    r"("
    r"完成|进度|当前|目标|本周|今日|今天|昨天|日报|周报|汇报|"
    r"\b(done|complete|completed|progress|current|target|status|report)\b"
    r").{0,12}"
    r"((?<![A-Za-z0-9_])(?:100|[1-9]?\d)\s*%|(?:100|[1-9]?\d)\s*(?:percent|pct)\b)"
    r"|"
    r"((?<![A-Za-z0-9_])(?:100|[1-9]?\d)\s*%|(?:100|[1-9]?\d)\s*(?:percent|pct)\b)"
    r".{0,12}"
    r"("
    r"完成|进度|当前|目标|本周|今日|今天|昨天|日报|周报|汇报|"
    r"\b(done|complete|completed|progress|current|target|status|report)\b"
    r")",
    re.IGNORECASE,
)
_QUESTION_INTENT_PATTERN = re.compile(
    r"(\?|？|吗|呢|么|如何|怎么|为什么|为何|是否|能否|是不是|"
    r"请问|查询|查一下|解释|说明|分析|对比|区别|确认|依据|原文|引用|"
    r"\b(what|why|how|whether|compare|explain|analy[sz]e|confirm|query|search|read|cite|citation|evidence)\b)",
    re.IGNORECASE,
)
_SHORT_CONTEXTUAL_REPLY_PATTERN = re.compile(
    r"^(?:"
    r"没有|没有了|没了|暂无|暂时没有|无|没|"
    r"不用|不用了|不需要|不要|不补充|不需要补充|"
    r"是|是的|对|对的|好的|好|可以|行|嗯|嗯嗯|"
    r"no|none|nothing|not\s+now|ok|okay|yes|yep|yeah"
    r")[。.!！~～]*$",
    re.IGNORECASE,
)
_LEADING_QUOTE_CONTEXT_PATTERN = re.compile(
    r"^\s*(?:\[引用[:：][^\n]*\]\s*)+",
    re.IGNORECASE,
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
_STATUS_QUERY_PATTERN = re.compile(
    r"((查询|看一下|看看|当前).{0,16}(状态|后端状态)|"
    r"\b(backend|service|agent)\s+status\b)",
    re.IGNORECASE,
)
_STATUS_QUERY_DEV_ACTION_PATTERN = re.compile(
    r"(\b(fix|debug|implement|develop|code|bug|refactor|test)\b|"
    r"修复|调试|开发|代码|报错|异常)",
    re.IGNORECASE,
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


def is_knowledge_task(content: Any) -> bool:
    """Return True when a request needs local/source-backed knowledge lookup."""
    text = strip_leading_quote_context(content)
    if not text:
        return False
    if is_plain_progress_update(text):
        return False
    return any(pattern.search(text) for pattern in _KNOWLEDGE_TASK_PATTERNS)


def strip_leading_quote_context(content: Any) -> str:
    text = str(content or "").strip()
    if not text:
        return ""
    return _LEADING_QUOTE_CONTEXT_PATTERN.sub("", text).strip()


def is_plain_progress_update(content: Any) -> bool:
    """Return True for declarative work-progress snapshots, not questions."""
    text = strip_leading_quote_context(content)
    if not text:
        return False
    if _QUESTION_INTENT_PATTERN.search(text):
        return False
    percent_count = len(_PERCENT_PROGRESS_PATTERN.findall(text))
    if percent_count < 1:
        return False
    return percent_count >= 2 or bool(_PROGRESS_STATUS_PATTERN.search(text))


def is_short_contextual_reply(content: Any) -> bool:
    """Return True for short replies that should bind to the latest bot prompt."""
    text = strip_leading_quote_context(content)
    if not text or len(text) > 32:
        return False
    if _QUESTION_INTENT_PATTERN.search(text):
        return False
    return bool(_SHORT_CONTEXTUAL_REPLY_PATTERN.match(text))


def is_plain_status_query(content: Any) -> bool:
    text = str(content or "").strip()
    if not text:
        return False
    return bool(_STATUS_QUERY_PATTERN.search(text)) and not bool(_STATUS_QUERY_DEV_ACTION_PATTERN.search(text))


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

    if is_plain_status_query(content):
        return AgentTaskBudget(base_steps, "default")

    if is_development_task(content):
        development_steps = _positive_int(
            settings.get("agent_development_max_steps", DEFAULT_DEVELOPMENT_MAX_STEPS),
            DEFAULT_DEVELOPMENT_MAX_STEPS,
        )
        return AgentTaskBudget(max(base_steps, development_steps), "development")

    if is_knowledge_task(content):
        knowledge_steps = _positive_int(
            settings.get("agent_knowledge_max_steps", DEFAULT_KNOWLEDGE_MAX_STEPS),
            DEFAULT_KNOWLEDGE_MAX_STEPS,
        )
        return AgentTaskBudget(max(base_steps, knowledge_steps), "knowledge")

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
