#!/usr/bin/env python3
"""Round 2b: Orderly variants, Nado gateway from docs, depth limits."""
import json
import re

import aiohttp

TIMEOUT = 15
H = {"orderly-account-id": "0x0000000000000000000000000000000000000000",
     "orderly-timestamp": "1700000000000"}


async def get(s, label, url, params=None, headers=None, body=None):
    try:
        async with s.get(url, params=params, headers=headers,
                         timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            txt = await r.text()
    except Exception as e:
        print(f"[{label}] ERR {type(e).__name__}: {str(e)[:110]}")
        return None
    try:
        d = json.loads(txt)
    except Exception:
        print(f"[{label}] HTTP {r.status} non-JSON: {txt[:100]}")
        return None
    if r.status != 200:
        print(f"[{label}] HTTP {r.status}: {txt[:130]}")
        return None
    print(f"[{label}] 200: {json.dumps(d)[:260]}")
    return d


async def post(s, label, url, body):
    try:
        async with s.post(url, json=body,
                          timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            txt = await r.text()
    except Exception as e:
        print(f"[{label}] ERR {type(e).__name__}: {str(e)[:110]}")
        return None
    try:
        d = json.loads(txt)
    except Exception:
        print(f"[{label}] HTTP {r.status} non-JSON: {txt[:100]}")
        return None
    if r.status != 200:
        print(f"[{label}] HTTP {r.status}: {txt[:130]}")
        return None
    print(f"[{label}] 200: {json.dumps(d)[:260]}")
    return d


async def main():
    async with aiohttp.ClientSession() as s:
        print("=" * 25, "NADO gateway docs", "=" * 25)
        try:
            async with s.get(
                    "https://docs.nado.xyz/developer-resources/api/gateway",
                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                txt = await r.text()
            hosts = sorted(set(re.findall(
                r"https?://[a-z0-9.-]*nado[a-z0-9.-]*\.[a-z]{2,6}", txt)))
            print("nado hosts in docs:", hosts[:12])
            kw = sorted(set(re.findall(
                r'"?(https?://[^"\s]*?(?:gateway|indexer|archive)[^"\s]*?)"?',
                txt)))
            print("gateway-like:", kw[:8])
        except Exception as e:
            print("docs ERR", str(e)[:100])

        print("=" * 25, "ORDERLY variants", "=" * 25)
        await get(s, "orderly dummy-id", "https://api.orderly.org/v1/orderbook",
                  params={"symbol": "PERP_XAG_USDC"}, headers=H)
        await get(s, "orderly pub", "https://api.orderly.org/v1/pub/orderbook/PERP_XAG_USDC")
        await get(s, "orderly w/ max_level", "https://api.orderly.org/v1/orderbook",
                  params={"symbol": "PERP_XAG_USDC", "max_level": 50}, headers=H)

        print("=" * 25, "limits", "=" * 25)
        for lim in (500,):
            await get(s, f"bingx limit={lim}",
                      "https://open-api.bingx.com/openApi/swap/v2/quote/depth",
                      params={"symbol": "INJ-USDT", "limit": lim})
        for lim in (100, 500):
            await get(s, f"backpack limit={lim}",
                      "https://api.backpack.exchange/api/v1/depth",
                      params={"symbol": "AAVE_USDC_PERP", "limit": lim})


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
