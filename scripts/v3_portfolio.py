#!/usr/bin/env python3
"""v3 M5 portfolio layer (spec Part 12 §3.5, build P4 / Part 16).

Gates / kill / accounting consumed by the M3 engine:

  m5_gate(con, coin, short, long, size_usd, pair_cap)
      -> (ok, gated_size, notes)
      kill file -> sizing (0.7x pair_cap incumbent / 0.5x new, first 14d)
      -> venue exposure caps across BOTH books (tier1 $20k / tier2 $5k)
      -> correlation (<= 2 open pairs sharing the same long coin)
      -> RWA/commodity macro bucket (<= 30% of deployed)
      -> total leverage <= 2.5x equity ($1k phase capital)
  kill_eval(con)   4 spec kill sources -> download/data/kill_switch.json
      1. floor-param change on any basket venue   (M4 FLOOR_PARAM_CHANGE)
      2. 3 held tripwire depth failures in 24h    (TW4, held_check only)
      3. Sharpe-vs-native disagreement spike      (vs M4 health baseline)
      4. paper-drift ratio > 1.5x in last 24h     (exec_drift)
  kill_halt()      read the kill file; missing/stale = HALT (fail-safe)
  trickle(con)     hourly accrual per open pair per book:
                   funding collected - (fee+slip RT) amortized over 14d
  weekly(con)      realized-vs-model report -> pnl_weekly.csv
  state(con)       snapshot -> portfolio_snap

CLI: kill_eval | gate --coin --short --long --size [--cap] | trickle |
     weekly | state
"""
import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import v3_store as st                 # noqa: E402
import paper_ledger as pl             # noqa: E402  (P0 DB, fees)

KILL_FILE = "/home/z/my-project/download/data/kill_switch.json"
KILL_FRESH_S = 3600          # kill file older than this -> re-evaluate
KILL_STALE_S = 21600         # kill file older than this -> HALT
EQUITY_USD = 1000.0          # phase-gated capital (spec §7)
MAX_LEVERAGE = 2.5
TIER1 = {"binance", "okx", "bybit", "bitget", "hl", "aster", "dydx"}
VENUE_CAP = {"tier1": 20000.0, "tier2": 5000.0}
RWA_MAX_FRAC = 0.30
RWA_BASES = {"XAG", "XAU", "CL", "TSLA", "SPX", "NDX", "BTC", "ETH"}
AMORT_DAYS = 14
DISAGREE_MULT = 3.0
DISAGREE_FLOOR = 0.15
DISAGREE_ABS = 0.50
DRIFT_KILL_RATIO = 1.5
TW4_DEPTH_KILLS = 3


def nows():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_tables(con):
    con.execute("""CREATE TABLE IF NOT EXISTS pnl_log(
        pid INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, book TEXT,
        pos_id INTEGER, pair TEXT, coin TEXT, size_usd REAL,
        funding_usd REAL, fee_amort_usd REAL, slip_amort_usd REAL,
        net_usd REAL, note TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS portfolio_snap(
        sid INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, equity_usd REAL,
        deployed_usd REAL, leverage REAL, open_pairs INTEGER,
        venue_exposure TEXT, kill_state TEXT)""")
    con.commit()


# ------------------------------------------------------------ positions
def _book_positions():
    """Open positions across BOTH books: [{book, pos_id, pair, coin,
    short_venue, long_venue, size_usd, ts_open}]."""
    out = []
    con = st.connect()
    try:
        rows = con.execute(
            "SELECT pos_id, pair, coin, short_venue, long_venue, size_usd, "
            "ts_open FROM exec_positions "
            "WHERE status IN ('open','unwinding')").fetchall()
        for r in rows:
            out.append(dict(zip(["pos_id", "pair", "coin", "short_venue",
                                 "long_venue", "size_usd", "ts_open"],
                                r), book="engine"))
    finally:
        con.close()
    try:
        p0 = sqlite3.connect(pl.DB)
        rows = p0.execute(
            "SELECT pos_id, pair, coin, short_venue, long_venue, size_usd, "
            "ts_open FROM positions "
            "WHERE status IN ('open','unwinding')").fetchall()
        p0.close()
        for r in rows:
            out.append(dict(zip(["pos_id", "pair", "coin", "short_venue",
                                 "long_venue", "size_usd", "ts_open"],
                                r), book="p0"))
    except sqlite3.Error:
        pass
    return out


