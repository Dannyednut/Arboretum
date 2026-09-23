#!/usr/bin/env python3
"""
Live cross-exchange funding rate collector for research.
Fetches funding rates + mark prices from Binance, Bybit, OKX, Hyperliquid public APIs.
No API keys required (public market data endpoints only).
Outputs: /home/z/my-project/scripts/live_funding_snapshot.json
"""
import asyncio
import aiohttp
import json
import time
from datetime import datetime, timezone

OUT = "/home/z/my-project/scripts/live_funding_snapshot.json"
TOP_N = 60  # per exchange, top coins by volume we care about

BINANCE = "https://fapi.binance.com/fapi/v1/premiumIndex"
BYBIT = "https://api.bybit.com/v5/market/tickers?category=linear"
OKX_TICKERS = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
HL = "https://api.hyperliquid.xyz/info"


async def fetch_json(session, url, params=None, method="GET", body=None):
    try:
        if method == "POST":
            async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=20)) as r:
                return await r.json()
        else:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
                return await r.json()
    except Exception as e:
        return {"__error__": str(e)}


async def get_binance(session):
    data = await fetch_json(session, BINANCE)
    if isinstance(data, dict) and "__error__" in data:
        return {"error": data["__error__"], "rows": []}
    rows = {}
    for d in data:
        symbol = d.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        rows[symbol[:-5]] = {
            "funding_8h_rate": float(d.get("lastFundingRate", 0) or 0),
            "mark_price": float(d.get("markPrice", 0) or 0),
            "next_funding_ms": int(d.get("nextFundingTime", 0) or 0),
        }
    return {"rows": rows}


async def get_bybit(session):
    data = await fetch_json(session, BYBIT)
    if isinstance(data, dict) and "__error__" in data:
        return {"error": data["__error__"], "rows": []}
    rows = {}
    lst = (data.get("result") or {}).get("list") or []
    for d in lst:
        symbol = d.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        rows[symbol[:-5]] = {
            "funding_8h_rate": float(d.get("fundingRate", 0) or 0),
            "mark_price": float(d.get("markPrice", 0) or 0),
            "next_funding_ms": int(d.get("nextFundingTime", 0) or 0),
        }
    return {"rows": rows}


async def get_okx(session):
    data = await fetch_json(session, OKX_TICKERS)
    if isinstance(data, dict) and "__error__" in data:
        return {"error": data["__error__"], "rows": []}

    # OKX tickers don't include funding; need per-instrument call. Do top ~40 by volCcy24h later.
    tickers = data.get("data", []) or []
    # sort by volume proxy
    def vol(t):
        try:
            return float(t.get("volCcy24h", 0) or 0)
        except Exception:
            return 0.0
    tickers.sort(key=vol, reverse=True)
    swap_ids = [t["instId"] for t in tickers if t.get("instId", "").endswith("-USDT-SWAP")][:45]

    rows = {}
    sem = asyncio.Semaphore(8)

    async def one(instId):
        async with sem:
            fr = await fetch_json(session, f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}")
            arr = (fr.get("data") or [{}])[0] if isinstance(fr, dict) and "__error__" not in fr else {}
            tick = next((t for t in tickers if t.get("instId") == instId), {})
            coin = instId.replace("-USDT-SWAP", "")
            rows[coin] = {
                "funding_8h_rate": float(arr.get("fundingRate", 0) or 0),
                "mark_price": float(tick.get("markPx", 0) or 0),
                "next_funding_ms": int(arr.get("fundingTime", 0) or 0) if arr.get("fundingTime") else 0,
                "funding_interval_hours": int((arr.get("fundingInterval") or "8") ) if str(arr.get("fundingInterval") or "8").isdigit() else 8,
            }

    await asyncio.gather(*[one(i) for i in swap_ids])
    return {"rows": rows}


async def get_hyperliquid(session):
    body = {"type": "metaAndAssetCtxs"}
    data = await fetch_json(session, HL, method="POST", body=body)
    if isinstance(data, dict) and "__error__" in data:
        return {"error": data["__error__"], "rows": []}
    rows = {}
    try:
        meta, ctxs = data
        universe = meta.get("universe", [])
        for u, ctx in zip(universe, ctxs):
            if u.get("isDelisted"):
                continue
            coin = u.get("name", "")
            funding = float(ctx.get("funding", 0) or 0)  # hourly rate on HL
            rows[coin] = {
                "funding_hourly_rate": funding,
                "funding_8h_equiv": funding * 8,
                "mark_price": float(ctx.get("markPx", 0) or 0),
                "open_interest": float(ctx.get("openInterest", 0) or 0),
            }
    except Exception as e:
        return {"error": f"parse: {e}", "rows": {}}
    return {"rows": rows}


def summarize(name, res):
    if res.get("error"):
        print(f"  {name}: ERROR {res['error']}")
        return 0
    print(f"  {name}: {len(res['rows'])} instruments")
    return len(res["rows"])


async def main():
    async with aiohttp.ClientSession() as session:
        binance, bybit, okx, hl = await asyncio.gather(
            get_binance(session), get_bybit(session), get_okx(session), get_hyperliquid(session)
        )

    print("Snapshot:", datetime.now(timezone.utc).isoformat())
    summarize("Binance USDT-M", binance)
    summarize("Bybit linear", bybit)
    summarize("OKX SWAP", okx)
    summarize("Hyperliquid perps", hl)

    snap = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "binance": binance,
        "bybit": bybit,
        "okx": okx,
        "hyperliquid": hl,
    }
    with open(OUT, "w") as f:
        json.dump(snap, f, indent=1)
    print(f"Saved -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
