#!/usr/bin/env python3
"""
Expanded cross-venue funding-differential analysis (snapshot_v3).

Venues: binance, bybit, okx, gate, hl  (v2 set)
      + aster, dydx, bitget            (new: user direction - Aster + DEX expansion)

Model (consistent with analyze_v2):
  funding_apr = rate * 24 / interval_h * 365          (interval-correct)
  pair: best short venue vs best long venue on base_norm
  net_apr(H) = gross - round_trip_taker * 365 / H     (H = 7 or 30 days)
Guards (new/kept):
  - identity: leg mark prices must agree within 5%
  - liquidity: 24h volume >= $3M on both legs where volume is known
Outputs: console tables + download/data/funding_spreads_expanded_2026-09.csv
"""
import json
import statistics
from datetime import datetime, timezone

SNAP = "/home/z/my-project/scripts/snapshot_v3.json"
CSV_OUT = "/home/z/my-project/download/data/funding_spreads_expanded_2026-09.csv"

# taker fees (verified: bitget from API median; others from prior study + docs check)
TAKER = {
    "binance": 0.0005, "bybit": 0.00055, "okx": 0.0005, "gate": 0.0005,
    "hl": 0.00045, "aster": 0.0004, "dydx": 0.0005, "bitget": 0.0006,
}
DEX_VENUES = ("aster", "dydx", "hl")
CEX_VENUES = ("binance", "bybit", "okx", "gate", "bitget")
MIN_VOL = 3_000_000  # 24h USD, applied only where volume is known

MAJORS = {
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "TON",
    "TRX", "SUI", "ARB", "OP", "INJ", "APT", "TIA", "WIF", "PEPE", "FET",
    "LTC", "ATOM", "NEAR", "DOT", "UNI", "AAVE", "SEI", "JUP", "HYPE", "ENA",
}

snap = json.load(open(SNAP))
PERPS = {v: snap["perps"][v].get("rows", {}) for v in
         ("binance", "bybit", "okx", "gate", "hl", "aster", "dydx", "bitget")}

report = []


def say(s=""):
    report.append(s)
    print(s)


def funding_apr(row):
    return row["rate"] * 24 / row.get("interval_h", 8) * 365


# ------------------------------------------------- venue sanity & fee check
say("=" * 104)
say(f"EXPANDED CROSS-VENUE FUNDING MAP  ({snap['timestamp_utc'][:16]} UTC)  "
    f"venues: {', '.join(PERPS)}")
say("=" * 104)

# Bitget fee median from API (sanity-check the 0.0006 default)
bg_fees = [r["taker_fee"] for r in PERPS["bitget"].values() if r.get("taker_fee")]
if bg_fees:
    med = statistics.median(bg_fees)
    say(f"Bitget API taker-fee median: {med*100:.4f}%  (n={len(bg_fees)})  "
        f"[assumed 0.0006 -> {'OK' if abs(med-0.0006)<0.0002 else 'UPDATE TAKER TABLE'}]")
    TAKER["bitget"] = med

say("\nFunding-interval distribution + structural funding bias per venue:")
for v, rows in PERPS.items():
    iv_dist = {}
    aprs, pos = [], 0
    for r in rows.values():
        iv_dist[r.get("interval_h", 8)] = iv_dist.get(r.get("interval_h", 8), 0) + 1
        a = funding_apr(r)
        aprs.append(a)
        pos += 1 if a > 0 else 0
    med_apr = statistics.median(aprs) if aprs else 0
    iv_str = ", ".join(f"{k:.0f}h:{n}" for k, n in sorted(iv_dist.items()))
    say(f"  {v:8s} n={len(rows):4d}  intervals[{iv_str}]  "
        f"median fAPR {med_apr*100:>7.2f}%  positive {pos/max(len(aprs),1)*100:.0f}%")

# ------------------------------------------------------------- pair builder
norm_index = {}   # base_norm -> venue -> raw symbol
for v, rows in PERPS.items():
    for raw, r in rows.items():
        bn = r.get("base_norm", raw)
        norm_index.setdefault(bn, {})[v] = raw


def liquid(v, bn):
    raw = norm_index[bn][v]
    r = PERPS[v][raw]
    if r.get("day_volume") is not None and PERPS[v][raw].get("day_volume") is not None:
        if v in ("aster", "dydx", "bitget") and r.get("day_volume", 0) < MIN_VOL:
            return False
    mark = r.get("mark", 0)
    return mark > 0.05 and (bn in MAJORS or mark > 1.0)


pairs = []
quarantined = []   # long leg is gate/aster with rate==0 (zero = missing data, not true zero)
identity_rejects = []
UNKNOWN_ZERO = {"gate", "aster"}


def zero_unknown(v, bn):
    """gate/aster ticker zeros can mean 'no data' -> if this is the LONG leg the
    spread may be phantom (we would actually pay unknown funding)."""
    return v in UNKNOWN_ZERO and norm_index[bn][v] in PERPS[v] and PERPS[v][norm_index[bn][v]]["rate"] == 0


