#!/usr/bin/env python3
"""P1 backfill: load the research caches into v3.db funding_obs.

Sources:
  funding_history_cache_v3.json  (Part 4, 7 native venues, ~90d, 493k obs)
     -> source='native_hist'   [event/zero-fill model applies]
  sharpe_floorscan_cache_v2.json (Part 10, ~25 venues via Sharpe, 1.07M obs)
     -> source='sharpe_hist'   [ffill per-hour-carry model applies]

interval_h is inferred per series with the Part-4 median-delta rule and
stored per row. Venue names normalized to lowercase canonical.
"""
import asyncio
import json
import os
import sys
import time

import v3_store as st
import v3_connectors as vc

P4 = "/home/z/my-project/scripts/funding_history_cache_v3.json"
P10 = "/home/z/my-project/scripts/sharpe_floorscan_cache_v2.json"


def infer_iv(events):
    """Part-4 rule: median of deltas in (0,48h]; default 8h if <3 events."""
    if len(events) < 3:
        return 8.0
    ds_ = sorted((events[i + 1][0] - events[i][0]) / 3600_000
                 for i in range(len(events) - 1))
    ds_ = [d for d in ds_ if 0 < d <= 48]
    return ds_[len(ds_) // 2] if ds_ else 8.0


def norm_venue(v):
    v = v.lower()
    return v.replace(".", "_").replace("-", "_").replace(" ", "_")


def main_load():
    con = st.connect()

    c4 = json.load(open(P4))
    rows = []
    for coin, venues in c4.items():
        for v, ev in venues.items():
            if not ev:
                continue
            iv = infer_iv(ev)
            for t, r in ev:
                rows.append((v, coin, int(t), float(r), iv, "native_hist"))
    before = con.execute("SELECT COUNT(*) FROM funding_obs").fetchone()[0]
    st.insert_funding(con, rows)
    after = con.execute("SELECT COUNT(*) FROM funding_obs").fetchone()[0]
    print(f"native_hist (Part-4 cache): {len(rows):,} rows offered, "
          f"{after - before:,} inserted (dedup skipped {len(rows)-(after-before):,})")

    p10 = os.path.exists(P10)
    if not p10:
        print(f"sharpe_hist: {P10} missing -> skip layer "
              f"(rebuild via: python3 v3_backfill.py fetch-coins <COINS>)")
    if p10:
        c10 = json.load(open(P10))
        rows = []
        for coin, venues in c10.items():
            for v, ev in venues.items():
                if not ev:
                    continue
                vv = norm_venue(v)
                # checkpoint cache stored only [ts, rate]: interval metadata
                # was dropped by the original cache writer -> stamp 0 (unknown)
                # and let the scanner reconstruct per-row intervals.
                for t, r in ev:
                    rows.append((vv, coin, int(t), float(r), 0.0, "sharpe_hist"))
        before = after
        st.insert_funding(con, rows)
        after = con.execute("SELECT COUNT(*) FROM funding_obs").fetchone()[0]
        print(f"sharpe_hist (Part-10 cache): {len(rows):,} rows offered, "
              f"{after - before:,} inserted (dedup skipped {len(rows)-(after-before):,})")

    print("\nDB summary:")
    for r in con.execute("SELECT source, COUNT(*), COUNT(DISTINCT venue||'|'||base) "
                         "FROM funding_obs GROUP BY source"):
        print(f"  {r[0]:12s} {r[1]:>9,} obs  {r[2]:>5} series")
    st.kv_set(con, "backfilled_at", __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ"))
    con.close()


async def restamp():
    """Re-stamp sharpe_hist intervals from the Sharpe CURRENT book.

    The Part-10 analyzer used Sharpe's interval metadata per (venue, coin)
    at scan time; delta inference from sparse history rows mis-annualizes
    (e.g. OKX rows are 8h cadence but ~14h apart on average). Intervals are
    venue properties -> stamp them from the live book, keep the inferred
    value only where the venue is absent from the current book.
    """
    con = st.connect()
    await vc.init()
    # ONE full-book fetch instead of per-coin calls (Part-9: 8.5k rows)
    book = await vc.Sharpe.current()
    ivmap = {(r["venue"], r["coin"]): r["interval_h"] for r in book}
    print(f"sharpe current book: {len(book)} rows, {len(ivmap)} venue/coin keys")
    changed = same = missing = 0
    pairs = con.execute(
        "SELECT DISTINCT venue, base FROM funding_obs "
        "WHERE source='sharpe_hist'").fetchall()
    for v, c in pairs:
        iv = ivmap.get((v, c))
        if iv is None or iv <= 0:
            missing += 1
            continue
        cur = con.execute(
            "SELECT interval_h, COUNT(*) FROM funding_obs "
            "WHERE source='sharpe_hist' AND venue=? AND base=?",
            (v, c)).fetchone()
        if cur[0] and abs(cur[0] - iv) < 1e-9:
            same += cur[1]
            continue
        con.execute(
            "UPDATE funding_obs SET interval_h=? WHERE "
            "source='sharpe_hist' AND venue=? AND base=?",
            (iv, v, c))
        changed += cur[1]
    con.commit()
    print(f"restamp: {changed:,} rows re-stamped, {same:,} already correct, "
          f"{missing} series without current-book metadata (kept inferred)")
    st.kv_set(con, "restamped_at", time.strftime("%Y-%m-%dT%H:%M:%SZ"))
    con.close()
    await vc.close()


async def fetch_sharpe_coins(coins):
    """Two-slice Sharpe history fetch (Part-10 strategy: free API ignores
    exchange/offset params; only coin/days/limit work; 5000-row cap) ->
    source='sharpe_hist'. Used for basket coins missing from the checkpoint
    cache (INJ/LINK/etc.) and for periodic DB refresh."""
    con = st.connect()
    await vc.init()
    total = 0
    for c in coins:
        try:
            hist, asof, stale = await vc.Sharpe.history(c, days=60)
        except Exception as e:
            print(f"  {c}: history ERR {str(e)[:70]}")
            continue
        n_rows = sum(len(v) for v in hist.values())
        if n_rows >= 5000:      # capped, oldest-first -> add recent slice
            try:
                hist2, _, _ = await vc.Sharpe.history(c, days=8)
                for v, pts in hist2.items():
                    hist.setdefault(v, []).extend(pts)
                print(f"  {c}: 60d slice capped -> +8d slice "
                      f"({sum(len(v) for v in hist2.values())} rows)")
            except Exception as e:
                print(f"  {c}: 8d slice ERR {str(e)[:60]}")
        rows = []
        for v, pts in hist.items():
            vv = norm_venue(v)
            pts.sort(key=lambda x: x[0])   # merge slices in time order
            for i, (t, r, iv_meta) in enumerate(pts):
                ivl = iv_meta
                if not ivl or ivl <= 0:
                    ivl = max(1, round((pts[i + 1][0] - t) / 3600000.0)
                              ) if i + 1 < len(pts) else 1
                rows.append((vv, c, int(t), float(r), float(ivl),
                             "sharpe_hist"))
        before = con.execute("SELECT COUNT(*) FROM funding_obs").fetchone()[0]
        st.insert_funding(con, rows)
        after = con.execute("SELECT COUNT(*) FROM funding_obs").fetchone()[0]
        n = after - before
        total += n
        venues = len(hist)
        print(f"  {c}: {n:,} new rows across {venues} venues "
              f"(asof={asof}, stale={stale})")
    print(f"total inserted: {total:,}")
    st.kv_set(con, "sharpe_fetch_at", time.strftime("%Y-%m-%dT%H:%M:%SZ"))
    con.close()
    await vc.close()


async def main():
    if len(sys.argv) > 1 and sys.argv[1] == "restamp":
        await restamp()
    elif len(sys.argv) > 2 and sys.argv[1] == "fetch-coins":
        await fetch_sharpe_coins([c.upper() for c in sys.argv[2:]])
    else:
        main_load()


if __name__ == "__main__":
    asyncio.run(main())
