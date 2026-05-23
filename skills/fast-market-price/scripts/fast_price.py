#!/usr/bin/env python3
"""Fast no-key market quote helper for CowWechat."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Any


ALIASES: list[tuple[tuple[str, ...], dict[str, str]]] = [
    (("比特币", "btc", "bitcoin"), {"provider": "coinbase", "symbol": "BTC-USD", "name": "比特币 BTC/USD"}),
    (("以太坊", "eth", "ethereum"), {"provider": "coinbase", "symbol": "ETH-USD", "name": "以太坊 ETH/USD"}),
    (("黄金", "金价", "xau", "gold"), {"provider": "yahoo", "symbol": "GC=F", "name": "国际黄金期货 GC=F"}),
    (("白银", "银价", "silver"), {"provider": "yahoo", "symbol": "SI=F", "name": "白银期货 SI=F"}),
    (("布伦特", "brent"), {"provider": "yahoo", "symbol": "BZ=F", "name": "布伦特原油期货 BZ=F"}),
    (("原油", "wti", "油价"), {"provider": "yahoo", "symbol": "CL=F", "name": "WTI原油期货 CL=F"}),
    (("美元人民币", "美元兑人民币", "usdcny"), {"provider": "yahoo", "symbol": "USDCNY=X", "name": "美元/人民币 USDCNY"}),
    (("离岸人民币", "usdcnh"), {"provider": "yahoo", "symbol": "USDCNH=X", "name": "美元/离岸人民币 USDCNH"}),
    (("纳指", "纳斯达克", "nasdaq", "ixic"), {"provider": "yahoo", "symbol": "^IXIC", "name": "纳斯达克综合指数"}),
    (("标普", "标普500", "sp500", "s&p"), {"provider": "yahoo", "symbol": "^GSPC", "name": "标普500指数"}),
    (("道指", "dow", "dji"), {"provider": "yahoo", "symbol": "^DJI", "name": "道琼斯工业平均指数"}),
]


def fetch_json(url: str, timeout: int = 15) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CowWechat fast-market-price",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            last_error = exc
            if attempt == 3:
                break
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"行情接口暂时不可用：{last_error}") from last_error


def coinbase_ticker(product: str, name: str | None = None) -> dict[str, Any]:
    url = f"https://api.exchange.coinbase.com/products/{urllib.parse.quote(product)}/ticker"
    data = fetch_json(url, timeout=15)
    price = data.get("price")
    if price is None:
        raise RuntimeError(f"Coinbase did not return a price for {product}")
    return {
        "source": "Coinbase Exchange ticker",
        "symbol": product,
        "name": name or product,
        "price": float(price),
        "currency": product.split("-")[-1] if "-" in product else "",
        "bid": float(data["bid"]) if data.get("bid") not in (None, "") else None,
        "ask": float(data["ask"]) if data.get("ask") not in (None, "") else None,
        "volume": float(data["volume"]) if data.get("volume") not in (None, "") else None,
        "time": str(data.get("time") or ""),
    }


def yahoo_quote(symbol: str, name: str | None = None) -> dict[str, Any]:
    encoded = urllib.parse.quote(symbol, safe="")
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{encoded}?interval=1m&range=1d"
    data = fetch_json(url, timeout=15)
    chart = data.get("chart") or {}
    result = (chart.get("result") or [None])[0] or {}
    meta = result.get("meta") or {}
    price = meta.get("regularMarketPrice")
    previous = meta.get("chartPreviousClose") or meta.get("previousClose")
    if price is None:
        indicators = result.get("indicators") or {}
        quote = (indicators.get("quote") or [{}])[0]
        closes = [value for value in quote.get("close") or [] if value is not None]
        if closes:
            price = closes[-1]
    if price is None:
        raise RuntimeError(f"Yahoo Finance did not return a price for {symbol}")
    change = None
    pct = None
    if previous not in (None, 0, ""):
        change = float(price) - float(previous)
        pct = change / float(previous) * 100
    market_time = meta.get("regularMarketTime")
    time_text = ""
    if isinstance(market_time, (int, float)):
        time_text = dt.datetime.fromtimestamp(market_time, tz=dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "source": "Yahoo Finance chart",
        "symbol": symbol,
        "name": name or meta.get("shortName") or meta.get("symbol") or symbol,
        "price": float(price),
        "currency": meta.get("currency") or "",
        "previous_close": float(previous) if previous not in (None, "") else None,
        "change": change,
        "pct": pct,
        "time": time_text,
    }


def detect(query: str, explicit_symbol: str | None = None) -> dict[str, str]:
    if explicit_symbol:
        return {"provider": "yahoo", "symbol": explicit_symbol, "name": explicit_symbol}
    text = (query or "").lower()
    for keywords, target in ALIASES:
        if any(keyword.lower() in text for keyword in keywords):
            return dict(target)
    match = re.search(r"\b[A-Z][A-Z0-9.\-=^]{0,12}\b", query.upper())
    if match:
        token = match.group(0)
        if token in {"BTC", "ETH"}:
            return {"provider": "coinbase", "symbol": f"{token}-USD", "name": f"{token}/USD"}
        return {"provider": "yahoo", "symbol": token, "name": token}
    raise RuntimeError("没有识别到行情标的。可试：比特币、黄金、美元人民币，或 --symbol GC=F")


def format_quote(q: dict[str, Any]) -> str:
    price = q.get("price")
    currency = q.get("currency") or ""
    line = f"{q.get('name') or q.get('symbol')} 当前值：{price:.4f} {currency}".strip()
    details = [line]
    if q.get("change") is not None and q.get("pct") is not None:
        details.append(f"涨跌：{q['change']:+.4f}（{q['pct']:+.2f}%）")
    if q.get("bid") is not None and q.get("ask") is not None:
        details.append(f"买一/卖一：{q['bid']:.4f} / {q['ask']:.4f}")
    if q.get("time"):
        details.append(f"时间：{q['time']}")
    details.append(f"来源：{q.get('source')}")
    details.append("提示：行情可能有延迟，仅供信息参考，不构成投资建议。")
    return "\n".join(details)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast quote lookup for crypto, gold, FX, indexes, and tickers.")
    parser.add_argument("query", nargs="*", help="Quote query, e.g. 比特币 or 黄金")
    parser.add_argument("--symbol", help="Explicit Yahoo Finance symbol, e.g. GC=F, AAPL, USDCNY=X")
    parser.add_argument("--json", action="store_true", help="Return JSON")
    args = parser.parse_args()
    target = detect(" ".join(args.query).strip(), args.symbol)
    if target["provider"] == "coinbase":
        quote = coinbase_ticker(target["symbol"], target.get("name"))
    else:
        quote = yahoo_quote(target["symbol"], target.get("name"))
    if args.json:
        print(json.dumps(quote, ensure_ascii=False, indent=2))
    else:
        print(format_quote(quote))
    return 0


if __name__ == "__main__":
    sys.exit(main())
