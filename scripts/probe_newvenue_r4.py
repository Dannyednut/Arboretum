#!/usr/bin/env python3
"""Round 4: Nado typed queries (product id + orderbook), Orderly full info."""
import json

import aiohttp

TIMEOUT = 15
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120"}


async def post(s, label, url, body, maxp=500):
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


async def get(s, label, url, params=None, headers=None, maxp=600):
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
        print("=" * 20, "NADO typed queries", "=" * 20)
        d = await post(s, "nado all_markets typed",
                       "https://gateway.prod.nado.xyz/v1/query",
                       {"type": "all_markets"}, maxp=700)
        if d and isinstance(d, dict) and d.get("status") == 0:
            data = d.get("data", {})
            spots = data.get("spot") or []
            perps = data.get("perp") or []
            print(f"spot mkts={len(spots)} perp mkts={len(perps)}")
            if perps:
                print("perp[0]:", json.dumps(perps[0])[:300])
                # find LINK perp market
                for m in perps:
                    n = (m.get("market") or m.get("oracle") or "")
                    meta = m.get("metadata") or {}
                    if "LINK" in json.dumps(m)[:300]:
                        print("LINK match:", json.dumps(m)[:400])
                        break

        print("=" * 20, "ORDERLY public info full", "=" * 20)
        d = await get(s, "orderly info full",
                      "https://api.orderly.org/v1/public/info/PERP_XAG_USDC",
                      maxp=2000)
        if d and d.get("success") and isinstance(d.get("data"), dict):
            keys = sorted(d["data"].keys())
            print("info keys:", keys)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
