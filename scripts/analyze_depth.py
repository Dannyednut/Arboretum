#!/usr/bin/env python3
"""Depth-gate analysis: join measured L2 slippage with 90d persistence stats.

Per candidate pair (51 E1 persistent + 18 dydx-short):
  - entry/exit/round-trip slippage from book walks (median of 3 passes)
  - measured round-trip cost = fees + slippage
  - net APR at 14d/30d hold after MEASURED costs (vs fees-only in Part 4)
  - breakeven hold days; capacity = max notional with <=25 bps/side both legs
  - gates: pass_$1k / pass_$10k

Output: download/data/depth_pair_gates.csv
"""
import csv
import statistics as st
from collections import defaultdict

SAMPLES = "/home/z/my-project/download/data/l2_depth_samples.csv"
PERSIST = "/home/z/my-project/download/data/funding_persistence_90d_v3.csv"
OUT = "/home/z/my-project/download/data/depth_pair_gates.csv"

TAKER = {"binance": 0.0005, "bybit": 0.00055, "okx": 0.0005, "hl": 0.00045,
         "aster": 0.0004, "dydx": 0.0005, "bitget": 0.0006}
LADDER = [500, 1_000, 2_500, 5_000, 10_000, 25_000]
GATE_BPS = 25.0          # per leg, per side
NOTIONALS = [1_000, 5_000, 10_000]

# ---------------------------------------------------------------- load legs
by_leg = defaultdict(list)
for r in csv.DictReader(open(SAMPLES)):
    if r["ok"] != "1":
        continue
    by_leg[(r["venue"], r["base"])].append(r)

leg_m = {}
for k, rs in by_leg.items():
    m = {"spread_bps": st.median(float(r["spread_bps"]) for r in rs)}
    for side in ("bid", "ask"):
        for n in LADDER:
            vals = [float(r[f"slip_{side}_{n}"]) for r in rs if r[f"slip_{side}_{n}"]]
            if vals:
                m[f"slip_{side}_{n}"] = st.median(vals)
            else:
                m[f"slip_{side}_{n}"] = None
        for b in (10, 25, 50, 100):
            m[f"depth_{side}_{b}"] = st.median(
                float(r[f"depth_{side}_{b}"]) for r in rs)
    leg_m[k] = m


def leg_slip(venue, base, side, n):
    v = leg_m.get((venue, base), {}).get(f"slip_{side}_{n}")
    return v if v is not None else 10_000.0   # missing leg = prohibitive


def leg_depth(venue, base, side, bps=25):
    return leg_m.get((venue, base), {}).get(f"depth_{side}_{bps}", 0.0)


def capacity_25bps(venue, base):
    """max ladder notional with slip <= GATE_BPS on BOTH sides."""
    cap = 0
    for n in LADDER:
        if (leg_m.get((venue, base), {}).get(f"slip_bid_{n}") or 9e9) <= GATE_BPS \
                and (leg_m.get((venue, base), {}).get(f"slip_ask_{n}") or 9e9) <= GATE_BPS:
            cap = n
        else:
            break
    return cap


# ---------------------------------------------------------------- load pairs
persist = {}
for r in csv.DictReader(open(PERSIST)):
    persist[(r["coin"], r["short_venue"], r["long_venue"])] = r

