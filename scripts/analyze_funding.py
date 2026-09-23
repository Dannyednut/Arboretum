#!/usr/bin/env python3
"""
Cross-exchange funding differential analyzer.
Finds top funding-differential pairs across Binance/Bybit/OKX/Hyperliquid,
computes net APR after realistic fee drag, and prints worked examples.
"""
import json

SNAP = "/home/z/my-project/scripts/live_funding_snapshot.json"
snap = json.load(open(SNAP))

# ---- Fee schedules (standard base tier, taker, USDT-M perps, as of research date) ----
# Binance: maker 0.02% / taker 0.05%   (regular user, USDT-M)
# Bybit:   maker 0.02% / taker 0.055%  (derivatives base tier)
# OKX:     maker 0.02% / taker 0.05%   (regular SWAP)
# Hyperliquid: maker 0.015% / taker 0.045% (base tier, no VIP)
TAKER = {"binance": 0.0005, "bybit": 0.00055, "okx": 0.0005, "hyperliquid": 0.00045}
MAKER = {"binance": 0.0002, "bybit": 0.0002, "okx": 0.0002, "hyperliquid": 0.00015}

def rows(ex):
    return snap.get(ex, {}).get("rows", {}) or {}

def annualize(rate_per_period, periods_per_day):
    return rate_per_period * periods_per_day * 365

# Funding intervals: Binance/Bybit mostly 8h (some 4h/1h), OKX mostly 8h, HL = hourly.
def binance_periods(coin):
    return 3.0  # default; premiumIndex doesn't expose interval
def bybit_periods(coin):
    return 3.0
def okx_periods(coin):
    r = rows("okx").get(coin, {})
    h = r.get("funding_interval_hours", 8) or 8
    return 24.0 / h
def hl_periods(coin):
    return 24.0  # HL funds hourly

def common_coins():
    sets = [set(rows("binance")), set(rows("bybit")), set(rows("hyperliquid"))]
    return set.intersection(*sets)

def okx_common():
    return set(rows("okx")) & set(rows("binance")) & set(rows("bybit"))

def price_of(ex, coin):
    return rows(ex).get(coin, {}).get("mark_price", 0)

def funding_rate(ex, coin):
    """Return (rate, periods_per_day) normalized: rate is per funding event."""
    r = rows(ex).get(coin)
    if not r:
        return None, None
    if ex == "hyperliquid":
        return r.get("funding_hourly_rate", 0), 24.0
    return r.get("funding_8h_rate", 0), 3.0

def pair_spread(ex_long, ex_short, coin):
    """Long perp on ex_low_funding, short perp on ex_high_funding.
    Net funding capture = funding_short_received - funding_long_paid."""
    fl, pl = funding_rate(ex_long, coin)
    fs, ps = funding_rate(ex_short, coin)
    if fl is None or fs is None:
        return None
    # daily funding differential (as fraction of notional)
    daily = fs * ps - fl * pl
    return daily, fl, pl, fs, ps

def trade_costs(coin, ex_long, ex_short, entry="taker", exit_style="taker"):
    """Round-trip fee drag as fraction of notional (both legs)."""
    fee_in = TAKER[ex_long] + TAKER[ex_short] if entry == "taker" else MAKER[ex_long] + MAKER[ex_short]
    fee_out = TAKER[ex_long] + TAKER[ex_short] if exit_style == "taker" else MAKER[ex_long] + MAKER[ex_short]
    return fee_in + fee_out

print("=" * 100)
print(f"CROSS-EXCHANGE FUNDING DIFFERENTIALS — snapshot {snap['timestamp_utc']}")
print("=" * 100)

pairs = [("binance", "bybit"), ("binance", "okx"), ("bybit", "okx"),
         ("binance", "hyperliquid"), ("bybit", "hyperliquid"), ("okx", "hyperliquid")]

results = []
for ex_l, ex_s in pairs:
    coins = set(rows(ex_l)) & set(rows(ex_s))
    for coin in coins:
        sp = pair_spread(ex_l, ex_s, coin)
        if not sp:
            continue
        daily, fl, pl, fs, ps = sp
        if price_of(ex_l, coin) <= 0:
            continue
        gross_apr = daily * 365
        costs = trade_costs(coin, ex_l, ex_s)
        # assume 7-day hold: cost drag amortized over hold
        for hold_days in (7,):
            drag_apr = costs * (365.0 / hold_days)
            net_apr = gross_apr - drag_apr
            results.append({
                "pair": f"{ex_l}/{ex_s}", "coin": coin,
                "daily_funding_diff": daily, "gross_apr": gross_apr,
                "roundtrip_cost": costs, "net_apr_7d": net_apr,
                "px_long": price_of(ex_l, coin), "px_short": price_of(ex_s, coin),
            })

