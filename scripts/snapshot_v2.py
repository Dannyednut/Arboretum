#!/usr/bin/env python3
"""
Venue snapshot v2 - comprehensive market-state collector for arbitrage research.

Collects in one pass (all public endpoints, no API keys):
  PERPS : funding rate + funding interval + mark price
          (Binance USDT-M, Bybit linear, OKX SWAP, Gate USDT, Hyperliquid)
  SPOT  : bid/ask mids (Binance, Bybit, OKX, Hyperliquid spot incl. xStocks)
  DATED : Binance quarterly delivery futures (parsed from premiumIndex)

Interval-correct: Binance via /fapi/v1/fundingInfo, Bybit via instruments-info
fundingInterval, OKX per-instrument field, Gate via ticker funding_interval,
Hyperliquid hourly by construction.

Output: /home/z/my-project/scripts/snapshot_v2.json
"""
import asyncio
import aiohttp
import json
import re
from datetime import datetime, timezone

OUT = "/home/z/my-project/scripts/snapshot_v2.json"

OKX_PERP_TOP_N = 80


async def fetch_json(session, url, params=None, method="GET", body=None, timeout=25):
    try:
        if method == "POST":
            async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                return await r.json()
        else:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                return await r.json()
    except Exception as e:
        return {"__error__": str(e)}


def f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def norm_base(base: str) -> str:
    """Normalize 1000PEPE/10000SATS style multiplier prefixes to the plain asset
    (funding RATES are multiplier-independent, only prices differ)."""
    for p in ("1000000", "100000", "10000", "1000"):
        if base.startswith(p) and len(base) > len(p):
            return base[len(p):]
    return base


# ---------------------------------------------------------------- Binance perp
async def get_binance_perp(session):
    prem = await fetch_json(session, "https://fapi.binance.com/fapi/v1/premiumIndex")
    finfo = await fetch_json(session, "https://fapi.binance.com/fapi/v1/fundingInfo")
    if isinstance(prem, dict) and "__error__" in prem:
        return {"error": prem["__error__"], "rows": {}, "quarterly": []}

    interval = {}
    if isinstance(finfo, list):
        for d in finfo:
            interval[d.get("symbol", "")] = int(d.get("fundingIntervalHours", 8) or 8)

    rows, quarterly = {}, []
    for d in prem:
        sym = d.get("symbol", "")
        m = re.match(r"^([A-Z0-9]+)_([0-9]{6})$", sym)
        if m:  # quarterly delivery contract e.g. BTCUSDT_250926
            braw = m.group(1)
            quarterly.append({
                "symbol": sym, "base_raw": braw,
                "base": braw[:-4] if braw.endswith("USDT") else braw,
                "mark": f(d.get("markPrice")), "expiry_yymmdd": m.group(2),
            })
            continue
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        rows[base] = {
            "rate": f(d.get("lastFundingRate")),
            "interval_h": interval.get(sym, 8),
            "mark": f(d.get("markPrice")),
            "base_norm": norm_base(base),
        }
    return {"rows": rows, "quarterly": quarterly}


# ------------------------------------------------------------------- Binance spot
async def get_binance_spot(session):
    data = await fetch_json(session, "https://api.binance.com/api/v3/ticker/bookTicker")
    if isinstance(data, dict) and "__error__" in data:
        return {"error": data["__error__"], "rows": {}}
    rows = {}
    for d in data:
        sym = d.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        bid, ask = f(d.get("bidPrice")), f(d.get("askPrice"))
        if bid > 0 and ask > 0:
            rows[sym[:-4]] = {"mid": (bid + ask) / 2, "bid": bid, "ask": ask}
    return {"rows": rows}


# -------------------------------------------------------------------- Bybit perp
async def get_bybit_perp(session):
    tick = await fetch_json(session, "https://api.bybit.com/v5/market/tickers?category=linear")
    inst = await fetch_json(session, "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000")
    if isinstance(tick, dict) and "__error__" in tick:
        return {"error": tick["__error__"], "rows": {}}

    interval = {}
    if isinstance(inst, dict):
        for d in ((inst.get("result") or {}).get("list") or []):
            fi = d.get("fundingInterval")
            if fi:
                interval[d.get("symbol", "")] = int(fi) / 60.0  # minutes -> hours

    rows = {}
    for d in ((tick.get("result") or {}).get("list") or []):
        sym = d.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        rows[base] = {
            "rate": f(d.get("fundingRate")),
            "interval_h": interval.get(sym, 8.0),
            "mark": f(d.get("markPrice")),
            "base_norm": norm_base(base),
        }
    return {"rows": rows}


