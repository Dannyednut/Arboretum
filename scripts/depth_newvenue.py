#!/usr/bin/env python3
"""L2 depth sampler for Sharpe-discovered new venues (Part 11).

Pairs under gate (from Part 10 honest cohort):
  INJ  BingX->okx
  AAVE Backpack->okx
  LINK Nado->okx
  XAG  Orderly->dydx  (Orderly book auth-gated -> only dydx leg measured;
                       Orderly side uses Sharpe executableDepthUsd proxy)

Reuses depth_sampler.walk / book_metrics / COLS. 3 passes, 45s apart.
Also records native funding rates + Nado native taker fee (identity guard).
Raw rows -> download/data/l2_depth_samples_newvenue.csv
"""
import asyncio
import csv
import json
import os
import time

import aiohttp

import depth_sampler as ds

OUT = "/home/z/my-project/download/data/l2_depth_samples_newvenue.csv"
PASSES = 3
PASS_GAP_S = 45
GW = "https://gateway.prod.nado.xyz/v1/query"

LEGS = [("bingx", "INJ"), ("okx", "INJ"),
        ("backpack", "AAVE"), ("okx", "AAVE"),
        ("nado", "LINK"), ("okx", "LINK"),
        ("dydx", "XAG")]

NADO_PID = {}          # base -> (product_id, taker_fee, maker_fee)
NADO_DEPTH_LEVELS = 100

_session: aiohttp.ClientSession | None = None


