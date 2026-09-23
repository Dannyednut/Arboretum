#!/usr/bin/env python3
"""Probe Sharpe API accessibility (no key) + check what the bot's current feed returns."""
import asyncio, aiohttp, json

BASE = "https://www.sharpe.ai/api/v1/arbitrage"
ENDPOINTS = [
    ("cross-exchange", {"exchanges": "Binance,Bybit,OKX"}),
    ("spot-perp", {"exchange": "Binance"}),
    ("dated-futures-basis", {"exchanges": "Binance,Bybit,OKX"}),
]

async def main():
    async with aiohttp.ClientSession() as s:
        for name, params in ENDPOINTS:
            url = f"{BASE}/{name}"
            try:
                async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    body = await r.text()
                    status = r.status
                    try:
                        j = r._result if False else json.loads(body)
                        d = j.get("data") if isinstance(j, dict) else None
                        n = None
                        if isinstance(d, list): n = len(d)
                        elif isinstance(d, dict) and "rows" in d: n = len(d["rows"])
                        preview = json.dumps(j)[:300] if j else body[:300]
                    except Exception:
                        n, preview = None, body[:200]
                    print(f"{name:22s} -> HTTP {status}  rows={n}")
                    print(f"   preview: {preview}")
            except Exception as e:
                print(f"{name:22s} -> ERROR {e}")

asyncio.run(main())
