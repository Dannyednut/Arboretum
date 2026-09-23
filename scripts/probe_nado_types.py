#!/usr/bin/env python3
"""Round 5: capture full Nado error (valid types), then query orderbook."""
import json

import aiohttp

TIMEOUT = 15
GW = "https://gateway.prod.nado.xyz/v1/query"


async def post(s, body, maxp=2000):
    async with s.post(GW, json=body,
                      timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
        txt = await r.text()
        try:
            return r.status, json.loads(txt)
        except Exception:
            return r.status, txt


async def main():
    async with aiohttp.ClientSession() as s:
        # 1. full error lists valid types
        st, d = await post(s, {"type": "bogus_type"})
        print("status", st)
        print("full error:", d if isinstance(d, str) else json.dumps(d)[:3000])

        # 2. try likely orderbook/product queries once types known is hard;
        #    try common vertex-style with type wrapper variants
        for body in (
                {"type": "all_products"},
                {"type": "all_markets"},
                {"type": "markets", "product_ids": []},
        ):
            st, d = await post(s, body)
            print(f"\n== {body} -> {st}: "
                  f"{d if isinstance(d, str) else json.dumps(d)[:500]}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