def _venue_exposure(positions):
    exp = {}
    for p in positions:
        for v in (p["short_venue"], p["long_venue"]):
            exp[v] = exp.get(v, 0.0) + float(p["size_usd"] or 0)
    return exp


def _pair_history_age_days(pair):
    """Days since the EARLIEST open (any status) of this pair, either
    book. 0.0 = brand new pair (14d new-pair sizing applies)."""
    oldest = None
    for db, sql in (
            (st.DB, "SELECT MIN(ts_open) FROM exec_positions WHERE pair=?"),
            (pl.DB, "SELECT MIN(ts_open) FROM positions WHERE pair=?")):
        try:
            c = sqlite3.connect(db)
            r = c.execute(sql, (pair,)).fetchone()
            c.close()
            if r and r[0]:
                ts = str(r[0])
                try:
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                age = (datetime.now(timezone.utc) - t).total_seconds() / 86400
                oldest = age if oldest is None else max(oldest, age)
        except sqlite3.Error:
            continue
    return oldest if oldest is not None else 0.0


# ------------------------------------------------------------ kill file
def kill_eval(con, verbose=True):
    """Evaluate the 4 spec kill sources; write kill_switch.json."""
    _ensure_tables(con)
    sources, halt = {}, False

    def check(name, hit, detail):
        nonlocal halt
        sources[name] = {"hit": bool(hit), "detail": detail}
        if hit:
            halt = True

    # 1. floor-param change on any basket venue (last 24h)
    lo24 = datetime.fromtimestamp(time.time() - 86400, timezone.utc)\
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = con.execute(
        "SELECT COUNT(*) FROM events WHERE etype='FLOOR_PARAM_CHANGE' "
        "AND ts > ?", (lo24,)).fetchone()[0]
    check("floor_param_change", rows > 0, f"{rows} events/24h")

    # 2. three held TW4 depth failures in 24h (held_check mode only)
    rows = con.execute(
        "SELECT COUNT(*) FROM tripwire_log WHERE tw='TW4_depth_rewalk' "
        "AND result='fail' AND mode='held_check' AND ts > ?",
        (lo24,)).fetchone()[0]
    check("tripwire_depth_3x24h", rows >= TW4_DEPTH_KILLS,
          f"{rows} held TW4 fails/24h (threshold {TW4_DEPTH_KILLS})")

    # 3. Sharpe-vs-native disagreement spike vs M4 health baseline
    cur_gap = 0.0
    for p in _book_positions():
        vpair = sorted({p["short_venue"], p["long_venue"]})
        aprs = {"native": {}, "sharpe": {}}
        for v in vpair:
            for src, tag in (("native_live", "native"),
                             ("sharpe_live", "sharpe")):
                r = con.execute(
                    "SELECT rate, interval_h FROM funding_obs WHERE "
                    "venue=? AND base=? AND source LIKE ? "
                    "ORDER BY ts DESC LIMIT 1", (v, p["coin"], src))\
                    .fetchone()
                if r and r[1]:
                    aprs[tag][v] = float(r[0]) / float(r[1]) * 24 * 365
        if aprs["native"].keys() == aprs["sharpe"].keys() and \
                aprs["native"]:
            cur_gap = max(cur_gap, abs(sum(aprs["native"].values()) -
                                       sum(aprs["sharpe"].values())))
    base_kv = st.kv_get(con, "m4_disagreement_baseline")
    base_med = DISAGREE_FLOOR
    if base_kv:
        try:
            gaps = sorted(r["gap_apr"] for r in
                          json.loads(base_kv).get("rows", []))
            if gaps:
                base_med = max(gaps[len(gaps) // 2], DISAGREE_FLOOR)
        except (ValueError, KeyError, TypeError):
            pass
    spike = cur_gap > max(DISAGREE_MULT * base_med, DISAGREE_FLOOR) or \
        cur_gap > DISAGREE_ABS
    check("sharpe_native_disagreement", spike,
          f"gap {cur_gap:.3f} vs baseline median {base_med:.3f}")

    # 4. paper-drift ratio > 1.5x in last 24h
    # C2: immediate-class (emergency) exits carry a conservative assumed RT
    # (>= 0.30% floor) and are excluded here; legacy rows (no class tag in
    # detail JSON) count as planned and age out naturally with the 24h window
    rows = con.execute(
        "SELECT COUNT(*) FROM exec_drift WHERE ts > ? AND ratio > ? AND "
        "IFNULL(json_extract(detail,'$.class'),'planned')='planned'",
        (lo24, DRIFT_KILL_RATIO)).fetchone()[0]
    check("drift_over_15x", rows > 0,
          f"{rows} planned samples/24h > 1.5x (immediate class excluded)")

    state = {"ts": nows(), "halt": halt, "sources": sources}
    os.makedirs(os.path.dirname(KILL_FILE), exist_ok=True)
    with open(KILL_FILE, "w") as f:
        json.dump(state, f, indent=1)
    if verbose:
        print(f"kill_eval: halt={halt}")
        for k, v in sources.items():
            print(f"  [{'X' if v['hit'] else ' '}] {k}: {v['detail']}")
    return state


def kill_halt(max_stale_s=KILL_STALE_S):
    """Fail-safe read: missing file or too-old file = HALT."""
    try:
        mt = os.path.getmtime(KILL_FILE)
    except OSError:
        return True, "kill file missing"
    if time.time() - mt > max_stale_s:
        return True, f"kill file stale ({int(time.time() - mt)}s old)"
    try:
        with open(KILL_FILE) as f:
            st8 = json.load(f)
        return bool(st8.get("halt")), \
            f"evaluated {st8.get('ts')}"
    except (ValueError, OSError):
        return True, "kill file unreadable"


def ensure_kill_fresh(con, fresh_s=KILL_FRESH_S):
    """Re-evaluate (cheap SQL) if the kill file is older than fresh_s."""
    try:
        age = time.time() - os.path.getmtime(KILL_FILE)
    except OSError:
        age = None
    if age is None or age > fresh_s:
        kill_eval(con, verbose=False)


# ------------------------------------------------------------ m5 gate
def m5_gate(con, coin, short, long, size_usd, pair_cap=None):
    """Return (ok, gated_size, notes). size_usd = requested per-leg."""
    _ensure_tables(con)
    notes = []

    # 1. kill file
    halt, why = kill_halt()
    if halt:
        notes.append(f"deny: kill halt ({why})")
        return False, 0.0, notes
    ensure_kill_fresh(con)
    halt, why = kill_halt()
    if halt:
        notes.append(f"deny: kill halt ({why})")
        return False, 0.0, notes

    gated = float(size_usd)

    # 2. sizing: 0.7x pair_cap incumbent / 0.5x new (first 14d)
    pair = f"{coin} {short}->{long}"
    age = _pair_history_age_days(pair)
    mult = 0.7 if age >= AMORT_DAYS else 0.5
    if age < AMORT_DAYS:
        notes.append(f"new pair ({age:.1f}d) -> {mult}x sizing")
    if pair_cap is not None and pair_cap > 0:
        cap_size = mult * pair_cap
        if cap_size < gated:
            notes.append(f"sizing clamp ${gated:.0f} -> ${cap_size:.0f} "
                         f"({mult}x pair_cap ${pair_cap:.0f})")
            gated = cap_size

    positions = _book_positions()
    deployed = sum(float(p["size_usd"] or 0) for p in positions)

    # 3. venue caps across BOTH books
    exp = _venue_exposure(positions)
    for v in {short, long}:
        cap = VENUE_CAP["tier1" if v in TIER1 else "tier2"]
        projected = exp.get(v, 0.0) + gated
        if projected > cap:
            notes.append(f"deny: venue cap {v} ${projected:.0f} > ${cap:.0f}")
            return False, 0.0, notes
        notes.append(f"venue {v}: ${exp.get(v, 0):.0f}+{gated:.0f}"
                     f"/${cap:.0f} ok")

    # 4. correlation: <= 2 open pairs sharing the same long coin
    same_long = [p for p in positions if p["coin"] == coin]
    if len(same_long) >= 2:
        notes.append(f"deny: correlation, {len(same_long)} open pairs "
                     f"already long {coin}")
        return False, 0.0, notes

    # 5. RWA/commodity macro bucket <= 30% of deployed
    rwa_deployed = sum(float(p["size_usd"] or 0) for p in positions
                       if p["coin"] in RWA_BASES)
    if coin in RWA_BASES:
        rwa_new = rwa_deployed + gated
        if deployed > 0 and rwa_new > RWA_MAX_FRAC * (deployed + gated):
            notes.append(f"deny: RWA bucket ${rwa_new:.0f} > "
                         f"{RWA_MAX_FRAC:.0%} of deployed "
                         f"${deployed + gated:.0f}")
            return False, 0.0, notes
        notes.append(f"RWA bucket ${rwa_new:.0f} ok")

    # 6. leverage <= 2.5x equity
    lev = (deployed + gated) / EQUITY_USD
    if lev > MAX_LEVERAGE:
        notes.append(f"deny: leverage {lev:.2f}x > {MAX_LEVERAGE}x")
        return False, 0.0, notes
    notes.append(f"leverage {lev:.2f}x ok")

    return True, gated, notes


# ------------------------------------------------------------ trickle
def _latest_pair_apr(con, short, long, coin):
    """Latest live pair spread APR (fraction/yr) from funding_obs."""
    aprs = {}
    for v in (short, long):
        for src in ("native_live", "sharpe_live"):
            r = con.execute(
                "SELECT rate, interval_h FROM funding_obs WHERE venue=? "
                "AND base=? AND source LIKE ? ORDER BY ts DESC LIMIT 1",
                (v, coin, src)).fetchone()
            if r and r[1]:
                aprs[v] = float(r[0]) / float(r[1]) * 24 * 365
                break
    if short in aprs and long in aprs:
        return aprs[short] - aprs[long]
    return None


def trickle(con, verbose=True):
    """Hourly accrual per open pair per book -> pnl_log rows.

    funding_usd   = size x spread_apr / (24x365)  [1h slice]
    fee_amort     = entry RT cost x size / (14d x 24h)
    slip_amort    = exit RT slip estimate x size / (14d x 24h)
    net_usd       = funding - fee_amort - slip_amort
    """
    _ensure_tables(con)
    last = st.kv_get(con, "m5_trickle_last")
    if last is not None and time.time() - float(last) < 3600:
        if verbose:
            print("trickle: cadence guard (hourly), skip")
        return []
    st.kv_set(con, "m5_trickle_last", str(time.time()))
    rows_out = []
    ts = nows()
    for p in _book_positions():
        size = float(p["size_usd"] or 0)
        if size <= 0:
            continue
        spread = _latest_pair_apr(con, p["short_venue"], p["long_venue"],
                                  p["coin"])
        if spread is None:
            funding = 0.0
            note = "no funding_obs coverage"
        else:
            funding = size * spread / (24 * 365)
            note = f"spread {spread:.3f} APR"
        # RT cost proxy: entry one-way cost x2 (fee+slip), 14d amortization
        entry_cost = _entry_cost_rt(p)
        amort = size * entry_cost / (AMORT_DAYS * 24)
        net = funding - amort
        con.execute(
            "INSERT INTO pnl_log(ts, book, pos_id, pair, coin, size_usd, "
            "funding_usd, fee_amort_usd, slip_amort_usd, net_usd, note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ts, p["book"], p["pos_id"], p["pair"], p["coin"], size,
             round(funding, 6), round(amort / 2, 6), round(amort / 2, 6),
             round(net, 6), note))
        rows_out.append({"book": p["book"], "pair": p["pair"],
                         "funding": funding, "amort": amort, "net": net})
    con.commit()
    if verbose:
        for r in rows_out:
            print(f"trickle {r['book']:6s} {r['pair']:<22s} "
                  f"funding ${r['funding']:.4f} amort ${r['amort']:.4f} "
                  f"net ${r['net']:.4f}/h")
        print(f"trickle: {len(rows_out)} rows")
    return rows_out


def _entry_cost_rt(p):
    """RT cost fraction (one-way x2) from the position's own record;
    falls back to 0.20% (spec ballpark) if unavailable."""
    pair = p["pair"]
    for db in (st.DB, pl.DB):
        try:
            c = sqlite3.connect(db)
            r = c.execute("SELECT entry_cost_pct FROM positions "
                          "WHERE pair=? AND status='open'", (pair,))\
                .fetchone() if db == pl.DB else \
                c.execute("SELECT entry_cost_pct FROM exec_positions "
                          "WHERE pair=? AND status='open'",
                          (pair,)).fetchone()
            c.close()
            if r and r[0]:
                return float(r[0]) / 100 * 2
        except sqlite3.Error:
            continue
    return 0.002


# ------------------------------------------------------------ reports
def weekly(con, verbose=True):
    """Realized (trickle net) vs model (funding) last 7d -> CSV."""
    _ensure_tables(con)
    lo7 = datetime.fromtimestamp(time.time() - 7 * 86400, timezone.utc)\
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = con.execute(
        "SELECT book, pair, coin, SUM(funding_usd), SUM(fee_amort_usd), "
        "SUM(slip_amort_usd), SUM(net_usd), COUNT(*) FROM pnl_log "
        "WHERE ts > ? GROUP BY book, pair ORDER BY book, pair",
        (lo7,)).fetchall()
    path = "/home/z/my-project/download/data/pnl_weekly.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["book", "pair", "coin", "funding_usd", "fee_amort_usd",
                    "slip_amort_usd", "net_usd", "rows"])
        for r in rows:
            w.writerow([round(x, 6) if isinstance(x, float) else x
                        for x in r])
    if verbose:
        print(f"weekly: {len(rows)} pair-book rows -> {path}")
        for r in rows:
            print(f"  {r[0]:6s} {r[1]:<22s} funding ${r[3]:.4f} "
                  f"net ${r[6]:.4f} ({r[7]} rows)")
    return rows


