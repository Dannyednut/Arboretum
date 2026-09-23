#!/usr/bin/env python3
"""Round 3: Nado gateway query format, Orderly public data paths."""
import json

import aiohttp

TIMEOUT = 15
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120"}


async def post(s, label, url, body, maxp=350):
    try:
        async with s.post(url, json=body, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            txt = await r.text()
    except Exception as e:
        print(f"[{label}] ERR {type(e).__name__}: {str(e)[:110]}")
        return None
    try:
        d = json.loads(txt)
    except Exception:
        print(f"[{label}] HTTP {r.status} non-JSON: {txt[:110]}")
        return None
    print(f"[{label}] {r.status}: {json.dumps(d)[:maxp]}")
    return d


async def get(s, label, url, params=None, headers=None, maxp=350):
    try:
        async with s.get(url, params=params, headers=headers or UA,
                         timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            txt = await r.text()
    except Exception as e:
        print(f"[{label}] ERR {type(e).__name__}: {str(e)[:110]}")
        return None
    try:
        d = json.loads(txt)
    except Exception:
        print(f"[{label}] HTTP {r.status} non-JSON: {txt[:110]}")
        return None
    print(f"[{label}] {r.status}: {json.dumps(d)[:maxp]}")
    return d


async def main():
    async with aiohttp.ClientSession() as s:
        print("=" * 20, "NADO gateway.prod", "=" * 20)
        for body, lbl in (
                ({"all_markets": {}}, "all_markets"),
                ({"all_products": {}}, "all_products"),
                ({"query": {"markets": {}}}, "markets"),
        ):
            d = await post(s, f"nado {lbl}",
                           "https://gateway.prod.nado.xyz/v1/query", body)
            if d and isinstance(d, dict) and d.get("status") == 0:
                print("  -> WORKS with", lbl)
                break

        print("=" * 20, "ORDERLY public paths", "=" * 20)
        for path, lbl in (
                ("/v1/public/funding_rates/PERP_XAG_USDC", "pub fund"),
                ("/v1/public/info/PERP_XAG_USDC", "pub info"),
                ("/v1/public/orderbook/PERP_XAG_USDC", "pub book"),
                ("/v1/funding_rates/PERP_XAG_USDC", "fund"),
                ("/v1/info/PERP_XAG_USDC", "info"),
                ("/v1/system/info", "system info"),
                ("/v1/public/system_info", "pub system"),
        ):
            await get(s, f"orderly {lbl}",
                      f"https://api.orderly.org{path}", maxp=220)
        # with dummy account id (maybe 403 was UA-related)
        await get(s, "orderly book UA+dummy",
                  "https://api.orderly.org/v1/orderbook",
                  params={"symbol": "PERP_XAG_USDC", "max_level": 30},
                  headers={**UA, "orderly-account-id":
                           "0x1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"},
                  maxp=220)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
