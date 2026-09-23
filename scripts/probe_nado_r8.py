#!/usr/bin/env python3
"""Round 8: Nado pid->symbol from symbols map; LINK book + taker fee."""
import json

import aiohttp

TIMEOUT = 15
GW = "https://gateway.prod.nado.xyz/v1/query"


async def post(s, body, maxp=4000):
    async with s.post(GW, json=body,
                      timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
        txt = await r.text()
        try:
            return r.status, json.loads(txt)
        except Exception:
            return r.status, txt


async def main():
    async with aiohttp.ClientSession() as s:
        st, syms = await post(s, {"type": "symbols"}, maxp=200000)
        smap = syms["data"]["symbols"]
        print("n symbols:", len(smap))
        # dump one full entry to see fee fields
        k0 = next(iter(smap))
        print("entry example:", k0, json.dumps(smap[k0])[:500])

        pid2sym = {v["product_id"]: k for k, v in smap.items()
                   if isinstance(v, dict) and "product_id" in v}
        link = [k for k in smap if k.upper().startswith("LINK")]
        peng = [k for k in smap if k.upper().startswith("PENG")]
        print("LINK entries:", link, "PENG entries:", peng)

        for target_name in (link[0] if link else None,
                            peng[0] if peng else None):
            if not target_name:
                continue
            v = smap[target_name]
            pid = v["product_id"]
            print(f"\n== {target_name} pid={pid}")
            for f in ("maker_fee_x18", "taker_fee_x18", "maker_fee",
                      "taker_fee", "min_size", "size_increment"):
                if f in v:
                    print(f"   {f} = {v[f]}")
            st, d = await post(s, {"type": "market_liquidity",
                                   "product_id": pid, "depth": 100})
            if st == 200:
                bids = [(int(p) / 1e18, int(q) / 1e18)
                        for p, q in d["data"]["bids"]]
                asks = [(int(p) / 1e18, int(q) / 1e18)
                        for p, q in d["data"]["asks"]]
                mid = (bids[0][0] + asks[0][0]) / 2
                usd_bid = sum(p * q for p, q in bids)
                usd_ask = sum(p * q for p, q in asks)
                print(f"   mid ~ ${mid:.4f} | levels {len(bids)}/{len(asks)}"
                      f" | book usd bid/ask ~ ${usd_bid:,.0f}/${usd_ask:,.0f}"
                      f" | top5 ask cum ${sum(p*q for p,q in asks[:5]):,.0f}")
            else:
                print("   liquidity fail:", json.dumps(d)[:200])


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