rows = []
for (coin, sv, lv), p in sorted(persist.items()):
    e1p = "E1" in p["flag"] and float(p["pos_day_frac"]) >= 0.95
    dydxp = sv == "dydx" and float(p["net_apr_30d_hold"]) > 0.15
    if not (e1p or dydxp):
        continue

    apr30 = float(p["apr_30d"])          # recent-regime gross carry (APR)
    apr90 = float(p["apr_90d"])
    fees_rt = 2 * (TAKER[sv] + TAKER[lv])

    rec = {"coin": coin, "short_venue": sv, "long_venue": lv,
           "apr_30d": apr30, "apr_90d": apr90,
           "pos_day_frac": float(p["pos_day_frac"]),
           "med_ep_days": int(float(p["med_ep_days"])),
           "fees_rt_pct": fees_rt * 100,
           "sleg_spread_bps": leg_m.get((sv, coin), {}).get("spread_bps"),
           "lleg_spread_bps": leg_m.get((lv, coin), {}).get("spread_bps"),
           "cap_short": capacity_25bps(sv, coin),
           "cap_long": capacity_25bps(lv, coin)}

    for n in NOTIONALS:
        entry = leg_slip(sv, coin, "bid", n) + leg_slip(lv, coin, "ask", n)
        exit_ = leg_slip(sv, coin, "ask", n) + leg_slip(lv, coin, "bid", n)
        rt = entry + exit_
        rt_cost = fees_rt + rt / 1e4
        rec[f"rt_slip_{n}_bps"] = round(rt, 1)
        rec[f"rt_cost_{n}_pct"] = round(rt_cost * 100, 3)
        rec[f"net_apr30_{n}"] = apr30 - rt_cost * 365 / 30
        rec[f"net_apr14_{n}"] = apr30 - rt_cost * 365 / 14
        rec[f"breakeven_d_{n}"] = (rt_cost / (apr30 / 365)) if apr30 > 0 else None

    depth_ok_1k = min(leg_depth(sv, coin, "bid"), leg_depth(sv, coin, "ask"),
                      leg_depth(lv, coin, "ask"), leg_depth(lv, coin, "bid"))
    rec["min_leg_depth25"] = round(depth_ok_1k)

    # maker-entry variant: short leg entered post-only at touch
    #  -> no crossing slip on entry, maker fee on short venue (aster = 0%)
    maker_fee_s = 0.0 if sv == "aster" else TAKER[sv]
    mk_entry = leg_slip(lv, coin, "ask", 1000)          # long leg taker buy
    mk_exit = leg_slip(sv, coin, "ask", 1000) + leg_slip(lv, coin, "bid", 1000)
    mk_cost = (maker_fee_s + TAKER[lv]) * 2 + (mk_entry + mk_exit) / 1e4
    rec["rt_cost_1000_maker_pct"] = round(mk_cost * 100, 3)
    rec["net_apr30_1000_maker"] = apr30 - mk_cost * 365 / 30

    rec["pass_1k"] = 1 if (rec["cap_short"] >= 1000 and rec["cap_long"] >= 1000
                           and depth_ok_1k >= 1000
                           and rec["net_apr30_1000"] > 0.05) else 0
    rec["pass_10k"] = 1 if (rec["cap_short"] >= 10000 and rec["cap_long"] >= 10000
                            and depth_ok_1k >= 10000
                            and rec["net_apr30_10000"] > 0.05) else 0
    rec["pair_cap"] = min(rec["cap_short"], rec["cap_long"])
    rows.append(rec)

rows.sort(key=lambda r: r["net_apr30_1000"], reverse=True)
cols = list(rows[0].keys())
with open(OUT, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({c: (round(r[c], 6) if isinstance(r[c], float) else r[c])
                    for c in cols})
print(f"saved {len(rows)} pairs -> {OUT}")

n1 = sum(r["pass_1k"] for r in rows)
n10 = sum(r["pass_10k"] for r in rows)
wide = [r for r in rows if not r["pass_1k"] and r["net_apr30_1000_maker"] > 0.15]
print(f"pass_1k: {n1}, pass_10k: {n10}, wide-but-net>15% (maker): {len(wide)}\n")

print(f"{'coin':12s} {'pair':17s} {'apr30':>6s} {'rt1k':>6s} {'net@1k':>7s} "
      f"{'rt10k':>6s} {'net@10k':>8s} {'bkd1k':>6s} {'cap':>6s} {'pass':>9s}")
for r in rows[:40]:
    print(f"{r['coin']:12s} {r['short_venue']:>7s}->{r['long_venue']:<8s} "
          f"{r['apr_30d']*100:5.1f}% {r['rt_slip_1000_bps']:5.0f}b "
          f"{r['net_apr30_1000']*100:6.1f}% {r['rt_slip_10000_bps']:5.0f}b "
          f"{r['net_apr30_10000']*100:7.1f}% "
          f"{(r['breakeven_d_1000'] or 0):5.1f}d {r['pair_cap']:5d} "
          f"{'1k' if r['pass_1k'] else '--'}{'/10k' if r['pass_10k'] else ''}")
