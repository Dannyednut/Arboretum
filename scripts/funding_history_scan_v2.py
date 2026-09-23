#!/usr/bin/env python3
"""
90-day persistence scanner v2 — extends the Part-1 scanner (Binance/Bybit/OKX/HL)
to Aster (hourly cohort), dYdX v4 and Bitget legs (7 venues).

Purpose: convert E1 (Aster hourly floor harvest) and E2 (stock/RWA perp
cross-venue funding) from single-snapshot claims into DISTRIBUTIONS:
  - persistence %  (fraction of days carry >= 3 / 10 / 25 bps)
  - conditional survival (P(pos t+1 | pos t), P(pos t+7 | pos t))
  - episode structure (count, median/max duration)
  - widening/decay slopes (full-window OLS + within-episode median slope)

Funding stays a PER-EVENT cash flow: union-of-settlement-hours zero-fill model
(Part-1 methodology). Intervals inferred per contract from the history itself.

Outputs:
  scripts/funding_history_cache_v3.json               raw cache
  download/data/funding_persistence_90d_v3.csv        all pairs
  download/data/e1_aster_hourly_distribution.csv      E1 distribution
  download/data/e2_stock_rwa_distribution.csv         E2 distribution
"""
import asyncio
import aiohttp
import json
import time
import math
import csv
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/home/z/my-project/scripts")
from funding_history_scan import (hist_binance, hist_bybit, hist_okx, hist_hl,
                                  infer_interval_h, hourly_series, fetch)

SNAP = "/home/z/my-project/scripts/snapshot_v3.json"
CACHE = "/home/z/my-project/scripts/funding_history_cache_v3.json"
OUTDIR = "/home/z/my-project/download/data"
OUT_CSV = f"{OUTDIR}/funding_persistence_90d_v3.csv"
E1_CSV = f"{OUTDIR}/e1_aster_hourly_distribution.csv"
E2_CSV = f"{OUTDIR}/e2_stock_rwa_distribution.csv"
E2_BASES_CACHE = "/home/z/my-project/scripts/aster_stock_bases.json"

WINDOW_DAYS = 90
DAY_MS = 86400_000
VENUES = ["binance", "bybit", "okx", "hl", "aster", "dydx", "bitget"]
TAKER = {"binance": 0.0005, "bybit": 0.00055, "okx": 0.0005, "hl": 0.00045,
         "aster": 0.0004, "dydx": 0.0005, "bitget": 0.0006}
MAJORS = {
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "TON",
    "TRX", "SUI", "ARB", "OP", "INJ", "APT", "TIA", "WIF", "PEPE", "FET",
    "LTC", "ATOM", "NEAR", "DOT", "UNI", "AAVE", "SEI", "JUP", "HYPE",
    "PENGU", "VIRTUAL", "ENA", "ONDO", "LDO", "CRV", "RUNE", "TAO", "WLD",
    "ORDI", "ETC", "FIL", "RENDER", "AR", "S", "KAS", "PYTH",
}
# Aster underlyingSubType tags that define the E2 stock/RWA universe
E2_SUBTYPES = {"STOCK", "ETF", "Commodities", "USD1-RWA", "pre-launch",
               "Semiconductor"}
E2_FALLBACK = {  # used only if exchangeInfo fetch fails
    "SKHYNIX", "SAMSUNG", "GOOGL", "META", "TSLA", "AAPL", "NVDA", "AMZN",
    "MSFT", "HOOD", "COIN", "MSTR", "SBET", "VST", "DKNG", "PLTR", "UBER",
    "XAU", "XAG", "CL", "SPCX", "QQQ", "MU", "AMD", "AVGO", "BABA", "PDD",
}
CAP = 260           # max coins to fetch
MIN_OVERLAP_H = 14 * 24   # require >= 2 weeks common coverage


