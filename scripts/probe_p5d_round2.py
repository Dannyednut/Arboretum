#!/usr/bin/env python3
"""P5d probe 2: finalize endpoint shapes for gate/kucoin/bitmex/htx/deribit/paradex + Sharpe names."""
import asyncio, json, sqlite3, sys
import aiohttp

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
      "Accept": "application/json"}

URLS = {
    "bitmex_inst_xt": "https://www.bitmex.com/api/v1/instrument?symbol=XBTUSDTT",
    "bitmex_book_xt": "https://www.bitmex.com/api/v1/orderBook/L2?symbol=XBTUSDTT&depth=10",
    "gate_fundhist":  "https://api.gateio.ws/api/v4/futures/usdt/funding_rate?contract=BTC_USDT&limit=4",
    "kucoin_fundhist": "https://api-futures.kucoin.com/api/v1/contract/funding-rates?symbol=XBTUSDTM",
    "htx_book2":      "https://api.hbdm.com/swap-ex/market/depth?contract_code=BTC-USDT&type=step0",
    "htx_fundhist":   "https://api.hbdm.com/linear-swap-api/v1/swap_history_funding_rate?contract_code=BTC-USDT&page_size=4",
    "deribit_book":   "https://www.deribit.com/api/v2/public/get_order_book?instrument_name=BTC-PERPETUAL&depth=10",
    "lighter_retry":  "https://mainnet.zklighter.elliottech.org/api/v1/markets",
}

async def fetch_all():
    out = {}
    async with aiohttp.ClientSession(headers=UA, timeout=aiohttp.ClientTimeout(total=15)) as s:
        async def one(k, u):
            try:
                async with s.get(u) as r:
                    out[k] = (r.status, await r.read())
            except Exception as e:
                out[k] = ("ERR", str(e)[:100].encode())
        await asyncio.gather(*(one(k, u) for k, u in URLS.items()))
        # paradex markets (5.5MB) -> perp entry keys
        async with s.get("https://api.prod.paradex.trade/v1/markets") as r:
            out["paradex_markets"] = (r.status, await r.read())
    return out

async def main():
    out = await fetch_all()
    for k in URLS:
        st, body = out[k]
        try:
            j = json.loads(body)
            txt = json.dumps(j)[:260]
        except Exception:
            txt = body[:200].decode("utf-8", "replace").replace("\n", " ")
        print(f"== {k}: {st} len={len(body)}\n   {txt}")
    st, body = out["paradex_markets"]
    if st == 200:
        rows = json.loads(body)["results"]
        perp = next(x for x in rows if x.get("symbol") == "BTC-USD-PERP")
        keep = {k: v for k, v in perp.items() if any(w in k.lower() for w in ("fund", "symbol", "size", "tick", "status", "market_type"))}
        print("== paradex perp entry keys/funding:", json.dumps(keep)[:400])
        print("== paradex perp count:", sum(1 for x in rows if str(x.get("symbol", "")).endswith("-USD-PERP")))
    con = sqlite3.connect("/home/z/my-project/download/data/v3.db")
    names = [r[0] for r in con.execute("SELECT DISTINCT short_venue FROM candidates ORDER BY 1")]
    print("== sharpe venue names (short_venue):", names)

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
