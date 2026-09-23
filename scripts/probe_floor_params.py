#!/usr/bin/env python3
"""Probe native funding-param sources for the P2 floor monitor (TW5).

Per spec Part 12 §3.2 TW5: fetch venue funding params natively
(aster exchangeInfo; orderly /v1/public/info cap/floor/interest;
backpack markets; hl per-market config; dydx perpetualMarkets fields)
so any parameter change on a held/entering leg freezes that venue."""
import asyncio
import json

import aiohttp

HZ = {"User-Agent": "Mozilla/5.0"}


async def g(s, url, params=None):
    try:
        async with s.get(url, params=params, headers=HZ,
                         timeout=aiohttp.ClientTimeout(total=20)) as r:
            return await r.json(content_type=None)
    except Exception as e:
        return {"__err__": repr(e)}


async def main():
    async with aiohttp.ClientSession() as s:
        # 1) Aster exchangeInfo - funding params per symbol?
        d = await g(s, "https://fapi.asterdex.com/fapi/v1/exchangeInfo")
        if isinstance(d, dict) and d.get("symbols"):
            sy = [x for x in d["symbols"] if x.get("symbol") in
                  ("BTWUSDT", "UAIUSDT")] or d["symbols"][:2]
            print("aster keys:", sorted(sy[0].keys()))
            for x in sy:
                print("  aster", x.get("symbol"), {k: x.get(k) for k in
                      ("fundingRate", "fundingInterval", "disclaimer") if k in x})
        else:
            print("aster exchangeInfo ERR:", str(d)[:120])

        # 2) Orderly public info - cap_funding/floor_funding/interest_rate
        d = await g(s, "https://api.orderly.org/v1/public/info")
        if isinstance(d, dict):
            row = (d.get("data") or {})
            print("orderly info keys sample:", [k for k in row.keys()
                  if "fund" in k.lower() or "interest" in k.lower()][:8])
            print("  orderly", {k: row.get(k) for k in
                  ("cap_funding", "floor_funding", "interest_rate")})
        else:
            print("orderly ERR:", str(d)[:120])

        # 3) Backpack markets - funding fields per symbol
        d = await g(s, "https://api.backpack.exchange/api/v1/markets")
        if isinstance(d, list):
            m = next((x for x in d if x.get("symbol") == "XMR_USDC_PERP"), None)
            if m:
                fk = {k: v for k, v in m.items() if "fund" in k.lower()
                      or "interest" in k.lower()}
                print("backpack XMR funding fields:", fk)
        else:
            print("backpack markets ERR:", str(d)[:120])

        # 4) HL per-market config (meta + assetCtxs for XMR)
        d = await g(s, "https://api.hyperliquid.xyz/info", None) if False else \
            await g(s, "https://api.hyperliquid.xyz/info")
        # HL needs POST; do it manually below

        async with s.post("https://api.hyperliquid.xyz/info",
                          json={"type": "metaAndAssetCtxs"},
                          timeout=aiohttp.ClientTimeout(total=20)) as r:
            meta = await r.json(content_type=None)
        try:
            uni, ctxs = meta
            i = next(i for i, a in enumerate(uni["universe"])
                     if a["name"] == "XMR")
            m, c = uni["universe"][i], ctxs[i]
            print("hl XMR meta:", {k: m.get(k) for k in
                  ("fundingInterval", "impactNotional", "isDelisted")})
            print("hl XMR ctx funding:", c.get("funding"),
                  "openInterest:", c.get("openInterest"))
        except Exception as e:
            print("hl parse ERR:", repr(e)[:120])

        # 5) dYdX perpetualMarkets per-market funding fields
        d = await g(s, "https://indexer.dydx.trade/v4/perpetualMarkets",
                    params={"ticker": "XMR-USD"})
        m = (d.get("markets") or {}).get("XMR-USD") if isinstance(d, dict) \
            else None
        if m:
            fk = {k: m.get(k) for k in m.keys()
                  if "fund" in k.lower() or "interval" in k.lower()}
            print("dydx XMR funding fields:", fk)
        else:
            print("dydx ERR:", str(d)[:120])

        # 6) Bitget tickers funding fields (long leg BTW) - interval change detect
        d = await g(s, "https://api.bitget.com/api/v2/mix/market/tickers",
                    params={"productType": "usdt-futures"})
        if isinstance(d, dict) and d.get("data"):
            m = next((x for x in d["data"] if x.get("symbol") == "BTWUSDT"),
                     None)
            if m:
                print("bitget BTW ticker funding:", {k: m.get(k) for k in
                      ("fundingRate", "nextFundingTime") if k in m})


asyncio.run(main())
