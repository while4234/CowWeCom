#!/usr/bin/env python3
"""Local-only lightweight takeout recommendation helper."""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from typing import Iterable


RULES = [
    {
        "name": "加班热食",
        "keywords": ["加班", "累", "热的", "热乎", "暖和", "晚饭", "晚餐"],
        "items": ["黄焖鸡", "盖饭", "米粉", "馄饨", "热汤面"],
        "reason": "热乎、有主食，适合加班后快速补能，也比较容易控制预算。",
    },
    {
        "name": "清淡低脂",
        "keywords": ["清淡", "低脂", "不油", "少油", "健康", "减脂", "轻一点"],
        "items": ["轻食", "沙拉", "鸡胸肉饭", "粥", "鱼粉"],
        "reason": "油脂负担低，适合想吃清爽一点或控制热量的时候。",
    },
    {
        "name": "便宜省钱",
        "keywords": ["便宜", "省钱", "实惠", "优惠", "红包", "券", "预算低"],
        "items": ["盖饭", "麻辣烫", "米线", "炒饭", "馄饨"],
        "reason": "常见店铺多，价格带友好，也更容易遇到满减或红包可用。",
    },
    {
        "name": "夜宵",
        "keywords": ["夜宵", "宵夜", "半夜", "凌晨", "晚上晚点"],
        "items": ["粥", "粉面", "烧烤", "小馄饨", "砂锅"],
        "reason": "夜间可选概率较高，能按饱腹感在清淡和重口之间切换。",
    },
    {
        "name": "想辣",
        "keywords": ["辣", "麻", "重口", "川菜", "湘菜", "酸辣"],
        "items": ["麻辣烫", "川菜盖饭", "冒菜", "酸辣粉", "湘菜小炒"],
        "reason": "口味刺激、选择多，适合明确想吃辣的时候。",
    },
]

DEFAULT_ITEMS = ["黄焖鸡", "盖饭", "馄饨", "轻食", "麻辣烫"]

SEARCH_MODIFIERS = {
    "黄焖鸡": ["黄焖鸡 高评分 30分钟内", "黄焖鸡 满减 配送快"],
    "盖饭": ["盖饭 满减 配送快", "盖饭 月售高 30分钟内"],
    "米粉": ["米粉 热汤 配送快", "米粉 高评分 满减"],
    "馄饨": ["小馄饨 热汤 30分钟内", "馄饨 满减 配送快"],
    "热汤面": ["热汤面 配送快", "汤面 高评分 30分钟内"],
    "轻食": ["轻食 月售高 低脂", "轻食 鸡胸肉 满减"],
    "沙拉": ["沙拉 低脂 月售高", "沙拉 高评分 配送快"],
    "鸡胸肉饭": ["鸡胸肉饭 低脂 满减", "鸡胸肉饭 月售高"],
    "粥": ["粥 热乎 配送快", "粥 满减 30分钟内"],
    "鱼粉": ["鱼粉 清淡 热汤", "鱼粉 高评分 配送快"],
    "麻辣烫": ["麻辣烫 满减 配送快", "麻辣烫 高评分 月售高"],
    "米线": ["米线 满减 配送快", "米线 热汤 30分钟内"],
    "炒饭": ["炒饭 便宜 满减", "炒饭 月售高 配送快"],
    "粉面": ["粉面 夜宵 配送快", "粉面 热汤 满减"],
    "烧烤": ["烧烤 夜宵 满减", "烧烤 月售高 配送快"],
    "小馄饨": ["小馄饨 夜宵 热汤", "小馄饨 配送快"],
    "砂锅": ["砂锅 热乎 满减", "砂锅 配送快"],
    "川菜盖饭": ["川菜盖饭 辣 满减", "川菜盖饭 月售高"],
    "冒菜": ["冒菜 满减 配送快", "冒菜 高评分 月售高"],
    "酸辣粉": ["酸辣粉 配送快", "酸辣粉 满减"],
    "湘菜小炒": ["湘菜小炒 辣 满减", "湘菜小炒 月售高"],
}


@dataclass(frozen=True)
class Recommendation:
    item: str
    reason: str
    budget_range: str
    keywords: list[str]
    coupon_first: bool


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "是", "要", "需要"}