# -------------------------------------------------------------------- Bybit spot
async def get_bybit_spot(session):
    data = await fetch_json(session, "https://api.bybit.com/v5/market/tickers?category=spot")
    if isinstance(data, dict) and "__error__" in data:
        return {"error": data["__error__"], "rows": {}}
    rows = {}
    for d in ((data.get("result") or {}).get("list") or []):
        sym = d.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        bid, ask = f(d.get("bid1Price")), f(d.get("ask1Price"))
        if bid > 0 and ask > 0:
            rows[sym[:-4]] = {"mid": (bid + ask) / 2, "bid": bid, "ask": ask}
    return {"rows": rows}


# ---------------------------------------------------------------------- OKX perp
async def get_okx_perp(session):
    tick = await fetch_json(session, "https://www.okx.com/api/v5/market/tickers?instType=SWAP")
    if isinstance(tick, dict) and "__error__" in tick:
        return {"error": tick["__error__"], "rows": {}}
    tickers = [t for t in (tick.get("data") or []) if t.get("instId", "").endswith("-USDT-SWAP")]
    tickers.sort(key=lambda t: f(t.get("volCcy24h")), reverse=True)
    chosen = tickers[:OKX_PERP_TOP_N]

    rows = {}
    sem = asyncio.Semaphore(8)

    async def one(t):
        instId = t["instId"]
        async with sem:
            fr = await fetch_json(session, f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}")
        arr = (fr.get("data") or [{}])[0] if isinstance(fr, dict) and "__error__" not in fr else {}
        iv_raw = str(arr.get("fundingInterval") or "8")
        iv = int(iv_raw) if iv_raw.isdigit() else 8
        base = instId.replace("-USDT-SWAP", "")
        rows[base] = {
            "rate": f(arr.get("fundingRate")),
            "interval_h": iv,
            "mark": f(t.get("markPx")) or f(t.get("last")),
            "base_norm": norm_base(base),
        }

    await asyncio.gather(*[one(t) for t in chosen])
    return {"rows": rows}


# ---------------------------------------------------------------------- OKX spot
async def get_okx_spot(session):
    data = await fetch_json(session, "https://www.okx.com/api/v5/market/tickers?instType=SPOT")
    if isinstance(data, dict) and "__error__" in data:
        return {"error": data["__error__"], "rows": {}}
    rows = {}
    for t in (data.get("data") or []):
        instId = t.get("instId", "")
        if not instId.endswith("-USDT"):
            continue
        bid, ask = f(t.get("bidPx")), f(t.get("askPx"))
        if bid > 0 and ask > 0:
            rows[instId[:-5]] = {"mid": (bid + ask) / 2, "bid": bid, "ask": ask}
    return {"rows": rows}


