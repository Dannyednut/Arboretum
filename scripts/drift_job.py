#!/usr/bin/env python3
"""P0 nightly drift job (spec Part 12 §3.3 + §5 P0 acceptance).

For every OPEN paper position:
  1. Accrual row: incremental funding carry since the last accrual row,
     using live Sharpe funding (interval-normalized APR, per §2.3 rule).
  2. Drift row: simulated EXIT cost right now (walk-the-book both legs,
     taker exit fees per the gate's maker-variant convention) + recorded
     entry cost  ->  realized RT  vs  assumed RT from the gate tables.
       drift_ratio = realized_RT / assumed_RT
  3. Kill-switch flag if any ratio today > 1.5x (spec §3.5 M5).

Acceptance (P0 exit criterion): realized RT <= 1.3x assumed on >=80% of
drift samples.

Storage: drift_log table in paper_v3.db + CSV mirror paper_drift.csv.
Run:  python3 drift_job.py            # accrual + drift for open positions
      python3 drift_job.py --accrual-only
"""
import argparse
import asyncio
import csv
import os
import sqlite3
import sys
from datetime import datetime, timezone

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paper_ledger as pl          # noqa: E402  (books, walk, fees, db)
import depth_sampler as ds         # noqa: E402
import depth_newvenue as nv        # noqa: E402

DRIFT_CSV = "/home/z/my-project/download/data/paper_drift.csv"
GATES = [
    "/home/z/my-project/download/data/depth_pair_gates.csv",
    "/home/z/my-project/download/data/newvenue_pair_gates.csv",
]
MAKER_COL = ("rt_cost_1000_maker_pct", "rt_cost_1k_maker_pct")

_session: aiohttp.ClientSession | None = None


