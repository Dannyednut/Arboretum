#!/usr/bin/env python3
"""test_desk_basis.py - P5c spot-perp basis scanner tests, network-free.

Covers: basis_math (cross-interval APR, fee RT, one-time basis, breakeven),
basis_row honest-Nones + risk flags, min_hourly_from_series (flip stat),
filters, spot symbol candidates, all 4 spot ticker/book parsers, spot_book
symbol fallback, and a mocked cmd_basis end-to-end writing a schema-stable
artifact with native-funding recompute + basis@size.
Run: python3 scripts/test_desk_basis.py
"""
import asyncio
import json
import os
import sys
import time

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
    print("basis_math:")
    h = 0.0001                                  # 0.01%/h -> 87.6% apr
    m = dk.basis_math(h, 0.0010, 0.0005, 0.001)
    ok("funding_apr", approx(m["funding_apr"], h * 24 * 365))
    ok("f8h", approx(m["f8h"], h * 8))
    ok("rt_two_legs", approx(m["rt_frac"], 2 * (0.0010 + 0.0005)))
    ok("net30_fees_only", approx(m["net30"],
                                 h * 24 * 365 - 0.003 * 365 / 30))
    ok("basis_apr_30d", approx(m["basis_apr"], 0.001 * 365 / 30))
    ok("net30_incl", approx(m["net30_incl_basis"],
                            h * 24 * 365 - 0.003 * 365 / 30
                            + 0.001 * 365 / 30))
    ok("breakeven_d", approx(m["breakeven_days"], 0.003 / (h * 24)))
    # cross-interval: 1h vs 8h venues normalize to the same APR
    ok("ivl_norm", approx(dk.apr_of(dk.hourly_rate(0.0008, 8)),
                          dk.apr_of(dk.hourly_rate(0.0001, 1))))
    # None propagation
    m2 = dk.basis_math(h, None, 0.0005, 0.001)
    ok("fee_none_rt", m2["rt_frac"] is None)
    ok("fee_none_net30", m2["net30"] is None)
    ok("fee_none_incl", m2["net30_incl_basis"] is None)
    ok("h_zero_breakeven", dk.basis_math(0.0, 0.001, 0.0005,
                                         0.001)["breakeven_days"] is None)
    ok("h_none_apr", dk.basis_math(None, 0.001, 0.0005,
                                   0.001)["funding_apr"] is None)


def t_basis_row():
    print("basis_row:")
    r = dk.basis_row("INJ", "binance", "bingx", 100.0, 101.0, 0.0001)
    ok("basis_1pct", approx(r["basis_entry"], 0.01))
    ok("px_div", approx(r["px_div"], 0.01))
    ok("flags_clean", r["flags"] == [])
    ok("spot_fee_table", approx(r["rt_frac"], 2 * (0.0010 + 0.0005)))
    # negative basis -> flag (you PAY the premium)
    r2 = dk.basis_row("INJ", "binance", "bingx", 100.0, 99.0, 0.0001)
    ok("neg_basis_flag", "negative_basis" in r2["flags"])
    ok("neg_basis_val", approx(r2["basis_entry"], -0.01))
    # non-positive funding -> flag
    r3 = dk.basis_row("INJ", "binance", "bingx", 100.0, 101.0, -0.00001)
    ok("funding_neg_flag", "funding_not_positive" in r3["flags"])
    ok("funding_none_flag",
       "funding_not_positive" in dk.basis_row("INJ", "binance", "bingx",
                                              100.0, 101.0,
                                              None)["flags"])
    # unknown perp venue -> fee_unknown flag, no crash
    r4 = dk.basis_row("INJ", "binance", "madvdex", 100.0, 101.0, 0.0001)
    ok("fee_unknown_flag", "fee_unknown" in r4["flags"])
    # price divergence 3% -> suspect flag naming
    r5 = dk.basis_row("INJ", "binance", "bingx", 100.0, 103.0, 0.0001)
    ok("px_div_flag", any(f.startswith("px_divergence_") for f in r5["flags"]))
    # missing prices -> None row (honest, never guessed)
    ok("no_spot_none", dk.basis_row("INJ", "binance", "bingx", None, 101.0,
                                    0.0001) is None)
    ok("no_perp_none", dk.basis_row("INJ", "binance", "bingx", 100.0, 0,
                                    0.0001) is None)


