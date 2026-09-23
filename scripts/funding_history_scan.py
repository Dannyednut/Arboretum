#!/usr/bin/env python3
"""
90-day funding history persistence scanner.

For every candidate coin (top live funding differentials + liquid majors),
fetches ~90 days of funding-rate history from Binance, Bybit, OKX, Gate and
Hyperliquid, then measures for each venue pair whether the CURRENT funding
spread direction actually persisted historically:

  - APR over 90d in the direction suggested by today's snapshot
  - APR over the most recent 30d (regime check)
  - fraction of days with positive carry
  - max drawdown of the hourly-spread equity curve (fraction of notional)
  - net APR after taker round-trip fees amortized over 14d / 30d holds

Caches raw history so re-analysis is free.
Output:
  /home/z/my-project/scripts/funding_history_cache.json   (raw)
  /home/z/my-project/download/data/funding_persistence_90d.csv
"""
import asyncio
import aiohttp
import json
import time
import math
import csv
import os
from datetime import datetime, timezone

SNAP = "/home/z/my-project/scripts/snapshot_v2.json"
CACHE = "/home/z/my-project/scripts/funding_history_cache.json"
OUT_CSV = "/home/z/my-project/download/data/funding_persistence_90d.csv"

WINDOW_DAYS = 90
DAY_MS = 86400_000
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
err_counts = {}

TAKER = {"binance": 0.0005, "bybit": 0.00055, "okx": 0.0005,
         "gate": 0.0005, "hl": 0.00045}

MAJORS = {
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "TON",
    "TRX", "SUI", "ARB", "OP", "INJ", "APT", "TIA", "WIF", "PEPE", "FET",
    "LTC", "ATOM", "NEAR", "DOT", "UNI", "AAVE", "SEI", "JUP", "HYPE",
    "PENGU", "VIRTUAL", "ENA", "ONDO", "LDO", "CRV", "RUNE", "TAO", "WLD",
    "ORDI", "ETC", "FIL", "RENDER", "AR", "S", "KAS", "PYTH",
}

VENUES = ["binance", "bybit", "okx", "gate", "hl"]


# --------------------------------------------------------------- API fetchers
async def fetch(session, url, params=None, method="GET", body=None):
    try:
        if method == "POST":
            async with session.post(url, json=body, headers=HEADERS,
                                    timeout=aiohttp.ClientTimeout(total=25)) as r:
                return await r.json()
        else:
            async with session.get(url, params=params, headers=HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=25)) as r:
                return await r.json()
    except Exception as e:
        return {"__error__": str(e)}


async def hist_binance(session, coin, start_ms, end_ms):
    out, cursor = [], start_ms
    base_sym = f"{coin}USDT"
    for _ in range(4):
        d = await fetch(session, "https://fapi.binance.com/fapi/v1/fundingRate",
                        params={"symbol": base_sym, "startTime": cursor,
                                "endTime": end_ms, "limit": 1000})
        if not isinstance(d, list) or not d:
            break
        for r in d:
            out.append((int(r["fundingTime"]), float(r["fundingRate"])))
        last = int(d[-1]["fundingTime"])
        if len(d) < 1000 or last >= end_ms - 1000:
            break
        cursor = last + 1
        await asyncio.sleep(0.15)
    return out


async def hist_bybit(session, coin, start_ms, end_ms):
    out, cursor = [], None
    sym = f"{coin}USDT"
    seen = set()
    end_walk = end_ms
    for _ in range(5):
        params = {"category": "linear", "symbol": sym, "limit": 200,
                  "startTime": start_ms, "endTime": end_walk}
        if cursor:
            params["cursor"] = cursor
        d = await fetch(session, "https://api.bybit.com/v5/market/funding/history", params=params)
        if not isinstance(d, dict):
            break
        if d.get("retCode") != 0:
            err_counts["bybit_api"] = err_counts.get("bybit_api", 0) + 1
            if err_counts["bybit_api"] <= 2:
                print(f"  [bybit api] {sym}: retCode={d.get('retCode')} {str(d.get('retMsg'))[:80]}")
            break
        lst = (d.get("result") or {}).get("list") or []
        if not lst:
            break
        new = 0
        for r in lst:
            ts = int(r.get("fundingRateTimestamp") or r.get("fundingTime"))
            if ts not in seen:
                seen.add(ts)
                out.append((ts, float(r["fundingRate"])))
                new += 1
        oldest = min(int(r.get("fundingRateTimestamp") or r.get("fundingTime")) for r in lst)
        cursor = (d.get("result") or {}).get("nextPageCursor") or None
        if oldest <= start_ms or (new == 0 and not cursor):
            break
        # endpoint returns newest-first and may not expose a cursor -> walk back by time
        end_walk = oldest - 1
        await asyncio.sleep(0.15)
    return out


