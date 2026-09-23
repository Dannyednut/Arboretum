#!/usr/bin/env python3
"""
Charts + final stats for the market study.
1. Structural HL premium: 90d APR + positive-day fraction for hl-short pairs
2. Cumulative funding-spread equity curves for the 3 most consistent pairs
3. Stock-perp funding persistence check (HOOD/GOOGL/META on Binance)
Output: /home/z/my-project/download/charts/*.png
"""
import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",):
    try:
        fm.fontManager.addfont(p)
    except Exception:
        pass
import matplotlib.pyplot as plt
plt.rcParams["font.sans-serif"] = ["DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
import os

CACHE = "/home/z/my-project/scripts/funding_history_cache.json"
SNAP = "/home/z/my-project/scripts/snapshot_v2.json"
OUTDIR = "/home/z/my-project/download/charts"
os.makedirs(OUTDIR, exist_ok=True)

cache = json.load(open(CACHE))
snap = json.load(open(SNAP))
PERPS = {v: snap["perps"][v]["rows"] for v in ("binance", "bybit", "okx", "gate", "hl")}


def infer_iv(events):
    if len(events) < 3:
        return 8.0
    ds = sorted((events[i + 1][0] - events[i][0]) / 3600_000 for i in range(len(events) - 1))
    ds = [d for d in ds if 0 < d <= 48]
    return ds[len(ds) // 2] if ds else 8.0


def hourly(events):
    return {ts // 3600_000: rate for ts, rate in events}


def spread_series(coin, sv, lv):
    """Union-of-settlement-hours model: funding is a per-event cash flow.
    Returns (timestamps_ms, hourly cash-flow diffs) over common coverage."""
    h = cache.get(coin) or {}
    hev, lev = h.get(sv) or [], h.get(lv) or []
    if len(hev) < 30 or len(lev) < 30:
        return None, None
    iv_s, iv_l = infer_iv(hev), infer_iv(lev)
    hs, hl_ = hourly(hev), hourly(lev)
    lo = max(min(hs), min(hl_))
    hi_ = min(max(hs), max(hl_))
    hours = [x for x in sorted(set(hs) | set(hl_)) if lo <= x <= hi_]
    if len(hours) < 48 or (hi_ - lo) < 14 * 24:
        return None, None
    ts = [x * 3600_000 for x in hours]
    vals = [hs.get(x, 0.0) / iv_s - hl_.get(x, 0.0) / iv_l for x in hours]
    return ts, vals


def cum(vals):
    out, c = [], 0.0
    for v in vals:
        c += v
        out.append(c)
    return out


# ---------------------------------------------------------- chart 1: HL premium
majors_structural = [
    ("LIT", "hl", "bybit"), ("FET", "hl", "binance"), ("WLD", "hl", "binance"),
    ("AAVE", "hl", "bybit"), ("ENA", "hl", "okx"), ("HYPE", "hl", "bybit"),
    ("LINK", "hl", "bybit"), ("NEAR", "hl", "binance"), ("GMX", "hl", "binance"),
    ("AAVE", "hl", "binance"),
]
labels, aprs, poss = [], [], []
for coin, sv, lv in majors_structural:
    ts, vals = spread_series(coin, sv, lv)
    if not vals:
        continue
    days = (ts[-1] - ts[0]) / 86400_000
    aprs.append(sum(vals) / days * 365 * 100)
    d = {}
    for h, v in zip([t // 86400_000 for t in ts], vals):
        d[h] = d.get(h, 0) + v
    poss.append(sum(1 for x in d.values() if x > 0) / len(d) * 100)
    labels.append(f"{coin}\n{sv}|{lv}")

fig, ax = plt.subplots(figsize=(11, 5.6), constrained_layout=True)
bars = ax.bar(range(len(labels)), aprs, color="#2e7d32", alpha=0.85, width=0.62)
for i, (a, p) in enumerate(zip(aprs, poss)):
    ax.text(i, a + 0.25, f"{a:.1f}%", ha="center", fontsize=10, fontweight="bold")
    ax.text(i, 0.35, f"{p:.0f}%\npos.days", ha="center", fontsize=8.5,
            color="white", fontweight="bold")
ax.set_xticks(range(len(labels)))
ax.set_xticklabels(labels, fontsize=9)
ax.set_ylabel("90-day realized APR, short-HL / long-CEX (%)", fontsize=11)
ax.set_title("Structural funding premium: Hyperliquid short leg vs CEX long leg\n(90-day history in today's direction, positive-day % shown inside bars)",
             fontsize=12, fontweight="bold")
ax.grid(axis="y", alpha=0.3)
ax.set_ylim(0, max(aprs) * 1.18)
fig.savefig(f"{OUTDIR}/hl_structural_premium.png", dpi=150)
plt.close(fig)
print("chart 1 saved")

# ------------------------------------------------- chart 2: equity curves
fig, ax = plt.subplots(figsize=(11, 5.6), constrained_layout=True)
curves = [("ENA", "hl", "okx", "#1565c0"), ("FET", "hl", "binance", "#2e7d32"),
          ("AAVE", "hl", "bybit", "#ef6c00"), ("OP", "binance", "hl", "#6a1b9a")]
for coin, sv, lv, color in curves:
    ts, vals = spread_series(coin, sv, lv)
    if not vals:
        continue
    days = [(t - ts[0]) / 86400_000 for t in ts]
    ax.plot(days, [v * 100 for v in cum(vals)], label=f"{coin} short {sv} / long {lv}",
            color=color, lw=1.8)
ax.axhline(0, color="k", lw=0.8)
ax.set_xlabel("days since window start (2026-06-03)", fontsize=11)
ax.set_ylabel("cumulative funding spread, % of notional", fontsize=11)
ax.set_title("Cumulative funding-capture equity curves (per $1 notional per leg, before fees)",
             fontsize=12, fontweight="bold")
ax.legend(fontsize=9.5, loc="upper left", framealpha=0.9)
ax.grid(alpha=0.3)
fig.savefig(f"{OUTDIR}/funding_equity_curves.png", dpi=150)
plt.close(fig)
print("chart 2 saved")

# ------------------------------------------- stock-perp funding persistence
print("\nStock-perp funding history (single-venue funding APR, short-perp direction):")
for coin in ("HOOD", "GOOGL", "META", "AAPL", "TSLA", "NVDA", "AMZN", "MCD", "CRCL", "COIN"):
    ev = (cache.get(coin) or {}).get("binance") or []
    if len(ev) < 30:
        # not in cache: check live only
        r = PERPS["binance"].get(coin)
        if r:
            print(f"  {coin:6s} Binance: no history cached | live funding APR {r['rate']*24/r['interval_h']*365*100:.1f}% (iv {r['interval_h']}h)")
        continue
    iv = infer_iv(ev)
    # last 90d sum of funding (short receives when positive)
    end = max(t for t, _ in ev)
    start = end - 90 * 86400_000
    sel = [(t, r) for t, r in ev if t >= start]
    apr = sum(r for _, r in sel) / iv * 24 * 365 / ((end - start) / 86400_000)
    print(f"  {coin:6s} Binance 90d funding APR {apr*100:8.1f}%  (iv {iv:.0f}h, {len(sel)} events)")
