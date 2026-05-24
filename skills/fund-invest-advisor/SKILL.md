---
name: fund-invest-advisor
description: "基金投资顾问与定投规划。Use when the user asks about 基金、支付宝基金、微信基金、场外基金、基金定投、定投计算、收益模拟、资产配置、再平衡、风险偏好、基金类型科普、保守/稳健/激进投资方案、亏损后是否继续定投等。This skill provides local calculations and planning guidance only; it does not place orders and must not present outputs as guaranteed returns or personalized financial advice."
---

# 基金投资顾问

用于微信/支付宝常见场外基金的**应用层规划**：定投测算、收益率情景对比、资产配置、组合再平衡、基金类型科普和风险偏好策略建议。

## 边界与安全

- 输出仅供信息整理和学习参考，**不构成投资建议**。
- 不替用户下单，不说“必买/必卖/稳赚”。
- 真实买入前提醒用户结合自身风险承受能力、投资期限、现金流和平台基金详情页。
- 本技能主要做本地计算和规划；若用户要查某只基金当天净值/估值，应另行使用基金数据查询能力或联网查询。

## 常用命令

所有命令通过 Python 脚本运行：

```cmd
set "PYTHONUTF8=1" && python "<skill_dir>\scripts\fund_advisor.py" <command> [args]
```

在 CowWechat 当前部署目录中通常是：

```cmd
set "PYTHONUTF8=1" && python "C:\Users\RondleLiu\cow\skills\fund-invest-advisor\scripts\fund_advisor.py" help
```

## 功能

### 1. 定投收益测算

```cmd
python scripts\fund_advisor.py invest 1000 8 10
python scripts\fund_advisor.py dca 1000 8 10
```

含义：每月定投 1000 元，假设年化 8%，持续 10 年。输出累计投入、期末估算、收益和收益率。

### 2. 多收益率情景对比

```cmd
python scripts\fund_advisor.py compare 1000 10 3,5,8,10
```

含义：每月定投 1000 元，投 10 年，对比不同年化假设下的结果。

### 3. 资产配置建议

```cmd
python scripts\fund_advisor.py allocate 保守 50000
python scripts\fund_advisor.py allocate 稳健 50000
python scripts\fund_advisor.py allocate 激进 50000
```

按风险偏好输出货币基金、债券基金、宽基指数、行业主题、海外/QDII、黄金等配置比例和金额。

### 4. 组合再平衡

```cmd
python scripts\fund_advisor.py rebalance "股票50000,债券30000,货币20000"
```

解析当前组合金额，给出当前比例和相对稳健目标配置的调整方向。适合解释“涨多了要不要减一点、跌多了要不要补一点”。

### 5. 基金类型科普

```cmd
python scripts\fund_advisor.py types
```

解释货币基金、纯债、混合债、指数基金、混合基金、股票基金、QDII 等类型的风险和适合场景。

### 6. 风险偏好策略

```cmd
python scripts\fund_advisor.py strategy 保守
python scripts\fund_advisor.py strategy 稳健
python scripts\fund_advisor.py strategy 激进
```

输出对应风险偏好的定投、仓位、止盈、再平衡和避坑建议。

## 回复用户时的应用模板

当用户问“我该怎么买基金”时，优先追问或利用已有信息：

- 预算：每月定投多少/一次性多少钱；
- 投资期限：半年、1 年、3 年、5 年以上；
- 风险承受：最多能接受亏多少；
- 目标：保值、稳健增值、长期高收益；
- 当前持仓：基金名称/代码/金额/盈亏。

可给出这样的结果结构：

1. 先说结论：更适合保守/稳健/激进哪种方案；
2. 给出配置比例和金额；
3. 给出定投节奏；
4. 给出亏损时如何处理：继续定投/暂停观察/分批加仓/不要追涨杀跌；
5. 明确风险提示：不保证收益，短期波动正常。

## 参考

- `tips.md`：基金投资实用指南，包含基金类型、定投、资产配置、再平衡和避坑说明。
