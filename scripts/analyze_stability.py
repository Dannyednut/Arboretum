#!/usr/bin/env python3
"""Multi-session stability analysis (Part-5 §6.1 next-step 1).

Joins ALL sampling windows (Part-5 CSV tagged `P5` + multi-session CSV) and
computes, per candidate pair (69) and per leg (93):
  - per-window measured RT cost, net APR, capacity  (worst-case across windows)
  - per-leg slip distributions across windows (min/med/max per rung/side)
  - verdict:  STABLE  = pass_$1k in EVERY window
              FLAKY   = pass_$1k in some windows only        -> demote
              FAIL    = pass_$1k in no window
              swing   = max/median RT slippage at $1k (>2x = unstable book)

Inputs : download/data/l2_depth_samples.csv, l2_depth_samples_ms.csv,
         download/data/funding_persistence_90d_v3.csv
Outputs: download/data/stability_legs.csv, download/data/stability_pair_gates.csv
"""
import csv
import os
import statistics as st
from collections import defaultdict

D = "/home/z/my-project/download/data"
FILES = [(os.path.join(D, "l2_depth_samples.csv"), "P5"),
         (os.path.join(D, "l2_depth_samples_ms.csv"), None)]
PERSIST = os.path.join(D, "funding_persistence_90d_v3.csv")
OUT_LEGS = os.path.join(D, "stability_legs.csv")
OUT_PAIRS = os.path.join(D, "stability_pair_gates.csv")

TAKER = {"binance": 0.0005, "bybit": 0.00055, "okx": 0.0005, "hl": 0.00045,
         "aster": 0.0004, "dydx": 0.0005, "bitget": 0.0006}
LADDER = [500, 1_000, 2_500, 5_000, 10_000, 25_000]
GATE_BPS = 25.0
NOTIONALS = [1_000, 5_000, 10_000]

# ------------------------------------------------------- load windows
win_leg = defaultdict(lambda: defaultdict(list))   # window -> leg -> [rows]
for path, tag in FILES:
    if not os.path.exists(path):
        continue
    for r in csv.DictReader(open(path)):
        if r.get("ok") != "1":
            continue
        w = r.get("window") or tag
        if w:
            win_leg[w][(r["venue"], r["base"])].append(r)

windows = sorted(win_leg)
print(f"windows: {windows}")


def leg_med(w, venue, base, key):
    rs = win_leg.get(w, {}).get((venue, base))
    if not rs:
        return None
    vals = [float(r[key]) for r in rs if r.get(key)]
    return st.median(vals) if vals else None


def leg_slip(w, venue, base, side, n):
    """Median slip in window w; missing leg/window = prohibitive (10_000)."""
    v = leg_med(w, venue, base, f"slip_{side}_{n}")
    return v if v is not None else 10_000.0


def leg_depth(w, venue, base, side, bps=25):
    v = leg_med(w, venue, base, f"depth_{side}_{bps}")
    return v if v is not None else 0.0


def cap_w(w, venue, base):
    """Max ladder notional with slip<=25bps on BOTH sides in window w."""
    cap = 0
    for n in LADDER:
        b = leg_med(w, venue, base, f"slip_bid_{n}")
        a = leg_med(w, venue, base, f"slip_ask_{n}")
        if b is not None and a is not None and b <= GATE_BPS and a <= GATE_BPS:
            cap = n
        else:
            break
    return cap


# ------------------------------------------------- per-leg distributions
legs_all = sorted({leg for w in windows for leg in win_leg[w]})
leg_rows = []
for (venue, base) in legs_all:
    rec = {"venue": venue, "base": base, "n_windows": 0}
    for side in ("bid", "ask"):
        for n in LADDER:
            vals = []
            for w in windows:
                v = leg_med(w, venue, base, f"slip_{side}_{n}")
                if v is not None:
                    vals.append(v)
            if vals:
                rec[f"{side}_{n}_min"] = round(min(vals), 2)
                rec[f"{side}_{n}_med"] = round(st.median(vals), 2)
                rec[f"{side}_{n}_max"] = round(max(vals), 2)
                rec[f"{side}_{n}_winok"] = sum(1 for v in vals
                                               if v <= GATE_BPS)
                rec["n_windows"] = max(rec["n_windows"], len(vals))
    sp = [leg_med(w, venue, base, "spread_bps") for w in windows]
    sp = [x for x in sp if x is not None]
    if sp:
        rec["spread_med"] = round(st.median(sp), 2)
        rec["spread_max"] = round(max(sp), 2)
    leg_rows.append(rec)

with open(OUT_LEGS, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(leg_rows[0].keys()))
    w.writeheader()
    w.writerows(leg_rows)
print(f"saved {len(leg_rows)} legs -> {OUT_LEGS}")

# ------------------------------------------------------- pair-level gates
persist = {}
for r in csv.DictReader(open(PERSIST)):
    e1p = "E1" in r["flag"] and float(r["pos_day_frac"]) >= 0.95
    dydxp = r["short_venue"] == "dydx" and float(r["net_apr_30d_hold"]) > 0.15
    if e1p or dydxp:
        persist[(r["coin"], r["short_venue"], r["long_venue"])] = r

