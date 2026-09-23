#!/usr/bin/env python3
"""v3 M2 tripwire chain (spec Part 12 §3.2) - log-only in P2.

Runs the six tripwires IN ORDER before every (virtual) entry and on held
positions; every evaluation writes immutable rows to v3.db tripwire_log
(pass/fail/skip + measured JSON). No orders are placed by this module.

TW1 identity_guard   both-venue marks within 5%, margining type recorded
TW2 persistence      pair pos_day_frac >= 0.95 in DB models; live funding
                     interval per leg == DB median interval (venues change
                     intervals silently)
TW3 live_spread      current interval-true spread >= 40% of 60d median
TW4 depth_rewalk     walk both books both sides at target size:
                     slip <= 25bps/side, filled == notional,
                     swing vs recent windows < 2x
TW5 floor_monitor    native funding params (cap/floor/interval/interest/
                     bounds) change-detect vs kv baseline -> venue freeze
TW6 counterparty     (existing exposure + proposed size) <= venue cap

Modes:
  all                  pre-entry chain on the 5 basket pairs (ledger sizes)
  one COIN SV LV USD   pre-entry chain on one pair
  held                 chain on currently open ledger positions
  backfill-books       l2_depth_samples_ms.csv -> v3.db book_samples
"""
import asyncio
import csv
import json
import sqlite3
import statistics as stx
import sys
from datetime import datetime, timezone

import v3_store as st
import v3_connectors as vc
import depth_sampler as ds
import v3_scanner as v3scan

PAPER_DB = "/home/z/my-project/download/data/paper_v3.db"
L2_CSV = "/home/z/my-project/download/data/l2_depth_samples_ms.csv"

BASKET = [("XMR", "dydx", "binance", 400.0), ("BTW", "aster", "bitget", 400.0),
          ("UAI", "aster", "binance", 400.0), ("INJ", "bingx", "okx", 250.0),
          ("LINK", "nado", "okx", 250.0)]

MARGINING = {"binance": "linear_usdt", "okx": "linear_usdt",
             "bybit": "linear_usdt", "bitget": "linear_usdt",
             "aster": "linear_usdt", "bingx": "linear_usdt",
             "hl": "linear_usdc", "dydx": "linear_usd",
             "backpack": "linear_usdc", "nado": "linear_usdc",
             "orderly": "linear_usdt"}

CAPS = {"bingx": 5000.0, "nado": 5000.0, "backpack": 5000.0,
        "orderly": 5000.0}
DEFAULT_CAP = 20000.0

GATE_SLIP_BPS = 25.0
GATE_SWING_X = 2.0
GATE_LIVE_SPREAD_FRAC = 0.40
GATE_MARK_DEV = 0.05
GATE_POS_FRAC = 0.95


def nowiso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------- book helpers
async def _mid(venue, base):
    bids, asks = await vc.get_book(venue, base)
    if not bids or not asks:
        raise RuntimeError(f"{venue} {base}: empty book")
    return (bids[0][0] + asks[0][0]) / 2.0, bids, asks


def _walk(levels, mid, notional):
    r = ds.walk(levels, mid, notional)
    return (None, None) if r is None else (r[2], r[0])  # (vwap, slip_bps)


# ------------------------------------------------------------- TW1 identity
async def tw1_identity(coin, sv, lv):
    mid_s, _, _ = await _mid(sv, coin)
    mid_l, _, _ = await _mid(lv, coin)
    dev = abs(mid_s - mid_l) / mid_l
    ok = dev <= GATE_MARK_DEV
    return ok, {"mid_short": round(mid_s, 8), "mid_long": round(mid_l, 8),
                "dev_pct": round(dev * 100, 3),
                "margining": [MARGINING.get(sv, "?"),
                              MARGINING.get(lv, "?")],
                "gate_mark_dev_pct": GATE_MARK_DEV * 100}


# ---------------------------------------------------------- TW2 persistence
def _db_median_interval(con, coin, venue):
    ivs = [float(iv) for (iv,) in con.execute(
        "SELECT interval_h FROM funding_obs WHERE base=? AND venue=? "
        "AND interval_h>0 AND source IN ('native_hist','sharpe_hist')",
        (coin, venue))]
    return stx.median(ivs) if ivs else None


