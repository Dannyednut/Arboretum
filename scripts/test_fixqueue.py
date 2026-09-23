#!/usr/bin/env python3
"""Fix-queue regression tests (Part 18/19 scope, recreated after rollback).

Covers: C1 unwind unit-qty sizing, C2 emergency cost class + kill exclusion,
fix3 freeze TTL (4 formats + fail-safe + entry wiring), fix4 exec-side
FLOOR_PARAM_CHANGE emission, fix5 held-probe tripwire persistence.

ISOLATION: everything runs on a temp sqlite file; pf.KILL_FILE is patched to
a temp path so the real kill switch is never read or written.
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import v3_store as st
import v3_exec as ex
import v3_portfolio as pf
import v3_tripwires as tw

TMP = tempfile.mkdtemp(prefix="fixqueue_")
DB = os.path.join(TMP, "t.db")
KILLTMP = os.path.join(TMP, "kill.json")
pf.KILL_FILE = KILLTMP          # isolation: never touch the real kill file

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
con.executescript("""
CREATE TABLE IF NOT EXISTS exec_positions(
  pos_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_open TEXT, pair TEXT, coin TEXT, short_venue TEXT, long_venue TEXT,
  size_usd REAL, qty REAL, status TEXT, entry_cost_pct REAL,
  median_apr REAL, pair_cap_usd REAL, flags TEXT,
  unwind_reason TEXT, ts_close TEXT, reason_close TEXT);
CREATE TABLE IF NOT EXISTS exec_orders(
  oid INTEGER PRIMARY KEY AUTOINCREMENT,
  pos_id INT, ts_post TEXT, ts_done TEXT, leg TEXT, venue TEXT,
  side TEXT, style TEXT, target_qty REAL, limit_px REAL,
  notional_usd REAL, status TEXT, reposts INT DEFAULT 0, notes TEXT);
CREATE TABLE IF NOT EXISTS paper_fills(
  fid INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, pair TEXT, leg TEXT,
  venue TEXT, side TEXT, style TEXT, target_qty REAL, sim_fill_px REAL,
  sim_slip_bps REAL, fee_bps REAL, funding_interval REAL, notes TEXT);
CREATE TABLE IF NOT EXISTS exec_drift(
  did INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, pair TEXT,
  assumed_rt_pct REAL, realized_rt_pct REAL, ratio REAL, verdict TEXT,
  detail TEXT);
CREATE TABLE IF NOT EXISTS tripwire_log(
  tid INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, coin TEXT,
  short_venue TEXT, long_venue TEXT, size_usd REAL, mode TEXT, tw TEXT,
  result TEXT, measured TEXT);
