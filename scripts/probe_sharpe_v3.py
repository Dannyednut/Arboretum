#!/usr/bin/env python3
"""Sharpe AI deep-dive #2: venue coverage, RWA universe, arb benchmark vs ours."""
import json
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

BASE = "https://www.sharpe.ai"


def get(path):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"__err__": repr(e)}


print("=== 1. funding/current: venue census (paginate via limit) ===")
d = get("/api/funding/rates?type=current&limit=5000")
rows = d if isinstance(d, list) else []
venues = Counter(r["exchange"] for r in rows)
print(f"rows fetched: {len(rows)}, unique venues: {len(venues)}")
for v, n in venues.most_common():
    print(f"  {v:22s} {n}")

print("\n=== 2. per-asset-class row counts (current book) ===")
for ac in ("crypto", "equity", "commodity", "fx", "index"):
    d = get(f"/api/funding/rates?type=current&asset_class={ac}&limit=5000")
    rows = d if isinstance(d, list) else []
    vs = Counter(r["exchange"] for r in rows)
    print(f"  {ac:9s}: {len(rows):5d} rows across {len(vs)} venues: "
          f"{dict(vs.most_common(8))}")

print("\n=== 3. RWA perps: venue census ===")
d = get("/api/rwa-perps/rates?type=current&limit=5000")
rows = d if isinstance(d, list) else []
print(f"rows: {len(rows)}")
vs = Counter(r["venue"] for r in rows)
for v, n in vs.most_common():
    acs = Counter(r["asset_class"] for r in rows if r["venue"] == v)
    print(f"  {v:24s} {n:5d}  {dict(acs)}")

print("\n=== 4. arb/cross-exchange: benchmark table ===")
d = get("/api/arbitrage/cross-exchange?assetClass=all")
rows = d if isinstance(d, list) else []
print(f"rows: {len(rows)}")
if rows:
    print("keys:", list(rows[0].keys()))
    top = sorted(rows, key=lambda r: -float(r.get("apr") or 0))[:12]
    for r in top:
        print(f"  {r.get('symbol','?'):10s} {r.get('assetClass','?'):8s} "
              f"long={r.get('longExchange','?'):12s} short={r.get('shortExchange','?'):12s} "
              f"apr={float(r.get('apr') or 0):8.1f}% net={float(r.get('netApr') or 0):8.1f}% "
              f"spread={float(r.get('spreadRate') or 0)*100:6.2f}% ivl={r.get('intervalHours')}h "
              f"spreadSrc={r.get('spreadSource','-')}")

print("\n=== 5. XMR funding history coverage per venue (dydx leg check) ===")
d = get("/api/funding/rates?type=history&coin=XMR&days=200&limit=5000")
rows = d if isinstance(d, list) else []
vs = defaultdict(list)
for r in rows:
    vs[r["exchange"]].append(r["settled_at"])
print(f"rows: {len(rows)}")
for v, ts in sorted(vs.items()):
    print(f"  {v:22s} {len(ts):5d} settlements  {min(ts)[:16]} -> {max(ts)[:16]}")

print("\n=== 6. listings/recent sample ===")
d = get("/api/listings/recent")
rows = (d.get("rows") or [])[:5]
print("window_days:", d.get("window_days"), "count:", d.get("count"))
for r in rows:
    print("  ", json.dumps(r)[:220])