def t_min_hourly():
    print("min_hourly_from_series (flip stat):")
    now = 1_700_000_000_000
    ser = [(now - 3 * 3600_000, 0.0001), (now - 2 * 3600_000, -0.00005),
           (now - 1 * 3600_000, 0.0002), (now, 0.0001)]
    ok("min_all", dk.min_hourly_from_series(ser, 24) == -0.00005)
    ok("min_1h_window", dk.min_hourly_from_series(ser, 1) == 0.0001)
    ok("min_2h_window", dk.min_hourly_from_series(ser, 2) == -0.00005)
    ok("empty_none", dk.min_hourly_from_series([], 24) is None)


def t_filters():
    print("apply_basis_filters:")
    rows = [
        dk.basis_row("INJ", "binance", "bingx", 100.0, 101.0, 0.0002),
        dk.basis_row("INJ", "bybit", "bingx", 100.5, 101.0, 0.0001),
        dk.basis_row("XMR", "okx", "hl", 50.0, 51.0, 0.00015),
        dk.basis_row("LOW", "binance", "bingx", 1.0, 1.001, 0.00005),
    ]
    rows[3]["net30_incl_basis"] = None            # unsortable row
    kept = dk.apply_basis_filters(rows, {"min_fapr": 0.5})
    ok("min_fapr", [r["coin"] for r in kept] == ["INJ", "XMR", "INJ"])
    kept = dk.apply_basis_filters(rows, {"min_basis": 0.009})
    ok("min_basis", all(r["basis_entry"] >= 0.009 for r in kept))
    kept = dk.apply_basis_filters(rows, {"venues": "bingx,hl"})
    ok("perp_venues", all(r["perp_venue"] in ("bingx", "hl") for r in kept))
    kept = dk.apply_basis_filters(rows, {"spot_venues": "binance"})
    ok("spot_venues", all(r["spot_venue"] == "binance" for r in kept))
    kept = dk.apply_basis_filters(rows, {"exclude": "inj"})
    ok("exclude", all(r["coin"] != "INJ" for r in kept))
    kept = dk.apply_basis_filters(rows, {"top": 2})
    ok("top", len(kept) == 2)
    ok("sort_first_inj", kept and kept[0]["coin"] == "INJ"
       and kept[0]["spot_venue"] == "binance")
    ok("none_rows_dropped", dk.apply_basis_filters(
        [None] + rows, {}) == dk.apply_basis_filters(rows, {}))


def t_spot_candidates():
    print("spot_base_candidates:")
    ok("plain", dk.spot_base_candidates("INJ") == ["INJ", "1000INJ"])
    ok("prefixed", dk.spot_base_candidates("1000PEPE") == ["1000PEPE",
                                                           "PEPE"])


# ------------------------------------------------------------- parsers
P_BINANCE_TICK = [{"symbol": "INJUSDT", "bidPrice": "7.65",
                   "askPrice": "7.66"},
                  {"symbol": "INJBUSD", "bidPrice": "7.60",
                   "askPrice": "7.61"},
                  {"symbol": "BAD", "bidPrice": "1", "askPrice": "2"},
                  {"symbol": "XRPUSDT", "bidPrice": "0.5", "askPrice": "x"}]
P_BYBIT_TICK = {"result": {"list": [
    {"symbol": "INJUSDT", "bid1Price": "7.64", "ask1Price": "7.67"},
    {"symbol": "BTCUSDC", "bid1Price": "1", "ask1Price": "1"},
    {"symbol": "XRPUSDT", "bid1Price": "0.5"}]}}