async def _get(url, params=None, timeout=20):
    async with _session.get(url, params=params,
                            timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.json(content_type=None)


# ------------------------------------------------------------ gate lookup
def load_assumed():
    """(coin, sv, lv) -> {'taker': pct, 'maker': pct} assumed RT costs."""
    out = {}
    for f in GATES:
        if not os.path.exists(f):
            continue
        for r in csv.DictReader(open(f)):
            k = (r["coin"], r["short_venue"].lower(), r["long_venue"].lower())
            mk = None
            for c in MAKER_COL:
                if r.get(c) not in (None, ""):
                    mk = float(r[c])
                    break
            out[k] = {"taker": float(r["rt_cost_1000_pct"]), "maker": mk}
    return out


# ---------------------------------------------------------------- helpers
def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


async def live_spreads(coin, sv, lv):
    """live interval-true spread APR for a pair, or None."""
    book = await pl.sharpe_current([coin])
    legs = book.get(coin, {})
    if sv in legs and lv in legs:
        return pl.apr(*legs[sv][:2]) - pl.apr(*legs[lv][:2])
    return None


async def sim_exit(con, pos):
    """Simulate unwinding an open position at the CURRENT live book.

    Returns (exit_cost_frac, detail_str) where exit_cost includes taker
    fees on both legs (gate convention) + walk-the-book slippage.
    """
    coin, sv, lv = pos["coin"], pos["short_venue"], pos["long_venue"]
    sbids, sasks = await pl.get_book(sv, coin)
    lbids, lasks = await pl.get_book(lv, coin)
    smid = (sbids[0][0] + sasks[0][0]) / 2
    lmid = (lbids[0][0] + lasks[0][0]) / 2
    n = pos["size_usd"]
    # short leg: we are short -> buy back (walk asks)
    _, s_slip = pl.walk_vwap(sasks, smid, n)
    # long leg: we are long -> sell (walk bids)
    _, l_slip = pl.walk_vwap(lbids, lmid, n)
    if s_slip is None or l_slip is None:
        return None, "book too thin to walk at size"
    fees = (pl.TAKER[sv] + pl.TAKER[lv]) * 1e4   # bps, taker exit
    cost = (s_slip + l_slip + fees) / 1e4
    detail = f"exit slip short {s_slip:.1f} long {l_slip:.1f} + fee {fees:.0f}bps"
    return cost, detail


def last_accrual_ts(con, pos_id, ts_open):
    r = con.execute("SELECT MAX(ts) m FROM accruals WHERE pos_id=?",
                    (pos_id,)).fetchone()
    return parse_ts(r["m"]) if r and r["m"] else parse_ts(ts_open)


# -------------------------------------------------------------------- run
async def run(accrual_only=False):
    global _session
    async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0"}) as s:
        _session = s
        pl._session = s
        ds._session = s
        nv._session = s
        # symbol maps for aster/nado books (needed by pl.get_book)
        try:
            info = await ds._get("https://fapi.asterdex.com/fapi/v1/exchangeInfo")
            for x in info.get("symbols", []):
                if x.get("status") == "TRADING":
                    ds.ASTER_SYMS[x["baseAsset"].upper()] = x["symbol"]
        except Exception as e:
            print(f"aster symmap: ERR {e} (only matters for aster legs)")
        try:
            await nv.load_nado_symbols()
        except Exception as e:
            print(f"nado syms: ERR {e} (only matters for nado legs)")

        con = pl.db()
        con.execute("""CREATE TABLE IF NOT EXISTS drift_log(
            did INTEGER PRIMARY KEY, ts TEXT, pos_id INT, pair TEXT,
            hours_held REAL, assumed_rt_pct REAL, entry_cost_pct REAL,
            exit_cost_pct REAL, realized_rt_pct REAL, drift_ratio REAL,
            exit_detail TEXT, live_spread_apr REAL, accrual_usd REAL,
            accrual_hours REAL, accrual_total_usd REAL, kill_flag INT)""")

        assumed = load_assumed()
        pos = con.execute("SELECT * FROM positions WHERE status='open'") \
            .fetchall()
        if not pos:
            print("no open positions - nothing to drift-check")
            return
        now = datetime.now(timezone.utc)
        kill_today = False

        for p in pos:
            print(f"\n== pos #{p['pos_id']} {p['pair']} "
                  f"(${p['size_usd']:.0f}/leg)")
            coin, sv, lv = p["coin"], p["short_venue"], p["long_venue"]

            # ---- 1) accrual since last row
            spr = await live_spreads(coin, sv, lv)
            t_last = last_accrual_ts(con, p["pos_id"], p["ts_open"])
            hours = max((now - t_last).total_seconds() / 3600, 0.0)
            acc_usd, acc_total = 0.0, 0.0
            if spr is not None and hours > 0:
                net_hourly = spr / (24 * 365)
                acc_usd = net_hourly * p["size_usd"] * hours
                con.execute("INSERT INTO accruals(pos_id,ts,carry_hourly_usd,"
                            "spread_live_apr,note) VALUES (?,?,?,?,?)",
                            (p["pos_id"], pl.nowiso(), acc_usd, spr * 100,
                             f"{hours:.2f}h incremental"))
                con.commit()
            tot = con.execute("SELECT SUM(carry_hourly_usd) s FROM accruals "
                              "WHERE pos_id=?", (p["pos_id"],)).fetchone()["s"]
            acc_total = tot or 0.0
            spr_txt = f"{spr*100:.1f}%" if spr is not None else "n/a"
            print(f"  accrual: +${acc_usd:.4f} over {hours:.2f}h "
                  f"(live spread {spr_txt}, total ${acc_total:.4f})")

            if accrual_only:
                continue

            # ---- 2) drift: realized RT vs assumed
            k = (coin, sv, lv)
            a = assumed.get(k)
            if a is None:
                print("  drift: no gate row for pair - skip")
                continue
            fills = con.execute("SELECT * FROM fills WHERE pos_id=?",
                                (p["pos_id"],)).fetchall()
            style = "maker" if any(f["leg"] == "short" and
                                   f["style"] == "maker"
                                   for f in fills) else "taker"
            ass = a["maker"] if (style == "maker" and a["maker"]) \
                else a["taker"]
            entry_frac = sum((f["slip_bps"] + f["fee_bps"]) / 1e4
                             for f in fills)
            try:
                exit_frac, detail = await sim_exit(con, p)
            except Exception as e:
                print(f"  drift: exit sim ERR {str(e)[:90]} - skip")
                continue
            if exit_frac is None:
                print(f"  drift: {detail} - skip")
                continue
            realized = (entry_frac + exit_frac) * 100.0   # percent
            ratio = realized / ass if ass > 0 else float("nan")
            hours_held = (now - parse_ts(p["ts_open"])).total_seconds() / 3600
            kill = ratio > 1.5
            kill_today |= kill
            con.execute("INSERT INTO drift_log(ts,pos_id,pair,hours_held,"
                        "assumed_rt_pct,entry_cost_pct,exit_cost_pct,"
                        "realized_rt_pct,drift_ratio,exit_detail,"
                        "live_spread_apr,accrual_usd,accrual_hours,"
                        "accrual_total_usd,kill_flag) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pl.nowiso(), p["pos_id"], p["pair"], hours_held,
                         ass, entry_frac * 100, exit_frac * 100,
                         realized, ratio, detail,
                         spr, acc_usd, hours, acc_total, int(kill)))
            con.commit()
            print(f"  drift: assumed {ass:.3f}% vs realized {realized:.3f}% "
                  f"(entry {entry_frac*100:.3f} + exit {exit_frac*100:.3f}) "
                  f"-> ratio {ratio:.2f}x "
                  f"[{'KILL-FLAG' if kill else 'ok' if ratio <= 1.3 else 'drift'}]"
                  f" | {detail}")

        # ---- 3) acceptance + kill-switch summary
        rows = con.execute("SELECT * FROM drift_log").fetchall()
        print(f"\ndrift samples: {len(rows)}")
        if rows:
            ok = sum(1 for r in rows if r["drift_ratio"] <= 1.3)
            worst = max(rows, key=lambda r: r["drift_ratio"])
            print(f"  acceptance: {ok}/{len(rows)} <= 1.3x "
                  f"({ok/len(rows)*100:.0f}%, criterion >=80%)")
            print(f"  worst: {worst['pair']} {worst['drift_ratio']:.2f}x "
                  f"at {worst['ts']}")
        if kill_today:
            print("  *** KILL-SWITCH FLAG: drift ratio > 1.5x today - "
                  "halt new paper entries per spec M5 ***")
        _csv_mirror(con)


def _csv_mirror(con):
    rows = [dict(r) for r in con.execute("SELECT * FROM drift_log")]
    if not rows:
        return
    cols = list(rows[0].keys())
    with open(DRIFT_CSV, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"csv mirror -> {DRIFT_CSV}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--accrual-only", action="store_true")
    a = ap.parse_args()
    await run(accrual_only=a.accrual_only)


if __name__ == "__main__":
    asyncio.run(main())