def extract_budget(text: str, explicit_budget: str | None) -> int | None:
    if explicit_budget:
        match = re.search(r"\d+", explicit_budget)
        return int(match.group()) if match else None
    patterns = [
        r"预算\s*(\d+)",
        r"(\d+)\s*元",
        r"人均\s*(\d+)",
        r"(\d+)\s*块",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None


def budget_range(budget: int | None) -> str:
    if budget is None:
        return "约 25-40 元，可按实际红包和配送费调整"
    low = max(12, budget - 8)
    high = budget + 5
    return f"约 {low}-{high} 元，优先在美团 App 内按红包、满减和配送费核验"


def matched_rules(text: str, spicy: str | None) -> list[dict]:
    normalized = text.lower()
    matches = []
    for rule in RULES:
        if any(keyword.lower() in normalized for keyword in rule["keywords"]):
            matches.append(rule)
    if parse_bool(spicy) and all(rule["name"] != "想辣" for rule in matches):
        matches.append(next(rule for rule in RULES if rule["name"] == "想辣"))
    return matches


def unique_items(rules: Iterable[dict]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen = set()
    for rule in rules:
        for item in rule["items"]:
            if item not in seen:
                pairs.append((item, rule["reason"]))
                seen.add(item)
            if len(pairs) >= 3:
                return pairs
    for item in DEFAULT_ITEMS:
        if item not in seen:
            pairs.append((item, "稳定、常见、适合作为今天外卖的轻量候选。"))
            seen.add(item)
        if len(pairs) >= 3:
            break
    return pairs


def build_recommendations(
    text: str,
    budget: int | None,
    coupon: bool,
    spicy: str | None,
) -> list[Recommendation]:
    rules = matched_rules(text, spicy)
    pairs = unique_items(rules)
    return [
        Recommendation(
            item=item,
            reason=reason,
            budget_range=budget_range(budget),
            keywords=SEARCH_MODIFIERS.get(item, [f"{item} 高评分 配送快", f"{item} 满减 30分钟内"])[:2],
            coupon_first=coupon,
        )
        for item, reason in pairs
    ]


def infer_coupon(text: str, explicit_coupon: str | None) -> bool:
    if explicit_coupon is not None:
        return parse_bool(explicit_coupon)
    return any(word in text for word in ["美团", "红包", "券", "优惠", "省钱", "便宜", "满减"])


def render_markdown(text: str, args: argparse.Namespace) -> str:
    budget = extract_budget(text, args.budget)
    coupon = infer_coupon(text, args.coupon)
    mode = args.mode or ("外卖" if "外卖" in text or "点" in text else "外卖优先")
    city = f"；城市/商圈：{args.city}" if args.city else ""
    recommendations = build_recommendations(text, budget, coupon, args.spicy)

    lines = [
        "一、直接结论",
        "",
        f"按你的描述，我建议走“{mode}”思路{city}：先选热乎、稳妥、预算友好的品类，再到美团 App 里按红包、满减、配送费和实际可配送状态筛选。",
        "",
        "二、3 个推荐选项",
        "",
    ]
    for idx, rec in enumerate(recommendations, 1):
        lines.extend(
            [
                f"### {idx}. {rec.item}",
                f"- 适合理由：{rec.reason}",
                f"- 预算区间：{rec.budget_range}",
                f"- 美团搜索关键词：{'；'.join(rec.keywords)}",
                f"- 是否建议先领券：{'是，建议先调用或查看 meituan-coupons 领取/查询红包。' if rec.coupon_first else '可先看店铺满减；如果想省钱，再领取/查询美团红包。'}",
                "",
            ]
        )
    lines.extend(
        [
            "三、提醒",
            "",
            "我没有查询真实附近店铺、实时菜单、评分、配送费、配送时间或可配送状态。店铺评分、配送时间、配送费、满减和可配送状态以美团 App 实际页面为准。",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local lightweight takeout recommender")
    parser.add_argument("request", help="User food/takeout request")
    parser.add_argument("--budget", help="Budget, such as 35 or 35元")
    parser.add_argument("--mode", help="Dining mode, such as 外卖")
    parser.add_argument("--spicy", help="Whether spicy food is preferred")
    parser.add_argument("--city", help="City or business district")
    parser.add_argument("--coupon", help="Whether to include coupon reminder")
    args = parser.parse_args()
    print(render_markdown(args.request, args))


if __name__ == "__main__":
    main()
