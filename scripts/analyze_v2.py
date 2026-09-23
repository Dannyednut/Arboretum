#!/usr/bin/env python3
"""
Arb-family quantifier: turns snapshot_v2.json into per-strategy live evidence.

Families measured here:
  A. Cross-exchange perp funding differential  (interval-correct, 5 venues)
  B. Same-venue spot-perp basis + funding capture
  C. Cross-exchange spot price dislocation     (viability check)
  D. Binance quarterly futures annualized basis (dated carry)
  E. Tokenized equities (xStocks) cross-venue premium
Output: /home/z/my-project/scripts/arb_family_summary.json + console report
"""
import json
import re
from datetime import datetime, timezone

SNAP = "/home/z/my-project/scripts/snapshot_v2.json"
OUT = "/home/z/my-project/scripts/arb_family_summary.json"

TAKER = {"binance": 0.0005, "bybit": 0.00055, "okx": 0.0005, "gate": 0.0005, "hl": 0.00045}
SPOT_TAKER = {"binance": 0.001, "bybit": 0.001, "okx": 0.0008, "hl": 0.0007, "gate": 0.002}

MAJORS = {
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "TON",
    "TRX", "SUI", "ARB", "OP", "INJ", "APT", "TIA", "WIF", "PEPE", "FET",
    "LTC", "ATOM", "NEAR", "DOT", "UNI", "AAVE", "SEI", "JUP", "HYPE", "ENA",
}

snap = json.load(open(SNAP))
PERPS = {v: snap["perps"][v].get("rows", {}) for v in ("binance", "bybit", "okx", "gate", "hl")}
SPOT = {v: snap["spot"][v].get("rows", {}) for v in ("binance", "bybit", "okx", "hl")}
summary = {"timestamp": snap["timestamp_utc"], "families": {}}

report = []


def say(s=""):
    report.append(s)
    print(s)


# =========================================================== A. funding diff
def funding_apr(row):
    return row["rate"] * 24 / row.get("interval_h", 8) * 365


say("=" * 100)
say(f"A. CROSS-EXCHANGE PERP FUNDING DIFFERENTIALS  (snapshot {snap['timestamp_utc'][:16]}, interval-correct)")
say("=" * 100)

res_a = []
vs = list(PERPS)
for i, va in enumerate(vs):
    for vb in vs[i + 1:]:
        common = set(PERPS[va]) & set(PERPS[vb])
        # match via base_norm for multiplier variants
        nb = {}
        for v in (va, vb):
            nb[v] = {}
            for raw, r in PERPS[v].items():
                nb[v][r.get("base_norm", raw)] = raw
        for bn in set(nb[va]) & set(nb[vb]):
            ra, rb = PERPS[va][nb[va][bn]], PERPS[vb][nb[vb][bn]]
            mark = max(ra.get("mark", 0), rb.get("mark", 0))
            if mark <= 0:
                continue
            liquid = mark > 0.05 and (bn in MAJORS or mark > 1.0)
            if not liquid:
                continue
            apr_a, apr_b = funding_apr(ra), funding_apr(rb)
            if apr_a >= apr_b:
                short_v, long_v, gross = va, vb, apr_a - apr_b
            else:
                short_v, long_v, gross = vb, va, apr_b - apr_a
            cost_rt = 2 * (TAKER[va] + TAKER[vb])
            res_a.append({
                "pair": f"{long_v}->{short_v}", "coin": bn,
                "gross_apr": gross, "cost_rt": cost_rt,
                "net_apr_7d": gross - cost_rt * 365 / 7,
                "net_apr_30d": gross - cost_rt * 365 / 30,
                "iv_short": PERPS[short_v][nb[short_v][bn]]["interval_h"],
                "iv_long": PERPS[long_v][nb[long_v][bn]]["interval_h"],
            })
res_a.sort(key=lambda r: r["net_apr_30d"], reverse=True)
summary["families"]["A_cross_ex_funding"] = {"count_pairs": len(res_a), "top30": res_a[:30]}

say(f"{'pair (long->short)':26s} {'coin':12s} {'grossAPR':>9s} {'netAPR@7d':>10s} {'netAPR@30d':>10s} {'ivS':>4s} {'ivL':>4s}")
for r in res_a[:25]:
    say(f"{r['pair']:26s} {r['coin']:12s} {r['gross_apr']*100:>8.1f}% {r['net_apr_7d']*100:>9.1f}% "
        f"{r['net_apr_30d']*100:>9.1f}% {r['iv_short']:>3.0f}h {r['iv_long']:>3.0f}h")

