#!/usr/bin/env python3
"""P1 nightly persistence scanner (spec Part 12 §3.1 M1).

Recomputes Part-4/Part-10 distributions FROM THE DB (acceptance: scanner
output reproduces those numbers). Two faithful models by source:

  native_hist  -> event/zero-fill model (Part 4): per settlement hour,
                  hourly carry = rate/interval_h; hours without settlement
                  contribute 0; pair spread on the UNION of settlement
                  hours inside the common coverage window.
  sharpe_hist  -> ffill model (Part 10): per-hour carry = rate/interval_h
                  ffilled across the union-hour grid; spread APR = diff
                  x 8760.

Modes:
  validate   reproduce published Part-4 + Part-10 rows from the DB and
             report deltas            -> p1_scanner_validation.csv
  capture    pull LIVE funding (native + Sharpe) for the basket coins into
             funding_obs (source='native_live'/'sharpe_live') - the
             longitudinal dataset starts here.
  pairs      recompute the basket pair metrics from the DB and print
"""
import asyncio
import csv
import json
import statistics as stx
import sys
from datetime import datetime, timezone

import v3_store as st
import v3_connectors as vc

OUT = "/home/z/my-project/download/data/p1_scanner_validation.csv"
P4_CSV = "/home/z/my-project/download/data/funding_persistence_90d_v3.csv"
P10_CSV = "/home/z/my-project/download/data/sharpe_newvenue_pairs.csv"
BASKET = [("XMR", "dydx", "binance"), ("BTW", "aster", "bitget"),
          ("UAI", "aster", "binance"), ("INJ", "bingx", "okx"),
          ("LINK", "nado", "okx")]