async def _get(url, params=None, timeout=20):
    async with _session.get(url, params=params,
                            timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.json(content_type=None)


async def _post(url, body, timeout=20):
    async with _session.post(url, json=body,
                             timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.json(content_type=None)


# ------------------------------------------------------------------- fetchers
async def book_bingx(base):
    d = await _get("https://open-api.bingx.com/openApi/swap/v2/quote/depth",
                   params={"symbol": f"{base}-USDT", "limit": 500})
    if d.get("code") != 0 or not d.get("data"):
        raise RuntimeError(f"bingx {base}: {str(d)[:90]}")
    b = d["data"]
    bids = [(float(p), float(q)) for p, q in b.get("bids") or []]
    asks = [(float(p), float(q)) for p, q in b.get("asks") or []]
    return bids, asks


async def book_backpack(base):
    d = await _get("https://api.backpack.exchange/api/v1/depth",
                   params={"symbol": f"{base}_USDC_PERP", "limit": 500})
    if not d.get("bids"):
        raise RuntimeError(f"backpack {base}: {str(d)[:90]}")
    bids = [(float(p), float(q)) for p, q in d["bids"]]
    asks = [(float(p), float(q)) for p, q in d["asks"]]
    return bids, asks


async def book_nado(base):
    pid = NADO_PID.get(base)
    if not pid:
        raise RuntimeError(f"nado {base}: no product id")
    d = await _post(GW, {"type": "market_liquidity", "product_id": pid,
                         "depth": NADO_DEPTH_LEVELS})
    if not (isinstance(d, dict) and d.get("status") == "success"):
        raise RuntimeError(f"nado {base}: {str(d)[:90]}")
    dd = d["data"]
    bids = [(int(p) / 1e18, int(q) / 1e18) for p, q in dd["bids"]]
    asks = [(int(p) / 1e18, int(q) / 1e18) for p, q in dd["asks"]]
    return bids, asks


async def load_nado_symbols():
    d = await _post(GW, {"type": "symbols"})
    smap = d["data"]["symbols"]
    for k, v in smap.items():
        if isinstance(v, dict) and v.get("type") == "perp":
            base = k.replace("-PERP", "")
            NADO_PID[base] = int(v["product_id"])
            NADO_PID[f"__fee__{base}"] = (
                float(v.get("taker_fee_rate_x18", 0)) / 1e18,
                float(v.get("maker_fee_rate_x18", 0)) / 1e18)
    print(f"nado perp symbols: {len(NADO_PID) // 2}")


# ------------------------------------------------------- native guard probes
async def native_guard(results):
    """Live funding on each leg + Nado native fee (tripwire #1 data)."""
    print("\n--- native guard (live funding / fees) ---")
    # Nado LINK native taker fee
    fee = NADO_PID.get("__fee__LINK")
    if fee:
        print(f"nado LINK-PERP native taker={fee[0]*1e4:.2f}bps "
              f"maker={fee[1]*1e4:.2f}bps")
        results.append(("nado_LINK_taker_bps", round(fee[0] * 1e4, 2)))

    probes = []
    for inst, base in (("AAVE_USDC_PERP", "AAVE"),):
        probes.append(("backpack", f"funding {base}",
                       "https://api.backpack.exchange/api/v1/fundingRates",
                       {"symbol": inst}))
    probes.append(("bingx", "funding INJ",
                   "https://open-api.bingx.com/openApi/swap/v2/quote"
                   "/premiumIndex", {"symbol": "INJ-USDT"}))
    for c in ("AAVE", "INJ", "LINK"):
        probes.append(("okx", f"funding {c}",
                       "https://www.okx.com/api/v5/public/funding-rate",
                       {"instId": f"{c}-USDT-SWAP"}))
    probes.append(("dydx", "funding XAG",
                   "https://indexer.dydx.trade/v4/perpetualMarkets",
                   {"ticker": "XAG-USD"}))
    probes.append(("sharpe", "funding XAG orderly",
                   "https://www.sharpe.ai/api/funding/rates",
                   {"type": "current", "coin": "XAG"}))

    for venue, lbl, url, params in probes:
        try:
            d = await _get(url, params=params)
            if venue == "backpack":
                rate = d[-1]["fundingRate"] if isinstance(d, list) and d \
                    else None
                print(f"backpack {lbl}: last={rate}")
                results.append(("backpack_AAVE_funding", rate))
            elif venue == "bingx":
                dd = (d.get("data") or {})
                print(f"bingx {lbl}: fundingRate={dd.get('fundingRate')} "
                      f"next={dd.get('predictedFundingRate')}")
                results.append(("bingx_INJ_funding", dd.get("fundingRate")))
            elif venue == "okx":
                dd = (d.get("data") or [{}])[0]
                print(f"okx {lbl}: rate={dd.get('fundingRate')} "
                      f"next={dd.get('nextFundingRate')}")
                results.append((f"okx_{lbl.split()[-1]}_funding",
                                dd.get("fundingRate")))
            elif venue == "dydx":
                m = (d.get("markets") or {}).get("XAG-USD", {})
                print(f"dydx {lbl}: fundingRate={m.get('fundingRate')} "
                      f"next={m.get('nextFundingRate')} "
                      f"oracle={m.get('oraclePrice')}")
                results.append(("dydx_XAG_funding", m.get("fundingRate")))
            elif venue == "sharpe":
                rows = d if isinstance(d, list) else (d.get("data") or [])
                hit = [r for r in rows
                       if "orderly" in json.dumps(r).lower()
                       and ("XAG" in json.dumps(r))]
                if hit:
                    print(f"sharpe orderly XAG: "
                          f"{json.dumps(hit[0])[:220]}")
                    results.append(("sharpe_orderly_XAG", json.dumps(hit[0])))
        except Exception as e:
            print(f"{venue} {lbl}: ERR {type(e).__name__} {str(e)[:100]}")


# ------------------------------------------------------------------- passes
async def sample_pass(p, results):
    sem = asyncio.Semaphore(4)
    lock = asyncio.Lock()

    async def one(venue, base):
        async with sem:
            ts = int(time.time())
            try:
                if venue in ("okx", "dydx"):
                    if venue == "dydx":
                        bids, asks = await ds.book_dydx(base)
                    else:
                        bids, asks = await ds.book_okx(base)
                elif venue == "bingx":
                    bids, asks = await book_bingx(base)
                elif venue == "backpack":
                    bids, asks = await book_backpack(base)
                elif venue == "nado":
                    bids, asks = await book_nado(base)
                else:
                    raise RuntimeError(f"no fetcher for {venue}")
                m = ds.book_metrics(bids, asks)
                if m is None:
                    raise RuntimeError("empty/crossed book")
                async with lock:
                    results.append(ds.row_of(venue, base, p, ts, m))
                return True
            except Exception as e:
                async with lock:
                    results.append(ds.row_fail(venue, base, p, ts, e))
                return False

    await asyncio.gather(*(one(v, b) for (v, b) in sorted(LEGS)))


async def main():
    global _session
    async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0"}) as session:
        _session = session
        ds._session = session
        await load_nado_symbols()

        guard = []
        await native_guard(guard)

        write_header = not os.path.exists(OUT)
        for p in range(1, PASSES + 1):
            t0 = time.time()
            results = []
            await sample_pass(p, results)
            ok = sum(r["ok"] for r in results)
            with open(OUT, "a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=ds.COLS + ["err"],
                                   extrasaction="ignore")
                if write_header:
                    w.writeheader()
                    write_header = False
                for r in results:
                    w.writerow(r)
            print(f"pass {p}: {ok}/{len(results)} legs ok "
                  f"({time.time()-t0:.0f}s)")
            if p < PASSES:
                await asyncio.sleep(PASS_GAP_S)

        print("\n--- guard results ---")
        for k, v in guard:
            print(f"  {k} = {v}")


if __name__ == "__main__":
    asyncio.run(main())
