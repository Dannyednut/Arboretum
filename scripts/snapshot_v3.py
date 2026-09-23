#!/usr/bin/env python3
"""
Venue snapshot v3 - EXPANDED venue map (user direction: Aster + DEX integration).

Adds to snapshot_v2 (which it reuses via import):
  ASTER  (fapi.asterdex.com - Binance-compatible perp DEX, 1h/8h funding intervals)
  dYdX   (v4 indexer - hourly funding)
  BITGET (v2 mix API - fundInterval + fee schedule from API)

Keeps: Binance, Bybit, OKX (top-80), Gate, Hyperliquid perps; Binance/Bybit/OKX/HL spot.

Output: /home/z/my-project/scripts/snapshot_v3.json
"""
import asyncio
import aiohttp
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/home/z/my-project/scripts")
from snapshot_v2 import (fetch_json, f, norm_base, summarize,
                         get_binance_perp, get_binance_spot,
                         get_bybit_perp, get_bybit_spot,
                         get_okx_perp, get_okx_spot,
                         get_gate_perp, get_hl_perp, get_hl_spot)

OUT = "/home/z/my-project/scripts/snapshot_v3.json"


# ------------------------------------------------------------------ Aster perp
async def get_aster_perp(session):
    exch = await fetch_json(session, "https://fapi.asterdex.com/fapi/v1/exchangeInfo")
    prem = await fetch_json(session, "https://fapi.asterdex.com/fapi/v1/premiumIndex")
    finfo = await fetch_json(session, "https://fapi.asterdex.com/fapi/v1/fundingInfo")
    tick = await fetch_json(session, "https://fapi.asterdex.com/fapi/v1/ticker/24hr")
    if isinstance(exch, dict) and "__error__" in exch:
        return {"error": exch["__error__"], "rows": {}}

    # only TRADING symbols
    live = {d.get("symbol") for d in exch.get("symbols", []) if d.get("status") == "TRADING"}
    interval = {}
    if isinstance(finfo, list):
        for d in finfo:
            interval[d.get("symbol", "")] = int(d.get("fundingIntervalHours", 8) or 8)
    vol = {}
    if isinstance(tick, list):
        for d in tick:
            vol[d.get("symbol", "")] = f(d.get("quoteVolume"))

    rows = {}
    if isinstance(prem, list):
        for d in prem:
            sym = d.get("symbol", "")
            if sym not in live or not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            rows[base] = {
                "rate": f(d.get("lastFundingRate")),
                "interval_h": interval.get(sym, 8),
                "mark": f(d.get("markPrice")),
                "day_volume": vol.get(sym, 0.0),
                "base_norm": norm_base(base),
            }
    return {"rows": rows}


# ------------------------------------------------------------------- dYdX perp
async def get_dydx_perp(session):
    data = await fetch_json(session, "https://indexer.dydx.trade/v4/perpetualMarkets")
    if isinstance(data, dict) and "__error__" in data:
        return {"error": data["__error__"], "rows": {}}
    rows = {}
    for m in (data.get("markets") or {}).values():
        if m.get("status") != "ACTIVE":
            continue
        tick = m.get("ticker", "")
        if not tick.endswith("-USD"):
            continue
        base = tick.replace("-USD", "")
        # dYdX v4: funding accrues and settles EVERY HOUR; nextFundingRate is 1h rate
        rows[base] = {
            "rate": f(m.get("nextFundingRate")),
            "interval_h": 1.0,
            "mark": f(m.get("oraclePrice")),
            "day_volume": f(m.get("volume24H")),
            "base_norm": norm_base(base),
        }
    return {"rows": rows}


# ----------------------------------------------------------------- Bitget perp
async def get_bitget_perp(session):
    tick = await fetch_json(session, "https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES")
    contr = await fetch_json(session, "https://api.bitget.com/api/v2/mix/market/contracts?productType=USDT-FUTURES")
    if isinstance(tick, dict) and "__error__" in tick:
        return {"error": tick["__error__"], "rows": {}}
    info = {}
    if isinstance(contr, dict):
        for d in (contr.get("data") or []):
            if d.get("symbolStatus") == "normal" and d.get("quoteCoin") == "USDT":
                info[d.get("symbol", "")] = {
                    "iv": int(f(d.get("fundInterval"), 8) or 8),
                    "taker": f(d.get("takerFeeRate"), 0.0006),
                }
    rows = {}
    for d in ((tick.get("data") or [])):
        sym = d.get("symbol", "")
        if not sym.endswith("USDT") or sym not in info:
            continue
        base = sym[:-4]
        rows[base] = {
            "rate": f(d.get("fundingRate")),
            "interval_h": info[sym]["iv"],
            "mark": f(d.get("indexPrice")) or f(d.get("lastPr")),
            "day_volume": f(d.get("usdtVolume")),
            "taker_fee": info[sym]["taker"],
            "base_norm": norm_base(base),
        }
    return {"rows": rows}


async def main():
    async with aiohttp.ClientSession() as session:
        (bin_perp, bin_spot, bb_perp, bb_spot,
         okx_perp, okx_spot, gate_perp, hl_perp, hl_spot,
         aster, dydx, bitget) = await asyncio.gather(
            get_binance_perp(session), get_binance_spot(session),
            get_bybit_perp(session), get_bybit_spot(session),
            get_okx_perp(session), get_okx_spot(session),
            get_gate_perp(session),
            get_hl_perp(session), get_hl_spot(session),
            get_aster_perp(session), get_dydx_perp(session), get_bitget_perp(session),
        )

    print("Snapshot v3:", datetime.now(timezone.utc).isoformat())
    summarize("Binance perp", bin_perp)
    summarize("Bybit perp", bb_perp)
    summarize("OKX perp", okx_perp)
    summarize("Gate perp", gate_perp)
    summarize("HL perp", hl_perp)
    summarize("ASTER perp", aster)
    summarize("dYdX perp", dydx)
    summarize("Bitget perp", bitget)
    summarize("Binance spot", bin_spot)
    summarize("Bybit spot", bb_spot)
    summarize("OKX spot", okx_spot)
    summarize("HL spot", hl_spot)

    snap = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "perps": {
            "binance": {"rows": bin_perp["rows"], "error": bin_perp.get("error")},
            "bybit": {"rows": bb_perp["rows"], "error": bb_perp.get("error")},
            "okx": {"rows": okx_perp["rows"], "error": okx_perp.get("error")},
            "gate": {"rows": gate_perp["rows"], "error": gate_perp.get("error")},
            "hl": {"rows": hl_perp["rows"], "error": hl_perp.get("error")},
            "aster": {"rows": aster["rows"], "error": aster.get("error")},
            "dydx": {"rows": dydx["rows"], "error": dydx.get("error")},
            "bitget": {"rows": bitget["rows"], "error": bitget.get("error")},
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