async def hist_okx(session, coin, start_ms, end_ms):
    out, after = [], None
    inst = f"{coin}-USDT-SWAP"
    for _ in range(6):
        params = {"instId": inst, "limit": 100}
        if after:
            params["after"] = str(after)
        d = await fetch(session, "https://www.okx.com/api/v5/public/funding-rate-history", params=params)
        if not isinstance(d, dict):
            break
        rows = d.get("data") or []
        if not rows:
            break
        for r in rows:
            ts = int(r["fundingTime"])
            out.append((ts, float(r["fundingRate"])))
        oldest = min(int(r["fundingTime"]) for r in rows)
        if oldest <= start_ms or len(rows) < 100:
            break
        after = oldest
        await asyncio.sleep(0.15)
    return out


async def hist_gate(session, coin, start_ms, end_ms):
    # Gate v4 now requires a signed Timestamp header even for funding_history
    # (verified 2026-09: HTTP 400 MISSING_REQUIRED_HEADER) -> live-only venue.
    return []


async def hist_hl(session, coin, start_ms, end_ms):
    out, cursor = [], start_ms
    for _ in range(30):
        d = await fetch(session, "https://api.hyperliquid.xyz/info", method="POST",
                        body={"type": "fundingHistory", "coin": coin,
                              "startTime": cursor, "endTime": end_ms})
        if not isinstance(d, list) or not d:
            if isinstance(d, dict) and "__error__" in d:
                err_counts["hl"] = err_counts.get("hl", 0) + 1
            break
        for r in d:
            out.append((int(r["time"]), float(r["fundingRate"])))
        last = max(int(r["time"]) for r in d)
        if len(d) < 400 or last >= end_ms - 3600_000:
            break
        cursor = last + 3600_000
        await asyncio.sleep(0.12)
    return out


HIST = {"binance": hist_binance, "bybit": hist_bybit, "okx": hist_okx,
        "gate": hist_gate, "hl": hist_hl}


