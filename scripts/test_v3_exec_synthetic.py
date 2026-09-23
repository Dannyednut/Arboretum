#!/usr/bin/env python3
"""Synthetic lifecycle test for v3_exec.py (no network, isolated DB).

Drives the state-machine paths that live ticks may wait days to hit:
  1. sm_lt entry: maker post -> book crosses -> maker fill -> long taker
  2. unwind ladder: trigger -> tranche progression -> close + drift row
  3. entry patience: maker never crosses -> cancel + defer
  4. legging cap: long leg unfilled past 90s -> force taker + event
Cleans up its DB file at the end (pass --keep to inspect).
"""
import asyncio
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import v3_exec as ex

TESTDB = "/home/z/my-project/scripts/_test_exec.db"
FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print(f"[{tag}] {name} {detail}")


def book(px_bid, px_ask, qty=50.0):
    return ([(px_bid, qty)] * 5, [(px_ask, qty)] * 5)


async def main(keep=False):
    if os.path.exists(TESTDB):
        os.remove(TESTDB)
    ex.V3DB = TESTDB
    con = ex.econ()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS funding_obs(venue TEXT, base TEXT, ts INT,
        rate REAL, interval_h REAL, source TEXT);
    CREATE TABLE IF NOT EXISTS events(eid INTEGER PRIMARY KEY, ts TEXT,
        etype TEXT, coin TEXT, venue TEXT, detail TEXT);
    """)
    con.commit()

    async def fake_ivl(venue, coin):
        return 1.0
    ex.funding_interval = fake_ivl

    # kill-switch freshness is an ops concern, out of scope here: stub it.
    # (A >24h-old kill file denies ALL entries via m5_gate -> start_entry
    # returns None whenever this test runs days after the last engine window.)
    ex.pf.kill_halt = lambda: (False, "")
    ex.pf.ensure_kill_fresh = lambda con: None

    PAIR = {"pair": "TEST dydx->binance", "coin": "XMR", "short": "dydx",
            "long": "binance", "size_usd": 400, "seq": "sm_lt",
            "median_apr": 0.50}

    # ---- 1. sm_lt entry lifecycle
    books = {("dydx", "XMR"): book(99.9, 100.0),
             ("binance", "XMR"): book(100.0, 100.1)}
    pos_id = await ex.start_entry(con, PAIR, books)
    pos = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                           (pos_id,)).fetchone())
    check("1a maker posted at touch", pos["status"] == "seq_open"
          and pos["qty"] > 0, f"qty={pos['qty']:.6g}")
    o = dict(con.execute("SELECT * FROM exec_orders WHERE pos_id=? AND "
                         "status='resting'", (pos_id,)).fetchone())
    check("1b resting sell @ best ask", o["side"] == "sell"
          and abs(o["limit_px"] - 100.0) < 1e-9)

    # book crosses our sell: best bid lifts to 100.0 -> maker fill
    books[("dydx", "XMR")] = book(100.0, 100.1)
    await ex.advance_resting(con, pos, books)
    pos = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                           (pos_id,)).fetchone())
    fills = con.execute("SELECT * FROM paper_fills WHERE pair LIKE 'TEST%'"
                        ).fetchall()
    check("1c maker filled + long taker -> open",
          pos["status"] == "open" and len(fills) == 2,
          f"status={pos['status']} fills={len(fills)}")
    ecp = pos["entry_cost_pct"]
    check("1d entry cost = dydx maker fee + binance taker slip+fee",
          0 < ecp < 0.2, f"{ecp:.3f}%")

    # ---- 2. unwind ladder
    await ex.start_unwind(con, pos, "test_trigger")
    # thin within 10bps: only one level at 12bps off mid -> tranche ~$200
    books[("dydx", "XMR")] = ([(99.98, 50.0)], [(100.15, 2.0)])
    books[("binance", "XMR")] = ([(99.98, 50.0)], [(100.15, 2.0)])
    pos = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                           (pos_id,)).fetchone())
    await ex.progress_unwind(con, pos, books)
    n_after_1 = con.execute("SELECT COUNT(*) c FROM paper_fills WHERE "
                            "notes LIKE '%unwind%'").fetchone()["c"]
    pos = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                           (pos_id,)).fetchone())
    # exits priced 12bps out: tranche = within-25bps slice = ~$200/leg
    check("2a tranche 1 partial (thin book)", pos["status"] == "unwinding"
          and n_after_1 == 2, f"fills={n_after_1} status={pos['status']}")
    # deepen the book -> remaining tranches complete
    books[("dydx", "XMR")] = book(99.98, 100.08, qty=50.0)
    books[("binance", "XMR")] = book(99.98, 100.08, qty=50.0)
    await ex.progress_unwind(con, pos, books)
    pos = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                           (pos_id,)).fetchone())
    d = con.execute("SELECT * FROM exec_drift WHERE pair LIKE 'TEST%'"
                    ).fetchone()
    check("2b ladder completed -> closed + drift row",
          pos["status"] == "closed" and d is not None,
          f"status={pos['status']}")
    if d:
        check("2c drift ratio sane", d["ratio"] and 0 < d["ratio"] < 5,
              f"assumed={d['assumed_rt_pct']}% realized="
              f"{d['realized_rt_pct']}% ratio={d['ratio']}")

    # ---- 3. entry patience -> defer
    pos_id2 = await ex.start_entry(con, PAIR, books)
    con.execute("UPDATE exec_orders SET ts_post='2026-09-01T00:00:00Z' "
                "WHERE pos_id=?", (pos_id2,))
    con.commit()
    pos2 = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                            (pos_id2,)).fetchone())
    await ex.check_entry_patience(con, pos2, books)
    pos2 = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                            (pos_id2,)).fetchone())
    ev2 = con.execute("SELECT COUNT(*) c FROM events WHERE "
                      "etype='EXEC_DEFER'").fetchone()["c"]
    check("3 patience expired -> cancelled + DEFER event",
          pos2["status"] == "cancelled" and ev2 == 1)

    # ---- 4. legging cap: long unfilled past 90s -> force taker
    pos_id3 = await ex.start_entry(con, PAIR, books)
    check("4-pre maker posted", pos_id3 is not None)
    books[("binance", "XMR")] = None          # long book dies
    pos3 = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                            (pos_id3,)).fetchone())
    o3 = dict(con.execute("SELECT * FROM exec_orders WHERE pos_id=? AND "
                          "status='resting'", (pos_id3,)).fetchone())
    # force the short maker to fill while long book is missing
    books[("dydx", "XMR")] = book(o3["limit_px"] + 0.01, 100.2)
    await ex.advance_resting(con, pos3, books)
    pos3 = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                            (pos_id3,)).fetchone())
    f3 = ex.jload(pos3["flags"])
    check("4a short filled, long pending w/ deadline",
          pos3["status"] == "seq_open"
          and f3.get("pending_deadline") is not None,
          f"stage={f3.get('stage')}")
    # deadline passes with long book now available -> force taker
    f3["pending_deadline"] = ex.nows() - 1
    ex.set_flags(con, pos_id3, f3)
    pos3 = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                            (pos_id3,)).fetchone())   # fresh flags
    books[("binance", "XMR")] = book(100.0, 100.1)
    await ex.enforce_legging_cap(con, pos3, books)
    pos3 = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                            (pos_id3,)).fetchone())
    leg_ev = con.execute("SELECT COUNT(*) c FROM events WHERE "
                         "etype='EXEC_LEGGING'").fetchone()["c"]
    check("4b legging cap fired -> open via force taker",
          pos3["status"] == "open" and leg_ev >= 1)

    # ---- summary
    n = con.execute("SELECT COUNT(*) c FROM paper_fills").fetchone()["c"]
    print(f"\ntotal synthetic fills: {n}")
    con.close()
    if not keep:
        os.remove(TESTDB)
        print("test db removed")
    print("RESULT:", "ALL PASS" if not FAILS else f"FAILURES: {FAILS}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(keep="--keep" in sys.argv)))
