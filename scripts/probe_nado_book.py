#!/usr/bin/env python3
"""Round 6: Nado LINK product_id -> market_liquidity book -> fee_rates."""
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
        st, d = await post(s, {"type": "all_products"})
        if st != 200:
            print("all_products fail:", d)
            return
        perps = d["data"]["perp_products"]
        print(f"perp products: {len(perps)}")
        # map product_id -> metadata symbol
        st2, syms = await post(s, {"type": "symbols"}, maxp=100000)
        symmap = {}
        if st2 == 200 and isinstance(syms, dict):
            for sid, meta in (syms.get("data") or {}).items():
                symmap[sid] = meta.get("symbol") if isinstance(meta, dict) \
                    else meta
        links = []
        for p in perps:
            pid = p["product_id"]
            tok = (p.get("config") or {}).get("token", "")
            sym = symmap.get(tok, tok[:10])
            p["_sym"] = sym
            if "LINK" in str(sym).upper():
                links.append(p)
        print("LINK matches:", [(p["product_id"], p["_sym"]) for p in links])
        # also show a sample of symbols for sanity
        print("sample symbols:", [(p["product_id"], p["_sym"])
                                  for p in perps[:8]])

        target = links[0]["product_id"] if links else perps[0]["product_id"]
        for depth in (10, 50):
            st3, d3 = await post(s, {"type": "market_liquidity",
                                     "product_id": target, "depth": depth})
            print(f"\nmarket_liquidity pid={target} depth={depth} -> {st3}:",
                  json.dumps(d3)[:1200])

        st4, d4 = await post(s, {"type": "contracts"})
        if st4 == 200:
            data = d4.get("data")
            if isinstance(data, list):
                for c in data:
                    if c.get("product_id") == target:
                        print("\nLINK contract:", json.dumps(c)[:800])
                        break
            else:
                print("\ncontracts:", json.dumps(d4)[:600])

        st5, d5 = await post(s, {"type": "fee_rates", "address":
                                 "0x0000000000000000000000000000000000000000"})
        print("\nfee_rates:", st5, json.dumps(d5)[:400])


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