P_OKX_TICK = {"data": [
    {"instId": "INJ-USDT", "bidPx": "7.63", "askPx": "7.68"},
    {"instId": "INJ-USDC", "bidPx": "1", "askPx": "1"},
    {"instId": "-USDT", "bidPx": "1", "askPx": "1"},
    {"instId": "XRP-USDT", "bidPx": "x", "askPx": "0.5"}]}
P_BITGET_TICK = {"data": [
    {"symbol": "INJUSDT", "bidPr": "7.62", "askPr": "7.69"},
    {"symbol": "XRPUSDT", "bidPr": "0.5"},
    {"symbol": "BTCUSDC", "bidPr": "1", "askPr": "1"}]}


def t_ticker_parsers():
    print("spot ticker parsers (USDT-only, malformed skipped):")
    m = dk._parse_spot_tickers("binance", P_BINANCE_TICK)
    ok("binance_map", m == {"INJ": {"bid": 7.65, "ask": 7.66}})
    m = dk._parse_spot_tickers("bybit", P_BYBIT_TICK)
    ok("bybit_map", m == {"INJ": {"bid": 7.64, "ask": 7.67}})
    m = dk._parse_spot_tickers("okx", P_OKX_TICK)
    ok("okx_map", m == {"INJ": {"bid": 7.63, "ask": 7.68}})
    m = dk._parse_spot_tickers("bitget", P_BITGET_TICK)
    ok("bitget_map", m == {"INJ": {"bid": 7.62, "ask": 7.69}})
    ok("unknown_empty", dk._parse_spot_tickers("madvdex", {}) == {})
    ok("junk_empty", dk._parse_spot_tickers("binance", None) == {})


def t_book_parsers():
    print("spot book parsers:")
    b, a = dk._parse_spot_book("binance", {"bids": [["7.65", "10"],
                                                    ["7.64", "0"]],
                                           "asks": [["7.66", "5"]]})
    ok("binance_book", b == [(7.65, 10.0)] and a == [(7.66, 5.0)])
    b, a = dk._parse_spot_book("bybit", {"result": {
        "b": [["7.64", "8"]], "a": [["7.67", "9"]]}})
    ok("bybit_book", b == [(7.64, 8.0)] and a == [(7.67, 9.0)])
    b, a = dk._parse_spot_book("okx", {"data": [{"bids": [["7.63", "7", "0",
                                                          "1"]],
                                                 "asks": [["7.68", "6", "0",
                                                          "1"]]}]})
    ok("okx_book_4col", b == [(7.63, 7.0)] and a == [(7.68, 6.0)])
    b, a = dk._parse_spot_book("bitget", {"data": {"bids": [["7.62", "4"]],
                                                   "asks": [["7.69", "3"]]}})
    ok("bitget_book", b == [(7.62, 4.0)] and a == [(7.69, 3.0)])
    b, a = dk._parse_spot_book("madvdex", {})
    ok("unknown_none", b is None and a is None)


def t_spot_book_fallback():
    print("spot_book symbol fallback (PEPE -> 1000PEPE):")
    calls = []

    async def fake_get(url, params=None, timeout=20):
        calls.append(dict(params or {}))
        sym = (params or {}).get("symbol", "")
        if "1000PEPE" in str(sym):
            return {"bids": [["0.00994", "1000"]],
                    "asks": [["0.00995", "2000"]]}
        raise RuntimeError("not listed")

    orig = dk._get_json
    dk._get_json = fake_get
    try:
        bk = asyncio.run(dk.spot_book("binance", "PEPE"))
    finally:
        dk._get_json = orig
    ok("fallback_found", bk is not None
       and bk[1][0] == (0.00995, 2000.0))
    ok("tried_plain_first", calls and calls[0]["symbol"] == "PEPEUSDT")
    bk2 = asyncio.run(dk.spot_book("madvdex", "INJ"))
    ok("unknown_venue_none", bk2 is None)