# interval distribution on Binance (verifies the 4h-shift finding)
bin_iv = {}
for r in PERPS["binance"].values():
    bin_iv[r["interval_h"]] = bin_iv.get(r["interval_h"], 0) + 1
bybit_iv = {}
for r in PERPS["bybit"].values():
    bybit_iv[r["interval_h"]] = bybit_iv.get(r["interval_h"], 0) + 1
summary["binance_interval_dist"] = bin_iv
summary["bybit_interval_dist"] = bybit_iv
say(f"\nFunding interval distribution -> Binance: {bin_iv} | Bybit: {bybit_iv}")
say("  (annualizing a 4h-settlement symbol with 8h math understates APR by 2x)")

# ================================================= B. same-venue spot-perp basis
say("\n" + "=" * 100)
say("B. SAME-VENUE SPOT-PERP BASIS (long spot / short perp on the SAME exchange)")
say("=" * 100)

res_b = []
suspect_b = []
for v in ("binance", "bybit", "okx"):
    perp_norm = {}
    for raw, r in PERPS[v].items():
        if not re.search(r"\d", raw):  # skip 1000PEPE-style for basis (price scale)
            perp_norm[r.get("base_norm", raw)] = r
    for base, sp in SPOT[v].items():
        if re.search(r"\d", base) or base not in perp_norm:
            continue
        p = perp_norm[base]
        if sp["mid"] <= 0 or p["mark"] <= 0:
            continue
        if not (base in MAJORS or sp["mid"] > 1.0) or sp["mid"] < 0.05:
            continue
        basis = p["mark"] / sp["mid"] - 1          # perp premium vs spot
        fapr = funding_apr(p)                       # short perp earns this when positive
        carry_30d = fapr * 30 / 365 + basis
        cost = 2 * (SPOT_TAKER[v] + TAKER[v])       # spot+perp taker in/out
        rec = {
            "venue": v, "coin": base, "basis_pct": basis,
            "funding_apr": fapr, "carry_30d_gross": carry_30d,
            "net_30d": carry_30d - cost,
            "spot": sp["mid"], "perp_mark": p["mark"],
        }
        # |basis| > 20% = ticker-collision or pre-launch/new-listing anomaly -> quarantine
        (suspect_b if abs(basis) > 0.20 else res_b).append(rec)
res_b.sort(key=lambda r: r["net_30d"], reverse=True)
suspect_b.sort(key=lambda r: abs(r["basis_pct"]), reverse=True)
summary["families"]["B_spot_perp_basis"] = {"count": len(res_b), "top20": res_b[:20],
                                            "suspect_new_listing": suspect_b[:15]}

say(f"{'venue':9s} {'coin':10s} {'basis':>8s} {'fundingAPR':>11s} {'gross30d':>9s} {'net30d':>8s}")
for r in res_b[:20]:
    say(f"{r['venue']:9s} {r['coin']:10s} {r['basis_pct']*100:>7.3f}% {r['funding_apr']*100:>10.1f}% "
        f"{r['carry_30d_gross']*100:>8.2f}% {r['net_30d']*100:>7.2f}%")
if suspect_b:
    say(f"\n  SUSPECT / EVENT-DRIVEN WATCHLIST (|basis|>20% - verify ticker identity first!):")
    for r in suspect_b[:10]:
        say(f"{r['venue']:9s} {r['coin']:10s} basis {r['basis_pct']*100:>10.1f}%  "
            f"spot={r['spot']:.6g} perp={r['perp_mark']:.6g}")

# ============================================================== C. cross-ex spot spread
say("\n" + "=" * 100)
say("C. CROSS-EXCHANGE SPOT DISLOCATION (is plain spot arbitrage taker-viable?)")
say("=" * 100)

