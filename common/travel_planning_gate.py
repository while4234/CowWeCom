# encoding:utf-8

import re
from dataclasses import dataclass
from typing import Any, Optional


_PLAN_INTENT_KEYWORDS = (
    "规划",
    "计划",
    "安排",
    "方案",
    "行程",
    "攻略",
    "怎么玩",
    "玩几天",
    "帮我规划",
    "帮我做",
    "帮我安排",
)
_TRAVEL_CONTEXT_KEYWORDS = (
    "旅行",
    "旅游",
    "出行",
    "游玩",
    "机票",
    "航班",
    "高铁",
    "酒店",
    "住宿",
    "签证",
    "入境",
    "护照",
    "天气",
    "预算",
    "景点",
)
_DIRECT_PROCEED_KEYWORDS = (
    "直接给",
    "先给",
    "先做",
    "先出",
    "粗略",
    "大概",
    "不用问",
    "别问",
    "按假设",
    "先按",
    "不确定也先",
)
_COMMUTE_KEYWORDS = ("通勤", "上班", "下班", "公司", "回家")
_INTERNATIONAL_KEYWORDS = (
    "国外",
    "出境",
    "国际",
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
    "泰国",
    "新加坡",
    "马来西亚",
    "越南",
    "欧洲",
    "申根",
    "美国",
    "洛杉矶",
    "旧金山",
    "纽约",
)
_DESTINATION_KEYWORDS = _INTERNATIONAL_KEYWORDS + (
    "深圳",
    "广州",
    "北京",
    "上海",
    "成都",
    "重庆",
    "西安",
    "杭州",
    "南京",
    "厦门",
    "长沙",
    "武汉",
)

_MOVEMENT_PATTERN = re.compile(
    r"(从.{1,24}(去|到|飞|出发).{1,32}|"
    r"(去|到|飞).{1,24}(玩|旅行|旅游|住|几天|行程|攻略|规划)|"
    r"\b(from|to|fly|depart|return|trip|travel|itinerary)\b)",
    re.IGNORECASE,
)
_EXACT_DATE_PATTERN = re.compile(
    r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}|"
    r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]?|"
    r"\d{1,2}[-/]\d{1,2})"
)
_AMBIGUOUS_DATE_PATTERN = re.compile(r"\d{1,2}\s*月\s*(上旬|中旬|下旬)|几天|某天|近期|月底")
_DURATION_PATTERN = re.compile(r"(\d+\s*(天|晚)|[一二三四五六七八九十两]+天)")
_TRAVELER_PATTERN = re.compile(
    r"(\d+\s*(个|位)?\s*(人|成人|大人|小孩|儿童)|"
    r"[一二三四五六七八九十两]+(个|位)?(人|成人|大人|小孩|儿童)|"
    r"一家|情侣|夫妻|朋友|老人|儿童|小孩|孕妇|轮椅|慢性病|亲子)"
)
_BUDGET_PATTERN = re.compile(r"(预算|人均|总预算|费用|花费|[￥¥]|\bRMB\b|\bCNY\b|\bUSD\b|\d+\s*(万|元|块))", re.IGNORECASE)
_DOCUMENT_PATTERN = re.compile(
    r"(护照|国籍|中国护照|签证|免签|K-ETA|ETA|ESTA|申根|港澳通行证|台胞证|绿卡|永居)",
    re.IGNORECASE,
)
_ORIGIN_PATTERN = re.compile(r"从.{1,24}(去|到|飞|出发)")


@dataclass(frozen=True)
class TravelPlanningClarification:
    message: str
    missing_fields: tuple[str, ...]


def build_travel_planning_clarification(content: Any) -> Optional[TravelPlanningClarification]:
    """Return a deterministic first-turn clarification for under-specified travel plans."""
    text = str(content or "").strip()
    if not text or not _looks_like_travel_planning_request(text):
        return None
    if _contains_any(text, _DIRECT_PROCEED_KEYWORDS):
        return None

    missing: list[str] = []
    is_international = _is_international_trip(text)
    exact_date_count = len(_EXACT_DATE_PATTERN.findall(text))
    has_duration = bool(_DURATION_PATTERN.search(text))

    if exact_date_count < 2 and not has_duration:
        missing.append("dates")
    elif _AMBIGUOUS_DATE_PATTERN.search(text) and exact_date_count < 2:
        missing.append("dates")

    if is_international and not _ORIGIN_PATTERN.search(text):
        missing.append("origin")
    if not _TRAVELER_PATTERN.search(text):
        missing.append("travelers")
    if not _BUDGET_PATTERN.search(text):
        missing.append("budget")
    if is_international and not _DOCUMENT_PATTERN.search(text):
        missing.append("documents")

    if not _should_gate(missing, is_international=is_international):
        return None

    questions = _build_questions(missing, is_international=is_international)
    if not questions:
        return None

    lines = [
        "## 规划前确认",
        "",
        "这几个信息会直接影响航班、酒店区域、预算和证件判断。我先确认关键点，收到后再继续做完整方案：",
        "",
    ]
    lines.extend(f"{idx}. {question}" for idx, question in enumerate(questions, start=1))
    return TravelPlanningClarification(
        message="\n".join(lines),
        missing_fields=tuple(missing),
    )


def deterministic_travel_messages(query: str, response: str) -> list[dict]:
    return [
        {"role": "user", "content": [{"type": "text", "text": query}]},
        {"role": "assistant", "content": [{"type": "text", "text": response}]},
    ]


def _looks_like_travel_planning_request(text: str) -> bool:
    if (
        _contains_any(text, _COMMUTE_KEYWORDS)
        and not _contains_any(text, _TRAVEL_CONTEXT_KEYWORDS)
        and not _contains_any(text, _DESTINATION_KEYWORDS)
    ):
        return False
    has_intent = _contains_any(text, _PLAN_INTENT_KEYWORDS)
    if not has_intent:
        return False
    return (
        _contains_any(text, _TRAVEL_CONTEXT_KEYWORDS)
        or _contains_any(text, _DESTINATION_KEYWORDS)
        or bool(_MOVEMENT_PATTERN.search(text))
    )


def _is_international_trip(text: str) -> bool:
    return _contains_any(text, _INTERNATIONAL_KEYWORDS)


def _should_gate(missing: list[str], *, is_international: bool) -> bool:
    if not missing:
        return False
    if "dates" in missing:
        return True
    if is_international and ("documents" in missing or "origin" in missing):
        return True
    return len(missing) >= 2


def _build_questions(missing: list[str], *, is_international: bool) -> list[str]:
    questions: list[str] = []
    if "dates" in missing:
        questions.append("具体出发和返回日期是哪几天？如果还没定，请给可接受范围和总天数。")
    if "origin" in missing:
        questions.append("从哪个城市出发？是否需要同城接驳到机场/车站也一起规划？")
    if "travelers" in missing:
        questions.append("一共几位出行？是否有老人、儿童、孕妇、轮椅、慢性病或低强度节奏要求？")

    budget_or_docs: list[str] = []
    if "budget" in missing:
        budget_or_docs.append("总预算或人均预算大概多少")
    if is_international and "documents" in missing:
        budget_or_docs.append("出行人护照/国籍或签证状态是什么")
    if budget_or_docs:
        questions.append("；".join(budget_or_docs) + "？")

    return questions[:3]


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)
