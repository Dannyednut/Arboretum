#!/usr/bin/env python3
"""Part-9 D0: Sharpe floor scan - persistence distributions on venues we do NOT cover.

Free-tier constraints (probe_sharpe_v4.py):
  - /api/funding/rates?type=history&coin=X&days=N works; exchange/offset/end_time
    params are IGNORED; rows oldest-first, hard cap 5000.
  - 'days' is anchored to now -> two-slice strategy: days=60 (old sample, may be
    capped) + days=8 (recent sample) when the first pull comes back capped.

Pipeline:
  0. current book (5 asset_class slices) -> venue census + candidate selection
  1. SHORT candidates: new-venue legs pinned >=0.8x venue floor (floor >= 5% APR)
     LONG-COLLECT candidates: legs with APR <= -5% (paid-to-long, Kraken-type)
  2. history per unique coin (2-slice), sliced by exchange
  3. leg metrics: pos_frac, exact/near pin, median/mean APR, p10/p90, slope
  4. pairs: short new-venue x long direct-venue (cache first, Sharpe fallback)
     union-hour grid + ffill -> spread persistence; stress rt-cost -> net@14/30d
  5. CSVs + cross-validation of Sharpe vs our cached direct-venue series

Outputs:
  download/data/sharpe_newvenue_legs.csv
  download/data/sharpe_newvenue_pairs.csv
"""
import csv
import json
import os
import statistics as st
import time
import urllib.request

BASE = "https://www.sharpe.ai"
OUT_DIR = "/home/z/my-project/download/data"
CACHE = "/home/z/my-project/scripts/funding_history_cache_v3.json"
_CACHE = None          # lazy-loaded cache dict

DIRECT = {"Binance": "binance", "Bybit": "bybit", "OKX": "okx",
          "Bitget": "bitget", "Gate.io": "gate", "Hyperliquid": "hl",
          "dYdX": "dydx", "Aster": "aster"}
# verified taker fees (bps/side) from Parts 1-3; None = unverified -> 5bps stress
FEE_BPS = {"binance": 5.0, "bybit": 5.5, "okx": 5.0, "bitget": 6.0,
           "gate": 5.0, "hl": 4.5, "aster": 4.0, "dydx": 5.0}