# ------------------------------------------------------------------- analysis
def infer_interval_h(events):
    """events: sorted [(ts_ms, rate)] -> median interval in hours."""
    if len(events) < 3:
        return 8.0
    diffs = [(events[i + 1][0] - events[i][0]) / 3600_000 for i in range(len(events) - 1)]
    diffs = sorted(d for d in diffs if 0 < d <= 48)
    if not diffs:
        return 8.0
    return diffs[len(diffs) // 2]


def hourly_series(events):
    """-> {hour_floor: hourly_rate}"""
    s = {}
    for ts, rate in events:
        s[ts // 3600_000] = rate  # rate per event; interval applied later via infer
    return s


def spread_metrics(hist_a, hist_b, start_ms, end_ms):
    """A = short leg venue (receives positive funding), B = long leg venue.
    Funding is a PER-EVENT cash flow: an hour with no settlement on a venue
    contributes zero from that venue. Therefore iterate the UNION of settlement
    hours within the common data-coverage window (zero-filled), never the
    intersection (which would drop most of the hourly leg's payments).
    Returns dict of metrics for d(t) = fA_hourly(t) - fB_hourly(t)."""
    iv_a = infer_interval_h(hist_a)
    iv_b = infer_interval_h(hist_b)
    sa = hourly_series(hist_a)
    sb = hourly_series(hist_b)

    # common coverage window (both venues actively listed & reporting)
    lo = max(min(sa), min(sb))
    hi = min(max(sa), max(sb))
    hours = [h for h in sorted(set(sa) | set(sb)) if lo <= h <= hi]
    # need >= 2 weeks of wall-clock coverage
    if len(hours) < 24 or (hi - lo) < 14 * 24:
        return None

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    daily = {}
    for h in hours:
        d = sa.get(h, 0.0) / iv_a - sb.get(h, 0.0) / iv_b
        cum += d
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        day = h // 24
        daily[day] = daily.get(day, 0.0) + d

    days_list = sorted(daily)
    daily_vals = [daily[k] for k in days_list]
    pos_frac = sum(1 for v in daily_vals if v > 0) / len(daily_vals)
    mean_d = sum(daily_vals) / len(daily_vals)
    var_d = sum((v - mean_d) ** 2 for v in daily_vals) / len(daily_vals)
    overlap_days = (hi - lo + 1) / 24.0

    apr_full = cum / overlap_days * 365.0
    # recent 30d
    cutoff = (end_ms // 3600_000 - 30 * 24) // 24
    cum30 = sum(v for k, v in daily.items() if k >= cutoff)
    days30 = max(1.0, min(overlap_days, 30.0))
    apr_30d = cum30 / days30 * 365.0
    # recent 14d
    cutoff14 = (end_ms // 3600_000 - 14 * 24) // 24
    cum14 = sum(v for k, v in daily.items() if k >= cutoff14)
    days14 = max(1.0, min(overlap_days, 14.0))
    apr_14d = cum14 / days14 * 365.0

    return {
        "apr_90d": apr_full,
        "apr_30d": apr_30d,
        "apr_14d": apr_14d,
        "pos_day_frac": pos_frac,
        "max_dd_notional": max_dd,
        "daily_std": math.sqrt(var_d),
        "overlap_days": overlap_days,
        "iv_a": iv_a, "iv_b": iv_b,
    }


# ----------------------------------------------------------------------- main
def load_snapshot():
    snap = json.load(open(SNAP))
    perps = {v: snap["perps"][v].get("rows", {}) for v in VENUES}
    return snap, perps


def liquidity_ok(base_norm, resolve, perps):
    """Liquidity filter on normalized base: best mark across venues listing it."""
    px = 0.0
    for v in VENUES:
        raw = resolve.get(v, {}).get(base_norm)
        if raw:
            r = perps[v].get(raw, {})
            if r.get("mark", 0) > px:
                px = r["mark"]
    if px <= 0:
        return False
    return px > 0.05 and (base_norm in MAJORS or px > 1.0)


async def main():
    snap, perps = load_snapshot()
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - WINDOW_DAYS * DAY_MS

    # -- per-venue symbol resolution: base_norm -> raw venue symbol base --
    resolve = {v: {} for v in VENUES}
    for v in VENUES:
        for raw, r in perps[v].items():
            resolve[v][r.get("base_norm", raw)] = raw

    # -- candidate selection: top live differentials per venue pair + majors --
    def live_rate(v, base_norm):
        raw = resolve[v].get(base_norm)
        if not raw:
            return None
        r = perps[v][raw]
        return r["rate"], r.get("interval_h", 8.0)

    cand = {}  # base_norm -> set(venues)
    for i, va in enumerate(VENUES):
        for vb in VENUES[i + 1:]:
            common = set(resolve[va]) & set(resolve[vb])
            scored = []
            for base_norm in common:
                ra = live_rate(va, base_norm)
                rb = live_rate(vb, base_norm)
                if ra is None or rb is None:
                    continue
                if not liquidity_ok(base_norm, resolve, perps):
                    continue
                gross = (rb[0] * 24 / rb[1] - ra[0] * 24 / ra[1]) * 365
                scored.append((abs(gross), base_norm, gross))
            scored.sort(reverse=True)
            for _, base_norm, _ in scored[:30]:
                cand.setdefault(base_norm, set()).update({va, vb})
    for base_norm in MAJORS:
        listed = {v for v in VENUES if base_norm in resolve[v]}
        if listed:
            cand.setdefault(base_norm, set()).update(listed)

    coins = sorted(cand.keys())
    print(f"Candidates: {len(coins)} coins, window {WINDOW_DAYS}d, end {datetime.now(timezone.utc).isoformat()}")

    cache = {}
    if os.path.exists(CACHE):
        try:
            cache = json.load(open(CACHE))
        except Exception:
            cache = {}

    sem = asyncio.Semaphore(4)
    lock = asyncio.Lock()
    done_count = [0]
    fail_counts = {}

    async def collect(coin, venues):
        async with sem:
            async with lock:
                cached = cache.get(coin)
            if cached and all(v in cached for v in venues):
                async with lock:
                    done_count[0] += 1
                    if done_count[0] % 10 == 0:
                        print(f"  cache-hit {done_count[0]}/{len(coins)} coins")
                return
            res = {}
            for v in venues:
                sym = resolve.get(v, {}).get(coin, coin)
                try:
                    ev = await HIST[v](session, sym, start_ms, end_ms)
                except Exception as e:
                    fail_counts[v] = fail_counts.get(v, 0) + 1
                    ev = []
                res[v] = ev
                await asyncio.sleep(0.12)
            async with lock:
                cache[coin] = res
                done_count[0] += 1
                if done_count[0] % 10 == 0:
                    print(f"  fetched {done_count[0]}/{len(coins)} coins")
                    json.dump(cache, open(CACHE, "w"))  # incremental save

    async with aiohttp.ClientSession() as session:
        tasks = [collect(coin, sorted(venues)) for coin, venues in cand.items()]
        await asyncio.gather(*tasks)

    json.dump(cache, open(CACHE, "w"))
    print(f"History cached for {len(cache)} coins -> {CACHE}")
    print(f"Per-venue fetch failures: {fail_counts}")

    # ------------------------------------------------ evaluate venue pairs
    results = []
    skipped_identity = [0]
    for base, venues in sorted(cand.items()):
        h = cache.get(base) or {}
        for i, va in enumerate(sorted(venues)):
            for vb in sorted(venues)[i + 1:]:
                ha, hb = h.get(va) or [], h.get(vb) or []
                if len(ha) < 30 or len(hb) < 30:
                    continue
                # direction from live snapshot: short venue with higher funding APR
                raw_a, raw_b = resolve[va].get(base), resolve[vb].get(base)
                if not raw_a or not raw_b:
                    continue
                ra, rb = perps[va].get(raw_a), perps[vb].get(raw_b)
                if not ra or not rb:
                    continue
                # identity guard: same asset on both venues => marks must agree
                ma, mb = ra.get("mark", 0), rb.get("mark", 0)
                if ma <= 0 or mb <= 0 or abs(ma / mb - 1) > 0.05:
                    skipped_identity[0] += 1
                    continue
                apr_a = ra["rate"] * 24 / ra.get("interval_h", 8) * 365
                apr_b = rb["rate"] * 24 / rb.get("interval_h", 8) * 365
                if apr_a >= apr_b:
                    short_v, long_v = va, vb
                else:
                    short_v, long_v = vb, va
                hs, hl_ = h.get(short_v) or [], h.get(long_v) or []
                if len(hs) < 30 or len(hl_) < 30:
                    continue
                m = spread_metrics(hs, hl_, start_ms, end_ms)
                if m is None:
                    continue
                cost_rt = 2 * (TAKER[short_v] + TAKER[long_v])  # in + out, both legs
                results.append({
                    "coin": base,
                    "short_venue": short_v, "long_venue": long_v,
                    "live_gross_apr": max(apr_a, apr_b) - min(apr_a, apr_b),
                    **m,
                    "net_apr_14d_hold": m["apr_90d"] - cost_rt * 365 / 14,
                    "net_apr_30d_hold": m["apr_90d"] - cost_rt * 365 / 30,
                    "cost_roundtrip": cost_rt,
                })

    results.sort(key=lambda r: r["net_apr_30d_hold"], reverse=True)
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    cols = ["coin", "short_venue", "long_venue", "apr_90d", "apr_30d", "apr_14d",
            "pos_day_frac", "max_dd_notional", "daily_std", "overlap_days",
            "iv_a", "iv_b", "cost_roundtrip", "net_apr_14d_hold", "net_apr_30d_hold",
            "live_gross_apr"]
    with open(OUT_CSV, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in results:
            w.writerow({c: round(r[c], 6) if isinstance(r[c], float) else r[c] for c in cols})
    print(f"Saved {len(results)} venue-pair results -> {OUT_CSV} "
          f"(skipped {skipped_identity[0]} ticker-collision pairs)")

    print("\nTOP 25 PERSISTENT CROSS-EX FUNDING SPREADS (90d history, direction = today's)")
    print(f"{'coin':12s} {'short':10s} {'long':10s} {'APR90':>7s} {'APR30':>7s} {'APR14':>7s} "
          f"{'posDays':>7s} {'maxDD':>6s} {'net30':>7s}")
    for r in results[:25]:
        print(f"{r['coin']:12s} {r['short_venue']:10s} {r['long_venue']:10s} "
              f"{r['apr_90d']*100:>6.1f}% {r['apr_30d']*100:>6.1f}% {r['apr_14d']*100:>6.1f}% "
              f"{r['pos_day_frac']:>7.0%} {r['max_dd_notional']*100:>5.2f}% {r['net_apr_30d_hold']*100:>6.1f}%")

    print("\nWORST 10 (today's spread was historically NEGATIVE — trap warning):")
    for r in results[-10:]:
        print(f"{r['coin']:12s} {r['short_venue']:10s} {r['long_venue']:10s} "
              f"{r['apr_90d']*100:>6.1f}% (30d {r['apr_30d']*100:>6.1f}%) posDays {r['pos_day_frac']:.0%}")


if __name__ == "__main__":
    asyncio.run(main())
