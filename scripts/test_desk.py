#!/usr/bin/env python3
"""test_desk.py - P5a desk tests, network-free (Part 22 §5).

Covers: combo math (cross-interval), fee drag, net30, pspread, max-spread
bucket join, Sharpe row -> desk row, filters, rt_for_style, show_combo_rows,
and a mocked cmd_scan end-to-end writing a schema-stable artifact.
Run: python3 scripts/test_desk.py
"""
import asyncio
import json
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import v3_desk as dk  # noqa: E402

N = [0]
FAILS = [0]


def ok(name, cond, detail=""):
    N[0] += 1
    tag = "ok" if cond else "FAIL"
    if not cond:
        FAILS[0] += 1
    print(f"  {tag} {name}" + (f" - {detail}" if detail and not cond else ""))
    return cond


def approx(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol


# ------------------------------------------------------------- math
def t_math():
    print("math:")
    # hourly/apr: 0.01% per 8h -> 0.00125%/h -> 10.95% apr
    h = dk.hourly_rate(0.0001, 8)
    ok("hourly_8h", approx(h, 0.0001 / 8))
    ok("apr_10.95", approx(dk.apr_of(h), 0.0001 / 8 * 24 * 365))
    # cross-interval: 1h venue vs 8h venue normalize differently
    h1 = dk.hourly_rate(0.0001, 1)
    ok("hourly_1h", approx(h1, 0.0001))
    # bad intervals -> None
    ok("ivl_zero_none", dk.hourly_rate(0.0001, 0) is None)
    ok("ivl_72_none", dk.hourly_rate(0.0001, 72) is None)
    ok("rate_none", dk.hourly_rate(None, 8) is None)
    # combo math: short 0.01%/8h, long -0.005%/8h
    m = dk.combo_math(h, dk.hourly_rate(-0.00005, 8))
    ok("combo_apr", approx(m["apr"], (h + 0.00005 / 8) * 24 * 365))
    ok("combo_f8h", approx(m["f8h"], (0.0001 + 0.00005) / 8 * 8))
    ok("combo_none", dk.combo_math(h, None)["apr"] is None)
    # fee drag: rt 0.1% over 30d -> 0.1%*365/30 = 1.2167% apr
    ok("fee_drag", approx(dk.fee_drag_apr(0.001), 0.001 * 365 / 30))
    ok("net30", approx(dk.net30_of(0.20, 0.001), 0.20 - 0.001 * 365 / 30))
    ok("net30_none", dk.net30_of(None, 0.001) is None)
    # pspread: sell 101 bid, buy 100 ask -> 1%
    ok("pspread", approx(dk.pspread(101.0, 100.0), 0.01))
    ok("pspread_bad", dk.pspread(None, 100.0) is None)


def t_max_spread():
    print("max_spread_from_series:")
    base = 1_700_000_000_000
    a = [(base + i * 300_000, 0.0001) for i in range(10)]     # 0.01%/h flat
    b = [(base + i * 300_000, 0.00005) for i in range(10)]
    ok("flat_5e-5", approx(dk.max_spread_from_series(a, b, 24), 0.00005))
    # spike in b at bucket 5 -> spread there = 0.0001 - (-0.00005) = 0.00015
    b2 = list(b)
    b2[5] = (base + 5 * 300_000, -0.00005)
    ok("spike_neg", approx(dk.max_spread_from_series(a, b2, 24), 0.00015))
    # spike in a -> max spread 0.0002
    a2 = list(a)
    a2[5] = (base + 5 * 300_000, 0.0002)
    ok("spike_pos", approx(dk.max_spread_from_series(a2, b, 24), 0.00015))
    # 1h window: long series (2h, 5-min buckets) with early spike;
    # window must exclude it -> flat baseline spread
    base2 = 1_800_000_000_000
    a3 = [(base2 + i * 300_000, 0.0001) for i in range(25)]
    b3 = [(base2 + i * 300_000, 0.00005) for i in range(25)]
    a3[2] = (base2 + 2 * 300_000, 0.0009)          # spike at ~10min mark
    ok("window_excludes",
       approx(dk.max_spread_from_series(a3, b3, 1), 0.00005))
    ok("window_24h_includes",
       approx(dk.max_spread_from_series(a3, b3, 24), 0.00085))
    # empty series
    ok("empty_none", dk.max_spread_from_series([], b, 24) is None)
    # misaligned timestamps (no shared 5-min buckets) -> None
    c = [(base + 99_000 + i * 300_000, 0.0001) for i in range(3)]
    ok("misalign_none",
       dk.max_spread_from_series(c, [(base + 1_500_000, 0.00005)], 24) is None)


def t_rt_style():
    print("rt_for_style:")
    # bingx taker 5bps, okx taker 5bps -> tt 10bps = 0.001
    ok("tt", approx(dk.rt_for_style("bingx", "okx", "tt"), 0.001))
    # maker: bingx maker 2bps + okx maker (pl.MAKER inherits taker 5bps -
    # known fee-table divergence, P4b note) -> 0.0007
    ok("mm", approx(dk.rt_for_style("bingx", "okx", "mm"), 0.0007))
    ok("unknown_none", dk.rt_for_style("zzz", "okx", "tt") is None)


def t_scan_row():
    print("scan_row_from_sharpe:")
    med = {("INJ", "bingx", "okx"): 0.187}
    r = {"symbol": "INJ", "shortExchange": "BingX", "longExchange": "OKX",
         "apr": 25.0, "netApr": 24.0, "oiShort": "500000", "oiLong": 300000,
         "executableDepthUsd": "120000", "executionStatus": "executable"}
    row = dk.scan_row_from_sharpe(r, med)
    ok("coin", row["coin"] == "INJ")
    ok("venues_norm", row["short_venue"] == "bingx"
       and row["long_venue"] == "okx")
    ok("gross", approx(row["gross_apr"], 0.25))
    ok("net_sharpe", approx(row["net_sharpe"], 0.24))
    ok("oi_min", approx(row["oi_min_usd"], 300000.0))
    ok("legs_ours", row["legs_ours"] is True)
    ok("median", approx(row["median_apr_30d"], 0.187))
    ok("vs_median", approx(row["vs_median"], 0.25 - 0.187))
    # net_ours = gross - rt_tt*365/30 (bingx 5bps + okx 5bps)
    ok("net_ours", approx(row["net_ours_30d"], 0.25 - 0.001 * 365 / 30))
    # non-our venue legs
    r2 = dict(r, shortExchange="SomeDex", longExchange="OKX")
    row2 = dk.scan_row_from_sharpe(r2, med)
    ok("legs_not_ours", row2["legs_ours"] is False)
    ok("rt_none_kept", row2["rt_tt_frac"] is None)
    # junk row
    ok("junk_none", dk.scan_row_from_sharpe({"symbol": ""}, med) is None)


def t_filters():
    print("apply_filters:")
    rows = [
        {"coin": "AAA", "short_venue": "binance", "long_venue": "okx",
         "gross_apr": 0.3, "net_sharpe": 0.28, "net_ours_30d": 0.25,
         "oi_min_usd": 200000.0, "legs_ours": True},
        {"coin": "BBB", "short_venue": "zzz", "long_venue": "okx",
         "gross_apr": 0.5, "net_sharpe": 0.48, "net_ours_30d": None,
         "oi_min_usd": 50000.0, "legs_ours": False},
        {"coin": "CCC", "short_venue": "aster", "long_venue": "binance",
         "gross_apr": 0.2, "net_sharpe": 0.19, "net_ours_30d": 0.15,
         "oi_min_usd": None, "legs_ours": True},
    ]
    kept = dk.apply_filters(rows, {})
    ok("default_all", len(kept) == 3)
    ok("sorted_by_best", kept[0]["coin"] == "BBB")
    kept = dk.apply_filters(rows, {"min_apr": 0.24})
    ok("min_apr", {r["coin"] for r in kept} == {"AAA", "BBB"})
    kept = dk.apply_filters(rows, {"min_oi": 100000})
    ok("min_oi", {r["coin"] for r in kept} == {"AAA"})
    kept = dk.apply_filters(rows, {"venues": ["binance", "okx"]})
    ok("venues", {r["coin"] for r in kept} == {"AAA"})
    kept = dk.apply_filters(rows, {"ours_only": True})
    ok("ours_only", {r["coin"] for r in kept} == {"AAA", "CCC"})
    kept = dk.apply_filters(rows, {"exclude": "aaa"})
    ok("exclude", {r["coin"] for r in kept} == {"BBB", "CCC"})
    kept = dk.apply_filters(rows, {"top": 1})
    ok("top", len(kept) == 1 and kept[0]["coin"] == "BBB")
    ok("none_rows_skipped",
       len(dk.apply_filters([None] + rows, {})) == 3)


def t_show_combos():
    print("show_combo_rows:")
    fund = {"binance": {"rate": 0.0001, "ivl_h": 8, "next_time": 1,
                        "source": "native"},
            "okx": {"rate": -0.00005, "ivl_h": 8, "next_time": None,
                    "source": "native"},
            "aster": {"rate": 0.0002, "ivl_h": 1, "next_time": None,
                      "source": "native"}}
    books = {"binance": {"bid": 101.0, "ask": 101.1, "mid": 101.05},
             "okx": {"bid": 100.0, "ask": 100.1, "mid": 100.05},
             "aster": None}
    hist = {"binance": [(1_700_000_000_000, 0.0001)],
            "okx": [(1_700_000_000_000, -0.00005)]}
    rows = dk.show_combo_rows(fund, books, hist)
    ok("combo_count", len(rows) == 6)  # 3 venues, ordered pairs
    top = rows[0]
    # best: short aster (2.0bp/h) long okx (-0.00625bp/h) -> 2.00625bp/h
    ok("top_pair", top["short_venue"] == "aster" and top["long_venue"] == "okx")
    ok("top_apr", approx(top["apr"],
                         (0.0002 + 0.00005 / 8) * 24 * 365, tol=1e-6))
    # pspread: bid_s 101? aster has no book -> None for pairs w/ aster
    ok("pspread_no_book", top["pspread"] is None)
    inj = [r for r in rows if r["short_venue"] == "binance"
           and r["long_venue"] == "okx"][0]
    ok("pspread_calc", approx(inj["pspread"], (101.0 - 100.1) / 100.1))
    ok("max_f24h", approx(inj["max_f24h"], 0.0001 + 0.00005))
    ok("next_native", inj["next_short"] == 1)
    ok("net30_tt", approx(inj["net30_tt"], inj["apr"] - 0.001 * 365 / 30))


def t_cmd_scan_mock():
    print("cmd_scan (mocked feed):")
    feed = [
        {"symbol": "INJ", "shortExchange": "BingX", "longExchange": "OKX",
         "apr": 25.0, "netApr": 24.0, "oiShort": 500000, "oiLong": 300000,
         "executableDepthUsd": 120000, "executionStatus": "executable"},
        {"symbol": "XMR", "shortExchange": "DYDX", "longExchange": "Binance",
         "apr": 40.0, "netApr": 39.0, "oiShort": 900000, "oiLong": 800000,
         "executableDepthUsd": None, "executionStatus": "executable"},
    ]

    class FakeSharpe:
        @classmethod
        async def arb_table(cls):
            return feed, "2026-09-21T03:00:00Z", False

    orig = dk.vc.Sharpe
    dk.vc.Sharpe = FakeSharpe
    orig_med = dk.pl.load_medians
    dk.pl.load_medians = lambda: {("XMR", "dydx", "binance"): 0.30}
    args = type("A", (), {"min_apr": None, "min_oi": None, "venues": None,
                          "exclude": None, "ours_only": False, "top": 10,
                          "print_rows": 5, "json": "test_scan.json"})()
    try:
        art = asyncio.run(dk.cmd_scan(args))
        ok("kept_2", art["kept"] == 2)
        ok("asof", art["asof"] == "2026-09-21T03:00:00Z")
        ok("top_xmr", art["rows"][0]["coin"] == "XMR")
        p = os.path.join(dk.DESK_DIR, "test_scan.json")
        with open(p) as fh:
            disk = json.load(fh)
        ok("artifact_roundtrip", disk["kept"] == 2
           and disk["rows"][0]["coin"] == "XMR")
        ok("schema_keys", set(disk["rows"][0].keys()) == {
            "coin", "short_venue", "long_venue", "gross_apr", "net_sharpe",
            "net_ours_30d", "rt_tt_frac", "oi_min_usd", "depth_usd",
            "exec_status", "legs_ours", "median_apr_30d", "vs_median"})
        os.remove(p)
    finally:
        dk.vc.Sharpe = orig
        dk.pl.load_medians = orig_med


def t_walk_size():
    print("walk_size:")
    book = ([(100.0, 10), (99.0, 10)], [(101.0, 5), (102.0, 5)])
    vwap, slip = dk.walk_size(book, "sell", 500, 100.0)
    ok("sell_vwap", vwap is not None and vwap <= 100.0)
    vwap2, slip2 = dk.walk_size(book, "buy", 500, 100.0)
    ok("buy_vwap", vwap2 is not None and vwap2 >= 101.0)
    ok("empty_none", dk.walk_size(None, "sell", 500, 100.0)[0] is None)


def main():
    t_math()
    t_max_spread()
    t_rt_style()
    t_scan_row()
    t_filters()
    t_show_combos()
    t_walk_size()
    t_cmd_scan_mock()
    print(f"\n{N[0]} CHECKS RUN, {FAILS[0]} FAILED")
    if FAILS[0]:
        print("DESK TESTS FAIL")
        raise SystemExit(1)
    print("ALL DESK TESTS PASS")


if __name__ == "__main__":
    main()
