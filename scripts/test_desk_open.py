#!/usr/bin/env python3
"""test_desk_open.py - P5b desk-open tests, network-free (Part 22 s.6).

Covers: pin validation (schema/TTL/venue/size/style), pin->pair mapping,
combo resolution, preflight refusals, pin file round-trip, engine load_pin
(missing/stale/malformed/oversize), engine_pairs pin-prepend + dedupe,
new_position src audit (source-level), cmd_open artifact schema via
monkeypatched gather_show.

Run: python3 scripts/test_desk_open.py
"""
import asyncio
import inspect
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import v3_desk as dk          # noqa: E402
import v3_exec as ex          # noqa: E402
import v3_portfolio as pf     # noqa: E402  (kill-file isolation)

N = [0]
FAILS = [0]


def ok(name, cond, detail=""):
    N[0] += 1
    tag = "ok" if cond else "FAIL"
    if not cond:
        FAILS[0] += 1
    print(f"  {tag} {name}" + (f" - {detail}" if detail and not cond else ""))
    return cond


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ago(hours):
    ts = time.time() - hours * 3600
    return datetime.fromtimestamp(ts, tz=timezone.utc)\
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def good_pin(**over):
    p = {"schema": 1, "created": iso_now(), "ttl_h": 24.0, "coin": "INJ",
         "short": "bingx", "long": "okx", "size_usd": 250.0,
         "short_style": "taker", "median_apr": 0.187,
         "provenance": {"apr": 0.34}, "preflight": {"refusals": []}}
    p.update(over)
    return p


TAKER = {"bingx": 0.0005, "okx": 0.0005, "binance": 0.00045,
         "aster": 0.0004, "bybit": 0.00055, "bitget": 0.0006}
NOW = time.time()


# ------------------------------------------------------------- validate_pin
def t_validate():
    print("validate_pin:")
    o, why = dk.validate_pin(good_pin(), TAKER, NOW)
    ok("happy", o, why)
    ok("bad_schema", not dk.validate_pin(good_pin(schema=2), TAKER, NOW)[0])
    ok("not_dict", not dk.validate_pin("x", TAKER, NOW)[0])
    ok("missing_coin", not dk.validate_pin(good_pin(coin=""), TAKER, NOW)[0])
    ok("same_legs", not dk.validate_pin(good_pin(long="bingx"), TAKER,
                                        NOW)[0])
    o, why = dk.validate_pin(good_pin(short="kraken"), TAKER, NOW)
    ok("unknown_venue", not o and "coverage" in why, why)
    ok("bad_style", not dk.validate_pin(good_pin(short_style="sniper"),
                                        TAKER, NOW)[0])
    ok("size_zero", not dk.validate_pin(good_pin(size_usd=0), TAKER,
                                        NOW)[0])
    ok("size_neg", not dk.validate_pin(good_pin(size_usd=-5), TAKER,
                                       NOW)[0])
    o, why = dk.validate_pin(good_pin(size_usd=501), TAKER, NOW)
    ok("size_over_cap", not o and "outside" in why, why)
    ok("size_at_cap", dk.validate_pin(good_pin(size_usd=500), TAKER,
                                      NOW)[0])
    ok("size_str_ok", dk.validate_pin(good_pin(size_usd="250"), TAKER,
                                      NOW)[0])
    ok("bad_created", not dk.validate_pin(good_pin(created="yesterday"),
                                          TAKER, NOW)[0])
    ok("fresh_ago", dk.validate_pin(good_pin(created=iso_ago(23.9)), TAKER,
                                    NOW)[0])
    o, why = dk.validate_pin(good_pin(created=iso_ago(24.01)), TAKER, NOW)
    ok("stale_ttl", not o and "TTL" in why, why)
    # boundary: just under TTL -> valid (strictly-greater refusal; exact
    # 24h0m0s is racy against validate-time)
    ok("ttl_boundary", dk.validate_pin(good_pin(created=iso_ago(23.999)),
                                       TAKER, NOW)[0])


# ------------------------------------------------------------- pin mapping
def t_mapping():
    print("pin_pair_dict:")
    d = dk.pin_pair_dict(good_pin())
    ok("pair_name", d["pair"] == "INJ bingx->okx", d["pair"])
    ok("seq_tt", d["seq"] == "tt")
    ok("src_pin", d["src"] == "desk_pin")
    ok("median_pass", d["median_apr"] == 0.187)
    ok("size_float", d["size_usd"] == 250.0)
    d2 = dk.pin_pair_dict(good_pin(short_style="maker", coin="inj"))
    ok("seq_sm_lt", d2["seq"] == "sm_lt")
    ok("coin_upper", d2["pair"] == "INJ bingx->okx", d2["pair"])


