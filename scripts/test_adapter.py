#!/usr/bin/env python3
"""test_adapter.py - P4a execution adapter seam tests.

Covers:
  protocol/selection   make_adapter, unknown-mode refusal, P4b real-mode
                       refusal, protocol conformance
  parity invariant     LiveAdapter DRY_RUN returns results bit-identical
                       to PaperAdapter for the same book/failure paths
  rule checks          qty step, min qty, min notional, price sanity,
                       fee-table disagreement (pl vs vc)
  intent log           JSONL rows for taker/maker ops, append-only across
                       instances; silent PaperAdapter (no legacy drift)
  engine wiring        v3_exec.py source calls the adapter at all four
                       seam points + --mode flag + set_adapter in main
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import v3_exec_adapter as exa        # noqa: E402
import depth_sampler as ds           # noqa: E402
import paper_ledger as pl            # noqa: E402

FAILED = []


def ok(name, cond, extra=""):
    print(f"  {'ok' if cond else 'FAIL'} {name}"
          f"{'' if cond else f'  <- {extra}'}")
    if not cond:
        FAILED.append(name)


BOOK = ([(99.5, 2), (99.4, 3)], [(100.5, 2), (100.6, 3)])


def read_rows(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def test_protocol():
    print("protocol + selection:")
    pa = exa.make_adapter("paper")
    la = exa.make_adapter("live")
    ok("paper_is_adapter", isinstance(pa, exa.ExecutionAdapter))
    ok("live_is_adapter", isinstance(la, exa.ExecutionAdapter))
    ok("live_dry_run_default", la.dry_run is True)
    ok("paper_mode_name", pa.mode == "paper" and la.mode == "live")
    try:
        exa.make_adapter("yolo")
        ok("unknown_mode_refused", False, "no raise")
    except ValueError:
        ok("unknown_mode_refused", True)
    try:
        exa.LiveAdapter(dry_run=False)
        ok("real_mode_refused_p4a", False, "no raise")
    except RuntimeError as e:
        ok("real_mode_refused_p4a", "P4b" in str(e))
    # protocol completeness: P4b surface exists and refuses
    try:
        la.order_status("okx", 1)
        ok("p4b_stub_refuses", False, "no raise")
    except NotImplementedError:
        ok("p4b_stub_refuses", True)


def test_parity():
    print("parity invariant (live DRY_RUN == paper):")
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "i.jsonl")
        pa = exa.make_adapter("paper")
        la = exa.make_adapter("live", intent_log=exa.IntentLog(p))
        for side, usd in (("buy", 500.0), ("sell", 300.0), ("buy", 50.0)):
            rp = pa.place_taker("okx", "INJ", side, usd, BOOK, "t")
            rl = la.place_taker("okx", "INJ", side, usd, BOOK, "t")
            ok(f"parity_{side}_{usd}", rp == rl, f"{rp} vs {rl}")
        # failure paths identical too
        for book in (None, (None, None)):
            rp = pa.place_taker("okx", "INJ", "buy", 500.0, book)
            rl = la.place_taker("okx", "INJ", "buy", 500.0, book)
            ok("parity_nobook", rp == rl and rp["ok"] is False,
               f"{rp} vs {rl}")
        # walk math is the legacy math (ds.walk)
        slip, filled, vwap = ds.walk(BOOK[1], 100.0, 500.0)
        r = pa.place_taker("okx", "INJ", "buy", 500.0, BOOK)
        ok("paper_walk_math", abs(r["qty"] - filled / vwap) < 1e-12
           and abs(r["px"] - vwap) < 1e-12
           and abs(r["slip_bps"] - slip) < 1e-12)
        ok("paper_fee_table", abs(r["fee_bps"]
                                  - pl.TAKER["okx"] * 1e4) < 1e-9)


def test_intent_log():
    print("intent log:")
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "i.jsonl")
        pa = exa.make_adapter("paper")          # no log: legacy silence
        la = exa.make_adapter("live", intent_log=exa.IntentLog(p))
        pa.place_taker("okx", "INJ", "buy", 500.0, BOOK)
        ok("paper_silent", not os.path.exists(p))
        la.place_taker("okx", "INJ", "buy", 500.0, BOOK, "entry seq tt")
        rows = read_rows(p)
        ok("taker_intent_row", len(rows) == 1
           and rows[0]["op"] == "taker"
           and rows[0]["mode"] == "live"
           and rows[0]["dry_run"] is True
           and rows[0]["venue"] == "okx"
           and abs(rows[0]["qty"] - 500.0 / rows[0]["px"]) < 0.02
           and "entry seq tt" in rows[0]["note"], str(rows))
        # append-only across instances
        exa.IntentLog(p).write({"op": "marker"})
        rows = read_rows(p)
        ok("append_only", len(rows) == 2 and rows[1]["op"] == "marker")
        # maker ops
        la.place_maker("binance", "UAI", "buy", 12.3456, 0.52, 6.42,
                       "unwind tranche")
        la.on_maker_fill("binance", "UAI", "buy", 12.3456, 0.52, 77,
                         "maker fill observed")
        rows = read_rows(p)
        ops = [r["op"] for r in rows]
        ok("maker_ops_logged", "maker_post" in ops and "maker_fill" in ops,
           str(ops))
        post = next(r for r in rows if r["op"] == "maker_post")
        fill = next(r for r in rows if r["op"] == "maker_fill")
        ok("maker_oid_passthru", fill.get("oid_external") == 77)
        ok("maker_fee_fields", "fee_paper_bps" in post
           and "fee_live_bps" in post, str(post))


def test_rules():
    print("rule checks (DRY_RUN advisory):")
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "i.jsonl")
        la = exa.make_adapter("live", intent_log=exa.IntentLog(p))
        # bingx qty step 0.1: walk qty ~4.975 is off-step
        la.place_taker("bingx", "XMR", "buy", 500.0, BOOK)
        row = read_rows(p)[-1]
        ok("qty_step_flag", any(c.startswith("qty_step:")
                                for c in row["checks"]), str(row["checks"]))
        # nado min notional 1.0 / min qty 0.01: 0.5 usd ticket
        la.place_taker("nado", "LINK", "buy", 0.5, BOOK)
        row = read_rows(p)[-1]
        ok("min_notional_flag", any("min_notional:REJECT" in c
                                    for c in row["checks"]),
           str(row["checks"]))
        ok("min_qty_flag", any("min_qty:REJECT" in c
                               for c in row["checks"]), str(row["checks"]))
        # price sanity: sweep into a far-outlier bid level
        bad_book = ([(100.0, 1), (50.0, 1000)], [(100.5, 1)])
        la.place_taker("okx", "INJ", "sell", 500.0, bad_book)
        row = read_rows(p)[-1]
        ok("px_sanity_flag", any(c.startswith("px_sanity:")
                                 for c in row["checks"]),
           str(row["checks"]))
        # fee disagreement: MAKER tables differ binance pl 5bps vs vc 2bps
        la.place_maker("binance", "UAI", "sell", 100.0, 0.52, 52.0)
        row = read_rows(p)[-1]
        ok("fee_disagree_flag", any(c.startswith("fee_disagree:")
                                    for c in row["checks"]),
           str(row["checks"]))
        # unknown venue degrades to note, never raises
        la.place_taker("newvenue", "XYZ", "buy", 500.0, BOOK)
        row = read_rows(p)[-1]
        ok("unknown_venue_note", "rules:unknown-venue" in row["checks"],
           str(row["checks"]))
        # DRY_RUN never alters the result: rules are advisory only
        rp = exa.make_adapter("paper").place_taker("bingx", "XMR", "buy",
                                                   500.0, BOOK)
        rl = la.place_taker("bingx", "XMR", "buy", 500.0, BOOK)
        ok("rules_advisory_only", rp == rl)


def test_wiring():
    print("engine wiring (source-level):")
    src = open(os.path.join(HERE, "v3_exec.py"), encoding="utf-8").read()
    ok("take_taker_uses_adapter", "ADAPTER.place_taker" in src
       and "ds.walk(levels, mid, usd)" not in src)
    ok("unwind_uses_adapter", src.count("ADAPTER.place_taker") >= 2
       and "r = ds.walk(levels, mid, tranche)" not in src)
    ok("post_maker_uses_adapter", "ADAPTER.place_maker" in src)
    ok("order_fill_uses_adapter", "ADAPTER.on_maker_fill" in src)
    ok("mode_flag", '"--mode"' in src and 'choices=["paper", "live"]' in src)
    ok("main_sets_adapter", "set_adapter(exa.make_adapter(a.mode))" in src)
    ok("default_paper_import_time", "make_adapter(\"paper\")" in src)
    ok("no_taker_fee_left_in_engine",
       "fee = TAKER.get(venue, 0.0005) * 1e4" not in src)


def main():
    print("=== test_adapter (P4a seam) ===")
    test_protocol()
    test_parity()
    test_intent_log()
    test_rules()
    test_wiring()
    n = 33
    print(f"\n{33 - len(FAILED)}/33 CHECKS PASS")
    if FAILED:
        print("FAILED:", ", ".join(FAILED))
        sys.exit(1)
    print("ALL ADAPTER TESTS PASS")


if __name__ == "__main__":
    main()