# --------------------------------------------------------------------- Gate perp
async def get_gate_perp(session):
    data = await fetch_json(session, "https://api.gateio.ws/api/v4/futures/usdt/tickers")
    if isinstance(data, dict) and "__error__" in data:
        return {"error": data["__error__"], "rows": {}}
    rows = {}
    for d in data:
        contract = d.get("contract", "")
        if not contract.endswith("_USDT"):
            continue
        base = contract[:-5]
        iv = int(f(d.get("fundingInterval"), 28800))
        rows[base] = {
            "rate": f(d.get("funding_rate")),
            "interval_h": max(iv // 3600, 1),
            "mark": f(d.get("mark_price")) or f(d.get("last")),
            "base_norm": norm_base(base),
        }
    return {"rows": rows}


# --------------------------------------------------------------- Hyperliquid perp
async def get_hl_perp(session):
    data = await fetch_json(session, "https://api.hyperliquid.xyz/info",
                            method="POST", body={"type": "metaAndAssetCtxs"})
    if isinstance(data, dict) and "__error__" in data:
        return {"error": data["__error__"], "rows": {}}
    rows = {}
    try:
        meta, ctxs = data
        for u, ctx in zip(meta.get("universe", []), ctxs):
            if u.get("isDelisted"):
                continue
            name = u.get("name", "")
            rows[name] = {
                "rate": f(ctx.get("funding")),   # hourly rate
                "interval_h": 1.0,
                "mark": f(ctx.get("markPx")),
                "open_interest": f(ctx.get("openInterest")),
                "day_volume": f(ctx.get("dayNtlVlm")),
                "base_norm": name[1:] if (name.startswith("k") and len(name) > 1
                                          and name[1].isupper()) else name,
            }
    except Exception as e:
        return {"error": f"parse: {e}", "rows": {}}
    return {"rows": rows}


# ------------------------------------------------------------------ HL spot (+xStocks)
async def get_hl_spot(session):
    data = await fetch_json(session, "https://api.hyperliquid.xyz/info",
                            method="POST", body={"type": "spotMetaAndAssetCtxs"})
    if isinstance(data, dict) and "__error__" in data:
        return {"error": data["__error__"], "rows": {}}
    rows = {}
    try:
        meta, ctxs = data
        tokens = meta.get("tokens", [])
        for u, ctx in zip(meta.get("universe", []), ctxs):
            try:
                tidx = u.get("tokens") or []
                if len(tidx) < 2 or max(tidx) >= len(tokens):
                    continue
                t0, t1 = tokens[tidx[0]], tokens[tidx[1]]
                pair_name = f"{t0.get('name')}/{t1.get('name')}"
                bid, ask = f(ctx.get("bid")), f(ctx.get("ask"))
                mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else f(ctx.get("midPx")) or f(ctx.get("prevDayPx"))
                rows[pair_name] = {
                    "mid": mid, "bid": bid, "ask": ask,
                    "day_volume": f(ctx.get("dayNtlVlm")),
                    "base": t0.get("name", ""), "quote": t1.get("name", ""),
                }
            except Exception:
                continue
    except Exception as e:
        return {"error": f"parse: {e}", "rows": {}}
    return {"rows": rows}


def summarize(name, res):
    if res.get("error"):
        print(f"  {name:22s} ERROR: {res['error']}")
        return 0
    n = len(res.get("rows", {}))
    print(f"  {name:22s} {n} instruments")
    return n


async def main():
    async with aiohttp.ClientSession() as session:
        (bin_perp, bin_spot, bb_perp, bb_spot,
         okx_perp, okx_spot, gate_perp, hl_perp, hl_spot) = await asyncio.gather(
            get_binance_perp(session), get_binance_spot(session),
            get_bybit_perp(session), get_bybit_spot(session),
            get_okx_perp(session), get_okx_spot(session),
            get_gate_perp(session),
            get_hl_perp(session), get_hl_spot(session),
        )

    print("Snapshot v2:", datetime.now(timezone.utc).isoformat())
    summarize("Binance perp", bin_perp)
    summarize("Binance spot", bin_spot)
    summarize("Bybit perp", bb_perp)
    summarize("Bybit spot", bb_spot)
    summarize("OKX perp", okx_perp)
    summarize("OKX spot", okx_spot)
    summarize("Gate perp", gate_perp)
    summarize("HL perp", hl_perp)
    summarize("HL spot", hl_spot)
    print(f"  Binance quarterly      {len(bin_perp.get('quarterly', []))} contracts")

    snap = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "perps": {
            "binance": {"rows": bin_perp["rows"], "error": bin_perp.get("error")},
            "bybit": {"rows": bb_perp["rows"], "error": bb_perp.get("error")},
            "okx": {"rows": okx_perp["rows"], "error": okx_perp.get("error")},
            "gate": {"rows": gate_perp["rows"], "error": gate_perp.get("error")},
            "hl": {"rows": hl_perp["rows"], "error": hl_perp.get("error")},
        },
        "spot": {
            "binance": {"rows": bin_spot["rows"], "error": bin_spot.get("error")},
            "bybit": {"rows": bb_spot["rows"], "error": bb_spot.get("error")},
            "okx": {"rows": okx_spot["rows"], "error": okx_spot.get("error")},
            "hl": {"rows": hl_spot["rows"], "error": hl_spot.get("error")},
        },
        "quarterly_binance": bin_perp.get("quarterly", []),
    }
    with open(OUT, "w") as fh:
        json.dump(snap, fh)
    print(f"Saved -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