def state(con, verbose=True):
    """Portfolio snapshot -> portfolio_snap + stdout."""
    _ensure_tables(con)
    positions = _book_positions()
    deployed = sum(float(p["size_usd"] or 0) for p in positions)
    exp = _venue_exposure(positions)
    halt, why = kill_halt()
    snap = {"ts": nows(), "equity_usd": EQUITY_USD,
            "deployed_usd": round(deployed, 2),
            "leverage": round(deployed / EQUITY_USD, 3),
            "open_pairs": len(positions),
            "venue_exposure": {k: round(v, 2) for k, v in
                               sorted(exp.items())},
            "kill": f"halt={halt} ({why})"}
    con.execute(
        "INSERT INTO portfolio_snap(ts, equity_usd, deployed_usd, "
        "leverage, open_pairs, venue_exposure, kill_state) "
        "VALUES(?,?,?,?,?,?,?)",
        (snap["ts"], snap["equity_usd"], snap["deployed_usd"],
         snap["leverage"], snap["open_pairs"],
         json.dumps(snap["venue_exposure"]), snap["kill"]))
    con.commit()
    if verbose:
        print(f"equity ${snap['equity_usd']:.0f} | "
              f"deployed ${snap['deployed_usd']:.0f} | "
              f"leverage {snap['leverage']}x | kill: {snap['kill']}")
        for p in positions:
            print(f"  [{p['book']}] #{p['pos_id']} {p['pair']:<22s} "
                  f"${p['size_usd']:.0f}")
        for v, e in snap["venue_exposure"].items():
            cap = VENUE_CAP["tier1" if v in TIER1 else "tier2"]
            print(f"  {v:<10s} ${e:>8.2f} / ${cap}")
    return snap


# ------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["kill_eval", "gate", "trickle",
                                    "weekly", "state"])
    ap.add_argument("--coin"); ap.add_argument("--short")
    ap.add_argument("--long", dest="long_v")
    ap.add_argument("--size", type=float, default=250.0)
    ap.add_argument("--cap", type=float, default=None)
    a = ap.parse_args()
    con = st.connect()
    if a.cmd == "kill_eval":
        kill_eval(con)
    elif a.cmd == "gate":
        if not (a.coin and a.short and a.long_v):
            raise SystemExit("gate needs --coin --short --long --size")
        ok, size, notes = m5_gate(con, a.coin.upper(), a.short.lower(),
                                  a.long_v.lower(), a.size, a.cap)
        for n in notes:
            print(f"  {n}")
        print(f"gate: {'PASS' if ok else 'DENY'} size ${size:.0f}")
    elif a.cmd == "trickle":
        trickle(con)
    elif a.cmd == "weekly":
        weekly(con)
    elif a.cmd == "state":
        state(con)
    con.close()


if __name__ == "__main__":
    main()