# ---------------------------------------------------------- resolve_combo
def t_resolve():
    print("resolve_combo:")
    combos = [
        {"short_venue": "bingx", "long_venue": "okx", "apr": 0.30,
         "net30_tt": 0.20, "legs_ours": True, "pspread": 0.001},
        {"short_venue": "aster", "long_venue": "binance", "apr": 0.50,
         "net30_tt": 0.30, "legs_ours": True, "pspread": 0.002},
        {"short_venue": "bybit", "long_venue": "okx", "apr": 0.90,
         "net30_tt": 0.60, "legs_ours": False, "pspread": 0.003},
        {"short_venue": "hl", "long_venue": "bybit", "apr": 1.20,
         "net30_tt": 1.00, "legs_ours": True, "pspread": None},
        {"short_venue": "binance", "long_venue": "bitget", "apr": -0.10,
         "net30_tt": -0.20, "legs_ours": True, "pspread": 0.001},
        {"short_venue": "okx", "long_venue": "bingx", "apr": 0.25,
         "net30_tt": None, "legs_ours": True, "pspread": 0.001},
    ]
    c = dk.resolve_combo(combos)
    ok("best_ours_net30", c and c["short_venue"] == "aster", str(c))
    ok("no_book_excluded", not (c and c["short_venue"] == "hl"))
    c = dk.resolve_combo(combos, "bybit", "okx")
    ok("explicit_non_ours_allowed", c and c["short_venue"] == "bybit")
    ok("explicit_missing", dk.resolve_combo(combos, "bybit",
                                             "binance") is None)
    ok("none_executable", dk.resolve_combo(combos[2:]) is None)
    ok("empty", dk.resolve_combo([]) is None)


# ------------------------------------------------------------- preflight
def t_preflight():
    print("preflight_refusals:")
    combo = {"short_venue": "bingx", "long_venue": "okx", "apr": 0.30}
    base = {"funding_ok": True, "books_ok": True}
    ok("clean", dk.preflight_refusals(combo, dict(base, frozen=set())) == [])
    r = dk.preflight_refusals(combo, dict(base, kill=True, kill_src="drift",
                                          frozen=set()))
    ok("kill", len(r) == 1 and "kill-switch" in r[0], str(r))
    r = dk.preflight_refusals(combo, dict(base, frozen={"okx"}))
    ok("frozen", len(r) == 1 and "frozen" in r[0], str(r))
    r = dk.preflight_refusals(combo, dict(base, frozen=set(), active=True,
                                          cooldown=True))
    ok("active+cooldown", len(r) == 2, str(r))
    r = dk.preflight_refusals(combo, {"frozen": set(), "funding_ok": False,
                                      "books_ok": False})
    ok("data_missing", len(r) == 2, str(r))
    # fail-safe: unknown data presence keys count as missing
    r = dk.preflight_refusals(combo, {"frozen": set()})
    ok("ctx_defaults_refuse", len(r) == 2, str(r))
    r = dk.preflight_refusals(dict(combo, apr=None),
                              dict(base, frozen=set()))
    ok("apr_none", len(r) == 1, str(r))
    r = dk.preflight_refusals(dict(combo, apr=-0.05),
                              dict(base, frozen=set()))
    ok("apr_negative", len(r) == 1, str(r))


# ------------------------------------------------------------- file IO
def t_pin_file():
    print("write/load round-trip:")
    pin = good_pin()
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "desk", "pin.json")
        dk.write_pin(p, pin)
        loaded = json.load(open(p))
        ok("round_trip", loaded["coin"] == "INJ")
        o, why = dk.validate_pin(loaded, TAKER, time.time())
        ok("loaded_valid", o, why)
        # engine load_pin against patched path
        old = ex.PIN_PATH
        try:
            ex.PIN_PATH = p
            d, why = ex.load_pin()
            ok("engine_load", d is not None and d["pair"] == "INJ bingx->okx",
               why)
            # stale
            dk.write_pin(p, good_pin(created=iso_ago(25)))
            d, why = ex.load_pin()
            ok("engine_stale", d is None and "TTL" in why, why)
            # malformed
            with open(p, "w") as fh:
                fh.write("{not json")
            d, why = ex.load_pin()
            ok("engine_malformed", d is None and "unreadable" in why, why)
            # missing
            os.remove(p)
            d, why = ex.load_pin()
            ok("engine_missing", d is None and "no pin" in why, why)
        finally:
            ex.PIN_PATH = old