UNVERIFIED_FEE_BPS = 5.0
SLIP_RT_BPS = 20.0          # placeholder round-trip slippage stress (pre depth-gate)
MAX_COINS = 170
CAP_PER_VENUE = 12
# Part-10 D0: Gate.io history is auth-gated natively; Sharpe's Gate legs failed
# live spot-check (LTC ~0% now vs Sharpe-implied -42% median) -> excluded as
# counter-leg until a native history source exists.
QUARANTINED_COUNTERS = {"gate"}

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def get(path, timeout=60):
    req = urllib.request.Request(BASE + path, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def apr_pct(rate, interval_hours):
    """interval-true annualization: rate (decimal/settlement) -> APR %."""
    if not interval_hours or interval_hours <= 0:
        return None
    return rate * (8760.0 / interval_hours) * 100.0


def hour_bucket(ts_iso_or_ms):
    if isinstance(ts_iso_or_ms, str):
        return iso_to_epoch(ts_iso_or_ms) // 3600
    return None


def iso_to_epoch(s):
    from datetime import datetime, timezone
    t = s.replace("Z", "+00:00")
    return int(datetime.fromisoformat(t).timestamp())


# ---------------------------------------------------------------- phase 0/1
def load_current_book():
    rows, seen = [], set()
    for ac in ("crypto", "equity", "commodity", "fx", "index"):
        try:
            d = get(f"/api/funding/rates?type=current&asset_class={ac}&limit=5000")
        except Exception as e:
            print(f"  !! current {ac}: {e!r}")
            continue
        rr = d if isinstance(d, list) else (d.get("data") or [])
        n = 0
        for r in rr:
            k = (r.get("exchange"), r.get("symbol"))
            if k in seen:
                continue
            seen.add(k)
            rows.append(r)
            n += 1
        print(f"  current[{ac}]: {n} new rows (cum {len(rows)})")
        time.sleep(0.3)
    return rows


def venue_census(rows):
    cen = {}
    for r in rows:
        v = r.get("exchange")
        if not v:
            continue
        a = apr_pct(r.get("rate") or 0.0, r.get("interval_hours") or 0)
        if a is None:
            continue
        c = cen.setdefault(v, {"apr": [], "oi_apr": {}})
        c["apr"].append(a)
    out = {}
    for v, c in cen.items():
        ap = c["apr"]
        modal = st.median(ap)
        out[v] = {"n": len(ap), "modal_apr": modal,
                  "pin_frac": sum(1 for x in ap if abs(x - modal) < 0.25) / len(ap)}
    return out


def pick_candidates(rows, cen):
    shorts, longs = {}, {}
    for r in rows:
        v = r.get("exchange")
        if v in DIRECT or v not in cen:
            continue
        ivl = r.get("interval_hours") or 0
        a = apr_pct(r.get("rate") or 0.0, ivl)
        if a is None:
            continue
        coin = (r.get("base_coin") or "").upper()
        if not coin:
            coin = (r.get("symbol") or "").replace("USDT", "").replace(
                "USDC", "").replace("-USD", "").replace("USDT0", "").upper()
        if not coin or len(coin) > 12:
            continue
        floor = cen[v]["modal_apr"]
        oi = float(r.get("open_interest") or 0)
        rank = oi * abs(a)
        if floor >= 5.0 and a >= 0.8 * floor:
            shorts.setdefault(v, []).append((rank, coin, a, oi))
        elif a <= -5.0:
            longs.setdefault(v, []).append((rank, coin, a, oi))
    sl = {}
    for v, lst in shorts.items():
        lst.sort(reverse=True)
        sl[v] = [(c, a) for _, c, a, _ in lst[:CAP_PER_VENUE]]
    ll = {}
    for v, lst in longs.items():
        lst.sort(reverse=True)
        ll[v] = [(c, a) for _, c, a, _ in lst[:CAP_PER_VENUE]]
    return sl, ll


# ---------------------------------------------------------------- phase 2/3
def pull_history(coin):
    """Two-slice pull; returns {(exchange, symbol): [(epoch, rate, ivl), ...]}."""
    series = {}
    for days in (60, 8):
        try:
            d = get(f"/api/funding/rates?type=history&coin={urllib.request.quote(coin)}"
                    f"&days={days}&limit=5000")
        except Exception as e:
            print(f"    !! hist {coin} d{days}: {e!r}", flush=True)
            time.sleep(1.0)
            continue
        rr = d if isinstance(d, list) else (d.get("data") or [])
        for r in rr:
            k = (r.get("exchange"), r.get("symbol"))
            ts = r.get("settled_at")
            if not ts:
                continue
            series.setdefault(k, {})
            series[k][iso_to_epoch(ts)] = (r.get("rate") or 0.0,
                                           r.get("interval_hours") or 0)
        if len(rr) < 5000:      # not capped -> recent slice redundant
            break
        time.sleep(0.35)
    return {k: [(e, rv[0], rv[1]) for e, rv in sorted(v.items())]
            for k, v in series.items()}


CKPT = "/home/z/my-project/scripts/sharpe_hist_checkpoint.json"


def _norm_pts(v):
    """Normalize checkpoint rows to (epoch, rate, ivl) 3-tuples."""
    out = []
    for it in v:
        if len(it) == 3:
            out.append((it[0], it[1], it[2]))
        elif len(it) == 2 and isinstance(it[1], (list, tuple)) \
                and len(it[1]) == 2:
            out.append((it[0], it[1][0], it[1][1]))
    return out


def ckpt_load():
    if os.path.exists(CKPT):
        try:
            raw = json.load(open(CKPT))
            out = {}
            for coin, venues in raw.items():
                out[coin] = {tuple(k.split("||")): _norm_pts(v)
                             for k, v in venues.items()}
            return out
        except Exception:
            return {}
    return {}


def ckpt_save(hist):
    raw = {coin: {f"{k[0]}||{k[1]}": v for k, v in venues.items()}
           for coin, venues in hist.items()}
    tmp = CKPT + ".tmp"
    json.dump(raw, open(tmp, "w"))
    os.replace(tmp, CKPT)


def leg_metrics(pts, short_cand):
    """pts: [(epoch, rate, ivl)] -> metrics dict (APR % interval-true)."""
    if len(pts) < 20:
        return None
    ap, ts = [], []
    for e, rate, ivl in pts:
        a = apr_pct(rate, ivl)
        if a is not None:
            ap.append(a)
            ts.append(e)
    if len(ap) < 20:
        return None
    modal = st.median(ap)
    gaps = [(ts[i + 1] - ts[i]) / 3600.0 for i in range(len(ts) - 1)
            if ts[i + 1] > ts[i]]
    cad = st.median(gaps) if gaps else 0
    # OLS slope of APR vs day
    d0 = min(ts)
    xs = [(e - d0) / 86400.0 for e in ts]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ap) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ap))
    den = sum((x - mx) ** 2 for x in xs) or 1.0
    slope = num / den
    srt = sorted(ap)
    p = lambda q: srt[min(n - 1, int(q * n))]
    return {
        "n": n, "start": min(ts), "end": max(ts),
        "days_covered": (max(ts) - min(ts)) / 86400.0,
        "cadence_h": cad, "pos_frac": sum(1 for x in ap if x > 0) / n,
        "modal_apr": modal,
        "pin_frac": sum(1 for x in ap if abs(x - modal) < 0.25) / n,
        "near_pin_frac": (sum(1 for x in ap if modal > 0 and x >= 0.8 * modal) / n
                          if modal > 0 else 0.0),
        "median_apr": modal, "mean_apr": my,
        "p10": p(0.10), "p90": p(0.90), "slope_apr_pts_day": slope,
        "side": "short" if short_cand else "long_collect",
    }


