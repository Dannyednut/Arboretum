#!/usr/bin/env python3
"""P0 paper-ledger for the v3 carry system (spec Part 12 §3.3).

Simulated fills driven by LIVE books. No real orders ever.

Commands:
  open            run the tripwire chain on configured pairs; open paper
                  positions for pairs that pass (log deferrals otherwise)
  report          mark-to-market: accruals, live spread vs median, drift
  close --pos N --reason TEXT
  state           raw dump of positions/fills

Ledger: SQLite at download/data/paper_v3.db + CSV mirror in
download/data/paper_ledger.csv. All fills are SIMULATED at walk-the-book
VWAP (taker) or touch (maker) of the live book at decision time.
"""
import argparse
import asyncio
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import depth_sampler as ds          # noqa: E402
import depth_newvenue as nv         # noqa: E402  (bingx/backpack/nado books)

DB = "/home/z/my-project/download/data/paper_v3.db"
CSV_MIRROR = "/home/z/my-project/download/data/paper_ledger.csv"
PERSIST = "/home/z/my-project/download/data/funding_persistence_90d_v3.csv"
SHARPE_PAIRS = "/home/z/my-project/download/data/sharpe_newvenue_pairs.csv"

TAKER = dict(ds.TAKER)
TAKER.update(nv.TAKER_NEW if hasattr(nv, "TAKER_NEW") else {})
TAKER.update({"bingx": 0.0005, "backpack": 0.00095, "nado": 0.00035,
              "orderly": 0.0003})
MAKER = dict(TAKER, aster=0.0, nado=0.0001, backpack=0.00085, orderly=0.0,
             bingx=0.0002)

LIVE_SPREAD_FRAC_MIN = 0.40      # Part-11 rule
MARK_DEV_MAX = 0.05

# pair configs: P0 $1k basket (Part 12 §4 + Part-7 FINAL worst-case basket)
PAIRS = [
    {"pair": "XMR dydx->binance", "coin": "XMR", "short": "dydx",
     "long": "binance", "size_usd": 400, "short_style": "maker",
     "median_src": "persist"},
    {"pair": "BTW aster->bitget", "coin": "BTW", "short": "aster",
     "long": "bitget", "size_usd": 400, "short_style": "maker",
     "median_src": "persist"},
    {"pair": "UAI aster->binance", "coin": "UAI", "short": "aster",
     "long": "binance", "size_usd": 400, "short_style": "maker",
     "median_src": "persist"},
    {"pair": "INJ bingx->okx", "coin": "INJ", "short": "bingx",
     "long": "okx", "size_usd": 250, "short_style": "taker",
     "median_src": "sharpe"},
    {"pair": "LINK nado->okx", "coin": "LINK", "short": "nado",
     "long": "okx", "size_usd": 250, "short_style": "maker",
     "median_src": "sharpe"},
]

_session: aiohttp.ClientSession | None = None