# ------------------------------------------------------------- new fetchers
def _iso_to_ms(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


async def hist_dydx(session, sym, start_ms, end_ms):
    """dYdX v4 indexer historicalFunding. Cursor params (beforeOrAt) were
    observed IGNORED on 2026-09-01 -> self-detecting pager: stop when a page
    yields no new records. Without a working cursor this yields the newest
    ~1000 hourly records (~42d), which the overlap-aware analysis handles."""
    ticker = sym if sym.endswith("-USD") else f"{sym}-USD"
    out, seen, cursor = [], set(), None
    for _ in range(5):
        url = f"https://indexer.dydx.trade/v4/historicalFunding/{ticker}?limit=1000"
        if cursor:
            url += f"&beforeOrAt={cursor}"
        d = await fetch(session, url)
        rows = d.get("historicalFunding") if isinstance(d, dict) else None
        if not rows:
            break
        new = 0
        for r in rows:
            ts = _iso_to_ms(r["effectiveAt"])
            if start_ms <= ts <= end_ms and ts not in seen:
                seen.add(ts)
                out.append((ts, float(r["rate"])))
                new += 1
        oldest_iso = min(r["effectiveAt"] for r in rows)
        if new == 0 or _iso_to_ms(oldest_iso) <= start_ms:
            break
        cursor = oldest_iso
        await asyncio.sleep(0.25)
    return out


async def hist_bitget(session, base, start_ms, end_ms):
    """Bitget v2 history-fund-rate. pageSize silently capped at 100 -> pageNo
    paging, newest first. usdt-futures symbols are BASE+USDT."""
    sym = f"{base}USDT"
    out, seen = [], set()
    for page in range(1, 10):
        d = await fetch(session,
                        "https://api.bitget.com/api/v2/mix/market/history-fund-rate",
                        params={"symbol": sym, "productType": "usdt-futures",
                                "pageNo": page, "pageSize": 100})
        rows = d.get("data") or [] if isinstance(d, dict) else []
        if not rows:
            break
        new = 0
        for r in rows:
            ts = int(r["fundingTime"])
            if start_ms <= ts <= end_ms and ts not in seen:
                seen.add(ts)
                out.append((ts, float(r["fundingRate"])))
                new += 1
        oldest = min(int(r["fundingTime"]) for r in rows)
        if oldest <= start_ms or new == 0:
            break
        await asyncio.sleep(0.15)
    return sorted(out)


# Aster base -> API symbol map, filled from exchangeInfo in main()
# (USD1-quoted RWA pairs are not plain BASE+"USDT")
ASTER_SYMS = {}


async def hist_aster(session, base, start_ms, end_ms):
    """Aster: Binance-compatible /fapi/v1/fundingRate, startTime pagination."""
    sym = ASTER_SYMS.get(base, f"{base}USDT")
    out, cursor = [], start_ms
    for _ in range(5):
        d = await fetch(session, "https://fapi.asterdex.com/fapi/v1/fundingRate",
                        params={"symbol": sym, "startTime": cursor,
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


HIST = {"binance": hist_binance, "bybit": hist_bybit, "okx": hist_okx,
        "hl": hist_hl, "aster": hist_aster, "dydx": hist_dydx,
        "bitget": hist_bitget}


# ------------------------------------------------------------------ metrics
def ols_slope(ys):
    """OLS slope with x = 0..n-1."""
    n = len(ys)
    if n < 3:
        return 0.0
    sx = n * (n - 1) / 2.0
    sxx = n * (n - 1) * (2 * n - 1) / 6.0
    sy = sum(ys)
    sxy = sum(i * y for i, y in enumerate(ys))
    denom = n * sxx - sx * sx
    return (n * sxy - sx * sy) / denom if denom else 0.0


def spread_metrics_v2(hist_s, hist_l, end_ms):
    """S = short leg venue history, L = long leg venue history.
    d(t) = fS_hourly(t) - fL_hourly(t) on the UNION of settlement hours
    (zero-fill), restricted to the common coverage window."""
    iv_s = infer_interval_h(hist_s)
    iv_l = infer_interval_h(hist_l)
    ss = hourly_series(hist_s)
    sl = hourly_series(hist_l)

    lo = max(min(ss), min(sl))
    hi = min(max(ss), max(sl))
    hours = [h for h in sorted(set(ss) | set(sl)) if lo <= h <= hi]
    if len(hours) < 24 or (hi - lo) < MIN_OVERLAP_H:
        return None

    cum = peak = max_dd = 0.0
    daily = {}
    for h in hours:
        d = ss.get(h, 0.0) / iv_s - sl.get(h, 0.0) / iv_l
        cum += d
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        day = h // 24
        daily[day] = daily.get(day, 0.0) + d

    days = sorted(daily)
    dv = [daily[k] for k in days]
    n = len(dv)
    mean_d = sum(dv) / n
    std_d = math.sqrt(sum((v - mean_d) ** 2 for v in dv) / n)
    pos_frac = sum(1 for v in dv if v > 0) / n
    overlap_days = (hi - lo + 1) / 24.0

    persist = {t: sum(1 for v in dv if v >= t) / n for t in
               (0.0003, 0.0010, 0.0025)}   # 3 / 10 / 25 bps per day

    # conditional survival P(pos t+k | pos t)
    n1 = t1 = n7 = t7 = 0
    for i, v in enumerate(dv):
        if v > 0:
            n1 += 1
            if i + 1 < n and dv[i + 1] > 0:
                t1 += 1
            if i + 7 < n:
                n7 += 1
                if dv[i + 7] > 0:
                    t7 += 1
    cond1 = t1 / n1 if n1 else 0.0
    cond7 = t7 / n1 if n1 else 0.0

    # episodes: contiguous days with positive carry
    eps, cur = [], []
    for v in dv:
        if v > 0:
            cur.append(v)
        elif cur:
            eps.append(cur)
            cur = []
    if cur:
        eps.append(cur)
    long_eps = [e for e in eps if len(e) >= 3]
    ep_days = [len(e) for e in eps]
    med_ep_days = sorted(ep_days)[len(ep_days) // 2] if ep_days else 0
    max_ep_days = max(ep_days) if ep_days else 0
    ep_slopes = [ols_slope(e) * 365.0 for e in long_eps]  # APR pts/day
    med_ep_slope = sorted(ep_slopes)[len(ep_slopes) // 2] if ep_slopes else 0.0

    # regime trend: full-window OLS + 7d-MA OLS (APR pts per day)
    trend = ols_slope(dv) * 365.0
    ma7 = [sum(dv[max(0, i - 6):i + 1]) / len(dv[max(0, i - 6):i + 1])
           for i in range(n)]
    trend7 = ols_slope(ma7) * 365.0

    def apr_last(days_n):
        cut = days[-days_n:] if n >= days_n else days
        return sum(daily[k] for k in cut) / len(cut) * 365.0

    return {
        "apr_90d": cum / overlap_days * 365.0,
        "apr_30d": apr_last(30), "apr_14d": apr_last(14),
        "pos_day_frac": pos_frac,
        "persist_3bps": persist[0.0003], "persist_10bps": persist[0.0010],
        "persist_25bps": persist[0.0025],
        "cond1_pos": cond1, "cond7_pos": cond7,
        "n_episodes": len(eps), "med_ep_days": med_ep_days,
        "max_ep_days": max_ep_days, "med_ep_slope_apr_day": med_ep_slope,
        "trend_apr_pts_day": trend, "trend7_apr_pts_day": trend7,
        "max_dd_notional": max_dd, "daily_std": std_d,
        "overlap_days": overlap_days, "iv_s": iv_s, "iv_l": iv_l,
    }


def aster_floor_stats(events):
    """Floor stability for one Aster contract: events [(ts, rate_per_event)].
    Expected per-event floor = 0.0001 * (iv/8). Returns (iv, frac_at_floor,
    n_distinct_levels, n_events)."""
    iv = infer_interval_h(events)
    floor = 0.0001 * (iv / 8.0)
    rates = [r for _, r in events]
    if not rates or floor <= 0:
        return iv, 0.0, len(set(rates)), len(rates)
    at_floor = sum(1 for r in rates if abs(r - floor) <= 0.1 * floor)
    levels = len({round(r, 8) for r in rates})
    return iv, at_floor / len(rates), levels, len(rates)


# ------------------------------------------------------------ candidates
def load_snapshot():
    snap = json.load(open(SNAP))
    perps = {v: snap["perps"][v].get("rows", {}) for v in VENUES}
    return snap, perps


async def build_candidates(session, perps, resolve):
    def mark_of(v, base):
        raw = resolve[v].get(base)
        return perps[v][raw].get("mark", 0.0) if raw else 0.0

    def vol_of(base):
        best = 0.0
        for v in VENUES:
            raw = resolve[v].get(base)
            if raw:
                best = max(best, perps[v][raw].get("day_volume") or 0.0)
        return best

    def liq_ok(base):
        px = max(mark_of(v, base) for v in VENUES)
        if px <= 0.05:
            return False
        if base in MAJORS:
            return True
        vol = vol_of(base)
        if vol > 0:
            return vol >= 1_000_000
        return px > 1.0

    def listed_on(base, exclude=()):
        return [v for v in VENUES if v not in exclude and base in resolve[v]]

    cand = {}   # base -> set(venues)
    # E1 cohort: Aster hourly contracts with >=1 alternative venue
    for base, raw in resolve["aster"].items():
        if perps["aster"][raw].get("interval_h") == 1 and liq_ok(base):
            others = listed_on(base, exclude=("aster",))
            if others:
                cand.setdefault(base, set()).update(["aster"] + others)
    n_e1 = len(cand)
    # E2 cohort: stock/RWA bases (any venue pair)
    for base in e2_universe:
        others = listed_on(base)
        if len(others) >= 2 and liq_ok(base):
            cand.setdefault(base, set()).update(others)
    n_e1e2 = len(cand)
    # top-25 live differentials per venue pair
    def live_apr(v, base):
        raw = resolve[v].get(base)
        if not raw:
            return None
        r = perps[v][raw]
        return r["rate"] * 24 / r.get("interval_h", 8) * 365
    for i, va in enumerate(VENUES):
        for vb in VENUES[i + 1:]:
            common = set(resolve[va]) & set(resolve[vb])
            scored = []
            for base in common:
                if not liq_ok(base):
                    continue
                ra, rb = live_apr(va, base), live_apr(vb, base)
                if ra is None or rb is None:
                    continue
                scored.append((abs(ra - rb), base))
            scored.sort(reverse=True)
            for _, base in scored[:25]:
                cand.setdefault(base, set()).update({va, vb})
    # majors
    for base in MAJORS:
        others = listed_on(base)
        if others:
            cand.setdefault(base, set()).update(others)
    # cap: prefer multi-venue coverage, then max live |diff|
    if len(cand) > CAP:
        def key(item):
            base, vs = item
            best = 0.0
            vs_l = sorted(vs)
            for i, va in enumerate(vs_l):
                for vb in vs_l[i + 1:]:
                    ra, rb = live_apr(va, base), live_apr(vb, base)
                    if ra is not None and rb is not None:
                        best = max(best, abs(ra - rb))
            return (-len(vs), -best)
        keep = sorted(cand.items(), key=key)[:CAP]
        cand = dict(keep)
    print(f"Candidates: {len(cand)} coins (E1 cohort {n_e1}, +E2 {n_e1e2}, "
          f"+top-diff/majors to final), venues/cap applied")
    return cand


# ------------------------------------------------------------------- main
async def main():
    global e2_universe
    snap, perps = load_snapshot()
    resolve = {v: {} for v in VENUES}
    for v in VENUES:
        for raw, r in perps[v].items():
            resolve[v][r.get("base_norm", raw)] = raw

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - WINDOW_DAYS * DAY_MS

    async with aiohttp.ClientSession() as session:
        info = await fetch(session, "https://fapi.asterdex.com/fapi/v1/exchangeInfo")
        bases = []
        if isinstance(info, dict):
            for s in info.get("symbols", []):
                if s.get("status") == "TRADING":
                    ASTER_SYMS[s["baseAsset"].upper()] = s["symbol"]
            print(f"Aster symbol map: {len(ASTER_SYMS)} trading symbols")
        if isinstance(info, dict):
            for s in info.get("symbols", []):
                if set(s.get("underlyingSubType") or []) & E2_SUBTYPES \
                        and s.get("status") == "TRADING":
                    bases.append(s["baseAsset"].upper())
        if bases:
            json.dump({"ts": time.time(), "bases": bases},
                      open(E2_BASES_CACHE, "w"))
        else:
            bases = sorted(E2_FALLBACK)
        e2_universe = set(bases)
        print(f"E2 universe (Aster STOCK/ETF/Commodity/RWA): {len(e2_universe)} bases")

        cand = await build_candidates(session, perps, resolve)

        cache = {}
        if os.path.exists(CACHE):
            try:
                cache = json.load(open(CACHE))
            except Exception:
                cache = {}

        sem = asyncio.Semaphore(5)
        lock = asyncio.Lock()
        done = [0]
        fails = {}

        async def collect(coin, venues):
            async with sem:
                async with lock:
                    cached = cache.get(coin)
                if cached and all(len(cached.get(v) or []) >= 30
                                  for v in venues):
                    return
                res = {}
                for v in venues:
                    sym = resolve.get(v, {}).get(coin, coin)
                    try:
                        ev = await HIST[v](session, sym, start_ms, end_ms)
                    except Exception:
                        fails[v] = fails.get(v, 0) + 1
                        ev = []
                    res[v] = sorted(set(ev))  # dedup, sort
                    await asyncio.sleep(0.1)
                async with lock:
                    cache[coin] = res
                    done[0] += 1
                    if done[0] % 20 == 0:
                        print(f"  fetched {done[0]}/{len(cand)} coins")
                        json.dump(cache, open(CACHE, "w"))

        await asyncio.gather(*[collect(c, sorted(vs)) for c, vs in cand.items()])
        json.dump(cache, open(CACHE, "w"))
    print(f"History cached for {len(cache)} coins; fetch failures: {fails}")

    # ------------------------------------------------- evaluate venue pairs
    rows = []
    skipped_identity = skipped_short_window = 0
    for base, vs in sorted(cand.items()):
        h = cache.get(base) or {}
        vs_l = sorted(vs)
        for i, va in enumerate(vs_l):
            for vb in vs_l[i + 1:]:
                raw_a, raw_b = resolve[va].get(base), resolve[vb].get(base)
                if not raw_a or not raw_b:
                    continue
                ra, rb = perps[va].get(raw_a), perps[vb].get(raw_b)
                if not ra or not rb:
                    continue
                ma, mb = ra.get("mark", 0), rb.get("mark", 0)
                if ma <= 0 or mb <= 0 or abs(ma / mb - 1) > 0.05:
                    skipped_identity += 1
                    continue
                apr_a = ra["rate"] * 24 / ra.get("interval_h", 8) * 365
                apr_b = rb["rate"] * 24 / rb.get("interval_h", 8) * 365
                short_v, long_v = (va, vb) if apr_a >= apr_b else (vb, va)
                hs, hl_ = h.get(short_v) or [], h.get(long_v) or []
                if len(hs) < 30 or len(hl_) < 30:
                    continue
                m = spread_metrics_v2(hs, hl_, end_ms)
                if m is None:
                    skipped_short_window += 1
                    continue
                cost_rt = 2 * (TAKER[short_v] + TAKER[long_v])
                med_ep = m["med_ep_days"]
                net_ep = (m["apr_90d"] - cost_rt * 365 / med_ep) if med_ep >= 2 else None
                flag = []
                if short_v == "aster" and m["iv_s"] < 1.5:
                    flag.append("E1")
                if base in e2_universe:
                    flag.append("E2")
                rows.append({
                    "coin": base, "short_venue": short_v, "long_venue": long_v,
                    "iv_short": m["iv_s"], "iv_long": m["iv_l"],
                    "overlap_days": m["overlap_days"],
                    "apr_90d": m["apr_90d"], "apr_30d": m["apr_30d"],
                    "apr_14d": m["apr_14d"], "pos_day_frac": m["pos_day_frac"],
                    "persist_3bps": m["persist_3bps"],
                    "persist_10bps": m["persist_10bps"],
                    "persist_25bps": m["persist_25bps"],
                    "cond1_pos": m["cond1_pos"], "cond7_pos": m["cond7_pos"],
                    "n_episodes": m["n_episodes"], "med_ep_days": med_ep,
                    "max_ep_days": m["max_ep_days"],
                    "med_ep_slope_apr_day": m["med_ep_slope_apr_day"],
                    "trend_apr_pts_day": m["trend_apr_pts_day"],
                    "trend7_apr_pts_day": m["trend7_apr_pts_day"],
                    "max_dd_notional": m["max_dd_notional"],
                    "daily_std": m["daily_std"],
                    "live_gross_apr": max(apr_a, apr_b) - min(apr_a, apr_b),
                    "cost_roundtrip": cost_rt,
                    "net_apr_14d_hold": m["apr_90d"] - cost_rt * 365 / 14,
                    "net_apr_30d_hold": m["apr_90d"] - cost_rt * 365 / 30,
                    "net_apr_ep_hold": net_ep,
                    "flag": "+".join(flag) or "GEN",
                })

    os.makedirs(OUTDIR, exist_ok=True)
    cols = list(rows[0].keys()) if rows else []
    def dump(path, data):
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in data:
                w.writerow({c: (round(r[c], 6) if isinstance(r[c], float) else r[c])
                            for c in cols})
    dump(OUT_CSV, rows)
    print(f"\nSaved {len(rows)} pairs -> {OUT_CSV} "
          f"(identity-skipped {skipped_identity}, short-window {skipped_short_window})")

    def fmt(r):
        return (f"{r['coin']:14s} {r['short_venue']:8s}->{r['long_venue']:8s} "
                f"APR90 {r['apr_90d']*100:6.1f}%  30d {r['apr_30d']*100:6.1f}%  "
                f"pos {r['pos_day_frac']:4.0%}  p3bp {r['persist_3bps']:4.0%}  "
                f"ep {r['med_ep_days']:3d}d  slope {r['med_ep_slope_apr_day']:+5.1f}  "
                f"net30 {r['net_apr_30d_hold']*100:6.1f}%")

    rows.sort(key=lambda r: r["net_apr_30d_hold"], reverse=True)
    print("\n=== TOP 25 ALL PAIRS (by net APR @30d hold) ===")
    for r in rows[:25]:
        print(fmt(r))

    # ---------------------------------------------------------- E1 section
    e1 = [r for r in rows if r["flag"].find("E1") >= 0]
    e1.sort(key=lambda r: r["apr_30d"], reverse=True)
    dump(E1_CSV, e1)
    print(f"\n=== E1: ASTER HOURLY-COHORT SHORTS ({len(e1)} pairs) ===")
    for r in e1[:20]:
        print(fmt(r))

    def dist(vals, name):
        if not vals:
            print(f"  {name}: n/a")
            return
        vals = sorted(vals)
        q = lambda p: vals[min(len(vals) - 1, int(p * len(vals)))]
        print(f"  {name}: p10 {q(0.10):.1f}  p25 {q(0.25):.1f}  med {q(0.50):.1f}  "
              f"p75 {q(0.75):.1f}  p90 {q(0.90):.1f}")

    print("\nE1 DISTRIBUTIONS (per-pair, % units):")
    dist([r["apr_90d"] * 100 for r in e1], "apr_90d %")
    dist([r["apr_30d"] * 100 for r in e1], "apr_30d %")
    dist([r["pos_day_frac"] * 100 for r in e1], "pos_day_frac %")
    dist([r["persist_3bps"] * 100 for r in e1], "persist>=3bps %")
    dist([r["med_ep_days"] for r in e1], "median episode days")
    dist([r["med_ep_slope_apr_day"] for r in e1], "episode slope APRpts/day")

    # floor stability per Aster hourly coin
    fl = []
    for base in sorted(cand):
        raw = resolve["aster"].get(base)
        if not raw:
            continue
        ev = (cache.get(base) or {}).get("aster") or []
        if len(ev) >= 30 and perps["aster"][raw].get("interval_h") == 1:
            iv, frac, levels, n = aster_floor_stats(ev)
            fl.append((base, frac, levels, n))
    if fl:
        print(f"\nAster hourly floor stability (n={len(fl)} coins): "
              f"median frac@floor {sorted(f[1] for f in fl)[len(fl)//2]:.0%}, "
              f"coins >=90% at floor: {sum(1 for f in fl if f[1] >= 0.9)}, "
              f"coins >=50%: {sum(1 for f in fl if f[1] >= 0.5)}")

    # ---------------------------------------------------------- E2 section
    e2 = [r for r in rows if r["flag"].find("E2") >= 0]
    e2.sort(key=lambda r: r["apr_30d"], reverse=True)
    dump(E2_CSV, e2)
    print(f"\n=== E2: STOCK/RWA PERP CROSS-VENUE ({len(e2)} pairs) ===")
    for r in e2[:20]:
        print(f"{fmt(r)}  ovl {r['overlap_days']:.0f}d  trend7 {r['trend7_apr_pts_day']:+6.1f}")
    print("\nE2 DISTRIBUTIONS (per-pair, % units):")
    dist([r["apr_90d"] * 100 for r in e2], "apr_full %")
    dist([r["apr_30d"] * 100 for r in e2], "apr_30d %")
    dist([r["overlap_days"] for r in e2], "overlap days")
    dist([r["med_ep_days"] for r in e2], "median episode days")
    dist([r["trend7_apr_pts_day"] for r in e2], "trend7 APRpts/day")


if __name__ == "__main__":
    asyncio.run(main())