def load_cache():
    if os.path.exists(CACHE):
        return json.load(open(CACHE))
    return {}


def cache_series(coin, venue_id):
    """Our own 90d cache: {venue: [[ts_ms, rate], ...]} -> [(epoch, rate, ivl inferred)]."""
    global _CACHE
    if _CACHE is None:
        _CACHE = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    c = _CACHE.get(coin) or {}
    pts = c.get(venue_id) or []
    if len(pts) < 20:
        return None
    out = []
    for i, (t, r) in enumerate(pts):
        if i + 1 < len(pts):
            ivl = max(1, round((pts[i + 1][0] - t) / 3600000.0))
        else:
            ivl = max(1, round((t - pts[i - 1][0]) / 3600000.0)) if i else 1
        out.append((t / 1000.0, r, ivl))
    return out


def sharpe_series_for(coin, venue_name, hist):
    for (ex, sym), pts in hist.items():
        if ex == venue_name:
            return pts
    return None


# ---------------------------------------------------------------- phase 4
def pair_metrics(short_pts, long_pts):
    """Union-hour grid + ffill of PER-HOUR carry (rate/interval_hours);
    spread APR = (short_ph - long_ph) * 8760 * 100. Per-hour conversion is
    mandatory: raw-rate ffill would over-count 4h/8h venues by cadence factor."""
    def hourly(pts):
        h = {}
        for e, r, ivl in pts:
            if not ivl or ivl <= 0:
                continue
            h[int(e // 3600)] = r / float(ivl)   # per-hour carry equivalent
        return h
    hs, hl_ = hourly(short_pts), hourly(long_pts)
    lo = max(min(hs), min(hl_))
    hi = min(max(hs), max(hl_))
    if hi <= lo:
        return None
    spread, ss, ls_ = [], [], []
    cs = cl = None
    for h in range(lo, hi + 1):
        if h in hs:
            cs = hs[h]
        if h in hl_:
            cl = hl_[h]
        if cs is None or cl is None:
            continue
        spread.append((cs - cl) * 8760.0 * 100.0)
        ss.append(cs * 8760.0 * 100.0)
        ls_.append(cl * 8760.0 * 100.0)
    if len(spread) < 48:
        return None
    n = len(spread)
    d0 = lo * 3600
    xs = [((h * 3600 - d0) / 86400.0) for h in range(lo, hi + 1)]
    xs = xs[:n]
    mx, my = sum(xs) / n, sum(spread) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, spread))
    den = sum((x - mx) ** 2 for x in xs) or 1.0
    srt = sorted(spread)
    return {
        "hours": n, "days": n / 24.0,
        "pos_frac": sum(1 for x in spread if x > 0) / n,
        "median_apr": st.median(spread), "mean_apr": my,
        "p25": srt[int(0.25 * n)], "slope": num / den,
        "short_raw_apr": st.median(ss), "long_raw_apr": st.median(ls_),
    }


