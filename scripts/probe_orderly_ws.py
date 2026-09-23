#!/usr/bin/env python3
"""Round 9: Orderly public WS orderbook probe."""
import asyncio
import json

import aiohttp


async def main():
    async with aiohttp.ClientSession() as s:
        # Orderly public WS variants
        candidates = [
            ("wss://ws-orderly-v2.orderly.network/ws/public/order",
             {"id": "1", "event": "subscribe",
              "topic": "PERP_XAG_USDC@orderbook20"}),
            ("wss://ws.orderly.org/ws/public/order",
             {"id": "1", "event": "subscribe",
              "topic": "PERP_XAG_USDC@orderbook20"}),
            ("wss://ws.orderly.org/ws/public",
             {"id": "1", "event": "subscribe",
              "topic": "PERP_XAG_USDC@orderbook20"}),
        ]
        for url, sub in candidates:
            print(f"--- {url}")
            try:
                async with s.ws_connect(
                        url, timeout=aiohttp.ClientWSTimeout(ws_close=10),
                        heartbeat=15) as ws:
                    await ws.send_str(json.dumps(sub))
                    for _ in range(4):
                        msg = await asyncio.wait_for(ws.receive(), timeout=12)
                        print("   msg:", msg.type, str(msg.data)[:220])
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            break
                    break_ = False
                    if break_:
                        break
            except Exception as e:
                print("   ERR:", type(e).__name__, str(e)[:140])


if __name__ == "__main__":
    asyncio.run(main())
