#!/usr/bin/env python3
"""Analyze sharpe_newvenue_pairs.csv: separate structural floor pairs from
negative-funding event pairs; venue rollup; window-coverage honesty."""
import csv
from collections import defaultdict

P = "/home/z/my-project/download/data/sharpe_newvenue_pairs.csv"
rows = list(csv.DictReader(open(P)))
for r in rows:
    for k in ("pos_frac", "median_spread_apr", "p25_spread_apr", "days",
              "short_leg_median_apr", "long_leg_median_apr",
              "net_apr_30d_pct", "slope_apr_pts_day"):
        r[k] = float(r[k]) if r[k] not in ("", None) else 0.0

print(f"total pairs: {len(rows)}")
short_new = [r for r in rows if r["short_venue_reachable"] != "LONG_EVENT"]
# distinguish branches: long-collect rows have short_venue in DIRECT set
DIRECT = {"binance", "bybit", "okx", "bitget", "gate", "hl", "dydx", "aster"}
events = [r for r in rows if r["short_venue"] in DIRECT]
floors = [r for r in rows if r["short_venue"] not in DIRECT]
print(f"floor pairs (short=NEW venue): {len(floors)}")
print(f"event pairs (long=NEW venue, negative funding): {len(events)}")

wc = defaultdict(int)
for r in rows:
    wc[min(60, int(r["days"])) // 10 * 10] += 1
print("window coverage (days buckets):", dict(sorted(wc.items())))

print("\n=== TOP 25 STRUCTURAL FLOOR PAIRS (short NEW venue -> long DIRECT),"
      " pos>=85%, days>=25 ===")
f = [r for r in floors if r["pos_frac"] >= 0.85 and r["days"] >= 25]
f.sort(key=lambda r: -r["median_spread_apr"])
for r in f[:25]:
    print(f"  {r['coin']:11s} short {r['short_venue']:11s} -> long {r['long_venue']:8s}"
          f" med {r['median_spread_apr']:6.1f}% p25 {r['p25_spread_apr']:6.1f}%"
          f" pos {r['pos_frac']:.0%} days {r['days']:4.1f}"
          f" net30 {r['net_apr_30d_pct']:6.1f}% slope {r['slope_apr_pts_day']:+.2f}/d"
          f" [{r['short_venue_reachable']}]")

print("\n=== TOP 25 NEGATIVE-FUNDING EVENT PAIRS (short DIRECT -> long NEW)"
      " ===")
e = [r for r in events if r["pos_frac"] >= 0.80 and r["days"] >= 20]
e.sort(key=lambda r: -r["median_spread_apr"])
for r in e[:25]:
    print(f"  {r['coin']:11s} short {r['short_venue']:11s} -> long {r['long_venue']:11s}"
          f" med {r['median_spread_apr']:6.1f}% p25 {r['p25_spread_apr']:6.1f}%"
          f" pos {r['pos_frac']:.0%} days {r['days']:4.1f}"
          f" shLeg {r['short_leg_median_apr']:6.1f}% lgLeg {r['long_leg_median_apr']:7.1f}%"
          f" [{r['long_venue_reachable']}]")

print("\n=== VENUE ROLLUP (new venues as SHORT leg) ===")
vs = defaultdict(lambda: {"n": 0, "med": [], "pos": [], "best": None})
for r in floors:
    v = vs[r["short_venue"]]
    v["n"] += 1
    v["med"].append(r["median_spread_apr"])
    v["pos"].append(r["pos_frac"])
    if v["best"] is None or r["median_spread_apr"] > v["best"][1]:
        v["best"] = (r["coin"], r["median_spread_apr"])
for v, d in sorted(vs.items(), key=lambda kv: -max(kv[1]["med"])):
    med = sorted(d["med"])[len(d["med"]) // 2]
    print(f"  {v:12s} legs {d['n']:4d}  med-of-med {med:7.1f}%  "
          f"best {d['best'][0]} {d['best'][1]:.1f}%")

print("\n=== VENUE ROLLUP (new venues as LONG leg = negative funding) ===")
vn = defaultdict(lambda: {"n": 0, "med": [], "best": None})
for r in events:
    v = vn[r["long_venue"]]
    v["n"] += 1
    v["med"].append(r["median_spread_apr"])
    if v["best"] is None or r["median_spread_apr"] > v["best"][1]:
        v["best"] = (r["coin"], r["median_spread_apr"])
for v, d in sorted(vn.items(), key=lambda kv: -max(kv[1]["med"]))[:12]:
    med = sorted(d["med"])[len(d["med"]) // 2]
    print(f"  {v:12s} legs {d['n']:4d}  med-of-med {med:7.1f}%  "
          f"best {d['best'][0]} {d['best'][1]:.1f}%")

# what the $1k-realistic floor cohort looks like (stress rt ~0.40-0.49%)
print("\n=== FLOOR COHORT @ $1k stress (pos>=90%, days>=25, med>=8%) ===")
c = [r for r in floors if r["pos_frac"] >= 0.90 and r["days"] >= 25
     and r["median_spread_apr"] >= 8]
c.sort(key=lambda r: -r["net_apr_30d_pct"])
for r in c[:20]:
    print(f"  {r['coin']:11s} {r['short_venue']:11s}->{r['long_venue']:8s}"
          f" med {r['median_spread_apr']:6.1f}% net30 {r['net_apr_30d_pct']:6.1f}%"
          f" be {r['breakeven_days']}d days {r['days']:4.1f}"
          f" [{r['short_venue_reachable']}]")