async def tw2_persistence(con, coin, sv, lv):
    ev = v3scan.event_index(con, "native_hist")
    ff = v3scan.ffill_index(con, "sharpe_hist")
    ks, kl = (coin, sv), (coin, lv)
    m = None
    if ks in ev and kl in ev:
        ev_s, iv_s = ev[ks]
        ev_l, iv_l = ev[kl]
        m = v3scan.pair_metrics_native_iv(ev_s, ev_l, iv_s, iv_l)
    model1 = m["model"] if m else None
    pos1 = m["pos_day_frac"] if m else None
    if m is None and ks in ff and kl in ff:
        m = v3scan.pair_metrics_ffill(ff[ks], ff[kl])
    if m is None and pos1 is None:
        return False, {"error": "no pair data in DB for either model",
                       "model": "none"}
    pos = pos1 if pos1 is not None else m["pos_day_frac"]
    model = model1 or m["model"]
    iv_ok, ivs = True, {}
    for v in (sv, lv):
        live = await vc.get_funding(v, coin)
        med = _db_median_interval(con, coin, v)
        li = float(live["interval_h"] or 0)
        ivs[v] = {"live_interval_h": li, "db_median_interval_h": med}
        if med and li and abs(li - med) / med > 0.01:
            iv_ok = False
    ok = (pos >= GATE_POS_FRAC) and iv_ok
    return ok, {"pos_day_frac": round(pos, 4), "model": model,
                "gate_pos_frac": GATE_POS_FRAC, "intervals": ivs,
                "interval_match": iv_ok}