# ------------------------------------------------------------- e2e
def t_cmd_basis_mock():
    print("cmd_basis (mocked feeds, e2e artifact):")
    now_ms = int(time.time() * 1000)
    cur = [
        {"coin": "INJ", "venue": "bingx", "rate": 0.0004, "interval_h": 8,
         "mark": 7.70},
        {"coin": "PEPE", "venue": "aster", "rate": 0.0000125,
         "interval_h": 1, "mark": 0.0100},
        {"coin": "INJ", "venue": "somecex", "rate": 0.9, "interval_h": 8,
         "mark": 7.7},                       # non-our venue -> dropped
        {"coin": "NEG", "venue": "binance", "rate": -0.0001, "interval_h": 8,
         "mark": 5.0},                       # negative funding -> dropped
    ]
    spots = {"binance": {"INJ": {"bid": 7.65, "ask": 7.66},
                         "PEPE": {"bid": 0.00990, "ask": 0.00995}},
             "bybit": {"INJ": {"bid": 7.64, "ask": 7.67}},
             "okx": {}, "bitget": {}}

    class FakeSharpe:
        @classmethod
        async def current(cls, coin=None):
            return cur

        @classmethod
        async def history(cls, coin, days=2):
            if coin == "INJ":
                return {"bingx": [(now_ms - 2 * 3600_000, 0.0002, 8),
                                  (now_ms, 0.0004, 8)]}, "asof", False
            if coin == "PEPE":
                return {"aster": [(now_ms - 2 * 3600_000, -0.00001, 1),
                                  (now_ms, 0.0000125, 1)]}, "asof", False
            return {}, "asof", False

    async def fake_funding(v, c):
        if (v, c) == ("bingx", "INJ"):
            return {"rate": 0.00042, "interval_h": 8, "source": "native"}
        return None

    async def fake_pbook(v, c):
        if (v, c) == ("bingx", "INJ"):
            return ([(7.71, 100.0)], [(7.72, 100.0)])
        if (v, c) == ("aster", "PEPE"):
            return ([(0.01005, 50000.0)], [(0.01006, 50000.0)])
        return None

    async def fake_sbook(v, c):
        if (v, c) == ("binance", "INJ"):
            return ([(7.65, 100.0)], [(7.66, 50.0)])
        if (v, c) == ("bybit", "INJ"):
            return ([(7.64, 100.0)], [(7.67, 50.0)])
        if (v, c) == ("binance", "PEPE"):
            return ([(0.00994, 1000.0)], [(0.00995, 2000.0)])
        return None

    orig = (dk.vc.Sharpe, dk.vc.get_funding, dk.pl.get_book,
            dk.spot_ticker_map, dk.spot_book, dk.pp_best_net30_by_coin)
    dk.vc.Sharpe = FakeSharpe
    dk.vc.get_funding = fake_funding
    dk.pl.get_book = fake_pbook
    dk.spot_ticker_map = lambda v: _async(spots.get(v, {}))
    dk.spot_book = fake_sbook
    dk.pp_best_net30_by_coin = lambda: _async({"INJ": 0.15, "PEPE": 0.05})
    args = type("A", (), {"min_fapr": 0.10, "min_basis": None,
                          "venues": None, "spot_venues": None,
                          "exclude": None, "size_usd": 250.0,
                          "max_coins": 25, "scan_keep": 12, "top": 20,
                          "print_rows": 15,
                          "json": "test_basis.json"})()
    try:
        art = asyncio.run(dk.cmd_basis(args))
        ok("kept_3", art["kept"] == 3,
           f"got {art['kept']}: {[(r['coin'], r['spot_venue']) for r in art['rows']]}")
        inj = [r for r in art["rows"] if r["coin"] == "INJ"
               and r["spot_venue"] == "binance"][0]
        ok("native_recompute", approx(inj["funding_apr"],
                                      0.00042 / 8 * 24 * 365))
        ok("basis_at_size", approx(inj["basis_at_size"],
                                   (7.71 - 7.66) / 7.66, 1e-6))
        # slips are measured vs the TRUE mid (half-spread each side here)
        hs = (7.66 - (7.65 + 7.66) / 2) / ((7.65 + 7.66) / 2) * 1e4
        hp = ((7.71 + 7.72) / 2 - 7.71) / ((7.71 + 7.72) / 2) * 1e4
        ok("slips_present", approx(inj["slip_spot_bps"], round(hs, 2))
           and approx(inj["slip_perp_bps"], round(hp, 2)),
           f"got {inj['slip_spot_bps']}/{inj['slip_perp_bps']} "
           f"want {round(hs, 2)}/{round(hp, 2)}")
        ok("pp_col", inj["pp_best_net30"] == 0.15)
        ok("min_f24h", approx(inj["min_f24h"], 0.0002 / 8))
        ok("no_flip_flag", "funding_flip_24h" not in inj["flags"])
        # dedup: (INJ,bingx) context fetched once for both spot rows
        ok("both_spot_rows", len([r for r in art["rows"]
                                  if r["coin"] == "INJ"]) == 2)
        pepe = [r for r in art["rows"] if r["coin"] == "PEPE"][0]
        ok("pepe_sharpe_fallback", approx(pepe["funding_apr"],
                                          0.0000125 * 24 * 365))
        ok("pepe_flip_flag", "funding_flip_24h" in pepe["flags"],
           f"flags={pepe['flags']}")
        # books beat the pass-1 mark: basis_entry from real best bid/ask
        ok("px_src_books", inj["px_src"] == "books")
        ok("basis_from_books", approx(inj["basis_entry"],
                                      (7.71 - 7.66) / 7.66, 1e-9))
        ok("flag_recomputed", inj["flags"] == [],
           f"flags={inj['flags']}")
        byb = [r for r in art["rows"] if r["coin"] == "INJ"
               and r["spot_venue"] == "bybit"][0]
        ok("bybit_books_basis", approx(byb["basis_entry"],
                                       (7.71 - 7.67) / 7.67, 1e-9))
        ok("pepe_books_basis", approx(pepe["basis_entry"],
                                      (0.01005 - 0.00995) / 0.00995, 1e-9))
        p = os.path.join(dk.DESK_DIR, "test_basis.json")
        with open(p) as fh:
            disk = json.load(fh)
        ok("artifact_roundtrip", disk["kept"] == 3)
        need = {"coin", "spot_venue", "perp_venue", "spot_ask", "perp_bid",
                "basis_entry", "funding_apr", "f8h", "rt_frac", "net30",
                "basis_apr", "net30_incl_basis", "breakeven_days",
                "basis_at_size", "min_f1h", "min_f24h", "pp_best_net30",
                "flags", "px_src"}
        ok("schema_keys", need <= set(disk["rows"][0].keys()),
           str(need - set(disk["rows"][0].keys())))
        ok("artifact_note", "no execution path" in disk["note"])
    finally:
        (dk.vc.Sharpe, dk.vc.get_funding, dk.pl.get_book,
         dk.spot_ticker_map, dk.spot_book, dk.pp_best_net30_by_coin) = orig
        p = os.path.join(dk.DESK_DIR, "test_basis.json")
        if os.path.exists(p):
            os.remove(p)


async def _async(v):
    return v


def main():
    t_math()
    t_basis_row()
    t_min_hourly()
    t_filters()
    t_spot_candidates()
    t_ticker_parsers()
    t_book_parsers()
    t_spot_book_fallback()
    t_cmd_basis_mock()
    print(f"\n{N[0]} CHECKS RUN, {FAILS[0]} FAILED")
    if FAILS[0]:
        sys.exit(1)
    print("ALL DESK BASIS TESTS PASS")


if __name__ == "__main__":
    main()
