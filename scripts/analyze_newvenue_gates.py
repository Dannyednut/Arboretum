#!/usr/bin/env python3
"""Part 11: depth-gate analysis for Sharpe-discovered new-venue pairs.

Mirrors analyze_depth.py math exactly (same walk metrics, same 25bps gate,
same net-APR formula) but:
  - persistence stats come from sharpe_newvenue_pairs.csv (Part 10)
  - fee table extended with VERIFIED new-venue taker fees:
      bingx 5.0bps (published VIP0), nado 3.5bps (native API field),
      orderly 3.0bps (base fee, broker-configurable), backpack 9.5bps
      (tier table; needs app confirmation -> sensitivity shown)
  - XAG Orderly leg has no native book (auth-gated): pair reported as
    PROVISIONAL using Sharpe's executableDepthUsd as a one-number proxy.
Outputs: download/data/newvenue_pair_gates.csv + console report.
"""
import csv
import json
import statistics as st
import urllib.request
from collections import defaultdict

import depth_sampler as ds

SAMPLES = "/home/z/my-project/download/data/l2_depth_samples_newvenue.csv"
PAIRS_CSV = "/home/z/my-project/download/data/sharpe_newvenue_pairs.csv"
OUT = "/home/z/my-project/download/data/newvenue_pair_gates.csv"

TAKER = dict(ds.TAKER)                      # 7 direct venues
TAKER.update({"bingx": 0.0005, "backpack": 0.00095,
              "nado": 0.00035, "orderly": 0.0003})
MAKER = dict(TAKER)
MAKER.update({"nado": 0.0001,               # native field (LINK-PERP)
              "backpack": 0.00085,          # tier table
              "orderly": 0.0000,            # possible rebate/0 maker
              "aster": 0.0})
LADDER = ds.LADDER
NOTIONALS = [1_000, 5_000, 10_000]
GATE_BPS = 25.0

PAIRS = [("INJ", "BingX", "okx"), ("AAVE", "Backpack", "okx"),
         ("LINK", "Nado", "okx"), ("XAG", "Orderly", "dydx")]

# ---------------------------------------------------------------- load legs
by_leg = defaultdict(list)
for r in csv.DictReader(open(SAMPLES)):
    if r["ok"] == "1":
        by_leg[(r["venue"], r["base"])].append(r)

leg_m = {}
for k, rs in by_leg.items():
    m = {"spread_bps": st.median(float(r["spread_bps"]) for r in rs),
         "n_pass": len(rs)}
    for side in ("bid", "ask"):
        for n in LADDER:
            vals = [float(r[f"slip_{side}_{n}"]) for r in rs
                    if r[f"slip_{side}_{n}"]]
            m[f"slip_{side}_{n}"] = st.median(vals) if vals else None
        for b in (10, 25, 50, 100):
            m[f"depth_{side}_{b}"] = st.median(
                float(r[f"depth_{side}_{b}"]) for r in rs)
    leg_m[k] = m


def leg_slip(venue, base, side, n):
    v = leg_m.get((venue, base), {}).get(f"slip_{side}_{n}")
    return v if v is not None else 10_000.0


def leg_depth(venue, base, side, bps=25):
    return leg_m.get((venue, base), {}).get(f"depth_{side}_{bps}", 0.0)


def capacity_25bps(venue, base):
    cap = 0
    for n in LADDER:
        if (leg_m.get((venue, base), {}).get(f"slip_bid_{n}") or 9e9) <= GATE_BPS \
                and (leg_m.get((venue, base), {}).get(f"slip_ask_{n}") or 9e9) <= GATE_BPS:
            cap = n
        else:
            break
    return cap


# ------------------------------------------------- sharpe pairs persistence
sharpe_pairs = {}
for r in csv.DictReader(open(PAIRS_CSV)):
    sharpe_pairs[(r["coin"], r["short_venue"], r["long_venue"])] = r