# ---------------------------------------------------------- TW3 live spread
def _spread60d_apr(con, coin, sv, lv):
    """(median60d_apr_fraction, model, n_hours) - ffill model first."""
    ff = v3scan.ffill_index(con, "sharpe_hist")
    ks, kl = (coin, sv), (coin, lv)

    def hourly(pts):
        return {int(t // 3600_000): r / float(iv)
                for t, r, iv in pts if iv and iv > 0}

    if ks in ff and kl in ff:
        hs, hl_ = hourly(ff[ks]), hourly(ff[kl])
        lo, hi = max(min(hs), min(hl_)), min(max(hs), max(hl_))
        if hi > lo:
            cut = hi - 60 * 24
            ser = []
            cs = cl = None
            for h in range(lo, hi + 1):
                if h in hs:
                    cs = hs[h]
                if h in hl_:
                    cl = hl_[h]
                if cs is not None and cl is not None and h >= cut:
                    ser.append((cs - cl) * 8760.0)
            if len(ser) >= 48:
                return stx.median(ser), "sharpe_ffill_60d", len(ser)
    ev = v3scan.event_index(con, "native_hist")
    if ks in ev and kl in ev:
        ev_s, iv_s = ev[ks]
        ev_l, iv_l = ev[kl]
        m = v3scan.pair_metrics_native_iv(ev_s, ev_l, iv_s, iv_l)
        if m:
            return m["apr_30d"], "native_event_30d", m["days"]
    return None, "none", 0


async def tw3_live_spread(con, coin, sv, lv):
    f_s = await vc.get_funding(sv, coin)
    f_l = await vc.get_funding(lv, coin)
    cur_apr = (f_s["rate"] / float(f_s["interval_h"] or 1)
               - f_l["rate"] / float(f_l["interval_h"] or 1)) * 8760.0
    med, model, n = _spread60d_apr(con, coin, sv, lv)
    if med is None:
        return False, {"error": "no 60d median in DB", "live_apr": cur_apr}
    if med <= 0:
        return False, {"live_apr": round(cur_apr, 4),
                       "median60d_apr": round(med, 4), "model": model,
                       "note": "structural inversion in 60d window"}
    frac = cur_apr / med
    ok = frac >= GATE_LIVE_SPREAD_FRAC
    return ok, {"live_apr": round(cur_apr, 4),
                "median60d_apr": round(med, 4), "frac_of_median":
                round(frac, 3), "gate_frac": GATE_LIVE_SPREAD_FRAC,
                "model": model, "n_hours": n}


# -------------------------------------------------------- TW4 depth re-walk
def _recent_rt1k(con, venue, base, max_windows=5):
    """median rt1k over the last N windows in book_samples (None if absent)."""
    wins = [r[0] for r in con.execute(
        "SELECT DISTINCT window FROM book_samples WHERE venue=? AND base=? "
        "ORDER BY window DESC LIMIT ?", (venue, base, max_windows))]
    vals = []
    for w in wins:
        xs = [max(s or 999.0, b or 999.0) for (s, b) in con.execute(
            "SELECT slip_buy_1000, slip_sell_1000 FROM book_samples "
            "WHERE venue=? AND base=? AND window=? AND ok=1",
            (venue, base, w))]
        xs = [x for x in xs if x < 999.0]
        if xs:
            vals.append(stx.median(xs))
    return (stx.median(vals), wins) if vals else (None, wins)


async def tw4_depth(con, coin, sv, lv, size_usd):
    meas, walks, worst = {}, {}, 0.0
    for v, side in ((sv, "short"), (lv, "long")):
        mid, bids, asks = await _mid(v, coin)
        _, slip_in = _walk(asks if side == "long" else bids, mid, size_usd)
        _, slip_out = _walk(bids if side == "long" else asks, mid, size_usd)
        walks[f"{v}_{side}"] = {"entry_slip_bps": slip_in,
                                "unwind_slip_bps": slip_out}
        for x in (slip_in, slip_out):
            if x is None:
                return False, {"error": f"{v} {side}: book too thin to fill "
                              f"${size_usd:.0f}", "walks": walks}
            worst = max(worst, x)
    swing = {}
    for v in (sv, lv):
        med, wins = _recent_rt1k(con, v, coin)
        if med:
            live = max(walks[f"{v}_short" if v == sv else f"{v}_long"]
                       ["entry_slip_bps"],
                       walks[f"{v}_short" if v == sv else f"{v}_long"]
                       ["unwind_slip_bps"])
            swing[v] = {"live_rt1k": round(live, 1),
                        "recent_median": round(med, 1),
                        "x": round(live / med, 2), "windows": wins}
    swing_ok = all(s["x"] < GATE_SWING_X for s in swing.values())
    ok = worst <= GATE_SLIP_BPS and swing_ok
    out = {"worst_slip_bps": round(worst, 1),
           "gate_slip_bps": GATE_SLIP_BPS, "size_usd": size_usd,
           "walks": walks}
    if swing:
        out["swing"] = swing
        out["gate_swing_x"] = GATE_SWING_X
        out["swing_ok"] = swing_ok
    return ok, out


# -------------------------------------------------------- TW5 floor monitor
async def _params_aster(base):
    d, _ = await vc._get("https://fapi.asterdex.com/fapi/v1/fundingInfo")
    for r in (d if isinstance(d, list) else []):
        if r.get("symbol") == base + "USDT":
            return {"fundingFeeCap": float(r["fundingFeeCap"]),
                    "fundingFeeFloor": float(r["fundingFeeFloor"]),
                    "fundingIntervalHours": float(r["fundingIntervalHours"]),
                    "interestRate": float(r["interestRate"])}
    return None


async def _params_binance(base):
    d, _ = await vc._get("https://fapi.binance.com/fapi/v1/fundingInfo")
    for r in (d if isinstance(d, list) else []):
        if r.get("symbol") == base + "USDT":
            return {"cap": r.get("cap"), "floor": r.get("floor"),
                    "fundingIntervalHours": r.get("fundingIntervalHours")}
    return {"default": "no adjustment row (0.05%/interval default)"}


async def _params_backpack(base):
    d, _ = await vc._get("https://api.backpack.exchange/api/v1/markets")
    for r in (d if isinstance(d, list) else []):
        if r.get("symbol") == base + "_USDC_PERP":
            return {"fundingIntervalMs": r.get("fundingInterval"),
                    "lowerBound": r.get("fundingRateLowerBound"),
                    "upperBound": r.get("fundingRateUpperBound"),
                    "oiLimit": r.get("openInterestLimit")}
    return None


async def _params_dydx(base):
    d, _ = await vc._get("https://indexer.dydx.trade/v4/perpetualMarkets",
                         params={"ticker": base + "-USD"})
    m = (d.get("markets") or {}).get(base + "-USD") if isinstance(d, dict) \
        else None
    return {"defaultFundingRate1H": m.get("defaultFundingRate1H")} if m \
        else None


async def _params_hl(base):
    async with vc._session.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "metaAndAssetCtxs"},
            timeout=aiohttp.ClientTimeout(total=20)) as r:
        meta = await r.json(content_type=None)
    uni, ctxs = meta
    for i, a in enumerate(uni["universe"]):
        if a["name"] == base:
            return {"ctx_funding_record_only": ctxs[i].get("funding")}
    return None


PARAM_FETCHERS = {"aster": _params_aster, "binance": _params_binance,
                  "backpack": _params_backpack, "dydx": _params_dydx,
                  "hl": _params_hl}