def rt_cost_bps(short_venue_id, long_venue_id):
    fs = FEE_BPS.get(short_venue_id, UNVERIFIED_FEE_BPS)
    fl = FEE_BPS.get(long_venue_id, UNVERIFIED_FEE_BPS)
    return 2.0 * (fs + fl) + SLIP_RT_BPS


# ------------------------------------------------------- reachability probe
PROBE_URLS = {
    "BitMEX": ("https://www.bitmex.com/api/v1/instrument?count=1", "known"),
    "Kraken": ("https://futures.kraken.com/derivatives/api/v3/tickers", "known"),
    "MEXC": ("https://contract.mexc.com/api/v1/contract/ping", "known"),
    "KuCoin": ("https://api-futures.kucoin.com/api/v1/contracts/active", "known"),
    "CoinEx": ("https://api.coinex.com/perp/v1/market/list", "guess"),
    "BingX": ("https://open-api.bingx.com/openApi/swap/v2/quote/contracts", "known"),
    "HTX": ("https://api.hbdm.com/linear-swap-api/v1/swap_contract_info", "known"),
    "LBank": ("https://api.lbkex.com/v2/exchangeList.do", "guess"),
    "Crypto.com": ("https://api.crypto.com/exchange/v1/public/get-instruments", "known"),
    "Backpack": ("https://api.backpack.exchange/api/v1/markets", "guess"),
    "Pacifica": ("https://api.pacifica.fi/info/markets", "guess"),
    "Extended": ("https://api.extended.exchange/api/v1/perpetuals/markets", "guess"),
    "Lighter": ("https://mainnet.zklighter.elliptic.co/api/v1/exchangeConfig", "guess"),
    "GRVT": ("https://api.grvt.io/v1/markets", "guess"),
    "edgeX": ("https://proapi.edgex.exchange/api/v1/public/meta", "guess"),
    "ApeX": ("https://api.omni.apex.exchange/api/v3/public/symbols", "guess"),
    "Orderly": ("https://api.orderly.org/v1/public/info", "guess"),
    "Nado": ("https://nado.xyz", "guess"),
    "tradeXYZ": ("https://www.trade.xyz", "guess"),
    "Variational": ("https://api.variational.io", "guess"),
    "WhiteBIT": ("https://whitebit.com/api/v2/public/markets", "guess"),
    "Bitunix": ("https://fapi.bitunix.com/api/v1/futures/market/symbols", "guess"),
    "Coinbase": ("https://api.intx.coinbase.com/v1/instruments", "guess"),
}


def probe_reachability(venues):
    out = {}
    for v in sorted(venues):
        if v not in PROBE_URLS:
            out[v] = ("untested", "")
            continue
        url, conf = PROBE_URLS[v]
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=8) as r:
                out[v] = ("reachable" if r.status == 200 else
                          f"http{r.status}", conf)
        except Exception as e:
            out[v] = ("blocked", conf)
        time.sleep(0.2)
    return out


