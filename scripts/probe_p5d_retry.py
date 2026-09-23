#!/usr/bin/env python3
"""Task 46: retry venues that were WAF-blocked / probe-pending at Task 44
(mexc, lighter, blofin, extended) + paradex funding (Sharpe fallback) +
bitmex listing recheck. Read-only, key-free, through v3_connectors only."""
import asyncio, sys
sys.path.insert(0, "/home/z/my-project/scripts")
import v3_connectors as vc

CHECKS = [
    ("mexc", "BTC"), ("mexc", "SOL"),
    ("lighter", "BTC"),
    ("blofin", "BTC"),
    ("extended", "BTC"),
    ("paradex", "BTC"), ("paradex", "SOL"),
    ("bitmex", "BTC"), ("bitmex", "ETH"), ("bitmex", "SOL"),
]

async def main():
    await vc.init()
    for venue, base in CHECKS:
        try:
            f = await vc.get_funding(venue, base)
            apr = f["rate"] / f["interval_h"] * 24 * 365 * 100
            print(f"F {venue:8s} {base:4s} rate={f['rate']:+.6f} "
                  f"iv={f['interval_h']:.1f}h apr={apr:+.1f}% src={f['source']}")
        except Exception as e:
            print(f"F {venue:8s} {base:4s} ERR {str(e)[:90]}")
        try:
            b, a = await vc.get_book(venue, base)
            bid, ask = (b[0][0] if b else 0), (a[0][0] if a else 0)
            dep_b = sum(p * q for p, q in b[:5]) if b else 0
            dep_a = sum(p * q for p, q in a[:5]) if a else 0
            print(f"B {venue:8s} {base:4s} bid={bid:.2f} ask={ask:.2f} "
                  f"spread={((ask - bid) / bid * 1e4 if bid and ask else 0):.2f}bps "
                  f"top5dep=${dep_b:.0f}/${dep_a:.0f}")
        except Exception as e:
            print(f"B {venue:8s} {base:4s} ERR {str(e)[:90]}")
    await vc.close()

if __name__ == "__main__":
    asyncio.run(main())