async def tw5_floor(con, coin, sv, lv, held=False):
    out, changed, fails, errors = {}, [], 0, 0
    for v in {sv, lv}:
        fn = PARAM_FETCHERS.get(v)
        if fn is None:
            out[v] = {"note": "no native funding-param endpoint (rate/interval"
                      " watched via TW2)"}
            continue
        try:
            p = await fn(coin)
        except Exception as e:
            out[v] = {"error": str(e)[:120]}
            errors += 1
            continue
        if p is None:
            out[v] = {"note": "symbol not found in param endpoint"}
            continue
        key = f"floor_params_{v}_{coin}"
        base = st.kv_get(con, key)
        if base is None:
            st.kv_set(con, key, json.dumps(p))
            out[v] = {"params": p, "baseline_set": True}
        else:
            old = json.loads(base)
            if old != p:
                changed.append({"venue": v, "old": old, "new": p})
                st.kv_set(con, key, json.dumps(p))
                out[v] = {"params": p, "changed_from": old}
                fails += 1
            else:
                out[v] = {"params": p, "unchanged": True}
    if fails:
        for v in {sv, lv}:
            st.kv_set(con, f"venue_freeze_{v}",
                      f"frozen {nowiso()}: funding-param change on {coin}")
        for c in changed:
            # fix4: exec-side TW5 emits FLOOR_PARAM_CHANGE so kill source #1
            # sees held-probe catches too (M4 watcher tags path:m4-floor-watch)
            st.ev(con, "FLOOR_PARAM_CHANGE", coin, c["venue"], json.dumps(
                {"old": c["old"], "new": c["new"], "path": "exec-side"}))
    ok = fails == 0 and errors == 0
    return ok, {"venues": out, "changes": changed, "held_legs": held,
                "probe_outage": errors > 0,
                "note": ("param change -> venue freeze; probe outage -> "
                         "unverifiable, treated as unsafe")
                if (fails or errors) else "all params unchanged"}


# --------------------------------------------------------- TW6 counterparty
def _venue_exposure(venue):
    con = sqlite3.connect(PAPER_DB)
    try:
        rows = con.execute(
            "SELECT size_usd FROM positions WHERE status='open' AND "
            "(short_venue=? OR long_venue=?)", (venue, venue)).fetchall()
    finally:
        con.close()
    return sum(float(r[0]) for r in rows)


async def tw6_counterparty(coin, sv, lv, size_usd):
    out = {}
    ok = True
    for v in {sv, lv}:
        cap = CAPS.get(v, DEFAULT_CAP)
        cur = _venue_exposure(v)
        proj = cur + size_usd
        out[v] = {"cap_usd": cap, "exposure_now_usd": cur,
                  "projected_usd": proj, "tier": 2 if v in CAPS else 1}
        ok = ok and proj <= cap
    return ok, out


# ------------------------------------------------------------------ runner
TW_NAMES = ["TW1_identity", "TW2_persistence", "TW3_live_spread",
            "TW4_depth_rewalk", "TW5_floor_monitor", "TW6_counterparty"]


async def run_chain(con, coin, sv, lv, size_usd, mode="pre_entry",
                    verbose=True):
    """Full chain in order; returns (verdict, {tw: (result, measured)})."""
    await vc.init()
    res = {}

    def log(tw, result, measured):
        con.execute("INSERT INTO tripwire_log(ts,coin,short_venue,"
                    "long_venue,size_usd,mode,tw,result,measured) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (nowiso(), coin, sv, lv, size_usd, mode, tw, result,
                     json.dumps(measured, default=str)[:1400]))
        con.commit()
        if verbose:
            r = str(result).lower()
            tag = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}.get(r,
                                                                       r.upper())
            print(f"  [{tag}] {tw}: "
                  f"{json.dumps(measured, default=str)[:110]}")

    aborted = False
    for i, name in enumerate(TW_NAMES):
        if aborted:
            res[name] = ("skip", {"note": "chain aborted at TW1"})
            log(name, "skip", res[name][1])
            continue
        try:
            if name == "TW1_identity":
                ok, meas = await tw1_identity(coin, sv, lv)
            elif name == "TW2_persistence":
                ok, meas = await tw2_persistence(con, coin, sv, lv)
            elif name == "TW3_live_spread":
                ok, meas = await tw3_live_spread(con, coin, sv, lv)
            elif name == "TW4_depth_rewalk":
                ok, meas = await tw4_depth(con, coin, sv, lv, size_usd)
            elif name == "TW5_floor_monitor":
                ok, meas = await tw5_floor(con, coin, sv, lv,
                                           held=(mode == "held_check"))
            else:
                ok, meas = await tw6_counterparty(coin, sv, lv, size_usd)
            res[name] = ("pass" if ok else "fail", meas)
            log(name, res[name][0], meas)
            if name == "TW1_identity" and not ok:
                aborted = True   # hard stop: identity is non-negotiable
        except Exception as e:
            res[name] = ("fail", {"error": repr(e)[:200]})
            log(name, "fail", res[name][1])
    await vc.close()

    hard_fail = res["TW1_identity"][0] == "fail"
    verdict = "FAIL" if any(r == "fail" for r, _ in res.values()) else "PASS"
    if hard_fail:
        verdict = "FAIL_HARD"
    log("CHAIN", verdict, {"tripwires": {k: v for k, (v, _) in
                                         res.items()},
                           "mode": mode})
    return verdict, res


