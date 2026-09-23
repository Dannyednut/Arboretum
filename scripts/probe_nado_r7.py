#!/usr/bin/env python3
"""Round 7: Nado symbols structure, LINK product, fee_rates w/ sender."""
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
        st, syms = await post(s, {"type": "symbols"}, maxp=5000)
        print("symbols status:", st)
        data = syms.get("data") if isinstance(syms, dict) else None
        if isinstance(data, dict):
            items = list(data.items())
            print("n entries:", len(items))
            for k, v in items[:6]:
                print(" ", k, "->", json.dumps(v)[:200])
        elif isinstance(data, list):
            print("list n:", len(data))
            for v in data[:6]:
                print(" ", json.dumps(v)[:200])

        st, prods = await post(s, {"type": "all_products"}, maxp=100000)
        perps = prods["data"]["perp_products"]
        # symbols keyed by token address -> symbol name
        tok2sym = {}
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict) and v.get("symbol"):
                    tok2sym[k.lower()] = v["symbol"]
                elif isinstance(v, str):
                    tok2sym[k.lower()] = v
        print("tok2sym sample:", list(tok2sym.items())[:5], "n=", len(tok2sym))

        link_pid = btc_pid = None
        for p in perps:
            tok = ((p.get("config") or {}).get("token") or "").lower()
            sname = tok2sym.get(tok, "?")
            if sname == "LINK":
                link_pid = p["product_id"]
            if sname == "BTC":
                btc_pid = p["product_id"]
            print(f"  pid={p['product_id']} sym={sname}")
        print("LINK pid:", link_pid, "BTC pid:", btc_pid)

        if link_pid:
            st, d = await post(s, {"type": "market_liquidity",
                                   "product_id": link_pid, "depth": 50})
            if st == 200:
                bids = d["data"]["bids"]
                px = int(bids[0][0]) / 1e18
                print(f"LINK best bid ~ ${px:.4f}, levels={len(bids)}")

        st, d = await post(s, {"type": "fee_rates",
                               "sender": "0x000000000000000000000000"
                                         "0000000000000000"})
        print("fee_rates:", st, json.dumps(d)[:600])


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
