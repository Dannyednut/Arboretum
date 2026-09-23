#!/usr/bin/env python3
"""Fetch 90d Binance funding history for tokenized-stock perps and report APR."""
import urllib.request, json, time

COINS = ["HOOD", "GOOGL", "META", "AAPL", "TSLA", "NVDA", "AMZN", "MCD",
         "CRCL", "COIN", "PDD", "ZM", "AMAT", "GDX", "TMF", "MEITUAN",
         "KUAISHOU", "TENCENT", "POPMART", "KODEX200"]
HDR = {"User-Agent": "Mozilla/5.0"}
now_ms = int(time.time() * 1000)
start_ms = now_ms - 90 * 86400_000

print(f"{'coin':12s} {'APR90':>8s} {'APR30':>8s} {'APR14':>8s} {'pos8h':>6s} {'events':>6s}")
for coin in COINS:
    sym = f"{coin}USDT"
    url = (f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={sym}"
           f"&startTime={start_ms}&endTime={now_ms}&limit=1000")
    try:
        req = urllib.request.Request(url, headers=HDR)
        d = json.load(urllib.request.urlopen(req, timeout=20))
    except Exception as e:
        print(f"{coin:12s}  fetch failed: {str(e)[:60]}")
        continue
    if not isinstance(d, list) or not d:
        print(f"{coin:12s}  no data")
        continue
    ev = sorted((int(r["fundingTime"]), float(r["fundingRate"])) for r in d)
    diffs = sorted((ev[i + 1][0] - ev[i][0]) / 3600_000 for i in range(len(ev) - 1))
    iv = diffs[len(diffs) // 2] if diffs else 8.0

    def apr_since(days):
        cutoff = now_ms - days * 86400_000
        sel = [r for t, r in ev if t >= cutoff]
        if not sel:
            return 0.0, 0
        span_days = (now_ms - max(t for t, r in ev if t >= cutoff)) / 86400_000
        span_days = max(span_days, days - 1)
        return sum(sel) / iv * 24 * 365 / span_days, len(sel)

    a90, n90 = apr_since(90)
    a30, _ = apr_since(30)
    a14, _ = apr_since(14)
    recent = [r for t, r in ev if t >= now_ms - 30 * 86400_000]
    pos = sum(1 for r in recent if r > 0) / len(recent) if recent else 0
    print(f"{coin:12s} {a90*100:>7.1f}% {a30*100:>7.1f}% {a14*100:>7.1f}% {pos:>6.0%} {n90:>6d}")
    time.sleep(0.15)
