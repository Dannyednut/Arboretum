#!/usr/bin/env python3
"""Debug kucoin funding keys, bitmex active perps, deribit history payload."""
import asyncio, json
import aiohttp

async def main():
    async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get("https://api-futures.kucoin.com/api/v1/contracts/XBTUSDTM") as r:
            d = (await r.json())["data"]
            print("kucoin funding-ish keys:", {k: v for k, v in d.items()
                  if any(w in k.lower() for w in ("fund", "granul", "next"))})
        async with s.get("https://www.bitmex.com/api/v1/instrument/active") as r:
            rows = await r.json()
            print("bitmex active count:", len(rows))
            usdt = [x["symbol"] for x in rows if x.get("typ") == "FFWCSX"
                    and "USDT" in x["symbol"]]
            print("bitmex active FFWCSX USDT symbols:", usdt[:15])
            xbt = [x for x in rows if x["symbol"].startswith("XBT")][:6]
            for x in xbt:
                print("  xbt:", {k: x.get(k) for k in ("symbol", "state", "typ", "expiry")})
            print("state values:", sorted({str(x.get('state')) for x in rows})[:8])
        now = 1790074024659 * 1  # ms epoch approx; use live
        import time
        now = int(time.time() * 1000)
        async with s.get("https://www.deribit.com/api/v2/public/get_funding_rate_history",
                         params={"instrument_name": "BTC-PERPETUAL",
                                 "start_timestamp": now - 36 * 3600 * 1000,
                                 "end_timestamp": now, "count": 8}) as r:
            print("deribit hist status:", r.status, (await r.text())[:300])

asyncio.run(main())
