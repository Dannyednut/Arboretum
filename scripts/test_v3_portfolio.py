#!/usr/bin/env python3
"""Synthetic tests for v3 M5 portfolio layer (25 checks).
Run: python test_v3_portfolio.py   (self-contained, temp DBs, no network)
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import v3_portfolio as vp
import v3_store as st

TMP = tempfile.mkdtemp(prefix="m5test_")
ENGINE_DB = os.path.join(TMP, "engine.db")
P0_DB = os.path.join(TMP, "p0.db")
KILL = os.path.join(TMP, "kill_switch.json")

PASS_N = 0


def ok(cond, name):
    global PASS_N
    assert cond, f"FAIL: {name}"
    PASS_N += 1
    print(f"  ok {PASS_N:2d}  {name}")


def iso_days_ago(d):
    return datetime.fromtimestamp(time.time() - d * 86400,
                                  timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def fresh_dbs(engine_pos=None, p0_pos=None, kv=None, events=None,
              tw4_fails=0, drift_ratio=None, fund_rows=None):
    for f in (ENGINE_DB, P0_DB):
        if os.path.exists(f):
            os.remove(f)
    ec = sqlite3.connect(ENGINE_DB)
    ec.execute("""CREATE TABLE exec_positions(pos_id INTEGER PRIMARY KEY,
        pair TEXT, coin TEXT, short_venue TEXT, long_venue TEXT,
        size_usd REAL, ts_open TEXT, status TEXT, entry_cost_pct REAL)""")
    ec.execute("CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT)")
    ec.execute("""CREATE TABLE events(eid INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, etype TEXT, coin TEXT, venue TEXT, detail TEXT)""")
    ec.execute("""CREATE TABLE tripwire_log(tid INTEGER PRIMARY KEY
        AUTOINCREMENT, ts TEXT, coin TEXT, short_venue TEXT, long_venue
        TEXT, size_usd REAL, mode TEXT, tw TEXT, result TEXT,
        measured TEXT)""")
    ec.execute("""CREATE TABLE exec_drift(did INTEGER PRIMARY KEY
        AUTOINCREMENT, ts TEXT, pair TEXT, assumed_rt_pct REAL,
        realized_rt_pct REAL, ratio REAL, verdict TEXT, detail TEXT)""")
    ec.execute("""CREATE TABLE funding_obs(obs_id INTEGER PRIMARY KEY
        AUTOINCREMENT, venue TEXT, base TEXT, ts INTEGER, rate REAL,
        interval_h REAL, source TEXT)""")
    for p in engine_pos or []:
        ec.execute("INSERT INTO exec_positions(pair, coin, short_venue, "
                   "long_venue, size_usd, ts_open, status, entry_cost_pct) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   (p["pair"], p["coin"], p["short"], p["long"],
                    p["size"], p.get("ts_open", iso_days_ago(1)),
                    p.get("status", "open"), p.get("entry_cost_pct", 0.15)))
    for k, v in (kv or {}).items():
        ec.execute("INSERT INTO kv(key, value) VALUES(?,?)", (k, v))
    for e in events or []:
        ec.execute("INSERT INTO events(ts, etype, coin, venue, detail) "
                   "VALUES(?,?,?,?,?)",
                   (iso_days_ago(0), e[0], e[1], e[2], e[3]))
    for _ in range(tw4_fails):
        ec.execute("INSERT INTO tripwire_log(ts, coin, short_venue, "
                   "long_venue, size_usd, mode, tw, result, measured) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (iso_days_ago(0), "X", "a", "b", 100, "held_check",
                    "TW4_depth_rewalk", "fail", "{}"))
    if drift_ratio is not None:
        ec.execute("INSERT INTO exec_drift(ts, pair, assumed_rt_pct, "
                   "realized_rt_pct, ratio, verdict, detail) "
                   "VALUES(?,?,?,?,?,?,?)",
                   (iso_days_ago(0), "X a->b", 0.3, 0.5, drift_ratio,
                    "bad", "{}"))
    for fr in fund_rows or []:
        ec.execute("INSERT INTO funding_obs(venue, base, ts, rate, "
                   "interval_h, source) VALUES(?,?,?,?,?,?)",
                   (fr[0], fr[1], int(time.time() * 1000), fr[2], fr[3],
                    fr[4]))
    ec.commit()
    pc = sqlite3.connect(P0_DB)
    pc.execute("""CREATE TABLE positions(pos_id INTEGER PRIMARY KEY,
        pair TEXT, coin TEXT, short_venue TEXT, long_venue TEXT,
        size_usd REAL, ts_open TEXT, status TEXT, entry_cost_pct REAL)""")
    for p in p0_pos or []:
        pc.execute("INSERT INTO positions(pair, coin, short_venue, "
                   "long_venue, size_usd, ts_open, status, "
                   "entry_cost_pct) VALUES(?,?,?,?,?,?,?,?)",
                   (p["pair"], p["coin"], p["short"], p["long"],
                    p["size"], p.get("ts_open", iso_days_ago(1)),
                    p.get("status", "open"), p.get("entry_cost_pct", 0.15)))
    pc.commit()
    pc.close()
    ec.row_factory = sqlite3.Row
    return ec


def wire():
    """Point module globals at the temp DBs / kill file."""
    st.connect = lambda: sqlite3.connect(ENGINE_DB)
    st.DB = ENGINE_DB
    vp.pl.DB = P0_DB
    vp.KILL_FILE = KILL


def write_kill(halt, age_s=0):
    with open(KILL, "w") as f:
        json.dump({"ts": "2026-09-08T00:00:00Z", "halt": halt,
                   "sources": {}}, f)
    os.utime(KILL, (time.time() - age_s, time.time() - age_s))


def base_pos(coin="AAA", short="aster", long="binance", size=100.0):
    return [{"pair": f"{coin} {short}->{long}", "coin": coin,
             "short": short, "long": long, "size": size}]


# ------------------------------------------------------- kill_halt states
def test_kill_halt():
    print("kill_halt fail-safe states:")
    wire()
    if os.path.exists(KILL):
        os.remove(KILL)
    h, why = vp.kill_halt()
    ok(h and "missing" in why, "missing kill file -> HALT")
    write_kill(False, age_s=vp.KILL_STALE_S + 100)
    h, why = vp.kill_halt()
    ok(h and "stale" in why, "stale kill file -> HALT")
    write_kill(True)
    ok(vp.kill_halt() == (True, vp.kill_halt()[1]) and
       vp.kill_halt()[0], "halt=true fresh -> HALT")
    write_kill(False)
    h, why = vp.kill_halt()
    ok(not h, "halt=false fresh -> clear")
    with open(KILL, "w") as f:
        f.write("{corrupt")
    ok(vp.kill_halt()[0], "corrupt kill file -> HALT")


# ------------------------------------------------------------ kill_eval
def test_kill_eval_sources():
    print("kill_eval 4 spec sources:")
    wire()
    # source 1: floor-param change event
    con = fresh_dbs(engine_pos=base_pos(size=100),
                    events=[("FLOOR_PARAM_CHANGE", "AAA", "aster", "{}")])
    st8 = vp.kill_eval(con, verbose=False)
    ok(st8["halt"] and st8["sources"]["floor_param_change"]["hit"],
       "source1 floor-param change -> halt")
    # source 2: 3 held TW4 fails
    con = fresh_dbs(engine_pos=base_pos(size=100), tw4_fails=3)
    st8 = vp.kill_eval(con, verbose=False)
    ok(st8["halt"] and st8["sources"]["tripwire_depth_3x24h"]["hit"],
       "source2 3x TW4 held fails -> halt")
    con = fresh_dbs(engine_pos=base_pos(size=100), tw4_fails=2)
    st8 = vp.kill_eval(con, verbose=False)
    ok(not st8["sources"]["tripwire_depth_3x24h"]["hit"],
       "source2 2x TW4 fails -> no halt")
    # source 3: disagreement spike (baseline tiny, native-vs-sharpe gap big)
    con = fresh_dbs(engine_pos=base_pos(size=100),
                    kv={"m4_disagreement_baseline": json.dumps(
                        {"rows": [{"gap_apr": 0.01}]})},
                    fund_rows=[("aster", "AAA", 0.0005, 1.0, "native_live"),
                               ("aster", "AAA", 0.0005, 1.0, "sharpe_live"),
                               ("binance", "AAA", 0.0001, 4.0,
                                "native_live"),
                               ("binance", "AAA", 0.0009, 4.0,
                                "sharpe_live")])
    st8 = vp.kill_eval(con, verbose=False)
    ok(st8["sources"]["sharpe_native_disagreement"]["hit"],
       "source3 disagreement spike -> halt")
    # same rows, generous baseline -> no spike
    con = fresh_dbs(engine_pos=base_pos(size=100),
                    kv={"m4_disagreement_baseline": json.dumps(
                        {"rows": [{"gap_apr": 5.0}]})},
                    fund_rows=[("aster", "AAA", 0.0005, 1.0, "native_live"),
                               ("aster", "AAA", 0.0005, 1.0, "sharpe_live"),
                               ("binance", "AAA", 0.0001, 4.0,
                                "native_live"),
                               ("binance", "AAA", 0.0003, 4.0,
                                "sharpe_live")])
    st8 = vp.kill_eval(con, verbose=False)
    ok(not st8["sources"]["sharpe_native_disagreement"]["hit"],
       "source3 gap within baseline -> no halt")
    # source 4: drift ratio > 1.5x in 24h
    con = fresh_dbs(engine_pos=base_pos(size=100), drift_ratio=1.6)
    st8 = vp.kill_eval(con, verbose=False)
    ok(st8["halt"] and st8["sources"]["drift_over_15x"]["hit"],
       "source4 drift 1.6x -> halt")
    con = fresh_dbs(engine_pos=base_pos(size=100), drift_ratio=1.2)
    st8 = vp.kill_eval(con, verbose=False)
    ok(not st8["sources"]["drift_over_15x"]["hit"],
       "source4 drift 1.2x -> no halt")
    # all clear -> file written, halt false
    con = fresh_dbs(engine_pos=base_pos(size=100))
    st8 = vp.kill_eval(con, verbose=False)
    ok(not st8["halt"] and os.path.exists(KILL), "all clear -> file, clear")


# ------------------------------------------------------------- m5_gate
def test_gate():
    print("m5_gate paths:")
    wire()
    write_kill(False)
    con = fresh_dbs(engine_pos=[])            # empty book
    # kill deny
    write_kill(True)
    o, s, n = vp.m5_gate(con, "AAA", "aster", "binance", 100, 1000)
    ok(not o and any("kill" in x for x in n), "gate deny: kill halt")
    write_kill(False)
    # sizing: new pair 0.5x
    o, s, n = vp.m5_gate(con, "NEW", "aster", "binance", 1000, 1000)
    ok(o and abs(s - 500) < 1e-6, "gate sizing: new pair 0.5x cap")
    # sizing: incumbent 0.7x (pair exists 20d old)
    con = fresh_dbs(engine_pos=[dict(base_pos()[0], ts_open=iso_days_ago(20),
                                     status="closed")])
    o, s, n = vp.m5_gate(con, "AAA", "aster", "binance", 1000, 1000)
    ok(o and abs(s - 700) < 1e-6, "gate sizing: incumbent 0.7x cap")
    # venue cap deny tier2: bingx exposure 4800 + new 400 > 5000
    con = fresh_dbs(engine_pos=[{"pair": "BBB bingx->okx", "coin": "BBB",
                                 "short": "bingx", "long": "okx",
                                 "size": 4800}])
    o, s, n = vp.m5_gate(con, "CCC", "bingx", "binance", 400, 10000)
    ok(not o and any("venue cap bingx" in x for x in n),
       "gate deny: tier2 venue cap")
    # venue cap ok tier1
    con = fresh_dbs(engine_pos=[{"pair": "BBB okx->binance", "coin": "BBB",
                                 "short": "okx", "long": "binance",
                                 "size": 1800}])
    o, s, n = vp.m5_gate(con, "CCC", "okx", "binance", 200, 10000)
    ok(o, "gate ok: tier1 venue under cap")
    # correlation deny: 2 open pairs already long CCC
    con = fresh_dbs(engine_pos=[
        {"pair": "CCC a1->okx", "coin": "CCC", "short": "aster",
         "long": "okx", "size": 100},
        {"pair": "CCC a2->binance", "coin": "CCC", "short": "hl",
         "long": "binance", "size": 100}])
    o, s, n = vp.m5_gate(con, "CCC", "aster", "binance", 100, 10000)
    ok(not o and any("correlation" in x for x in n),
       "gate deny: correlation 2 same long")
    # RWA deny: bucket > 30% of deployed
    con = fresh_dbs(engine_pos=[
        {"pair": "TSLA a->binance", "coin": "TSLA", "short": "aster",
         "long": "binance", "size": 400},
        {"pair": "INJ bingx->okx", "coin": "INJ", "short": "bingx",
         "long": "okx", "size": 400}])
    o, s, n = vp.m5_gate(con, "TSLA", "aster", "binance", 100, 10000)
    ok(not o and any("RWA" in x for x in n), "gate deny: RWA bucket 30%")
    # leverage deny: 2300 deployed + 300 new = 2.6x
    con = fresh_dbs(engine_pos=[
        {"pair": f"C{i} aster->binance", "coin": f"C{i}",
         "short": "aster", "long": "binance", "size": 766.7}
        for i in range(3)])
    o, s, n = vp.m5_gate(con, "ZZZ", "aster", "binance", 300, 100000)
    ok(not o and any("leverage" in x for x in n),
       "gate deny: leverage > 2.5x")
    # clean pass with notes
    con = fresh_dbs(engine_pos=[])
    o, s, n = vp.m5_gate(con, "INJ", "bingx", "okx", 250, 500)
    ok(o and s == 250 and any("leverage" in x for x in n),
       "gate pass: clean book")


# ------------------------------------------------------------- trickle
def test_trickle():
    print("trickle math + cadence:")
    wire()
    write_kill(False)
    con = fresh_dbs(engine_pos=[
        {"pair": "AAA aster->binance", "coin": "AAA", "short": "aster",
         "long": "binance", "size": 400, "entry_cost_pct": 0.10}],
        fund_rows=[("aster", "AAA", 0.0005, 1.0, "native_live"),
                   ("binance", "AAA", 0.0002, 4.0, "native_live")])
    # clear cadence guard
    con.execute("DELETE FROM kv WHERE key='m5_trickle_last'")
    con.commit()
    rows = vp.trickle(con, verbose=False)
    ok(len(rows) == 1, "trickle: 1 row for 1 position")
    # exact math: aster apr = 0.0005*24*365 = 4.38; binance = 0.0002/4*8760
    apr_s = 0.0005 * 24 * 365
    apr_l = 0.0002 / 4 * 24 * 365
    funding_expected = 400 * (apr_s - apr_l) / (24 * 365)
    amort_expected = 400 * (0.10 / 100 * 2) / (14 * 24)
    r = rows[0]
    ok(abs(r["funding"] - funding_expected) < 1e-9,
       "trickle: funding math exact")
    ok(abs(r["amort"] - amort_expected) < 1e-9,
       "trickle: amortization math exact (RT/14d)")
    ok(abs(r["net"] - (funding_expected - amort_expected)) < 1e-9,
       "trickle: net = funding - amort")
    # cadence guard blocks immediate second run
    rows2 = vp.trickle(con, verbose=False)
    ok(rows2 == [], "trickle: hourly cadence guard")
    # both books aggregated
    con2 = fresh_dbs(engine_pos=base_pos(size=250),
                     p0_pos=base_pos(size=250))
    con2.execute("DELETE FROM kv WHERE key='m5_trickle_last'")
    con2.commit()
    rows = vp.trickle(con2, verbose=False)
    ok(len(rows) == 2 and {r["book"] for r in rows} == {"engine", "p0"},
       "trickle: both books covered")


# ------------------------------------------------------------ helpers
def test_helpers():
    print("helper functions:")
    wire()
    con = fresh_dbs(engine_pos=[
        dict(base_pos()[0], ts_open=iso_days_ago(20), status="closed")])
    age = vp._pair_history_age_days("AAA aster->binance")
    ok(19.9 < age < 20.1, "pair history age from closed position")
    ok(vp._pair_history_age_days("ZZZ new->pair") == 0.0,
       "pair history age: unknown pair = 0 (new)")
    con = fresh_dbs(engine_pos=base_pos(size=250),
                    p0_pos=[dict(base_pos()[0], size=400)])
    exp = vp._venue_exposure(vp._book_positions())
    ok(exp["aster"] == 650 and exp["binance"] == 650,
       "venue exposure aggregates BOTH books")
    dep = sum(float(p["size_usd"]) for p in vp._book_positions())
    ok(dep == 650, "deployed = sum over both books")


def main():
    test_kill_halt()
    test_kill_eval_sources()
    test_gate()
    test_trickle()
    test_helpers()
    print(f"\nALL {PASS_N} CHECKS PASS")


if __name__ == "__main__":
    main()