# ------------------------------------------------------------ models
def pair_metrics_native_iv(ev_s, ev_l, iv_s, iv_l):
    """ev_*: {hour: rate_per_event}; iv_*: interval hours per leg."""
    if not ev_s or not ev_l:
        return None
    lo = max(min(ev_s), min(ev_l))
    hi = min(max(ev_s), max(ev_l))
    if hi <= lo or (hi - lo) < 48:
        return None
    daily = {}
    cum = 0.0
    for h in range(lo, hi + 1):
        d = ev_s.get(h, 0.0) / iv_s - ev_l.get(h, 0.0) / iv_l
        cum += d
        daily[h // 24] = daily.get(h // 24, 0.0) + d
    dv = [daily[k] for k in sorted(daily)]
    n = len(dv)
    if n < 3:
        return None
    overlap_days = (hi - lo + 1) / 24.0

    def apr_last(k):
        cut = dv[-k:] if n >= k else dv
        return sum(cut) / len(cut) * 365.0

    return {"apr_30d": apr_last(30), "apr_90d": cum / overlap_days * 365.0,
            "pos_day_frac": sum(1 for v in dv if v > 0) / n,
            "days": n, "model": "native_event"}


def pair_metrics_ffill(pts_s, pts_l):
    """Part-10 model. pts_*: [(ts_ms, rate, interval_h)]."""
    def hourly(pts):
        h = {}
        for t, r, iv in pts:
            if not iv or iv <= 0:
                continue
            h[int(t // 3600_000)] = r / float(iv)
        return h
    hs, hl_ = hourly(pts_s), hourly(pts_l)
    if not hs or not hl_:
        return None
    lo = max(min(hs), min(hl_))
    hi = min(max(hs), max(hl_))
    if hi <= lo:
        return None
    spread = []
    cs = cl = None
    for h in range(lo, hi + 1):
        if h in hs:
            cs = hs[h]
        if h in hl_:
            cl = hl_[h]
        if cs is None or cl is None:
            continue
        spread.append((cs - cl) * 8760.0 * 100.0)
    if len(spread) < 48:
        return None
    med = stx.median(spread)
    pos_days = sum(1 for s in spread if s > 0) / len(spread)
    return {"apr30_spread": med / 100.0,
            "pos_day_frac": pos_days, "model": "sharpe_ffill"}


# ------------------------------------------------------------- helpers
def event_index(con, source, asof_ms=None):
    """{(base, venue): ({hour: rate}, interval_h)} for the event model."""
    q = ("SELECT base, venue, ts, rate, interval_h FROM funding_obs "
         "WHERE source=?")
    args = [source]
    if asof_ms:
        q += " AND ts<=?"
        args.append(asof_ms)
    idx = {}
    iv_acc = {}
    for b, v, t, r, iv in con.execute(q + " ORDER BY ts", args):
        k = (b, v)
        idx.setdefault(k, {})[int(t // 3600_000)] = float(r)
        iv_acc.setdefault(k, []).append(float(iv or 8.0))
    return {k: (idx[k], stx.median(iv_acc[k])) for k in idx}


def ffill_index(con, source, asof_ms=None):
    """{(base, venue): [(ts, rate, ivl_row)]} for the ffill model.

    Interval semantics by data provenance (faithful to the originals):
      - API-metadata regime: rows with interval_h>0 (fresh Sharpe fetches,
        the Part-10 published table's regime) use the stored metadata.
      - Reconstruction regime: rows with interval_h=0 (checkpoint cache,
        which dropped metadata) use Part-10 cache_series per-row deltas
        (hours until next row; last row from previous).
    """
    q = ("SELECT base, venue, ts, rate, interval_h FROM funding_obs "
         "WHERE source=?")
    args = [source]
    if asof_ms:
        q += " AND ts<=?"
        args.append(asof_ms)
    raw = {}
    for b, v, t, r, iv in con.execute(q + " ORDER BY ts", args):
        raw.setdefault((b, v), []).append((int(t), float(r),
                                           float(iv or 0.0)))
    out = {}
    for k, pts in raw.items():
        if len(pts) < 2:
            continue
        rows = []
        for i, (t, r, iv) in enumerate(pts):
            if iv > 0:
                rows.append((t, r, iv))
                continue
            if i + 1 < len(pts):
                ivl = max(1, round((pts[i + 1][0] - t) / 3600000.0))
            else:
                ivl = max(1, round((t - pts[i - 1][0]) / 3600000.0))
            rows.append((t, r, ivl))
        out[k] = rows
    return out


# ------------------------------------------------------------ validate
def validate_native(con, cap=300):
    ref = list(csv.DictReader(open(P4_CSV)))
    ev = event_index(con, "native_hist")
    n_done = 0
    rows = []
    for r in ref:
        k = (r["coin"], r["short_venue"].lower(), r["long_venue"].lower())
        ks = (k[0], k[1])
        kl = (k[0], k[2])
        if ks not in ev or kl not in ev:
            continue
        ev_s, iv_s = ev[ks]
        ev_l, iv_l = ev[kl]
        m = pair_metrics_native_iv(ev_s, ev_l, iv_s, iv_l)
        if not m:
            continue
        rows.append({"family": "part4_native", "coin": k[0],
                     "short_venue": k[1], "long_venue": k[2],
                     "ref_apr30": float(r["apr_30d"]),
                     "db_apr30": m["apr_30d"],
                     "d_apr30_pp": (m["apr_30d"] - float(r["apr_30d"])) * 100,
                     "ref_pos": float(r["pos_day_frac"]),
                     "db_pos": m["pos_day_frac"],
                     "d_pos_pp": (m["pos_day_frac"] - float(r["pos_day_frac"])) * 100})
        n_done += 1
        if n_done >= cap:
            break
    return rows


def validate_sharpe(con, cap=400):
    """Part-10 leg-level reproduction (same-data comparison).

    The published pairs CSV came from a live Sharpe fetch in that session;
    the checkpoint cache holds a different pull, so PAIR medians carry
    data-pull noise. The CSV's per-leg columns (short/long_leg_median_apr)
    ARE same-data -> compare the scanner's per-row-interval leg medians
    against them. Model fidelity test, not data-drift test.
    """
    ff = ffill_index(con, "sharpe_hist")
    rows = []
    for (coin, v), pts in ff.items():
        if len(pts) < 20:
            continue
        aps = [r / ivl * 8760.0 * 100.0 for t, r, ivl in pts]
        mine = stx.median(aps) / 100.0  # fraction APR
        # ref rows where this venue is the SHORT leg
        for side, col in (("short", "short_leg_median_apr"),
                          ("long", "long_leg_median_apr")):
            for r in REF_BY_LEG.get((coin, v, side), []):
                try:
                    refv = float(r[col])
                except (TypeError, ValueError, KeyError):
                    continue
                if refv == 0 and abs(mine * 100) > 1:
                    pass  # ref 0.0 with blank semantics - keep, delta huge
                rows.append({
                    "family": f"part10_leg_{side}", "coin": coin,
                    "short_venue": v if side == "short" else "",
                    "long_venue": v if side == "long" else "",
                    "ref_apr30": refv / 100.0,
                    "db_apr30": mine,
                    "d_apr30_pp": (mine - refv / 100.0) * 100,
                    "ref_pos": None, "db_pos": None, "d_pos_pp": None})
                if len(rows) >= cap:
                    return rows
    return rows


REF_BY_LEG = {}


def load_ref_legs():
    import csv as _csv
    for r in _csv.DictReader(open(P10_CSV)):
        coin = r["coin"]
        sv = r["short_venue"].lower().replace(".", "_")
        lv = r["long_venue"].lower().replace(".", "_")
        REF_BY_LEG.setdefault((coin, sv, "short"), []).append(r)
        REF_BY_LEG.setdefault((coin, lv, "long"), []).append(r)


def summarize(rows, label):
    if not rows:
        print(f"{label}: NO REPRODUCIBLE ROWS")
        return 0
    d30 = [abs(r["d_apr30_pp"]) for r in rows if r["d_apr30_pp"] is not None]
    ok2 = sum(1 for d in d30 if d <= 2.0)
    ok10 = sum(1 for d in d30 if d <= max(0.10 * 100, 0))
    rel = []
    for r in rows:
        if r["ref_apr30"]:
            rel.append(abs(r["d_apr30_pp"]) / max(abs(r["ref_apr30"] * 100), 1e-9) * 100)
    print(f"{label}: {len(rows)} pairs reproduced")
    print(f"  |d apr30| median {stx.median(d30):.2f}pp  p90 "
          f"{sorted(d30)[int(len(d30)*0.9)]:.2f}pp  <=2pp: {ok2}/{len(d30)} "
          f"({ok2/len(d30)*100:.0f}%)  <=10% rel: "
          f"{sum(1 for x in rel if x <= 10)}/{len(rel)}")
    return len(rows)


# -------------------------------------------------------------- capture
async def capture(con, coins):
    """Live funding pull -> funding_obs (longitudinal dataset starts)."""
    await vc.init()
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    n = 0
    sh_rows = []
    for coin in coins:
        # native
        for v in ["binance", "okx", "bybit", "bitget", "aster", "hl",
                  "dydx", "bingx", "backpack"]:
            try:
                f = await vc.get_funding(v, coin)
                if f["source"] != "native":
                    continue
                st.insert_funding(con, [(v, coin, now, f["rate"],
                                         f["interval_h"], "native_live")])
                n += 1
            except Exception as e:
                print(f"  capture {v} {coin}: {str(e)[:60]}")
        # sharpe current book for the coin
        try:
            for r in await vc.Sharpe.current(coin):
                if r["coin"] != coin:
                    continue
                sh_rows.append((r["venue"], coin, now, r["rate"],
                                r["interval_h"], "sharpe_live"))
        except Exception as e:
            print(f"  capture sharpe {coin}: {str(e)[:60]}")
    st.insert_funding(con, sh_rows)
    n += len(sh_rows)
    print(f"captured {n} live obs at {now} "
          f"(native {n - len(sh_rows)}, sharpe {len(sh_rows)})")
    await vc.close()


# -------------------------------------------------------------- discover
WATCHLIST = "/home/z/my-project/download/data/p1_watchlist.csv"
RWA_CSV = "/home/z/my-project/download/data/rwa_universe.csv"
WATCH_MIN_NET = 0.10        # 10% netApr fraction floor for watchlist
WATCH_MIN_DEPTH = 5000.0    # executable depth USD floor (OI fallback)
OUR_VENUES = {"binance", "okx", "bybit", "bitget", "hl", "aster", "dydx",
              "bingx", "backpack", "nado", "orderly",
              # P5d Wave-0 (Part 24): native funding/books shipped; bitmex/htx
              # funding-only (bookless combos refused by resolve, by design)
              "gate", "kucoin", "bitmex", "deribit", "htx", "paradex",
              "gate_io"}   # gate_io = Sharpe-normalized name for Gate.io


def _known_coins(con):
    """Our candidate universe: basket + research CSVs + prior candidates."""
    known = {c for c, _, _ in BASKET} | {"BTC", "ETH"}
    for (c,) in con.execute("SELECT DISTINCT coin FROM candidates"):
        if c:
            known.add(c)
    for path in (P4_CSV, P10_CSV):
        try:
            for r in csv.DictReader(open(path)):
                c = (r.get("coin") or "").upper()
                if c:
                    known.add(c)
        except FileNotFoundError:
            pass
    return known


async def discover(con):
    """Spec §3.1 Sharpe discovery poll (arb daily / settlement daily /
    RWA weekly): diff arb table vs candidate set, new names -> watchlist
    (never straight to basket), store all rows into candidates."""
    await vc.init()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    src = "sharpe_arb_" + now[:10].replace("-", "")

    # 1) arb table -> candidates (same-day stamp replace = idempotent)
    rows, asof, stale = await vc.Sharpe.arb_table()
    con.execute("DELETE FROM candidates WHERE source=?", (src,))
    known = _known_coins(con)
    best = {}   # coin -> row for watchlist aggregation
    n = 0
    for r in rows:
        try:
            coin = str(r.get("symbol", "")).upper()
            sv = str(r.get("shortExchange", "")).lower().replace(".", "_")
            lv = str(r.get("longExchange", "")).lower().replace(".", "_")
            if not coin or not sv or not lv:
                continue
            net = float(r.get("netApr") or 0.0)          # fraction
            con.execute(
                "INSERT INTO candidates(ts,coin,short_venue,long_venue,"
                "apr30_spread,pos_day_frac,net30,status,source,detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now, coin, sv, lv,
                 float(r.get("spreadRate") or 0) / 100.0, None, net,
                 "discovery", src,
                 json.dumps({"apr": float(r.get("apr") or 0),
                             "hold_d": float(r.get("holdingDays") or 0),
                             "depth_usd": float(r.get("executableDepthUsd") or 0),
                             "exec": r.get("executionStatus"),
                             "oi_short": r.get("oiShort"),
                             "oi_long": r.get("oiLong")})))
            n += 1
            if coin not in known:
                d = float(r.get("executableDepthUsd") or 0)
                ois = float(r.get("oiShort") or 0)
                if net >= WATCH_MIN_NET and (d >= WATCH_MIN_DEPTH
                                             or ois >= WATCH_MIN_DEPTH):
                    uni = (sv in OUR_VENUES) + (lv in OUR_VENUES)
                    cur = best.get(coin)
                    if cur is None or (uni, net) > (cur[7], cur[0]):
                        best[coin] = (net, sv, lv, d, r.get("executionStatus"),
                                      float(r.get("holdingDays") or 0), ois,
                                      uni)
        except (TypeError, ValueError):
            continue
    con.commit()
    print(f"arb table: {len(rows)} rows -> {n} candidates stored ({src}) "
          f"[asof {asof}, stale {stale}]")

    # 2) watchlist: new names passing the pre-filters, aggregated per coin
    #    (executableDepthUsd is sparse in the free tier -> OI fallback;
    #     universe_legs = how many legs sit on our 11 execution venues)
    wl = sorted(best.items(), key=lambda kv: (-kv[1][7], -kv[1][0]))
    with open(WATCHLIST, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["coin", "short_venue", "long_venue", "net_apr",
                    "exec_depth_usd", "exec_status", "hold_days",
                    "oi_short_usd", "universe_legs", "ts"])
        for coin, (net, sv, lv, d, exs, hd, ois, uni) in wl:
            w.writerow([coin, sv, lv, f"{net * 100:.1f}", f"{d:.0f}",
                        exs, f"{hd:.1f}", f"{ois:.0f}", uni, now])
    nin = sum(1 for _, v in wl if v[7] > 0)
    print(f"watchlist: {len(wl)} NEW coins net>={WATCH_MIN_NET * 100:.0f}% "
          f"+ depth/OI>=${WATCH_MIN_DEPTH:.0f} ({nin} with >=1 leg on our "
          f"venues) -> {WATCHLIST}")
    for coin, (net, sv, lv, d, exs, hd, ois, uni) in wl[:15]:
        print(f"  NEW {coin:14s} {sv}->{lv}  net {net * 100:6.1f}%  "
              f"OI ${ois:10.0f}  legs-ours {uni}  exec {exs}")

    # 3) settlement: dollar-weighted ranking (kv refresh)
    try:
        srows, sasof, _ = await vc.Sharpe.settlement()
        def net_now(r):
            return float((r["windows"].get("current") or {}).get("net") or 0)
        top = sorted((r for r in srows if net_now(r) > 0),
                     key=net_now, reverse=True)[:15]
        st.kv_set(con, "settlement_top_" + now[:10],
                  json.dumps([{"coin": r["coin"],
                               "net_usd": round(net_now(r), 0),
                               "oi_usd": round(r["oi_usd"], 0)}
                              for r in top]))
        print(f"settlement: {len(srows)} coins, top by current net USD "
              f"[asof {sasof}]:")
        for r in top[:10]:
            print(f"  {r['coin']:10s} net ${net_now(r):12,.0f}  "
                  f"OI ${r['oi_usd']:14,.0f}")
    except Exception as e:
        print(f"settlement poll failed: {str(e)[:80]}")

    # 4) RWA universe sync (weekly job, run here on demand)
    try:
        rrows, rasof, _ = await vc.Sharpe.rwa()
        with open(RWA_CSV, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["venue", "symbol", "market",
                                               "asset_class", "mechanism",
                                               "rate", "interval_h", "apr",
                                               "oi_usd", "stale", "suspect"])
            w.writeheader()
            w.writerows(rrows)
        vs = sorted({r["venue"] for r in rrows})
        st.kv_set(con, "rwa_census_" + now[:10],
                  f"venues={len(vs)} rows={len(rrows)}")
        print(f"rwa: {len(rrows)} rows across {len(vs)} venues "
              f"-> {RWA_CSV} [asof {rasof}]")
    except Exception as e:
        print(f"rwa poll failed: {str(e)[:80]}")

    st.kv_set(con, "last_discover_run", now)
    await vc.close()


# ----------------------------------------------------------------- pairs
def pairs_from_db(con):
    ev = event_index(con, "native_hist")
    ff = ffill_index(con, "sharpe_hist")
    print(f"{'pair':24s} {'model':14s} {'apr30':>7s} {'pos':>6s}")
    for coin, sv, lv in BASKET:
        ks, kl = (coin, sv), (coin, lv)
        m = None
        if ks in ev and kl in ev:
            ev_s, iv_s = ev[ks]
            ev_l, iv_l = ev[kl]
            m = pair_metrics_native_iv(ev_s, ev_l, iv_s, iv_l)
        if m is None and ks in ff and kl in ff:
            m = pair_metrics_ffill(ff[ks], ff[kl])
        if m:
            apr = m.get("apr_30d", m.get("apr30_spread", 0))
            print(f"{coin+' '+sv+'->'+lv:24s} {m['model']:14s} "
                  f"{apr*100:6.1f}% {m['pos_day_frac']*100:5.0f}%")
        else:
            print(f"{coin+' '+sv+'->'+lv:24s} (no data overlap in DB)")


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "pairs"
    con = st.connect()
    if mode == "validate":
        load_ref_legs()
        rows = validate_native(con) + validate_sharpe(con)
        summarize([r for r in rows if r["family"] == "part4_native"],
                  "Part-4 native reproduction")
        summarize([r for r in rows if r["family"].startswith("part10")],
                  "Part-10 sharpe reproduction")
        if rows:
            with open(OUT, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            print(f"-> {OUT}")
    elif mode == "capture":
        coins = sorted({c for c, _, _ in BASKET} | {"BTC", "ETH"})
        await capture(con, coins)
    elif mode == "pairs":
        pairs_from_db(con)
    elif mode == "discover":
        await discover(con)
    con.close()


if __name__ == "__main__":
    asyncio.run(main())