# filter: meaningful price (no illiquid junk), positive funding on short side
def liquid(r):
    px = r["px_long"]
    coin = r["coin"]
    major = {"BTC","ETH","SOL","BNB","XRP","DOGE","ADA","AVAX","LINK","TON","TRX","DOT","MATIC","NEAR","APT","ARB","OP","SUI","SEI","TIA","INJ","FET","RNDR","LTC","BCH","ATOM","ETC","FIL","PEPE","WIF","BONK","JUP","PYTH","STRK","ORDI","AABA","WLD","LDO","CRV","AAVE","UNI","MKR","ALGO","FTM","AXS","SAND","MANA","GALA","EOS","XLM","HBAR","VET","THETA","RUNE","KAVA","GRT","SNX","COMP","SUSHI","1000PEPE","1000BONK","1000FLOKI"}
    return px > 0.05 and (coin in major or px > 1.0)

big = [r for r in results if abs(r["gross_apr"]) > 0.02]
big_major = [r for r in big if liquid(r)]
big_major.sort(key=lambda r: r["net_apr_7d"], reverse=True)

print(f"\nTOP 25 POSITIVE NET FUNDING CARRY (short high-funding venue, long low-funding venue; 7-day hold, taker in/out):")
print(f"{'pair':28s} {'coin':10s} {'dailyΔ':>9s} {'grossAPR':>9s} {'costRT':>8s} {'netAPR@7d':>10s}")
for r in big_major[:25]:
    print(f"{r['pair']:28s} {r['coin']:10s} {r['daily_funding_diff']*100:>8.4f}% {r['gross_apr']*100:>8.1f}% {r['roundtrip_cost']*100:>7.3f}% {r['net_apr_7d']*100:>9.1f}%")

print(f"\nTOP 10 REVERSE (funding flip watch — long high-funding side pays):")
big_major_rev = sorted(big_major, key=lambda r: r["net_apr_7d"])
for r in big_major_rev[:10]:
    print(f"{r['pair']:28s} {r['coin']:10s} {r['daily_funding_diff']*100:>8.4f}% {r['gross_apr']*100:>8.1f}% {r['roundtrip_cost']*100:>7.3f}% {r['net_apr_7d']*100:>9.1f}%")

# ---- Worked example: $1,000 notional, top pair, 7-day hold ----
print("\n" + "=" * 100)
print("WORKED EXAMPLES — $1,000 notional per side, delta-neutral cross-ex perp carry")
print("=" * 100)
for r in big_major[:3]:
    ex_l, ex_s = r["pair"].split("/")
    coin = r["coin"]
    notional = 1000.0
    daily_usd = r["daily_funding_diff"] * notional
    entry_cost = (TAKER[ex_l] + TAKER[ex_s]) * notional
    exit_cost = entry_cost
    weekly_usd = daily_usd * 7
    pnl_7d = weekly_usd - entry_cost - exit_cost
    print(f"\n• {coin}: LONG {ex_l} perp / SHORT {ex_s} perp, $1,000 notional each leg")
    print(f"  Funding capture: ${daily_usd:.3f}/day  → ${weekly_usd:.2f} over 7 days")
    print(f"  Fees: entry ${entry_cost:.2f} + exit ${exit_cost:.2f} = ${entry_cost+exit_cost:.2f}")
    print(f"  Net P&L (7d, ignore basis drift): ${pnl_7d:.2f}  ({pnl_7d/ (notional) *100:.2f}% on $1k collateral per side)")

# HL special: funding accrues hourly → note
print("\nNotes:")
print("  * Hyperliquid funding accrues hourly (rate shown is hourly); CEXs mostly 8h cycles (some 4h/1h).")
print("  * fee drag assumes taker both legs both sides; maker entry could roughly halve it.")
print("  * 'netAPR@7d' = gross funding APR minus round-trip cost amortized over 7 days.")
