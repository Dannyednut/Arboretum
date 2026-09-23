#!/usr/bin/env python3
"""Round 2: Orderly public-book variants, Nado host discovery, depth limits."""
import json

import aiohttp

TIMEOUT = 15


async def show(s, label, method, url, params=None, body=None, headers=None,
               maxprint=300):
    try:
        async with session_req(s, method, url, params, body, headers) as (
                r, txt):
            try:
                d = json.loads(txt)
            except Exception:
                print(f"[{label}] HTTP {r.status} non-JSON: {txt[:100]}")
                return None
            if r.status != 200:
                print(f"[{label}] HTTP {r.status}: {txt[:130]}")
                return None
            print(f"[{label}] HTTP 200: {json.dumps(d)[:maxprint]}")
            return d
    except Exception as e:
        print(f"[{label}] ERR {type(e).__name__}: {str(e)[:110]}")
        return None


def session_req(s, method, url, params, body, headers):
    ctx = s.request(method, url, params=params, json=body, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=TIMEOUT))

    async def _wrap():
        r = await ctx.__aenter__()
        txt = await r.text()
        return r, txt

    return _wrap()


async def main():
    async with aiohttp.ClientSession() as s:
        print("=" * 25, "ORDERLY variants", "=" * 25)
        H = {"orderly-account-id": "0x0000000000000000000000000000000000000000"}
        await show(s, "orderly book w/ dummy id", "GET",
                   "https://api.orderly.org/v1/orderbook",
                   params={"symbol": "PERP_XAG_USDC"}, headers=H)
        await show(s, "orderly pub path", "GET",
                   "https://api.orderly.org/v1/pub/orderbook/PERP_XAG_USDC")
        await show(s, "orderly viewer", "GET",
                   "https://api.orderly.org/v1/viewer/orderbook",
                   params={"symbol": "PERP_XAG_USDC"}, headers=H)

        print("=" * 25, "NADO host discovery", "=" * 25)
        for u in ("https://nado.xyz", "https://www.nado.xyz"):
            try:
                async with s.get(u, timeout=aiohttp.ClientTimeout(total=15),
                                 allow_redirects=True) as r:
                    txt = await r.text()
                hits = sorted({frag for frag in
                               ("gateway.", "indexer.", "api.", "archive.",
                                "ws.") for frag in [frag]})[:5]
                import re
                urls = set(re.findall(
                    r"https?://[a-zA-Z0-9.-]*nado[a-zA-Z0-9.-]*\.[a-z]{2,4}"
                    r"[/a-zA-Z0-9._-]*", txt))
                hosts = set(re.findall(
                    r"https?://(gateway|indexer|archive|api)"
                    r"[a-zA-Z0-9.-]*\.[a-z]{2,6}", txt))
                print(f"[{u}] HTTP {r.status} len={len(txt)} "
                      f"nado-urls={sorted(urls)[:8]} api-hosts={sorted(hosts)}")
            except Exception as e:
                print(f"[{u}] ERR {str(e)[:90]}")

        print("=" * 25, "BINGX limit / BACKPACK limit", "=" * 25)
        for lim in (500, 1000):
            d = await show(s, f"bingx depth limit={lim}", "GET",
                           "https://open-api.bingx.com/openApi/swap/v2/quote"
                           "/depth", params={"symbol": "INJ-USDT",
                                             "limit": lim}, maxprint=120)
        for lim in (100, 500):
            d = await show(s, f"backpack depth limit={lim}", "GET",
                           "https://api.backpack.exchange/api/v1/depth",
                           params={"symbol": "AAVE_USDC_PERP",
                                   "limit": lim}, maxprint=120)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