# ---------------------------------------------------------------- main
def main():
    print("=== 0. current book (5 asset_class slices)")
    book = load_current_book()
    cen = venue_census(book)
    newv = [v for v in cen if v not in DIRECT]
    print(f"  venues: {len(cen)} total, {len(newv)} new-to-us")

    print("=== 1. candidate selection")
    shorts, longs = pick_candidates(book, cen)
    cand_coins = sorted({c for lst in shorts.values() for c, _ in lst} |
                        {c for lst in longs.values() for c, _ in lst})
    print(f"  short-cand venues: {len(shorts)} | long-collect venues: {len(longs)}"
          f" | unique coins: {len(cand_coins)}")
    if len(cand_coins) > MAX_COINS:
        # keep coins that appear on the most venues (most pairable)
        freq = {}
        for lst in list(shorts.values()) + list(longs.values()):
            for c, _ in lst:
                freq[c] = freq.get(c, 0) + 1
        cand_coins = sorted(cand_coins, key=lambda c: -freq.get(c, 0))[:MAX_COINS]
        print(f"  capped to {len(cand_coins)} coins")

    print("=== 2. history pulls (2-slice strategy, checkpointed)", flush=True)
    hist_by_coin = ckpt_load()
    if hist_by_coin:
        print(f"  resume: {len(hist_by_coin)} coins already in checkpoint", flush=True)
    todo = [c for c in cand_coins if c not in hist_by_coin]
    for i, coin in enumerate(todo):
        hist_by_coin[coin] = pull_history(coin)
        ckpt_save(hist_by_coin)
        if (i + 1) % 10 == 0:
            print(f"  ... {i+1}/{len(todo)} coins pulled", flush=True)
        time.sleep(0.25)
    leg_rows = []

    print("=== 3. leg metrics")
    side_map = {}
    for v, lst in shorts.items():
        for c, _ in lst:
            side_map[(v, c)] = True
    for v, lst in longs.items():
        for c, _ in lst:
            side_map.setdefault((v, c), False)

    for coin in cand_coins:
        hist = hist_by_coin.get(coin) or {}
        for (ex, sym), pts in hist.items():
            if ex in DIRECT:
                continue
            sc = side_map.get((ex, coin), False)
            m = leg_metrics(pts, sc)
            if not m:
                continue
            m.update({"venue": ex, "symbol": sym, "coin": coin})
            leg_rows.append(m)
    print(f"  new-venue legs with metrics: {len(leg_rows)}")

    print("=== 4. pair construction (counter = direct venues)")
    pair_rows = []
    for leg in leg_rows:
        coin = leg["coin"]
        vname = leg["venue"]
        if leg["side"] == "long_collect":
            continue  # handled below as long leg
        short_pts = sharpe_series_for(coin, vname, hist_by_coin.get(coin) or {})
        if not short_pts:
            continue
        best = None
        for dv in DIRECT.values():
            if dv in QUARANTINED_COUNTERS:
                continue
            pts = cache_series(coin, dv) or sharpe_series_for(
                coin, [k for k, vv in DIRECT.items() if vv == dv][0],
                hist_by_coin.get(coin) or {})
            if not pts:
                continue
            lp = [p for p in pts if p[1] is not None]
            if not lp:
                continue
            pm = pair_metrics(short_pts, lp)
            if not pm:
                continue
            if best is None or pm["median_apr"] > best[1]["median_apr"]:
                best = (dv, pm, len(pts))
        if not best:
            continue
        dv, pm, nlen = best
        rt_pct = rt_cost_bps("unverified", dv) / 100.0   # percent per RT
        net30 = pm["median_apr"] - rt_pct * 365.0 / 30.0
        net14 = pm["median_apr"] - rt_pct * 365.0 / 14.0
        be = (rt_pct * 365.0 / pm["median_apr"]
              if pm["median_apr"] > 0 else -1)
        pair_rows.append({
            "coin": coin, "short_venue": vname, "long_venue": dv,
            "long_leg_n": nlen, "hours": pm["hours"], "days": round(pm["days"], 1),
            "pos_frac": round(pm["pos_frac"], 3),
            "median_spread_apr": round(pm["median_apr"], 1),
            "mean_spread_apr": round(pm["mean_apr"], 1),
            "p25_spread_apr": round(pm["p25"], 1),
            "slope_apr_pts_day": round(pm["slope"], 3),
            "short_leg_median_apr": round(pm["short_raw_apr"], 1),
            "long_leg_median_apr": round(pm["long_raw_apr"], 1),
            "rt_stress_pct": round(rt_pct, 2),
            "net_apr_30d_pct": round(net30, 1),
            "net_apr_14d_pct": round(net14, 1),
            "breakeven_days": round(be, 1) if be > 0 else "",
        })

    # long-collect legs: long new-venue x short direct (direct must be POSITIVE)
    for leg in leg_rows:
        if leg["side"] != "long_collect":
            continue
        coin = leg["coin"]
        long_pts = sharpe_series_for(coin, leg["venue"],
                                     hist_by_coin.get(coin) or {})
        if not long_pts:
            continue
        best = None
        for dv in DIRECT.values():
            if dv in QUARANTINED_COUNTERS:
                continue
            pts = cache_series(coin, dv) or sharpe_series_for(
                coin, [k for k, vv in DIRECT.items() if vv == dv][0],
                hist_by_coin.get(coin) or {})
            if not pts:
                continue
            pm = pair_metrics(pts, long_pts)  # short=direct, long=new-venue
            if not pm:
                continue
            if best is None or pm["median_apr"] > best[1]["median_apr"]:
                best = (dv, pm, len(pts))
        if not best:
            continue
        dv, pm, nlen = best
        rt_pct = rt_cost_bps(dv, "unverified") / 100.0   # percent per RT
        net30 = pm["median_apr"] - rt_pct * 365.0 / 30.0
        pair_rows.append({
            "coin": coin, "short_venue": dv, "long_venue": leg["venue"],
            "long_leg_n": nlen, "hours": pm["hours"], "days": round(pm["days"], 1),
            "pos_frac": round(pm["pos_frac"], 3),
            "median_spread_apr": round(pm["median_apr"], 1),
            "mean_spread_apr": round(pm["mean_apr"], 1),
            "p25_spread_apr": round(pm["p25"], 1),
            "slope_apr_pts_day": round(pm["slope"], 3),
            "short_leg_median_apr": round(pm["short_raw_apr"], 1),
            "long_leg_median_apr": round(pm["long_raw_apr"], 1),
            "rt_stress_pct": round(rt_pct, 2),
            "net_apr_30d_pct": round(net30, 1),
            "net_apr_14d_pct": round(pm["median_apr"] - rt_pct * 365.0 / 14.0, 1),
            "breakeven_days": round(rt_pct * 365.0 / pm["median_apr"], 1)
            if pm["median_apr"] > 0 else "",
        })

    print(f"  pairs built: {len(pair_rows)}")

    print("=== 5. cross-validation: Sharpe vs our cache (3 cached coins)")
    global _CACHE
    if _CACHE is None:
        _CACHE = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    xval = []
    for coin in ("XMR", "HYPE", "FIL"):
        if coin not in _CACHE or coin not in hist_by_coin:
            continue
        hist = hist_by_coin[coin]
        for vn, vid in (("Aster", "aster"), ("Binance", "binance"),
                        ("Hyperliquid", "hl")):
            ours = cache_series(coin, vid)
            theirs = sharpe_series_for(coin, vn, hist)
            if not ours or not theirs:
                continue
            oh = {int(e // 3600): r for e, r, _ in ours}
            th = {int(e // 3600): r for e, r, _ in theirs}
            common = sorted(set(oh) & set(th))
            if len(common) < 100:
                continue
            agree = sum(1 for h in common if abs(oh[h] - th[h]) < 1e-9)
            close = sum(1 for h in common if abs(oh[h] - th[h]) < 5e-6)
            xval.append({"coin": coin, "venue": vn, "overlap_hours": len(common),
                         "exact_agree_frac": round(agree / len(common), 3),
                         "near_agree_frac": round(close / len(common), 3)})
            print(f"  {coin} {vn}: {len(common)}h overlap, "
                  f"exact {agree/len(common):.1%}, near {close/len(common):.1%}")

    print("=== 6. reachability probes for candidate venues")
    vens = sorted({r["venue"] for r in leg_rows})
    reach = probe_reachability(vens)
    for v, (s, conf) in reach.items():
        print(f"  {v:14s} {s:10s} ({conf})")

    reach_map = {v: s for v, (s, _) in reach.items()}
    for r in leg_rows:
        r["venue_reachable"] = reach_map.get(r["venue"], "untested")
    for r in pair_rows:
        r["short_venue_reachable"] = reach_map.get(r["short_venue"], "untested")
        r["long_venue_reachable"] = reach_map.get(r["long_venue"], "untested")

    os.makedirs(OUT_DIR, exist_ok=True)
    p1 = f"{OUT_DIR}/sharpe_newvenue_legs.csv"
    if leg_rows:
        cols = list(leg_rows[0].keys())
        with open(p1, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(leg_rows)
    p2 = f"{OUT_DIR}/sharpe_newvenue_pairs.csv"
    if pair_rows:
        cols = list(pair_rows[0].keys())
        with open(p2, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(sorted(pair_rows,
                               key=lambda r: -r["median_spread_apr"]))
    print(f"\nWROTE {p1} ({len(leg_rows)} rows)")
    print(f"WROTE {p2} ({len(pair_rows)} rows)")

    print("\n=== TOP 20 pairs by median spread APR ===")
    for r in sorted(pair_rows, key=lambda r: -r["median_spread_apr"])[:20]:
        print(f"  {r['coin']:10s} {r['short_venue']:11s}->{r['long_venue']:8s} "
              f"med {r['median_spread_apr']:7.1f}% pos {r['pos_frac']:.0%} "
              f"days {r['days']:5.1f} net30 {r['net_apr_30d_pct']:7.1f}% "
              f"[{r['short_venue_reachable']}]")


if __name__ == "__main__":
    main()