# ---------------------------------------------------------- engine_pairs
def t_engine_pairs():
    print("engine_pairs pin handling:")
    pin = good_pin()                     # INJ bingx->okx collides w/ static
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "pin.json")
        dk.write_pin(p, pin)
        old_path, old_mode = ex.PIN_PATH, ex.DESK_PIN_MODE
        try:
            ex.PIN_PATH = p
            ex.set_pin_mode(True)
            pairs = ex.engine_pairs()
            names = [x["pair"] for x in pairs]
            ok("pin_first", names[0] == "INJ bingx->okx", str(names[:2]))
            ok("dedupe", names.count("INJ bingx->okx") == 1, str(names))
            ok("statics_present", "XMR dydx->binance" in names)
            ok("static_src", pairs[-1]["src"] == "static")
            pin_pair = pairs[0]
            ok("pin_src", pin_pair["src"] == "desk_pin")
            ok("pin_seq", pin_pair["seq"] == "tt")
            ok("pin_median_fallback", pin_pair["median_apr"] == 0.187)
            # distinct pin prepends, no dedupe hit
            dk.write_pin(p, good_pin(coin="TON", short="aster",
                                     long="binance"))
            pairs = ex.engine_pairs()
            names = [x["pair"] for x in pairs]
            ok("distinct_pin_first", names[0] == "TON aster->binance")
            ok("static_kept", "INJ bingx->okx" in names)
            ok("count", len(names) == len(ex.pl.PAIRS) + 1)
            # pin mode off -> statics only
            ex.set_pin_mode(False)
            pairs = ex.engine_pairs()
            ok("mode_off", all(x["src"] == "static" for x in pairs))
            ok("mode_off_n", len(pairs) == len(ex.pl.PAIRS))
        finally:
            ex.PIN_PATH, ex.DESK_PIN_MODE = old_path, old_mode
            ex.set_pin_mode(False)


# ---------------------------------------------------------- source audits
def t_wiring():
    print("wiring (source-level):")
    src_np = inspect.getsource(ex.new_position)
    ok("pos_src_flag", '"src"' in src_np or "'src'" in src_np)
    src_se = inspect.getsource(ex.start_entry)
    ok("pin_entry_event", "DESK_PIN_ENTRY" in src_se)
    src_ep = inspect.getsource(ex.engine_pairs)
    ok("ep_uses_pin", "load_pin" in src_ep and "pin_name" in src_ep)
    src_main = inspect.getsource(ex.main)
    ok("cli_flag", "--pin-desk" in src_main)
    ok("mode_set", "set_pin_mode" in src_main)
    src_dk = inspect.getsource(dk.cmd_open)
    ok("open_selfvalidate", "validate_pin" in src_dk)
    ok("open_no_auto_exec", "subprocess" in src_dk and "--pin-desk" in src_dk)


# ---------------------------------------------------------- cmd_open e2e
def fresh_kill_file(td):
    """Fresh evaluated kill file, halt=false (desk treats as clear)."""
    p = os.path.join(td, "kill.json")
    with open(p, "w") as fh:
        json.dump({"ts": iso_now(), "halt": False, "sources": {}}, fh)
    return p