CREATE TABLE IF NOT EXISTS events(
  eid INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, etype TEXT, coin TEXT,
  venue TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS funding_obs(
  obs_id INTEGER PRIMARY KEY AUTOINCREMENT, venue TEXT, base TEXT,
  ts INTEGER, rate REAL, interval_h REAL, source TEXT);
""")

N = [0]
FAILS = []


def ok(name, cond):
    N[0] += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)
    return cond


def mkpos(pid=1, status="open", entry_cost=0.10, size=100.0,
           flags=None, pair="TEST v1->v2"):
    f = json.dumps(flags if flags is not None else
                   {"cost_bps": {"short:sell:entry": 5.0,
                                 "long:buy:entry": 7.0},
                    "comp_streak": 0, "inv_streak": 0})
    con.execute("INSERT INTO exec_positions(pos_id,ts_open,pair,coin,"
                "short_venue,long_venue,size_usd,qty,status,entry_cost_pct,"
                "median_apr,pair_cap_usd,flags) VALUES (?,?,?,?,?,?,?,"
                "100,?,?,0.5,500,?)",
                (pid, ex.nowiso(), pair, "TEST", "v1", "v2", size, status,
                 entry_cost, f))
    return dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                            (pid,)).fetchone())


def reload(pid):
    return dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                            (pid,)).fetchone())


def entry_fills(pid, qty=100.0, px=1.0):
    for leg, side, v in (("short", "sell", "v1"), ("long", "buy", "v2")):
        con.execute("INSERT INTO paper_fills(ts,pair,leg,venue,side,style,"
                    "target_qty,sim_fill_px,sim_slip_bps,fee_bps,"
                    "funding_interval,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ex.nowiso(), "TEST v1->v2", leg, v, side, "taker",
                     qty, px, 2.0, 5.0, 8, f"pos:{pid};entry"))


BOOK = ([(0.99, 5000.0)], [(1.01, 5000.0)])   # deep synthetic book
BOOKS = {("v1", "TEST"): BOOK, ("v2", "TEST"): BOOK}


async def taker_ivl(venue, coin):
    return 8


# ------------------------------------------------------------- C1 sizing
async def c1():
    print("C1 unwind unit-qty sizing:")
    ex.funding_interval = taker_ivl
    # snapshot from paper_fills
    p = mkpos(1)
    entry_fills(1, qty=100.0)
    snap = ex._entry_leg_qty(con, p)
    ok("c1_snapshot", snap == {"short": 100.0, "long": 100.0})
    # immediate unwind: exact 100u/leg, zero residual
    await ex.start_unwind(con, p, "test", immediate=True)
    p = reload(1)
    ok("c1_start_flags", p["status"] == "unwinding" and
       json.loads(p["flags"])["entry_leg_qty"]["short"] == 100.0)
    for _ in range(3):
        await ex.progress_unwind(con, p, BOOKS)
        p = reload(1)
    f = json.loads(p["flags"])
    fills = con.execute("SELECT leg, side, target_qty FROM paper_fills "
                        "WHERE notes LIKE 'pos:1;%unwind%'").fetchall()
    short_out = sum(r["target_qty"] for r in fills
                    if r["leg"] == "short" and r["side"] == "buy")
    long_out = sum(r["target_qty"] for r in fills
                   if r["leg"] == "long" and r["side"] == "sell")
    ok("c1_exact_exit_qty", abs(short_out - 100.0) < 1e-9 and
       abs(long_out - 100.0) < 1e-9)
    ok("c1_zero_residual", p["status"] == "closed")
    ok("c1_done_qty", abs(f.get("unwind_done_qty", {})
                          .get("short", 0) - 100.0) < 1e-9)
    # _px_covering worst-price vwap
    lv = [(1.00, 10.0), (1.02, 100.0)]
    ok("c1_px_covering", abs(ex._px_covering(lv, 50.0) -
                             (10 * 1.0 + 40 * 1.02) / 50.0) < 1e-12)
    # ladder (non-immediate) accumulates across ticks to unit completion
    p2 = mkpos(2, flags={"cost_bps": {"short:sell:entry": 5.0,
                                      "long:buy:entry": 7.0}})
    entry_fills(2, qty=100.0)
    await ex.start_unwind(con, p2, "test", immediate=False)
    p2 = reload(2)
    thin = ([(0.99, 30.0)], [(1.01, 30.0)])   # forces multi-tick ladder
    b2 = {("v1", "TEST"): thin, ("v2", "TEST"): thin}
    for _ in range(12):
        if p2["status"] != "unwinding":
            break
        await ex.progress_unwind(con, p2, b2)
        p2 = reload(2)
    ok("c1_ladder_completes", p2["status"] == "closed")
    # legacy USD fallback (no snapshot -> old 95%-USD rule)
    p3 = mkpos(3, flags={"cost_bps": {"short:sell:entry": 5.0,
                                      "long:buy:entry": 7.0},
                         "unwind_done": {"short": 96.0, "long": 96.0},
                         "unwind_immediate": False})
    ex.check_unwind_complete(con, p3)
    ok("c1_legacy_fallback",
       reload(3)["status"] == "closed")
    # post-walk clamp: huge immediate tranche cannot overfill remain qty
    p4 = mkpos(4, flags={"cost_bps": {"short:sell:entry": 5.0,
                                      "long:buy:entry": 7.0}})
    entry_fills(4, qty=100.0)
    await ex.start_unwind(con, p4, "test", immediate=True)
    p4 = reload(4)
    await ex.progress_unwind(con, p4, BOOKS)
    p4 = reload(4)
    fills = con.execute("SELECT target_qty FROM paper_fills WHERE notes "
                        "LIKE 'pos:4;%unwind%' AND side='buy'").fetchall()
    ok("c1_no_overfill",
       all(r["target_qty"] <= 100.0 + 1e-9 for r in fills))


# ------------------------------------------------------------- C2 class
async def c2():
    print("C2 emergency cost class:")
    real_bp = pf._book_positions
    pf._book_positions = lambda: []   # isolation: no real books in kill eval
    # immediate close: assumed = max(2*entry, 0.30)
    p = mkpos(10, entry_cost=0.10,
              flags={"cost_bps": {"short:sell:entry": 5.0,
                                  "long:buy:entry": 7.0,
                                  "short:buy:exit": 20.0,
                                  "long:sell:exit": 25.0},
                     "unwind_done": {"short": 100.0, "long": 100.0},
                     "unwind_immediate": True})
    ex.check_unwind_complete(con, p)
    d = dict(con.execute("SELECT * FROM exec_drift ORDER BY did DESC "
                         "LIMIT 1").fetchone())
    ok("c2_immediate_floor", d["assumed_rt_pct"] >= 0.30)
    ok("c2_class_immediate",
       json.loads(d["detail"]).get("class") == "immediate")
    # planned close: assumed = 2*entry exactly (0.20 here, no floor bump)
    p = mkpos(11, entry_cost=0.10,
              flags={"cost_bps": {"short:sell:entry": 5.0,
                                  "long:buy:entry": 7.0,
                                  "short:buy:exit": 20.0,
                                  "long:sell:exit": 25.0},
                     "unwind_done": {"short": 100.0, "long": 100.0},
                     "unwind_immediate": False})
    ex.check_unwind_complete(con, p)
    d = dict(con.execute("SELECT * FROM exec_drift ORDER BY did DESC "
                         "LIMIT 1").fetchone())
    ok("c2_planned_no_floor", abs(d["assumed_rt_pct"] - 0.20) < 1e-9)
    ok("c2_class_planned",
       json.loads(d["detail"]).get("class") == "planned")
    # EXEC_CLOSE event carries [cls]
    e = con.execute("SELECT detail FROM events WHERE etype='EXEC_CLOSE' "
                    "ORDER BY eid DESC LIMIT 1").fetchone()
    ok("c2_close_event_tag", "[planned]" in e["detail"] or
       "[immediate]" in e["detail"])
    # kill exclusion: immediate OVER row alone must NOT halt
    con.execute("DELETE FROM exec_drift")
    con.execute("INSERT INTO exec_drift(ts,pair,assumed_rt_pct,"
                "realized_rt_pct,ratio,verdict,detail) VALUES (?,?,?,?,?,?,?)",
                (ex.nowiso(), "T", 0.3, 0.6, 2.0, "OVER",
                 json.dumps({"class": "immediate"})))
    st_ = pf.kill_eval(con, verbose=False)
    ok("c2_kill_excludes_immediate", st_["halt"] is False)
    # planned OVER row DOES halt
    con.execute("UPDATE exec_drift SET detail=?",
                (json.dumps({"class": "planned"}),))
    st_ = pf.kill_eval(con, verbose=False)
    ok("c2_kill_counts_planned", st_["halt"] is True)
    # legacy row (no class tag) = planned -> halts (conservative)
    con.execute("UPDATE exec_drift SET detail=?",
                (json.dumps({"short:sell:entry": 5.0}),))
    st_ = pf.kill_eval(con, verbose=False)
    ok("c2_kill_legacy_planned", st_["halt"] is True)
    con.execute("DELETE FROM exec_drift")
    pf._book_positions = real_bp


# ---------------------------------------------------------- fix3 freeze TTL
def fix3():
    print("fix3 freeze TTL:")
    ok("fz_ttl_value", ex.FREEZE_TTL_S == 86400)
    # ISO fresh -> active (time-relative: 1h ago; a hard-coded date ages
    # past the 24h TTL and time-bombs the suite -- caught 2026-09-16)
    st.kv_set(con, "venue_freeze_a",
              f"frozen {ex.ts2iso(time.time() - 3600)}: x")
    ok("fz_iso_fresh", ex.freeze_active(con, "a") is True)
    # ISO expired -> cleared + event
    old = ex.ts2iso(time.time() - 86400 - 60)
    st.kv_set(con, "venue_freeze_a", f"frozen {old}: funding-param change")
    ok("fz_iso_expired", ex.freeze_active(con, "a") is False)
    ok("fz_iso_deleted", st.kv_get(con, "venue_freeze_a") is None)
    ok("fz_expire_event", con.execute(
        "SELECT COUNT(*) FROM events WHERE etype='EXEC_FREEZE_EXPIRE' "
        "AND venue='a'").fetchone()[0] == 1)
    # epoch fresh / expired
    st.kv_set(con, "venue_freeze_b", str(time.time()))
    ok("fz_epoch_fresh", ex.freeze_active(con, "b") is True)
    st.kv_set(con, "venue_freeze_b", str(time.time() - 86400 - 1))
    ok("fz_epoch_expired", ex.freeze_active(con, "b") is False)
    # unparseable -> fail-safe frozen, KV kept
    st.kv_set(con, "venue_freeze_c", "weird-value")
    ok("fz_unparseable_stays", ex.freeze_active(con, "c") is True)
    ok("fz_unparseable_kept", st.kv_get(con, "venue_freeze_c") is not None)
    # missing -> not frozen
    ok("fz_missing", ex.freeze_active(con, "zz") is False)
    # entry-path wiring: source must call freeze_active, not raw kv_get
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "v3_exec.py")).read()
    ok("fz_entry_wiring", "if freeze_active(con, v)]" in src and
       "kv_get(con, f\"venue_freeze_{v}\")" not in src)


# ------------------------------------------------------- fix4/fix5 emission
async def fix45():
    print("fix4/fix5 exec-side emission + held-probe persistence:")
    # fix4: tw5_floor change -> FLOOR_PARAM_CHANGE with path=exec-side
    async def fake_params(coin):
        return {"fundingIntervalHours": 4}      # differs from baseline 1h
    tw.PARAM_FETCHERS["v1"] = fake_params
    st.kv_set(con, "floor_params_v1_TEST", json.dumps(
        {"fundingIntervalHours": 1}))
    okf, meas = await tw.tw5_floor(con, "TEST", "v1", "v2", held=True)
    e = con.execute("SELECT detail FROM events WHERE "
                    "etype='FLOOR_PARAM_CHANGE' ORDER BY eid DESC "
                    "LIMIT 1").fetchone()
    ok("fix4_emitted", e is not None and
       json.loads(e["detail"]).get("path") == "exec-side")
    ok("fix4_freeze_set",
       st.kv_get(con, "venue_freeze_v1") is not None)
    # fix5+unwind: held floor change -> tripwire_log row + immediate unwind
    async def fake_tw5(con_, coin, sv, lv, held=False):
        return False, {"changes": [{"venue": sv, "old": 1, "new": 4}],
                       "held_legs": True}
    real_tw5 = tw.tw5_floor
    tw.tw5_floor = fake_tw5
    p = mkpos(20)
    await ex.trigger_floor(con, p)
    tw.tw5_floor = real_tw5
    r = con.execute("SELECT * FROM tripwire_log WHERE mode='held_check' "
                    "AND tw='TW5_floor_monitor'").fetchone()
    ok("fix5_persisted", r is not None and r["result"] == "fail" and
       "changes" in (r["measured"] or ""))
    ok("f5b_held_unwind_started", reload(20)["status"] == "unwinding")
    ok("fix5_immediate_flag",
       json.loads(reload(20)["flags"]).get("unwind_immediate") is True)


async def main():
    await c1()
    await c2()
    fix3()
    await fix45()
    print(f"\n{N[0] - len(FAILS)}/{N[0]} CHECKS PASS")
    if FAILS:
        print("FAILED:", ", ".join(FAILS))
        sys.exit(1)
    print("ALL FIXQUEUE TESTS PASS")


if __name__ == "__main__":
    asyncio.run(main())