res_c = {}
MAJ_ONLY = MAJORS
for i, va in enumerate(("binance", "bybit", "okx")):
    for vb in ("binance", "bybit", "okx")[i + 1:]:
        raw_common = set(SPOT[va]) & set(SPOT[vb])
        common = raw_common & MAJ_ONLY
        devs = []
        for base in common:
            pa, pb = SPOT[va][base]["mid"], SPOT[vb][base]["mid"]
            if pa > 0 and pb > 0:
                devs.append(abs(pa / pb - 1))
        devs.sort()
        if devs:
            med = devs[len(devs) // 2]
            p95 = devs[min(int(len(devs) * 0.95), len(devs) - 1)]
            mx = devs[-1]
            res_c[f"{va}|{vb}"] = {"n_majors": len(devs), "median_dev": med, "p95_dev": p95,
                                   "max_dev": mx,
                                   "taker_viable_threshold": 2 * (SPOT_TAKER[va] + SPOT_TAKER[vb]),
                                   "frac_taker_viable": sum(1 for d in devs if d > 2 * (SPOT_TAKER[va] + SPOT_TAKER[vb])) / len(devs)}
            say(f"{va:8s}|{vb:8s} n={len(devs):3d} majors  median |dev| {med*100:.4f}%   "
                f"p95 {p95*100:.4f}%   max {mx*100:.4f}%  (raw symbol overlap: {len(raw_common)})")
summary["families"]["C_cross_ex_spot"] = res_c

# ============================================================ D. quarterly carry
say("\n" + "=" * 100)
say("D. BINANCE QUARTERLY FUTURES BASIS (dated carry: long spot / short delivery)")
say("=" * 100)

res_d = []
now = datetime.now(timezone.utc)
for q in snap.get("quarterly_binance", []):
    base = q.get("base") or (q["base_raw"][:-4] if q["base_raw"].endswith("USDT") else q["base_raw"])
    sp = SPOT["binance"].get(base)
    if not sp or sp["mid"] <= 0 or q["mark"] <= 0:
        continue
    m = re.match(r"^(\d{2})(\d{2})(\d{2})$", q["expiry_yymmdd"])
    expiry = datetime(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)), 8, 0, tzinfo=timezone.utc)
    dte = (expiry - now).total_seconds() / 86400
    basis = q["mark"] / sp["mid"] - 1
    ann = basis * 365 / max(dte, 0.5)
    res_d.append({"symbol": q["symbol"], "coin": base, "basis_pct": basis,
                  "days_to_expiry": dte, "annualized_basis": ann,
                  "quarterly_mark": q["mark"], "spot": sp["mid"]})
    say(f"{q['symbol']:18s} basis {basis*100:>7.3f}%  dte {dte:>5.1f}  annualized {ann*100:>6.2f}%")
summary["families"]["D_quarterly_basis"] = res_d

# ======================================================= E. tokenized equities
say("\n" + "=" * 100)
say("E. TOKENIZED EQUITIES: xStocks SPOT (Bybit) vs STOCK PERPS (Binance/Bybit/Gate/OKX)")
say("=" * 100)

res_e = []
suspect_e = []
for tickx, sp in SPOT["bybit"].items():
    if not (tickx.endswith("X") and len(tickx) >= 3):
        continue
    tick = tickx[:-1]
    if not re.match(r"^[A-Z]{2,6}$", tick) or sp.get("mid", 0) <= 0:
        continue
    for v in ("binance", "bybit", "okx", "gate"):
        p = PERPS[v].get(tick)
        if not p or p.get("mark", 0) <= 0:
            continue
        basis = p["mark"] / sp["mid"] - 1
        fapr = funding_apr(p)
        rec = {
            "token": tickx, "stock": tick, "perp_venue": v,
            "xstock_mid": sp["mid"], "perp_mark": p["mark"],
            "basis_pct": basis, "funding_apr": fapr,
            "cost_rt": SPOT_TAKER["bybit"] + TAKER[v],
        }
        # identity guard: same asset must agree within 10%
        (suspect_e if abs(basis) > 0.10 else res_e).append(rec)
res_e.sort(key=lambda r: r["funding_apr"], reverse=True)
summary["families"]["E_xstock_vs_stock_perp"] = {"top20": res_e[:20], "suspect": suspect_e[:10]}

if res_e:
    say(f"{'token':10s} {'perp@':8s} {'basis':>8s} {'fundingAPR':>11s} {'costRT':>7s}")
    for r in res_e[:20]:
        say(f"{r['token']:10s} {r['perp_venue']:8s} {r['basis_pct']*100:>7.2f}% {r['funding_apr']*100:>10.1f}% "
            f"{r['cost_rt']*100:>6.2f}%")
if suspect_e:
    say("  SUSPECT (price mismatch >10% - different assets sharing ticker):")
    for r in suspect_e[:6]:
        say(f"  {r['token']} vs {r['perp_venue']}:{r['stock']}  xstock={r['xstock_mid']:.4g} perp={r['perp_mark']:.4g}")
if not res_e and not suspect_e:
    say("  (no xStocks tokens matched)")

json.dump(summary, open(OUT, "w"), indent=1)
print(f"\nSaved -> {OUT}")

# console report file for reference
with open("/home/z/my-project/scripts/arb_family_report.txt", "w") as fh:
    fh.write("\n".join(report))
