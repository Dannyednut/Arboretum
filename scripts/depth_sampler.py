#!/usr/bin/env python3
"""L2 depth + slippage sampler — Option 1 / Part-4 §6.1 gate.

Samples order books on 7 venues for the 69 candidate pairs from the 90d
persistence scan (51 E1 aster-short persistent pairs + 18 dydx-short pairs),
measures:
  - depth within 10/25/50/100 bps of mid, per side (USD)
  - VWAP slippage walking the book for notional ladder 0.5k..25k USD, per side
  - spread, mid, level counts

Protocol: 3 passes ~45s apart; per-venue fetchers normalized to (price, qty)
best-first. dYdX via v4_orderbook WebSocket snapshot (no REST book endpoint).
Raw rows -> download/data/l2_depth_samples.csv (append per pass, resume-safe).
"""
import asyncio
import csv
import json
import os
import time

import aiohttp

SNAP = "/home/z/my-project/scripts/snapshot_v3.json"
OUT_CSV = "/home/z/my-project/download/data/l2_depth_samples.csv"
PASSES = 3
PASS_GAP_S = 45
LADDER = [500, 1_000, 2_500, 5_000, 10_000, 25_000]
BANDS = [10, 25, 50, 100]          # bps
SEM_LIMIT = 4

VENUES = ["binance", "bybit", "okx", "hl", "aster", "dydx", "bitget"]
TAKER = {"binance": 0.0005, "bybit": 0.00055, "okx": 0.0005, "hl": 0.00045,
         "aster": 0.0004, "dydx": 0.0005, "bitget": 0.0006}

# ---- candidate legs (from funding_persistence_90d_v3.csv, computed below) ----
LEGS = {}          # (venue, base) -> {}


def load_legs():
    import functools
    rows = list(csv.DictReader(
        open("/home/z/my-project/download/data/funding_persistence_90d_v3.csv")))
    f = lambda r, k: float(r[k])
    pairs = {}
    for r in rows:
        e1p = "E1" in r["flag"] and f(r, "pos_day_frac") >= 0.95
        dydxp = r["short_venue"] == "dydx" and f(r, "net_apr_30d_hold") > 0.15
        if e1p or dydxp:
            pairs[(r["coin"], r["short_venue"], r["long_venue"])] = r
    legs = set()
    for (coin, sv, lv) in pairs:
        legs.add((sv, coin))
        legs.add((lv, coin))
    for v, b in sorted(legs):
        LEGS[(v, b)] = {}
    print(f"candidate pairs {len(pairs)}, unique legs {len(LEGS)}")
    return pairs


# ------------------------------------------------------------- book fetchers
ASTER_SYMS = {}
_session: aiohttp.ClientSession | None = None