# ---------------------------------------------------------------- backfill
def backfill_books(con):
    n = 0
    with open(L2_CSV) as fh:
        for r in csv.DictReader(fh):
            if r.get("ok") != "1":
                continue
            ts = datetime.fromtimestamp(int(r["ts"]), timezone.utc) \
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                con.execute(
                    "INSERT OR IGNORE INTO book_samples(ts,window,venue,"
                    "base,pass_n,mid,spread_bps,ok,slip_sell_500,slip_sell_1000,"
                    "slip_sell_2500,slip_sell_5000,slip_sell_10000,slip_sell_25000,"
                    "slip_buy_500,slip_buy_1000,slip_buy_2500,slip_buy_5000,"
                    "slip_buy_10000,slip_buy_25000,depth25_bid,depth25_ask,"
                    "source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                    "?,?,?,?,?)",
                    (ts, r["window"], r["venue"], r["base"], int(r["pass"]),
                     float(r["mid"]), float(r["spread_bps"]), 1,
                     *[float(r[k]) if r[k] else None
                       for k in ("slip_bid_500", "slip_bid_1000",
                                 "slip_bid_2500", "slip_bid_5000",
                                 "slip_bid_10000", "slip_bid_25000")],
                     *[float(r[k]) if r[k] else None
                       for k in ("slip_ask_500", "slip_ask_1000",
                                 "slip_ask_2500", "slip_ask_5000",
                                 "slip_ask_10000", "slip_ask_25000")],
                     float(r["depth_bid_25"]) if r["depth_bid_25"] else None,
                     float(r["depth_ask_25"]) if r["depth_ask_25"] else None,
                     "l2_csv"))
                n += 1
            except (KeyError, ValueError):
                continue
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM book_samples").fetchone()[0]
    print(f"backfill: +{n} rows -> book_samples total {total}")


# --------------------------------------------------------------------- CLI
async def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    con = st.connect()
    if cmd == "backfill-books":
        backfill_books(con)
    elif cmd == "all":
        for coin, sv, lv, size in BASKET:
            print(f"\n== {coin} {sv}->{lv} ${size:.0f}/leg (pre_entry)")
            v, _ = await run_chain(con, coin, sv, lv, size, "pre_entry")
            print(f"  CHAIN VERDICT: {v}")
    elif cmd == "held":
        pdb = sqlite3.connect(PAPER_DB)
        pdb.row_factory = sqlite3.Row
        open_pos = pdb.execute(
            "SELECT * FROM positions WHERE status='open'").fetchall()
        pdb.close()
        for p in open_pos:
            print(f"\n== pos #{p['pos_id']} {p['coin']} "
                  f"{p['short_venue']}->{p['long_venue']} "
                  f"${p['size_usd']:.0f}/leg (held_check)")
            v, _ = await run_chain(con, p["coin"], p["short_venue"],
                                   p["long_venue"], p["size_usd"],
                                   "held_check")
            print(f"  CHAIN VERDICT: {v}")
    elif cmd == "one":
        coin, sv, lv, size = sys.argv[2], sys.argv[3], sys.argv[4], \
            float(sys.argv[5])
        v, _ = await run_chain(con, coin, sv, lv, size, "pre_entry")
        print(f"CHAIN VERDICT: {v}")
    else:
        print(__doc__)
    con.close()


if __name__ == "__main__":
    asyncio.run(main())
