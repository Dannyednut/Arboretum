#!/usr/bin/env python3
"""Probe L2 orderbook endpoints on all 7 venues for the depth-gate sampler.
Prints response structure, level counts, top of book, and raw-symbol resolution
for a few tricky bases (XMR, 龙虾, BTW, MAGMA, SPX)."""
import asyncio
import json
import sys

import aiohttp

sys.path.insert(0, "/home/z/my-project/scripts")
SNAP = "/home/z/my-project/scripts/snapshot_v3.json"
VENUES = ["binance", "bybit", "okx", "hl", "aster", "dydx", "bitget"]

snap = json.load(open(SNAP))
perps = {v: snap["perps"][v].get("rows", {}) for v in VENUES}
resolve = {v: {} for v in VENUES}
for v in VENUES:
    for raw, r in perps[v].items():
        resolve[v][r.get("base_norm", raw)] = raw

print("== raw symbol resolution ==")
for b in ["XMR", "龙虾", "BTW", "MAGMA", "SPX", "USELESS", "ONDO", "ZEC", "ICP"]:
    print(f"  {b:10s}: " + "  ".join(
        f"{v}={resolve[v][b] if b in resolve[v] else '-'}" for v in VENUES))

PROBE_BASE = {"binance": "XMR", "bybit": "XMR", "okx": "USELESS", "hl": "ONDO",
              "aster": "XMR", "dydx": "XMR", "bitget": "XMR"}


async def fetch_json(session, url, params=None, timeout=20):
    async with session.get(url, params=params,
                           timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.json(content_type=None)


async def post_json(session, url, body, timeout=20):
    async with session.post(url, json=body,
                            timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.json(content_type=None)


def api_sym(venue, base):
    """snapshot rows are keyed by BASE name; build per-venue API symbols."""
    return {"binance": f"{base}USDT", "bybit": f"{base}USDT",
            "okx": f"{base}-USDT-SWAP", "hl": base,
            "aster": ASTER_SYMS.get(base, f"{base}USDT"),
            "dydx": f"{base}-USD", "bitget": f"{base}USDT"}[venue]


ASTER_SYMS = {}


def show(venue, label, d):
    s = json.dumps(d)
    print(f"  [{venue}] {label}: {s[:220]}")


async def probe(session, venue):
    base = PROBE_BASE[venue]
    raw = api_sym(venue, base)
    try:
        if venue == "binance":
            d = await fetch_json(session, "https://fapi.binance.com/fapi/v1/depth",
                                 params={"symbol": raw, "limit": 1000})
            if "bids" not in d:
                return show(venue, f"{raw} ERR", d)
            print(f"binance {raw}: bids={len(d['bids'])} asks={len(d['asks'])} "
                  f"top={d['bids'][0]}/{d['asks'][0]}")
        elif venue == "bybit":
            d = await fetch_json(session, "https://api.bybit.com/v5/market/orderbook",
                                 params={"category": "linear", "symbol": raw, "limit": 200})
            r = d.get("result") or {}
            if not r.get("bids"):
                return show(venue, f"{raw} ERR", d)
            print(f"bybit {raw}: retCode={d.get('retCode')} bids={len(r['bids'])} "
                  f"asks={len(r['asks'])} top={r['bids'][0]}/{r['asks'][0]}")
        elif venue == "okx":
            d = await fetch_json(session, "https://www.okx.com/api/v5/market/books",
                                 params={"instId": raw, "sz": 400})
            if not d.get("data"):
                return show(venue, f"{raw} ERR", d)
            b = d["data"][0]
            print(f"okx {raw}: code={d.get('code')} bids={len(b['bids'])} "
                  f"asks={len(b['asks'])} top={b['bids'][0][:2]}/{b['asks'][0][:2]}")
        elif venue == "hl":
            d = await post_json(session, "https://api.hyperliquid.xyz/info",
                                {"type": "l2Book", "coin": raw})
            if not (isinstance(d, dict) and d.get("levels")):
                return show(venue, f"{raw} ERR", d)
            bids, asks = d["levels"][0], d["levels"][1]   # levels = [bids, asks]
            print(f"hl {raw}: bids={len(bids)} asks={len(asks)} "
                  f"top={bids[0]}/{asks[0]}")
        elif venue == "aster":
            d = await fetch_json(session, "https://fapi.asterdex.com/fapi/v1/depth",
                                 params={"symbol": raw, "limit": 500})
            if "bids" not in d:
                return show(venue, f"{raw} ERR", d)
            print(f"aster {raw}: bids={len(d['bids'])} asks={len(d['asks'])} "
                  f"top={d['bids'][0]}/{d['asks'][0]}")
        elif venue == "dydx":
            t = raw
            d = await fetch_json(session, "https://indexer.dydx.trade/v4/orderbooks",
                                 params={"ticker": t})
            if not isinstance(d, dict):
                return show(venue, f"{t} ERR", d)
            bk = d.get(t)
            if not isinstance(bk, dict) or not bk.get("bids"):
                return show(venue, f"{t} ERR", d)
            print(f"dydx {t}: bids={len(bk['bids'])} asks={len(bk['asks'])} "
                  f"top={bk['bids'][0]}/{bk['asks'][0]}")
        elif venue == "bitget":
            d = await fetch_json(session, "https://api.bitget.com/api/v2/mix/market/merge-depth",
                                 params={"symbol": raw, "productType": "USDT-FUTURES",
                                         "limit": 150})
            bk = d.get("data") or {}
            if not bk.get("bids"):
                return show(venue, f"{raw} ERR", d)
            print(f"bitget {raw}: code={d.get('code')} bids={len(bk['bids'])} "
                  f"asks={len(bk['asks'])} top={bk['bids'][0]}/{bk['asks'][0]}")
    except Exception as e:
        print(f"{venue} {base} ({raw}): FAIL {type(e).__name__}: {e}")


async def main():
    async with aiohttp.ClientSession() as s:
        info = await fetch_json(s, "https://fapi.asterdex.com/fapi/v1/exchangeInfo")
        for x in info.get("symbols", []):
            if x.get("status") == "TRADING":
                ASTER_SYMS[x["baseAsset"].upper()] = x["symbol"]
        print(f"Aster symbol map: {len(ASTER_SYMS)} symbols")
        for v in VENUES:
            await probe(s, v)
            await asyncio.sleep(0.3)


if __name__ == "__main__":
    asyncio.run(main())
