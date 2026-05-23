---
name: fast-market-price
description: Quickly fetch real-time or near-real-time prices for Bitcoin, Ethereum, gold, silver, oil, USD/CNY, major indexes, and common tickers without API keys. Use when the user asks for 比特币价格, BTC, ETH, 黄金价格, 金价, 现货价格, 美元汇率, 当前行情, or quick market quote in CowWechat.
metadata:
  requires:
    bins: ["python"]
---

# Fast Market Price

Use this skill for quick quote lookups when the user wants a current price, not a full investment analysis. It localizes the `D:\qq_openclaw` fast market route: Coinbase for crypto and Yahoo Finance chart endpoints for metals, futures, FX, indexes, ETFs, and tickers.

## Quick Command

```powershell
python "<base_dir>\scripts\fast_price.py" "比特币"
python "<base_dir>\scripts\fast_price.py" "黄金"
python "<base_dir>\scripts\fast_price.py" "美元人民币"
python "<base_dir>\scripts\fast_price.py" --symbol GC=F
python "<base_dir>\scripts\fast_price.py" "BTC" --json
```

## Workflow

1. Use this skill when the user asks for a quick current price or quote.
2. Use `stock-analysis` instead when the user asks for portfolio analysis, dividend analysis, risk scoring, rumors, watchlists, or broader investment analysis.
3. Run `fast_price.py` with the user's raw query or an explicit `--symbol`.
4. Return price, source, time, and change when available.
5. Add a short caveat for markets that may lag, especially Yahoo Finance futures and non-US assets.

## Built-In Aliases

- `比特币`, `btc`, `bitcoin` -> Coinbase `BTC-USD`
- `以太坊`, `eth`, `ethereum` -> Coinbase `ETH-USD`
- `黄金`, `金价`, `xau`, `gold` -> Yahoo `GC=F`
- `白银`, `silver` -> Yahoo `SI=F`
- `原油`, `wti` -> Yahoo `CL=F`
- `布伦特` -> Yahoo `BZ=F`
- `美元人民币`, `usdcny` -> Yahoo `USDCNY=X`
- `纳指`, `nasdaq` -> Yahoo `^IXIC`
- `标普`, `sp500` -> Yahoo `^GSPC`

## Disclaimer

This skill provides informational market data only. It is not financial advice.