async def _get(url, params=None, timeout=20):
    async with _session.get(url, params=params,
                            timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.json(content_type=None)


# ------------------------------------------------------------------ storage
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS positions(
      pos_id INTEGER PRIMARY KEY, ts_open TEXT, pair TEXT, coin TEXT,
      short_venue TEXT, long_venue TEXT, size_usd REAL, status TEXT,
      reason_open TEXT, ts_close TEXT, reason_close TEXT,
      entry_cost_pct REAL, median_apr REAL);
    CREATE TABLE IF NOT EXISTS fills(
      fill_id INTEGER PRIMARY KEY, pos_id INT, ts TEXT, leg TEXT, venue TEXT,
      side TEXT, style TEXT, notional_usd REAL, fill_px REAL, slip_bps REAL,
      fee_bps REAL, simulated INT DEFAULT 1);
    CREATE TABLE IF NOT EXISTS accruals(
      acc_id INTEGER PRIMARY KEY, pos_id INT, ts TEXT, carry_hourly_usd REAL,
      spread_live_apr REAL, note TEXT);
    CREATE TABLE IF NOT EXISTS tripwire_log(
      tid INTEGER PRIMARY KEY, ts TEXT, pair TEXT, check_name TEXT,
      passed INT, detail TEXT);
    """)
    return con


def log_trip(con, pair, name, passed, detail):
    con.execute("INSERT INTO tripwire_log(ts,pair,check_name,passed,detail)"
                " VALUES (?,?,?,?,?)",
                (nowiso(), pair, name, int(passed), str(detail)[:240]))
    con.commit()
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}: {str(detail)[:110]}")


def nowiso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------- book access
BOOKS = {}


async def get_book(venue, base):
    if venue == "dydx":
        return await ds.book_dydx(base)
    if venue == "binance":
        return await ds.book_binance(base)
    if venue == "bitget":
        return await ds.book_bitget(base)
    if venue == "okx":
        return await ds.book_okx(base)
    if venue == "aster":
        return await ds.book_aster(base)
    if venue == "bingx":
        return await nv.book_bingx(base)
    if venue == "nado":
        return await nv.book_nado(base)
    if venue == "backpack":
        return await nv.book_backpack(base)
    raise RuntimeError(f"no fetcher {venue}")


def walk_vwap(levels, mid, notional):
    r = ds.walk(levels, mid, notional)
    if r is None:
        return None, None
    slip, filled, vwap = r
    return vwap, slip


# ------------------------------------------------------- funding/spread data
def load_medians():
    med = {}
    for r in csv.DictReader(open(PERSIST)):
        med[(r["coin"], r["short_venue"], r["long_venue"])] = \
            float(r["apr_30d"])
    for r in csv.DictReader(open(SHARPE_PAIRS)):
        k = (r["coin"], r["short_venue"], r["long_venue"].lower())
        if k[1].lower() not in ds.TAKER:
            med[(r["coin"], r["short_venue"].lower(), k[2])] = \
                float(r["median_spread_apr"]) / 100.0
    return med


async def sharpe_current(coins):
    """coin -> {venue_lower: (rate, interval_h)} from Sharpe current book."""
    out = {}
    for c in coins:
        try:
            rows = await _get("https://www.sharpe.ai/api/funding/rates",
                              params={"type": "current", "coin": c})
            rows = rows if isinstance(rows, list) else (rows.get("data") or [])
            m = {}
            for r in rows:
                if str(r.get("base_coin", "")).upper() != c:
                    continue
                try:
                    m[str(r.get("exchange", "")).lower()] = (
                        float(r.get("rate") or 0),
                        float(r.get("interval_hours") or 1),
                        float(r.get("mark_price") or 0))
                except (TypeError, ValueError):
                    continue
            out[c] = m
        except Exception as e:
            print(f"  sharpe current {c}: ERR {str(e)[:80]}")
            out[c] = {}
    return out


def apr(rate, interval_h):
    return rate / interval_h * 24 * 365


# ------------------------------------------------------------------- open
async def run_open():
    global _session
    async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0"}) as s:
        _session = s
        ds._session = s
        nv._session = s
        info = await ds._get("https://fapi.asterdex.com/fapi/v1/exchangeInfo")
        for x in info.get("symbols", []):
            if x.get("status") == "TRADING":
                ds.ASTER_SYMS[x["baseAsset"].upper()] = x["symbol"]
        await nv.load_nado_symbols()

        con = db()
        med = load_medians()
        coins = sorted({p["coin"] for p in PAIRS})
        print(f"sharpe current for {coins} ...")
        live = await sharpe_current(coins)

        for p in PAIRS:
            print(f"\n== {p['pair']}  size ${p['size_usd']:.0f}/leg "
                  f"({p['short_style']} short)")
            coin, sv, lv = p["coin"], p["short"], p["long"]
            dup = con.execute(
                "SELECT 1 FROM positions WHERE pair=? AND status='open'",
                (p["pair"],)).fetchone()
            if dup:
                log_trip(con, p["pair"], "duplicate", 0, "already open")
                continue

            # 1) books
            try:
                sbids, sasks = await get_book(sv, coin)
                lbids, lasks = await get_book(lv, coin)
            except Exception as e:
                log_trip(con, p["pair"], "books", 0, f"fetch err {e}")
                continue
            smid = (sbids[0][0] + sasks[0][0]) / 2
            lmid = (lbids[0][0] + lasks[0][0]) / 2
            log_trip(con, p["pair"], "identity-marks",
                     abs(smid - lmid) / lmid <= MARK_DEV_MAX,
                     f"short mid {smid:.4g} vs long mid {lmid:.4g} "
                     f"dev {abs(smid-lmid)/lmid*100:.2f}%")

            # 2) depth walk at size, both sides both legs (entry+exit)
            s_sell, s_sell_slip = walk_vwap(sbids, smid, p["size_usd"])
            s_buy, s_buy_slip = walk_vwap(sasks, smid, p["size_usd"])
            l_buy, l_buy_slip = walk_vwap(lasks, lmid, p["size_usd"])
            l_sell, l_sell_slip = walk_vwap(lbids, lmid, p["size_usd"])
            worst = max(x for x in (s_sell_slip, s_buy_slip, l_buy_slip,
                                    l_sell_slip) if x is not None)
            depth_gate = worst <= 25.0
            log_trip(con, p["pair"], "depth-25bps@size", depth_gate,
                     f"worst slip {worst:.1f}bps (sell {s_sell_slip:.1f}/"
                     f"{s_buy_slip:.1f} buy {l_buy_slip:.1f}/{l_sell_slip:.1f})")
            if not depth_gate:
                continue

            # 3) live spread rule vs 60d median
            key = (coin, sv, lv)
            med_apr = med.get(key)
            sl = live.get(coin, {})
            if med_apr is None or sv not in sl or lv not in sl:
                log_trip(con, p["pair"], "live-spread", 0,
                         f"missing data med={med_apr} "
                         f"legs={sorted(sl)[:6]}")
                continue
            spr_live = apr(*sl[sv][:2]) - apr(*sl[lv][:2])
            ok3 = spr_live >= LIVE_SPREAD_FRAC_MIN * med_apr
            log_trip(con, p["pair"], "live-spread>=40%median", ok3,
                     f"live {spr_live*100:.1f}% vs median {med_apr*100:.1f}%")
            if not ok3:
                continue

            # 3.5) P2: full M2 tripwire chain -> v3.db tripwire_log
            #      (log-only for the chain; verdict gates the entry)
            try:
                import v3_tripwires as tw
                twcon = tw.st.connect()
                verdict, _ = await tw.run_chain(
                    twcon, coin, sv, lv, p["size_usd"], mode="pre_entry")
                twcon.close()
                log_trip(con, p["pair"], "m2-chain", verdict == "PASS",
                         f"v3 chain verdict {verdict}")
                if verdict != "PASS":
                    continue
            except Exception as e:
                log_trip(con, p["pair"], "m2-chain", 0, f"chain err {e}")
                continue

            # 4) open: simulated fills
            ts = nowiso()
            cur = con.execute(
                "INSERT INTO positions(ts_open,pair,coin,short_venue,"
                "long_venue,size_usd,status,reason_open,entry_cost_pct,"
                "median_apr) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, p["pair"], coin, sv, lv, p["size_usd"], "open",
                 "P0 paper basket", 0.0, med_apr))
            pos_id = cur.lastrowid
            n = p["size_usd"]
            if p["short_style"] == "maker":
                fills = [("short", sv, "sell", "maker", sasks[0][0], 0.0,
                          MAKER[sv] * 1e4)]
            else:
                fills = [("short", sv, "sell", "taker", s_sell,
                          s_sell_slip, TAKER[sv] * 1e4)]
            fills += [("long", lv, "buy", "taker", l_buy, l_buy_slip,
                       TAKER[lv] * 1e4)]
            cost = 0.0
            for leg, v, side, style, px, slip_bps, fee_bps in fills:
                con.execute(
                    "INSERT INTO fills(pos_id,ts,leg,venue,side,style,"
                    "notional_usd,fill_px,slip_bps,fee_bps) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (pos_id, ts, leg, v, side, style, n, px, slip_bps,
                     fee_bps))
                cost += (slip_bps + fee_bps) / 1e4
            entry_pct = cost * 100
            con.execute("UPDATE positions SET entry_cost_pct=? WHERE pos_id=?",
                        (entry_pct, pos_id))
            con.commit()
            print(f"  OPENED pos #{pos_id}: entry cost {entry_pct:.3f}% "
                  f"(fees+slip, one-way)")

        print()
        await run_report(con)


# ----------------------------------------------------------------- report
async def run_report(con=None):
    global _session
    own = con is None
    if own:
        async with aiohttp.ClientSession(
                headers={"User-Agent": "Mozilla/5.0"}) as s:
            _session = s
            ds._session = s
            nv._session = s
            con = db()
            await _report(con)
    else:
        await _report(con)


async def _report(con):
    pos = con.execute("SELECT * FROM positions WHERE status='open'") \
        .fetchall()
    if not pos:
        print("no open positions")
        return
    coins = sorted({r["coin"] for r in pos})
    live = await sharpe_current(coins)
    print(f"\n{'pos':>4s} {'pair':22s} {'age_h':>5s} {'entry%':>7s} "
          f"{'accr$':>7s} {'liveSpr%':>8s} {'medSpr%':>8s} {'hours2be':>8s}")
    for r in pos:
        p = dict(r)
        t0 = datetime.fromisoformat(
            p["ts_open"].replace("Z", "+00:00"))
        age_h = (datetime.now(timezone.utc) - t0).total_seconds() / 3600
        sl = live.get(p["coin"], {})
        if p["short_venue"] in sl and p["long_venue"] in sl:
            spr = (apr(*sl[p["short_venue"]][:2])
                   - apr(*sl[p["long_venue"]][:2]))
            # hourly net carry on $1 per leg: short collects, long pays
            net_hourly = (apr(*sl[p["short_venue"]][:2])
                          - apr(*sl[p["long_venue"]][:2])) / (24 * 365)
            accr = net_hourly * p["size_usd"] * age_h
            h2be = (p["entry_cost_pct"] / 100.0) / (net_hourly * 2) \
                if net_hourly > 0 else float("nan")
            print(f"{p['pos_id']:>4d} {p['pair']:22s} {age_h:5.1f} "
                  f"{p['entry_cost_pct']:7.3f} {accr:7.3f} "
                  f"{spr*100:8.1f} {p['median_apr']*100:8.1f} "
                  f"{h2be:8.1f}")
        else:
            print(f"{p['pos_id']:>4d} {p['pair']:22s} {age_h:5.1f} "
                  f"{p['entry_cost_pct']:7.3f}  (no live funding data)")
    fail24 = con.execute(
        "SELECT COUNT(*) c FROM tripwire_log WHERE passed=0 AND "
        "ts > datetime('now','-1 day')").fetchone()["c"]
    print(f"\ntripwire failures last 24h: {fail24}")
    _csv_mirror(con)


def _csv_mirror(con):
    """Dump positions + fills + tripwire_log to one CSV (research mirror)."""
    import csv as _csv
    rows = []
    for r in con.execute("SELECT * FROM positions"):
        rows.append({"kind": "position", **dict(r)})
    for r in con.execute("SELECT * FROM fills"):
        rows.append({"kind": "fill", **dict(r)})
    for r in con.execute("SELECT * FROM tripwire_log"):
        rows.append({"kind": "tripwire", **dict(r)})
    cols = sorted({k for row in rows for k in row})
    with open(CSV_MIRROR, "w", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=["kind"] + [c for c in cols
                                                       if c != "kind"],
                            extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"csv mirror -> {CSV_MIRROR}")


# ------------------------------------------------------------------ close
def run_close(pos_id, reason):
    con = db()
    r = con.execute("SELECT * FROM positions WHERE pos_id=? AND "
                    "status='open'", (pos_id,)).fetchone()
    if not r:
        print(f"no open position {pos_id}")
        return
    con.execute("UPDATE positions SET status='closed', ts_close=?, "
                "reason_close=? WHERE pos_id=?",
                (nowiso(), reason, pos_id))
    con.commit()
    print(f"closed pos {pos_id} ({r['pair']}): {reason}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["open", "report", "close", "state"])
    ap.add_argument("--pos", type=int)
    ap.add_argument("--reason", default="manual")
    a = ap.parse_args()
    if a.cmd == "open":
        await run_open()
    elif a.cmd == "report":
        await run_report()
    elif a.cmd == "close":
        run_close(a.pos, a.reason)
    elif a.cmd == "state":
        con = db()
        for r in con.execute("SELECT * FROM positions"):
            print(dict(r))


if __name__ == "__main__":
    asyncio.run(main())
