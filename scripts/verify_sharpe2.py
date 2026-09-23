#!/usr/bin/env python3
"""Verification round 2: Kraken pf_/mr_ perps full scan; Backpack funding."""
import json
import urllib.request

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


print("=== Kraken futures: ALL perps with funding, |APR| >= 10% ===")
try:
    d = get("https://futures.kraken.com/derivatives/api/v3/tickers")
    tk = d.get("tickers", [])
    print(f"total tickers: {len(tk)}")
    kinds = {}
    for t in tk:
        kinds[t.get("type", "?")] = kinds.get(t.get("type", "?"), 0) + 1
    print("types:", kinds)
    hot = []
    for t in tk:
        sym = t.get("symbol") or ""
        fr = t.get("fundingRate")
        if fr is None:
            continue
        fr = float(fr)
        # kraken funding is per 1h for both pf_ and mr_
        a = fr * 8760.0 * 100.0
        if abs(a) >= 10.0:
            hot.append((sym, fr, a, t.get("fundingRatePrediction")))
    hot.sort(key=lambda x: x[2])
    print(f"|APR|>=10%: {len(hot)}")
    for sym, fr, a, pred in hot[:28]:
        print(f"  {sym:18s} {fr*100:+.4f}%/1h -> {a:+8.1f}% APR")
except Exception as e:
    print("  !!", repr(e))

print("\n=== Kraken historicalfunding probe (mr_KAITOUSD) ===")
for sym in ("mr_KAITOUSD", "pf_KAITOUSD"):
    try:
        d = get("https://futures.kraken.com/derivatives/api/v3/"
                f"historicalfunding?symbol={sym}&from=2026-07-01T00:00:00Z")
        rr = d.get("historicalFunding") or []
        print(f"  {sym}: {len(rr)} rows", str(rr[:1])[:160] if rr else "")
        if len(rr) > 100:
            rel = [float(x.get("fundingRate", 0)) for x in rr]
            import statistics as st
            print(f"    median {st.median(rel)*8760*100:+.1f}% APR over "
                  f"{len(rel)}h, neg-frac {sum(1 for x in rel if x < 0)/len(rel):.0%}")
        break
    except Exception as e:
        print(f"  !! {sym}: {e!r}")

print("\n=== Backpack funding endpoints probe ===")
for path in ("api/v1/fundingRates",
             "api/v1/fundingRates?symbol=LTC_USDC_PERP",
             "api/v1/markets/LTC_USDC_PERP"):
    try:
        d = get("https://api.backpack.exchange/" + path)
        s = json.dumps(d)[:260]
        print(f"  /{path} -> {s}")
    except Exception as e:
        print(f"  !! /{path}: {e!r}")
