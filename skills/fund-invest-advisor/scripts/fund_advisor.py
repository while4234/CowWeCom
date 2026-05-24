#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local fund planning helper for CowWechat.

This script performs deterministic calculations only. It does not fetch live
fund data and does not provide financial advice.
"""

import math
import sys
from typing import Dict, List, Tuple


def money(value: float) -> str:
    return f"{value:,.2f}"


def pct(value: float) -> str:
    return f"{value:.2f}%"


def calc_invest(monthly: float, annual_rate_pct: float, years: float) -> Dict[str, float]:
    monthly_rate = annual_rate_pct / 100.0 / 12.0
    months = int(round(years * 12))
    total_invested = monthly * months
    if months <= 0:
        final_value = 0.0
    elif monthly_rate == 0:
        final_value = total_invested
    else:
        final_value = monthly * ((math.pow(1 + monthly_rate, months) - 1) / monthly_rate) * (1 + monthly_rate)
    profit = final_value - total_invested
    profit_rate = profit / total_invested * 100 if total_invested else 0.0
    return {
        "monthly": monthly,
        "annual_rate": annual_rate_pct,
        "years": years,
        "months": months,
        "total_invested": total_invested,
        "final_value": final_value,
        "profit": profit,
        "profit_rate": profit_rate,
    }


def print_invest(monthly: float, annual_rate_pct: float, years: float) -> None:
    r = calc_invest(monthly, annual_rate_pct, years)
    print("📈 基金定投测算")
    print("=" * 48)
    print(f"每月定投：{money(r['monthly'])} 元")
    print(f"假设年化：{pct(r['annual_rate'])}")
    print(f"定投年限：{r['years']:g} 年（{int(r['months'])} 个月）")
    print("-" * 48)
    print(f"累计投入：{money(r['total_invested'])} 元")
    print(f"期末估算：{money(r['final_value'])} 元")
    print(f"估算收益：{money(r['profit'])} 元")
    print(f"收益率：  {pct(r['profit_rate'])}")
    print("\n⚠️ 以上为情景测算，不代表实际收益；基金有波动，投资需谨慎。")


def print_compare(monthly: float, years: float, rates_text: str) -> None:
    rates = [float(item.strip().replace("%", "")) for item in rates_text.split(",") if item.strip()]
    print("📊 定投收益率情景对比")
    print("=" * 72)
    print(f"每月定投：{money(monthly)} 元 | 定投年限：{years:g} 年")
    print("-" * 72)
    print(f"{'假设年化':>10} {'累计投入':>16} {'期末估算':>16} {'估算收益':>16} {'收益率':>10}")
    for rate in rates:
        r = calc_invest(monthly, rate, years)
        print(f"{pct(rate):>10} {money(r['total_invested']):>16} {money(r['final_value']):>16} {money(r['profit']):>16} {pct(r['profit_rate']):>10}")
    print("\n⚠️ 不同年化只是压力测试/情景假设，不能当作收益承诺。")


ALLOCATIONS: Dict[str, Dict[str, object]] = {
    "保守": {
        "title": "🛡️ 保守型：稳字当头，重视本金波动控制",
        "expected": "3%~5%",
        "items": [
            ("货币基金/现金", 0.30, "应急资金，余额宝/零钱通一类"),
            ("纯债/短债基金", 0.40, "稳健底仓，注意利率和信用风险"),
            ("宽基指数基金", 0.20, "少量长期权益，建议定投"),
            ("黄金/商品类", 0.10, "分散风险，波动仍存在"),
        ],
        "tips": ["适合 1~3 年可能要用钱、厌恶大幅亏损的人。", "权益比例低，收益上限也相对低。", "不要把短期生活费放进高波动基金。"],
    },
    "稳健": {
        "title": "⚖️ 稳健型：攻守兼备，适合多数长期定投用户",
        "expected": "6%~10%",
        "items": [
            ("货币基金/现金", 0.10, "保留 3~6 个月支出"),
            ("债券基金", 0.25, "组合稳定器"),
            ("宽基指数基金", 0.35, "核心仓位，沪深300/中证500/纳指等"),
            ("行业主题基金", 0.15, "医药/消费/科技等，控制比例"),
            ("QDII/黄金", 0.15, "海外和避险资产分散"),
        ],
        "tips": ["适合 3~5 年以上闲钱。", "行业主题总比例建议不要太高。", "每半年检查一次，偏离明显再调整。"],
    },
    "激进": {
        "title": "🚀 激进型：追求长期高收益，但要承受较大回撤",
        "expected": "10%~15%+",
        "items": [
            ("货币基金/现金", 0.05, "少量流动性"),
            ("宽基指数基金", 0.30, "长期核心"),
            ("行业指数基金", 0.25, "高波动赛道，严控集中度"),
            ("主动权益基金", 0.20, "依赖基金经理能力"),
            ("QDII海外基金", 0.15, "全球分散"),
            ("黄金/REITs等", 0.05, "另类分散"),
        ],
        "tips": ["必须是 5 年左右不用的闲钱。", "需要能承受 30% 甚至更高回撤。", "不要满仓单一行业，不借钱投资。"],
    },
}


def print_allocate(risk_type: str, total_amount: float) -> None:
    plan = ALLOCATIONS.get(risk_type)
    if not plan:
        raise SystemExit("风险偏好只支持：保守 / 稳健 / 激进")
    print(str(plan["title"]))
    print("=" * 72)
    print(f"总金额：{money(total_amount)} 元 | 参考年化区间：{plan['expected']}")
    print("-" * 72)
    print(f"{'资产类别':<16} {'比例':>8} {'金额':>16}  说明")
    for name, ratio, note in plan["items"]:  # type: ignore[index]
        print(f"{name:<16} {ratio*100:>7.0f}% {money(total_amount*ratio):>16}  {note}")
    print("\n💡 配置要点：")
    for i, tip in enumerate(plan["tips"], 1):  # type: ignore[index]
        print(f"{i}. {tip}")
    print("\n⚠️ 这是配置框架，不是具体基金推荐；买入前仍需查看基金详情、费率、风险等级和持仓。")


def parse_holdings(text: str) -> List[Tuple[str, float]]:
    result: List[Tuple[str, float]] = []
    for part in text.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        idx = None
        for i, ch in enumerate(part):
            if ch.isdigit() or ch == ".":
                idx = i
                break
        if idx is None:
            continue
        name = part[:idx].strip()
        amount_text = part[idx:].strip().replace("元", "")
        try:
            amount = float(amount_text)
        except ValueError:
            continue
        if name and amount > 0:
            result.append((name, amount))
    return result


def print_rebalance(holdings_text: str) -> None:
    holdings = parse_holdings(holdings_text)
    if not holdings:
        raise SystemExit("无法解析持仓。示例：股票50000,债券30000,货币20000")
    total = sum(amount for _, amount in holdings)
    print("🔄 基金组合再平衡分析")
    print("=" * 72)
    print(f"组合总额：{money(total)} 元")
    print("-" * 72)
    for name, amount in holdings:
        print(f"{name:<16} {money(amount):>16}  当前占比 {amount/total*100:>6.2f}%")
    print("\n参考目标（稳健型）：货币/现金10%、债券25%、宽基35%、行业15%、海外/黄金15%。")
    print("操作思路：")
    print("1. 如果某类资产明显超配（超过目标约 5%），后续少买或分批减一点。")
    print("2. 如果某类资产明显低配，优先用新增资金补，不必频繁卖出。")
    print("3. 行业主题基金不要因为短期涨幅好而越买越集中。")
    print("\n⚠️ 再平衡是纪律工具，不是预测市场顶部/底部。")


def print_types() -> None:
    rows = [
        ("货币基金", "低", "现金管理，流动性好，收益较低"),
        ("纯债基金", "中低", "适合稳健底仓，但也会受利率影响"),
        ("混合债基", "中", "债券为主，可含权益/转债，波动更高"),
        ("宽基指数基金", "中", "长期定投常用核心，如沪深300、中证500、纳指"),
        ("行业主题基金", "中高", "白酒、医药、半导体、新能源等，波动大"),
        ("主动混合/股票基金", "中高/高", "依赖基金经理能力，需关注风格和回撤"),
        ("QDII/海外基金", "中高", "全球配置，有汇率和估值时差影响"),
    ]
    print("📚 常见基金类型")
    print("=" * 72)
    print(f"{'类型':<18} {'风险':<8}  适合场景")
    for row in rows:
        print(f"{row[0]:<18} {row[1]:<8}  {row[2]}")
    print("\n💡 普通长期定投通常优先理解宽基指数、债券基金和少量行业主题。")


def print_strategy(risk_type: str) -> None:
    if risk_type not in ALLOCATIONS:
        raise SystemExit("风险偏好只支持：保守 / 稳健 / 激进")
    base = {
        "保守": [
            "优先保留现金和债券基金，权益基金只做少量定投。",
            "不要追热门行业基金，短期要用的钱不要买权益基金。",
            "亏损时先看权益比例是否过高，必要时降低波动资产比例。",
        ],
        "稳健": [
            "以宽基指数定投为核心，债券基金做稳定器。",
            "行业主题控制在 15%~20% 以内，避免组合过度集中。",
            "每半年检查一次，偏离目标比例明显再平衡。",
            "市场下跌时可以继续小额定投，不建议情绪化割肉。",
        ],
        "激进": [
            "权益比例高，必须接受大幅波动和较长持有周期。",
            "行业基金要分散，不要 all in 单一赛道。",
            "设置止盈/再平衡纪律，避免涨多后仓位失控。",
            "只用长期闲钱，不借钱、不满仓赌博。",
        ],
    }
    print(f"🎯 {risk_type}型基金策略")
    print("=" * 72)
    for i, item in enumerate(base[risk_type], 1):
        print(f"{i}. {item}")
    print("\n可配合命令：")
    print(f"- allocate {risk_type} <金额>：生成配置框架")
    print("- compare <月定投> <年限> 3,5,8,10：做收益情景对比")
    print("\n⚠️ 策略只是纪律参考，不保证收益。")


def print_help() -> None:
    print("基金投资顾问 fund-invest-advisor")
    print("=" * 72)
    print("用法：python scripts\\fund_advisor.py <command> [args]")
    print("")
    print("命令：")
    print("  invest 月定投额 年化收益% 年数       定投收益测算")
    print("  dca 月定投额 年化收益% 年数          同 invest")
    print("  compare 月定投额 年数 收益率列表     多收益率情景对比，如 3,5,8,10")
    print("  allocate 保守|稳健|激进 总金额       资产配置建议")
    print("  rebalance \"股票50000,债券30000\"   再平衡分析")
    print("  types                              基金类型科普")
    print("  strategy 保守|稳健|激进             风险偏好策略")
    print("  help                               显示帮助")
    print("\n说明：本工具仅做本地计算和规划，不联网查净值，不构成投资建议。")


def main(argv: List[str]) -> int:
    cmd = argv[1].lower() if len(argv) > 1 else "help"
    try:
        if cmd in {"invest", "dca"}:
            if len(argv) != 5:
                raise SystemExit("用法：invest 月定投额 年化收益% 年数")
            print_invest(float(argv[2]), float(argv[3].replace("%", "")), float(argv[4]))
        elif cmd == "compare":
            if len(argv) != 5:
                raise SystemExit("用法：compare 月定投额 年数 收益率列表，如 3,5,8,10")
            print_compare(float(argv[2]), float(argv[3]), argv[4])
        elif cmd == "allocate":
            if len(argv) != 4:
                raise SystemExit("用法：allocate 保守|稳健|激进 总金额")
            print_allocate(argv[2], float(argv[3]))
        elif cmd == "rebalance":
            if len(argv) != 3:
                raise SystemExit("用法：rebalance \"股票50000,债券30000,货币20000\"")
            print_rebalance(argv[2])
        elif cmd == "types":
            print_types()
        elif cmd == "strategy":
            if len(argv) != 3:
                raise SystemExit("用法：strategy 保守|稳健|激进")
            print_strategy(argv[2])
        elif cmd in {"help", "-h", "--help"}:
            print_help()
        else:
            raise SystemExit(f"未知命令：{cmd}。使用 help 查看帮助。")
        return 0
    except ValueError as exc:
        print(f"参数格式错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
