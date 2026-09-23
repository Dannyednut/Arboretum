#!/usr/bin/env python3
"""Probe L2 orderbook endpoints for the 4 new venues discovered via Sharpe
(Part 10): Orderly, BingX, Backpack, Nado. Also probes symbol/funding info
endpoints to pin conventions. Prints raw JSON shape (truncated) for each try.

Run: python3 scripts/probe_newvenue_l2.py
"""
import json

import aiohttp

TIMEOUT = 15


async def show(session, label, method, url, params=None, body=None,
               keys=None):
    try:
        async with session.request(
                method, url, params=params, json=body,
                timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            txt = await r.text()
            try:
                d = json.loads(txt)
            except Exception:
                print(f"[{label}] HTTP {r.status} non-JSON: {txt[:120]}")
                return None
            if r.status != 200:
                print(f"[{label}] HTTP {r.status}: {txt[:150]}")
                return None
            print(f"[{label}] HTTP 200 keys={list(d)[:8] if isinstance(d, dict) else type(d)}")
            print(f"    body[:400] = {json.dumps(d)[:400]}")
            return d
    except Exception as e:
        print(f"[{label}] ERR {type(e).__name__}: {str(e)[:120]}")
        return None


async def main():
    async with aiohttp.ClientSession() as s:
        print("=" * 30, "ORDERLY", "=" * 30)
        # symbol conventions: try PERP_XAG_USDC style on several hosts
        for host in ("api.orderly.org", "api-evm.orderly.org"):
            await show(s, f"orderly {host} depth", "GET",
                       f"https://{host}/v1/orderbook",
                       params={"symbol": "PERP_XAG_USDC",
                               "max_level": 50})
        await show(s, "orderly info", "GET",
                   "https://api.orderly.org/v1/public/info/PERP_XAG_USDC")

        print("=" * 30, "BINGX", "=" * 30)
        await show(s, "bingx depth v2", "GET",
                   "https://open-api.bingx.com/openApi/swap/v2/quote/depth",
                   params={"symbol": "INJ-USDT", "limit": 100})
        await show(s, "bingx contracts", "GET",
                   "https://open-api.bingx.com/openApi/swap/v2/market/contracts",
                   params={"symbol": "INJ-USDT"})

        print("=" * 30, "BACKPACK", "=" * 30)
        await show(s, "backpack depth", "GET",
                   "https://api.backpack.exchange/api/v1/depth",
                   params={"symbol": "AAVE_USDC_PERP"})
        await show(s, "backpack markets", "GET",
                   "https://api.backpack.exchange/api/v1/markets")

        print("=" * 30, "NADO", "=" * 30)
        # gateway / archive query style (vertex-like JSON-RPC)
        await show(s, "nado gateway all_markets", "POST",
                   "https://gateway.nado.xyz/v1/query",
                   body={"all_markets": {}})
        await show(s, "nado archive all_markets", "POST",
                   "https://archive.nado.xyz/v1/query",
                   body={"all_markets": {}})
        await show(s, "nado indexer markets", "POST",
                   "https://indexer.nado.xyz/v1/query",
                   body={"all_products": {}})


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