def sharpe_xag_orderly_depth():
    """One-number depth proxy for the Orderly XAG leg (no native book)."""
    try:
        req = urllib.request.Request(
            "https://www.sharpe.ai/api/arbitrage/cross-exchange",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            rows = json.loads(r.read())
        if isinstance(rows, dict):
            rows = rows.get("data") or []
        out = []
        for x in rows:
            s = json.dumps(x)
            if "Orderly" in s and "XAG" in s:
                out.append(x)
        return out
    except Exception as e:
        print("sharpe arb fetch ERR:", e)
        return []


# ---------------------------------------------------------------- per pair
rows = []
for coin, sv, lv in PAIRS:
    p = sharpe_pairs.get((coin, sv, lv))
    if not p:
        continue
    svk, lvk = sv.lower(), lv.lower()      # fee/leg tables keyed lowercase
    apr30 = float(p["median_spread_apr"]) / 100.0   # APR points -> fraction
    pos_frac = float(p["pos_frac"])
    fees_rt = 2 * (TAKER[svk] + TAKER[lvk])
    rec = {"coin": coin, "short_venue": sv, "long_venue": lv,
           "apr30_spread": apr30, "pos_day_frac": pos_frac,
           "days_cov": float(p["days"]),
           "fees_rt_pct": round(fees_rt * 100, 3),
           "sleg_spread_bps": leg_m.get((svk, coin), {}).get("spread_bps"),
           "lleg_spread_bps": leg_m.get((lvk, coin), {}).get("spread_bps"),
           "cap_short": capacity_25bps(svk, coin),
           "cap_long": capacity_25bps(lvk, coin)}
    for n in NOTIONALS:
        entry = leg_slip(svk, coin, "bid", n) + leg_slip(lvk, coin, "ask", n)
        exit_ = leg_slip(svk, coin, "ask", n) + leg_slip(lvk, coin, "bid", n)
        rt = entry + exit_
        rt_cost = fees_rt + rt / 1e4
        rec[f"rt_slip_{n}_bps"] = round(rt, 1)
        rec[f"rt_cost_{n}_pct"] = round(rt_cost * 100, 3)
        rec[f"net_apr30_{n}"] = round(apr30 - rt_cost * 365 / 30, 4)
        rec[f"breakeven_d_{n}"] = round(rt_cost / (apr30 / 365), 1) \
            if apr30 > 0 else None
    d4 = min(leg_depth(svk, coin, "bid"), leg_depth(svk, coin, "ask"),
             leg_depth(lvk, coin, "ask"), leg_depth(lvk, coin, "bid"))
    rec["min_leg_depth25"] = round(d4)
    # maker-entry variant on the short leg
    mk_entry = leg_slip(lvk, coin, "ask", 1000)
    mk_exit = leg_slip(svk, coin, "ask", 1000) + leg_slip(lvk, coin, "bid", 1000)
    mk_cost = (MAKER[svk] + TAKER[lvk]) * 2 + (mk_entry + mk_exit) / 1e4
    rec["rt_cost_1k_maker_pct"] = round(mk_cost * 100, 3)
    rec["net_apr30_1k_maker"] = round(apr30 - mk_cost * 365 / 30, 4)
    rec["pass_1k"] = int(rec["cap_short"] >= 1000 and rec["cap_long"] >= 1000
                         and d4 >= 1000 and rec["net_apr30_1000"] > 0.05)
    rec["pass_10k"] = int(rec["cap_short"] >= 10000 and rec["cap_long"] >= 10000
                          and d4 >= 10000 and rec["net_apr30_10000"] > 0.05)
    rec["pair_cap"] = min(rec["cap_short"], rec["cap_long"])
    rows.append(rec)

cols = list(rows[0].keys())
with open(OUT, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)
print(f"saved {len(rows)} pairs -> {OUT}\n")

hdr = (f"{'pair':22s} {'apr30':>6s} {'rt1k':>6s} {'net@1k':>7s} "
       f"{'rt10k':>6s} {'net@10k':>8s} {'bkd':>5s} {'caps':>11s} "
       f"{'d25':>7s} {'pass':>6s}")
print(hdr)
for r in rows:
    print(f"{r['coin']+' '+r['short_venue']+'->'+r['long_venue']:22s} "
          f"{r['apr30_spread']*100:5.1f}% "
          f"{r['rt_slip_1000_bps']:5.0f}b {r['net_apr30_1000']*100:6.1f}% "
          f"{r['rt_slip_10000_bps']:5.0f}b {r['net_apr30_10000']*100:7.1f}% "
          f"{r['breakeven_d_1000']:4.1f}d "
          f"{str(r['cap_short'])+'/'+str(r['cap_long']):>11s} "
          f"{r['min_leg_depth25']:7d} "
          f"{'1k' if r['pass_1k'] else '--'}{'/10k' if r['pass_10k'] else ''}")

print("\n--- leg detail (median of 3 passes) ---")
for (v, b), m in sorted(leg_m.items()):
    print(f"{v:9s} {b:5s} spread={m['spread_bps']:6.1f}bps "
          f"cap25={capacity_25bps(v, b):6d} "
          f"slip@1k a/b={m['slip_ask_1000']:.1f}/{m['slip_bid_1000']:.1f}bps "
          f"slip@10k a/b={m['slip_ask_10000']:.1f}/{m['slip_bid_10000']:.1f}bps "
          f"depth25 a/b={m['depth_ask_25']:,.0f}/{m['depth_bid_25']:,.0f}")

# XAG Orderly provisional via Sharpe arb table
hits = sharpe_xag_orderly_depth()
print("\n--- XAG Orderly (Sharpe arb-table proxy, no native book) ---")
for h in hits[:4]:
    print(json.dumps(h)[:300])