for bn, venues in norm_index.items():
    if len(venues) < 2:
        continue
    vs = [v for v in venues if liquid(v, bn)]
    for i, va in enumerate(vs):
        for vb in vs[i + 1:]:
            ra = PERPS[va][norm_index[bn][va]]
            rb = PERPS[vb][norm_index[bn][vb]]
            ma, mb = ra.get("mark", 0), rb.get("mark", 0)
            if ma <= 0 or mb <= 0:
                continue
            dev = abs(ma / mb - 1) if mb > 0 else 1
            if dev > 0.05:
                identity_rejects.append((bn, va, vb, dev))
                continue
            aa, ab = funding_apr(ra), funding_apr(rb)
            if aa >= ab:
                short_v, long_v, gross = va, vb, aa - ab
            else:
                short_v, long_v, gross = vb, va, ab - aa
            rec = {
                "coin": bn, "long": long_v, "short": short_v,
                "gross_apr": gross,
                "iv_short": PERPS[short_v][norm_index[bn][short_v]]["interval_h"],
                "iv_long": PERPS[long_v][norm_index[bn][long_v]]["interval_h"],
                "vol_min": min(PERPS[short_v][norm_index[bn][short_v]].get("day_volume") or -1,
                               PERPS[long_v][norm_index[bn][long_v]].get("day_volume") or -1),
            }
            if zero_unknown(long_v, bn):
                quarantined.append(rec)
                continue
            rec["net7"] = gross - 2 * (TAKER[short_v] + TAKER[long_v]) * 365 / 7
            rec["net30"] = gross - 2 * (TAKER[short_v] + TAKER[long_v]) * 365 / 30
            pairs.append(rec)

pairs.sort(key=lambda r: r["net30"], reverse=True)
quarantined.sort(key=lambda r: r["gross_apr"], reverse=True)
say(f"\nPair universe: {len(pairs)} viable pairs from {len(norm_index)} normalized bases "
    f"({len(identity_rejects)} identity rejects with mark-deviation >5%; "
    f"{len(quarantined)} quarantined: long-leg zero-rate on gate/aster = unknown data)")


def print_table(rows, title, n=25):
    say(f"\n{title}")
    say(f"{'coin':12s} {'long->short':22s} {'grossAPR':>9s} {'net@7d':>8s} {'net@30d':>8s} {'ivS':>4s} {'ivL':>4s}")
    for r in rows[:n]:
        n7 = f"{r['net7']*100:>7.1f}%" if r.get("net7") is not None else "    n/a"
        n30 = f"{r['net30']*100:>7.1f}%" if r.get("net30") is not None else "    n/a"
        say(f"{r['coin']:12s} {r['long']+'->'+r['short']:22s} {r['gross_apr']*100:>8.1f}% "
            f"{n7} {n30} {r['iv_short']:>3.0f}h {r['iv_long']:>3.0f}h")


print_table(pairs, "TOP OVERALL SPREADS (all 8 venues, net of taker round-trip)")

aster_pairs = [p for p in pairs if "aster" in (p["long"], p["short"])]
print_table(aster_pairs, "ASTER-INVOLVING SPREADS (either leg on Aster)")

dydx_pairs = [p for p in pairs if "dydx" in (p["long"], p["short"])]
print_table(dydx_pairs, "dYdX-INVOLVING SPREADS", n=15)

print_table(quarantined, "QUARANTINED (long-leg zero-rate on gate/aster - verify before trusting)", n=10)

# ------------------------------------------- DEX-vs-CEX structural bias map
say("\nDEX vs CEX structural funding bias (per coin: median DEX fAPR - median CEX fAPR):")
dex_bias = []
for bn, venues in norm_index.items():
    dex_aprs = [funding_apr(PERPS[v][norm_index[bn][v]]) for v in DEX_VENUES if v in venues]
    cex_aprs = [funding_apr(PERPS[v][norm_index[bn][v]]) for v in CEX_VENUES if v in venues]
    if not dex_aprs or not cex_aprs:
        continue
    d, c = statistics.median(dex_aprs), statistics.median(cex_aprs)
    if min(max(PERPS[v][norm_index[bn][v]].get("mark", 0), 0) for v in venues if v in DEX_VENUES + CEX_VENUES) <= 0:
        continue
    dex_bias.append({"coin": bn, "dex_minus_cex": d - c, "dex_med": d, "cex_med": c})
dex_bias.sort(key=lambda x: x["dex_minus_cex"], reverse=True)
pos = [x for x in dex_bias if x["dex_minus_cex"] > 0]
say(f"  coins with DEX funding > CEX funding: {len(pos)}/{len(dex_bias)}")
say(f"  {'coin':12s} {'DEX med':>9s} {'CEX med':>9s} {'diff':>9s}   top10 DEX-rich:")
for x in dex_bias[:10]:
    say(f"  {x['coin']:12s} {x['dex_med']*100:>8.1f}% {x['cex_med']*100:>8.1f}% {x['dex_minus_cex']*100:>+8.1f}%")
say("  ... top5 DEX-poor (reverse carry candidates):")
for x in dex_bias[-5:]:
    say(f"  {x['coin']:12s} {x['dex_med']*100:>8.1f}% {x['cex_med']*100:>8.1f}% {x['dex_minus_cex']*100:>+8.1f}%")

# ------------------------------------------------------------------- CSV out
import csv
with open(CSV_OUT, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["coin", "long", "short", "gross_apr",
                                       "net7", "net30", "iv_short", "iv_long", "vol_min", "flag"])
    w.writeheader()
    for p in pairs:
        w.writerow({**p, "gross_apr": f"{p['gross_apr']:.4f}",
                    "net7": f"{p['net7']:.4f}", "net30": f"{p['net30']:.4f}", "flag": ""})
    for p in quarantined:
        w.writerow({**p, "gross_apr": f"{p['gross_apr']:.4f}",
                    "net7": "", "net30": "", "flag": "QUARANTINE_long_leg_zero"})
say(f"\nCSV -> {CSV_OUT}")

with open("/home/z/my-project/scripts/arb_v3_report.txt", "w") as fh:
    fh.write("\n".join(report))
print("report -> scripts/arb_v3_report.txt")
