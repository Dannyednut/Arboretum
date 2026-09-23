#!/usr/bin/env python3
"""P5d Wave-0 probe: verify public funding/book endpoints live before wiring connectors."""
import asyncio, json, sys
import aiohttp

PROBES = [
    # (venue, slug, url)
    ("lighter", "markets",   "https://mainnet.zklighter.elliottech.org/api/v1/markets"),
    ("lighter", "funding",   "https://mainnet.zklighter.elliottech.org/api/v1/fundingRates"),
    ("lighter", "funding2",  "https://mainnet.zklighter.elliottech.org/api/v1/funding-rates"),
    ("lighter", "book",      "https://mainnet.zklighter.elliottech.org/api/v1/orderBooks?market_id=1"),
    ("paradex", "markets",   "https://api.prod.paradex.trade/v1/markets"),
    ("paradex", "funding",   "https://api.prod.paradex.trade/v1/funding_data?symbol=BTC-USD-PERP"),
    ("paradex", "book",      "https://api.prod.paradex.trade/v1/orderbook/BTC-USD-PERP"),
    ("extended", "markets",  "https://api.extended.exchange/api/v1/perpetuals/markets"),
    ("extended", "book",     "https://api.extended.exchange/api/v1/perpetuals/markets/BTC-USD-R/orderbook"),
    ("mexc", "funding",      "https://contract.mexc.com/api/v1/contract/funding_rate/BTC_USDT"),
    ("mexc", "hist",         "https://contract.mexc.com/api/v1/contract/funding_rate_history?symbol=BTC_USDT&limit=4"),
    ("mexc", "book",         "https://contract.mexc.com/api/v1/contract/depth/BTC_USDT?limit=20"),
    ("bitmex", "instrument", "https://www.bitmex.com/api/v1/instrument?symbol=XBTUSDT"),
    ("bitmex", "funding",    "https://www.bitmex.com/api/v1/funding?symbol=XBTUSDT&count=4&reverse=true"),
    ("bitmex", "book",       "https://www.bitmex.com/api/v1/orderBook/L2?symbol=XBTUSDT&depth=25"),
    ("kucoin", "contract",   "https://api-futures.kucoin.com/api/v1/contracts/XBTUSDTM"),
    ("kucoin", "book",       "https://api-futures.kucoin.com/api/v1/level2/snapshot?symbol=XBTUSDTM"),
    ("gate", "contract",     "https://api.gateio.ws/api/v4/futures/usdt/contracts/BTC_USDT"),
    ("gate", "book",         "https://api.gateio.ws/api/v4/futures/usdt/order_book?contract=BTC_USDT&limit=10"),
    ("deribit", "funding",   "https://www.deribit.com/api/v2/public/get_funding_rate_history?instrument_name=BTC-PERPETUAL&count=4"),
    ("deribit", "ticker",    "https://www.deribit.com/api/v2/public/ticker?instrument_name=BTC-PERPETUAL"),
    ("htx", "funding",       "https://api.hbdm.com/linear-swap-api/v1/swap_funding_rate?contract_code=BTC-USDT"),
    ("htx", "book",          "https://api.hbdm.com/linear-swap-api/v1/swap_depth?contract_code=BTC-USDT&type=step0"),
    ("kraken", "ticker",     "https://futures.kraken.com/derivatives/api/v3/tickers/BTCPERP"),
    ("kraken", "book",       "https://futures.kraken.com/derivatives/api/v3/orderbook/BTCPERP"),
    ("blofin", "funding",    "https://api.blofin.com/api/v1/market/funding-rate?instId=BTC-USDT"),
    ("blofin", "book",       "https://api.blofin.com/api/v1/market/books?instId=BTC-USDT"),
    ("whitebit", "markets",  "https://api.whitebit.com/api/v4/public/futures/markets"),
    ("apex", "funding",      "https://api.pro.apex.exchange/api/v3/funding-rates?symbol=BTC-USDT"),
    ("toobit", "premium",    "https://api.toobit.com/fapi/v1/premiumIndex?symbol=BTCUSDT"),
    ("weex", "funding",      "https://api-contract.weex.com/api/v1/front/market/getMarketFunding?symbol=cmt_btcusdt"),
]

async def main():
    out = []
    async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 (probe)"},
            timeout=aiohttp.ClientTimeout(total=12)) as s:
        async def one(venue, slug, url):
            try:
                async with s.get(url) as r:
                    body = await r.read()
                    try:
                        j = json.loads(body)
                        txt = json.dumps(j)[:200]
                    except Exception:
                        txt = body[:120].decode("utf-8", "replace")
                    out.append((venue, slug, r.status, len(body), txt.replace("\n", " ")))
            except Exception as e:
                out.append((venue, slug, "ERR", 0, str(e)[:120]))
        await asyncio.gather(*(one(v, sl, u) for v, sl, u in PROBES))
    for venue, slug, st, ln, txt in sorted(out):
        print(f"{venue:9s} {slug:10s} {st} len={ln:7d} {txt}")

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