async def _get(url, params=None, timeout=20):
    async with _session.get(url, params=params,
                            timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.json(content_type=None)


async def _post(url, body, timeout=20):
    async with _session.post(url, json=body,
                             timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.json(content_type=None)


def _pairs_levels(raw, rev=False):
    """[[p,q],...] strings -> [(float p, float q)] best-first."""
    out = [(float(p), float(q)) for p, q in raw]
    return out


async def book_binance(base):
    d = await _get("https://fapi.binance.com/fapi/v1/depth",
                   params={"symbol": f"{base}USDT", "limit": 1000})
    if "bids" not in d:
        raise RuntimeError(f"binance {base}: {str(d)[:80]}")
    return _pairs_levels(d["bids"]), _pairs_levels(d["asks"])


async def book_bybit(base):
    d = await _get("https://api.bybit.com/v5/market/orderbook",
                   params={"category": "linear", "symbol": f"{base}USDT",
                           "limit": 200})
    r = d.get("result") or {}
    if not r.get("b") and not r.get("bids"):
        raise RuntimeError(f"bybit {base}: {str(d)[:80]}")
    bids = _pairs_levels(r.get("b") or r.get("bids"))
    asks = _pairs_levels(r.get("a") or r.get("asks"))
    return bids, asks


async def book_okx(base):
    d = await _get("https://www.okx.com/api/v5/market/books",
                   params={"instId": f"{base}-USDT-SWAP", "sz": 400})
    if not d.get("data"):
        raise RuntimeError(f"okx {base}: {str(d)[:80]}")
    b = d["data"][0]
    bids = [(float(x[0]), float(x[1])) for x in b["bids"]]
    asks = [(float(x[0]), float(x[1])) for x in b["asks"]]
    return bids, asks


async def book_hl(base):
    d = await _post("https://api.hyperliquid.xyz/info",
                    {"type": "l2Book", "coin": base})
    if not (isinstance(d, dict) and d.get("levels")):
        raise RuntimeError(f"hl {base}: {str(d)[:80]}")
    bids = [(float(x["px"]), float(x["sz"])) for x in d["levels"][0]]
    asks = [(float(x["px"]), float(x["sz"])) for x in d["levels"][1]]
    return bids, asks


async def book_aster(base):
    d = await _get("https://fapi.asterdex.com/fapi/v1/depth",
                   params={"symbol": ASTER_SYMS.get(base, f"{base}USDT"),
                           "limit": 500})
    if "bids" not in d:
        raise RuntimeError(f"aster {base}: {str(d)[:80]}")
    return _pairs_levels(d["bids"]), _pairs_levels(d["asks"])


async def book_bitget(base):
    d = await _get("https://api.bitget.com/api/v2/mix/market/merge-depth",
                   params={"symbol": f"{base}USDT",
                           "productType": "USDT-FUTURES", "limit": 150})
    bk = d.get("data") or {}
    if not bk.get("bids"):
        raise RuntimeError(f"bitget {base}: {str(d)[:80]}")
    bids = [(float(p), float(q)) for p, q in bk["bids"]]
    asks = [(float(p), float(q)) for p, q in bk["asks"]]
    return bids, asks


BOOK = {"binance": book_binance, "bybit": book_bybit, "okx": book_okx,
        "hl": book_hl, "aster": book_aster, "bitget": book_bitget}


async def book_dydx(base):
    """Full-depth snapshot via v4_orderbook WS (REST book endpoint 404s)."""
    async with _session.ws_connect(
            "wss://indexer.dydx.trade/v4/ws",
            timeout=aiohttp.ClientWSTimeout(ws_close=10), heartbeat=20) as ws:
        await ws.send_str(json.dumps({"type": "subscribe",
                                      "channel": "v4_orderbook",
                                      "id": f"{base}-USD"}))
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=20)
            if msg.type != aiohttp.WSMsgType.TEXT:
                raise RuntimeError(f"dydx {base}: ws {msg.type}")
            d = json.loads(msg.data)
            if d.get("type") == "subscribed":
                c = d.get("contents", {})
                bids = [(float(x["price"]), float(x["size"]))
                        for x in c.get("bids") or []]
                asks = [(float(x["price"]), float(x["size"]))
                        for x in c.get("asks") or []]
                if not bids or not asks:
                    raise RuntimeError(f"dydx {base}: empty snapshot")
                return bids, asks
            if d.get("type") == "error":
                raise RuntimeError(f"dydx {base}: {str(d)[:100]}")


# ------------------------------------------------------------------- metrics
def walk(levels, mid, notional):
    """Walk best-first levels to fill `notional` USD.
    Returns (slip_bps>0, filled_notional, vwap). None if book empty."""
    remain_base = notional / mid
    cost = qty = 0.0
    for p, q in levels:
        if p <= 0:
            continue
        take = min(remain_base, q)
        cost += take * p
        qty += take
        remain_base -= take
        if remain_base <= 1e-18:
            break
    if qty <= 0:
        return None
    filled = qty * mid
    vwap = cost / qty
    slip = abs(vwap - mid) / mid * 1e4
    return slip, filled, vwap


def book_metrics(bids, asks):
    if not bids or not asks:
        return None
    bb, ba = bids[0][0], asks[0][0]
    if bb <= 0 or ba <= 0 or ba < bb:
        return None
    mid = (bb + ba) / 2.0
    m = {"mid": mid, "spread_bps": (ba - bb) / mid * 1e4,
         "n_bid": len(bids), "n_ask": len(asks)}
    for side, levels, sgn in (("bid", bids, -1), ("ask", asks, +1)):
        for k in BANDS:
            lim = mid * (1 + sgn * k / 1e4)
            m[f"depth_{side}_{k}"] = sum(p * q for p, q in levels
                                         if (p <= lim if sgn > 0 else p >= lim))
        for n in LADDER:
            r = walk(levels, mid, n)
            if r is None:
                m[f"slip_{side}_{n}"] = ""
                m[f"fill_{side}_{n}"] = 0.0
            else:
                slip, filled, _ = r
                m[f"slip_{side}_{n}"] = round(slip, 3)
                m[f"fill_{side}_{n}"] = round(filled / n, 4)
    return m


# --------------------------------------------------------------------- passes
COLS = ["venue", "base", "pass", "ts", "ok", "mid", "spread_bps", "n_bid",
        "n_ask"] + \
       [f"depth_{s}_{k}" for s in ("bid", "ask") for k in BANDS] + \
       [f"{p}_{s}_{n}" for p in ("slip", "fill") for s in ("bid", "ask")
        for n in LADDER]


def row_of(venue, base, p, ts, m):
    r = {"venue": venue, "base": base, "pass": p, "ts": ts, "ok": 1}
    r.update({k: (round(v, 6) if isinstance(v, float) else v)
              for k, v in m.items()})
    return {c: r.get(c, "") for c in COLS}


def row_fail(venue, base, p, ts, err):
    r = {"venue": venue, "base": base, "pass": p, "ts": ts, "ok": 0}
    return {c: r.get(c, "") for c in COLS} | {"err": str(err)[:120]}


async def sample_pass(session, p, results):
    sem = asyncio.Semaphore(SEM_LIMIT)
    lock = asyncio.Lock()

    async def one(venue, base):
        async with sem:
            ts = int(time.time())
            try:
                if venue == "dydx":
                    bids, asks = await book_dydx(base)
                else:
                    bids, asks = await BOOK[venue](base)
                m = book_metrics(bids, asks)
                if m is None:
                    raise RuntimeError("empty/crossed book")
                async with lock:
                    results.append(row_of(venue, base, p, ts, m))
                return True
            except Exception as e:
                async with lock:
                    results.append(row_fail(venue, base, p, ts, e))
                return False

    await asyncio.gather(*(one(v, b) for (v, b) in sorted(LEGS)))


async def main():
    global _session
    load_legs()
    async with aiohttp.ClientSession() as session:
        _session = session
        info = await _get("https://fapi.asterdex.com/fapi/v1/exchangeInfo")
        for x in info.get("symbols", []):
            if x.get("status") == "TRADING":
                ASTER_SYMS[x["baseAsset"].upper()] = x["symbol"]
        print(f"aster symbol map {len(ASTER_SYMS)}")

        write_header = not os.path.exists(OUT_CSV)
        for p in range(1, PASSES + 1):
            t0 = time.time()
            results = []
            await sample_pass(session, p, results)
            ok = sum(r["ok"] for r in results)
            with open(OUT_CSV, "a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=COLS + ["err"],
                                   extrasaction="ignore")
                if write_header:
                    w.writeheader()
                    write_header = False
                for r in results:
                    w.writerow(r)
            print(f"pass {p}: {ok}/{len(results)} legs ok "
                  f"({time.time()-t0:.0f}s) -> {OUT_CSV}")
            if p < PASSES:
                await asyncio.sleep(PASS_GAP_S)


if __name__ == "__main__":
    asyncio.run(main())