pair_rows = []
for (coin, sv, lv), p in sorted(persist.items()):
    apr30 = float(p["apr_30d"])
    fees_rt = 2 * (TAKER[sv] + TAKER[lv])
    maker_fee_s = 0.0 if sv == "aster" else TAKER[sv]

    rec = {"coin": coin, "short_venue": sv, "long_venue": lv,
           "apr_30d": apr30, "pos_day_frac": float(p["pos_day_frac"]),
           "n_windows": len(windows)}

    rts = {n: [] for n in NOTIONALS}
    nets = {n: [] for n in NOTIONALS}
    nets_mk, caps, passes1, passes10, depths = ([] for _ in range(5))
    for w in windows:
        for n in NOTIONALS:
            entry = leg_slip(w, sv, coin, "bid", n) + leg_slip(w, lv, coin, "ask", n)
            exit_ = leg_slip(w, sv, coin, "ask", n) + leg_slip(w, lv, coin, "bid", n)
            rt = entry + exit_
            cost = fees_rt + rt / 1e4
            rts[n].append(rt)
            nets[n].append(apr30 - cost * 365 / 30)

        mk_entry = leg_slip(w, lv, coin, "ask", 1000)
        mk_exit = leg_slip(w, sv, coin, "ask", 1000) + leg_slip(w, lv, coin, "bid", 1000)
        mk_cost = (maker_fee_s + TAKER[lv]) * 2 + (mk_entry + mk_exit) / 1e4
        nets_mk.append(apr30 - mk_cost * 365 / 30)

        caps.append(min(cap_w(w, sv, coin), cap_w(w, lv, coin)))
        d_ok = min(leg_depth(w, sv, coin, "bid"), leg_depth(w, sv, coin, "ask"),
                   leg_depth(w, lv, coin, "bid"), leg_depth(w, lv, coin, "ask"))
        depths.append(d_ok)
        passes1.append(1 if (caps[-1] >= 1000 and d_ok >= 1000
                             and nets[1000][-1] > 0.05) else 0)
        passes10.append(1 if (caps[-1] >= 10_000 and d_ok >= 10_000
                              and nets[10_000][-1] > 0.05) else 0)

    rec["rt1k_med"] = round(st.median(rts[1000]), 1)
    rec["rt1k_min"] = round(min(rts[1000]), 1)
    rec["rt1k_max"] = round(max(rts[1000]), 1)
    rec["rt5k_med"] = round(st.median(rts[5_000]), 1)
    rec["rt10k_med"] = round(st.median(rts[10_000]), 1)
    rec["rt10k_max"] = round(max(rts[10_000]), 1)
    med_safe = max(st.median(rts[1000]), 0.1)
    rec["swing_1k"] = round(max(rts[1000]) / med_safe, 1)
    rec["swing_10k"] = round(max(rts[10_000]) / max(st.median(rts[10_000]), 0.1), 1)
    rec["net1k_worst"] = round(min(nets[1000]), 4)
    rec["net1k_best"] = round(max(nets[1000]), 4)
    rec["net10k_worst"] = round(min(nets[10_000]), 4)
    rec["net1k_maker_worst"] = round(min(nets_mk), 4)
    rec["cap_stable"] = min(caps)
    rec["depth1k_worst"] = round(min(depths))
    rec["wins_pass1k"] = sum(passes1)
    rec["wins_pass10k"] = sum(passes10)
    rec["wins_total"] = len(windows)
    rec["rt1k_by_window"] = "|".join(f"{v:.0f}" for v in rts[1000])
    rec["cap_by_window"] = "|".join(str(c) for c in caps)

    if sum(passes1) == len(windows):
        rec["verdict"] = "STABLE"
    elif sum(passes1) > 0:
        rec["verdict"] = "FLAKY"
    else:
        rec["verdict"] = "FAIL"
    rec["pass_10k_stable"] = 1 if sum(passes10) == len(windows) else 0
    pair_rows.append(rec)

pair_rows.sort(key=lambda r: (r["verdict"] != "STABLE", -r["net1k_worst"]))
with open(OUT_PAIRS, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(pair_rows[0].keys()))
    w.writeheader()
    w.writerows(pair_rows)
print(f"saved {len(pair_rows)} pairs -> {OUT_PAIRS}")

# ------------------------------------------------------------- console
from collections import Counter
cnt = Counter(r["verdict"] for r in pair_rows)
print(f"\nverdicts over {len(windows)} windows: {dict(cnt)}")
print(f"pass_10k_stable: {sum(r['pass_10k_stable'] for r in pair_rows)}")

print(f"\n{'coin':10s} {'pair':18s} {'apr30':>6s} {'rt1k med/max':>12s} "
      f"{'swing':>6s} {'netW':>7s} {'netW_mk':>8s} {'capS':>6s} {'dpW':>6s} "
      f"{'wins':>5s} {'10k':>4s} verdict")
for r in pair_rows:
    if r["verdict"] == "FAIL" and r["apr_30d"] * 100 < 15:
        continue                       # keep console short: show the rest
    print(f"{r['coin']:10s} {r['short_venue']:>7s}->{r['long_venue']:<9s} "
          f"{r['apr_30d']*100:5.1f}% {r['rt1k_med']:5.0f}/{r['rt1k_max']:5.0f}b "
          f"{r['swing_1k']:5.1f}x {r['net1k_worst']*100:6.1f}% "
          f"{r['net1k_maker_worst']*100:7.1f}% {r['cap_stable']:5d} "
          f"{r['depth1k_worst']:5d} "
          f"{r['wins_pass1k']}/{r['wins_total']} "
          f"{'Y' if r['pass_10k_stable'] else '-'}  {r['verdict']}")