def t_cmd_open_mock():
    print("cmd_open mocked e2e:")
    combos = [
        {"short_venue": "bingx", "long_venue": "okx", "apr": 0.34,
         "f8h": 0.0004, "net30_tt": 0.24, "pspread": 0.001,
         "pspread_at_size": 0.0005, "max_f24h": 0.001, "legs_ours": True},
        {"short_venue": "okx", "long_venue": "bingx", "apr": -0.34,
         "f8h": -0.0004, "net30_tt": None, "pspread": None,
         "pspread_at_size": None, "max_f24h": None, "legs_ours": True},
    ]
    art = {"coin": "INJ", "generated": iso_now(), "size_usd": 250.0,
           "fund_map": {"bingx": {"rate": 0.0001, "ivl_h": 8},
                        "okx": {"rate": -0.00005, "ivl_h": 8}},
           "book_map": {"bingx": {"bid": 5.4, "ask": 5.41},
                        "okx": {"bid": 5.4, "ask": 5.41}},
           "hist_meta": {}, "combos": combos}

    class A:
        coin = "INJ"
        short = None
        long = None
        size_usd = 250.0
        style = "taker"
        minutes = 1
        every = 20
        mode = "paper"
        no_run = True

    with tempfile.TemporaryDirectory() as td:
        old_pin = dk.PIN_PATH
        old_kill = pf.KILL_FILE
        dk.PIN_PATH = os.path.join(td, "desk", "pin.json")
        pf.KILL_FILE = fresh_kill_file(td)
        real_gather, real_engine = dk.gather_show, dk.engine_state
        real_med = dk.pl.load_medians
        try:
            async def fake_gather(coin, size):
                return art
            dk.gather_show = fake_gather
            dk.engine_state = lambda cp, pn: {"active": False,
                                              "cooldown": False}
            med_map = {("INJ", "bingx", "okx"): 0.187}
            dk.pl.load_medians = lambda: med_map
            rc = asyncio.run(dk.cmd_open(A()))
            ok("rc_ok", rc == 0, str(rc))
            pin = json.load(open(dk.PIN_PATH))
            ok("pin_combo", pin["short"] == "bingx" and pin["long"] == "okx")
            ok("pin_median", pin["median_apr"] == 0.187)
            ok("pin_prov", pin["provenance"]["apr"] == 0.34)
            ok("pin_preflight_clean", pin["preflight"]["refusals"] == [])
            ok("no_kill_warn", not any("kill" in w
                                       for w in pin["preflight"]["warnings"]))
            # stale kill file -> NOT a refusal, becomes a warning
            stale = os.path.getmtime(pf.KILL_FILE) - 9_999_999
            os.utime(pf.KILL_FILE, (stale, stale))
            os.remove(dk.PIN_PATH)

            async def fg4(coin, size):
                return art
            dk.gather_show = fg4
            rc = asyncio.run(dk.cmd_open(A()))
            ok("stale_kill_rc", rc == 0, str(rc))
            pin = json.load(open(dk.PIN_PATH))
            ok("stale_kill_warn", any("kill" in w for w in
                                      pin["preflight"]["warnings"]))
            # engine consumes the same pin end-to-end
            old_ep = ex.PIN_PATH
            try:
                ex.PIN_PATH = dk.PIN_PATH
                ex.set_pin_mode(True)
                pairs = ex.engine_pairs()
                ok("e2e_engine_first", pairs[0]["pair"] == "INJ bingx->okx"
                   and pairs[0]["src"] == "desk_pin")
            finally:
                ex.PIN_PATH = old_ep
                ex.set_pin_mode(False)
            # refusal: apr missing on best combo
            art2 = dict(art, combos=[dict(combos[0], apr=None)])
            dk.gather_show = fake_gather
            globals()["_art2"] = art2

            async def fake_gather2(coin, size):
                return art2
            dk.gather_show = fake_gather2
            if os.path.exists(dk.PIN_PATH):
                os.remove(dk.PIN_PATH)      # isolate the refusal case
            rc = asyncio.run(dk.cmd_open(A()))
            ok("refuse_rc", rc == 2, str(rc))
            ok("no_pin_written", not os.path.exists(dk.PIN_PATH))
            # explicit combo not in list
            A.short, A.long = "bybit", "okx"

            async def fake_gather3(coin, size):
                return art
            dk.gather_show = fake_gather3
            rc = asyncio.run(dk.cmd_open(A()))
            ok("explicit_miss_rc", rc == 2)
            A.short, A.long = None, None
        finally:
            dk.PIN_PATH = old_pin
            pf.KILL_FILE = old_kill
            dk.gather_show = real_gather
            dk.engine_state = real_engine
            dk.pl.load_medians = real_med


def main():
    t_validate()
    t_mapping()
    t_resolve()
    t_preflight()
    t_pin_file()
    t_engine_pairs()
    t_wiring()
    t_cmd_open_mock()
    print(f"\n{N[0] - FAILS[0]}/{N[0]} passed"
          + ("" if not FAILS[0] else f"  ({FAILS[0]} FAILED)"))
    sys.exit(1 if FAILS[0] else 0)


if __name__ == "__main__":
    main()
