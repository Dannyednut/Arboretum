#!/usr/bin/env python3
"""Reconstruct exec position #5 + its 2 fills after sandbox rollback #3.

The /tmp mirror snapshot of download/data/v3.db predates window 23 (Sep 16
06:37Z), so the OPEN position #5 (INJ bingx->okx) and its two taker fills were
lost. All facts below are transcribed from worklog Task 37 + Part 20 docs:

  - pos #5: INJ bingx->okx, $250/leg, both taker, opened 2026-09-16T06:37:50Z
  - qty 46.2706 per leg, entry px 5.403 (short bingx sell) / 5.404 (long okx buy)
  - entry RT 0.128%, median APR 0.187 (sharpe src)
  - fees: bingx taker 5 bps, okx taker 5 bps; INJ funding interval 8h both legs

Rows are marked 'reconstructed after rollback #3 (worklog T37 facts)' in
notes so they are auditable. Idempotent: skips if pos_id=5 already present.
"""
import sqlite3

DB = "/home/z/my-project/download/data/v3.db"
NOTE = "reconstructed after rollback #3 (worklog T37 facts)"

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

if con.execute("SELECT COUNT(*) FROM exec_positions WHERE pos_id=5").fetchone()[0]:
    print("pos_id=5 already present - nothing to do")
    raise SystemExit(0)

con.execute(
    "INSERT INTO exec_positions(pos_id, ts_open, pair, coin, short_venue,"
    " long_venue, size_usd, qty, status, entry_cost_pct, median_apr,"
    " pair_cap_usd, flags, unwind_reason, ts_close, reason_close)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (5, "2026-09-16T06:37:50Z", "INJ bingx->okx", "INJ", "bingx", "okx",
     250, 46.2706, "OPEN", 0.128, 0.187, None, "{}", None, None, None))

for leg, venue, side, px in (("short", "bingx", "sell", 5.403),
                             ("long", "okx", "buy", 5.404)):
    con.execute(
        "INSERT INTO paper_fills(ts,pair,leg,venue,side,style,target_qty,"
        "sim_fill_px,sim_slip_bps,fee_bps,funding_interval,notes)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-09-16T06:37:50Z", "INJ bingx->okx", leg, venue, side, "taker",
         46.2706, px, None, 5.0, 8.0, NOTE))

con.commit()
p = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=5").fetchone())
f = con.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
print("pos5:", {k: p[k] for k in ("pos_id", "pair", "status", "qty",
                                  "size_usd", "entry_cost_pct")})
print("fills now:", f)
con.close()
