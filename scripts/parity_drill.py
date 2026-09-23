#!/usr/bin/env python3
"""parity_drill.py - deterministic P4a parity drill on a TEMP DB copy.

Real v3.db is never touched.  Drill:
  1. copy download/data/v3.db -> tmpdir/v3.db, patch st.DB / ex.V3DB
  2. swap in LiveAdapter (DRY_RUN) with a tmpdir intent log
  3. engine-path coverage of all four seam points:
     a. cmd_unwind(pos 5) + 2 ticks  -> place_taker via unwind tranche
        (bingx/okx legs are taker-style: exercises the C1-clamp branch too)
     b. post_maker(...) directly     -> place_maker  (resting order row)
     c. order_fill(...) on that row  -> on_maker_fill (maker fill row)
  4. parity_report.join_parity over new fills vs intent rows -> PASS/FAIL

All fills/intents/orders land in the TEMP db/log; the real ledger is
append-only w.r.t. this drill (nothing written to download/data/).
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

REAL_DB = os.path.join(HERE, "..", "download", "data", "v3.db")

import v3_store as st                # noqa: E402
import v3_exec_adapter as exa        # noqa: E402
import parity_report as pr           # noqa: E402


def main():
    pos_id = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    with tempfile.TemporaryDirectory(prefix="p4a_drill_") as td:
        tmp_db = os.path.join(td, "v3.db")
        print(f"drill db: {tmp_db}")
        shutil.copy(REAL_DB, tmp_db)
        st.DB = tmp_db                    # patch store module
        import v3_exec as ex
        ex.V3DB = tmp_db                  # patch engine module
        ilog = os.path.join(td, "intents.jsonl")
        ex.set_adapter(exa.make_adapter(
            "live", intent_log=exa.IntentLog(ilog)))

        con = ex.econ()
        since_fid = con.execute(
            "SELECT COALESCE(MAX(fid),0) FROM paper_fills").fetchone()[0]
        pos = dict(con.execute(
            "SELECT * FROM exec_positions WHERE pos_id=?",
            (pos_id,)).fetchone())
        n_orders0 = con.execute(
            "SELECT COUNT(*) FROM exec_orders").fetchone()[0]
        print(f"pos #{pos_id}: {pos['pair']} status={pos['status']} "
              f"since_fid={since_fid} orders0={n_orders0}")

        # a. unwind ladder through the adapter (taker legs)
        asyncio.run(ex.cmd_unwind(pos_id, "p4a parity drill"))
        for i in range(2):
            print(f"--- drill tick {i + 1} ---")
            asyncio.run(ex.tick())

        # b/c. maker seam through the engine functions (resting + fill)
        pos = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                               (pos_id,)).fetchone())
        oid = ex.post_maker(con, pos, "short", pos["short_venue"], "buy",
                            1.0, 5.0, 5.0, "p4a drill maker post")
        o = dict(con.execute("SELECT * FROM exec_orders WHERE oid=?",
                             (oid,)).fetchone())
        asyncio.run(ex.order_fill(con, pos, o, 5.001, "p4a drill fill"))
        con.close()

        fills = pr.load_fills(tmp_db, since_fid)
        intents = pr.load_intents(ilog)
        rep = pr.join_parity(fills, intents)
        print(pr.render(rep))
        good = (not rep["unmatched_fills"]
                and not rep["leftover_intents"]
                and rep["fills_total"] >= 3)   # >=2 unwind takers + 1 maker
        print(f"DRILL PARITY: {'PASS' if good else 'FAIL'} "
              f"(fills={rep['fills_total']}, matched={rep['matched']})")
        sys.exit(0 if good else 1)


if __name__ == "__main__":
    main()
