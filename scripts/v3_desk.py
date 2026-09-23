#!/usr/bin/env python3
"""v3_desk.py - P5a/P5b headless arbitrage desk surface (Part 22).

Read surface key-free. Mirrors the VOOI Arbitrage Desk concept (Part 21 scan)
with OUR fee/RT model and OUR median-APR merit signal.

Commands:
  scan     broad cross-venue strategy table (Sharpe arb feed + our math)
  show     per-coin deep dive: all long/short combos, live funding, books,
           P-spread @ size, Max F 1h/24h
  history  per-coin funding series export (+stats, optional CSV)
  open     P5b gated on-demand entry: preflight -> pin file -> engine window.
           Desk rows NEVER auto-execute: the pin only ADDS a candidate pair;
           the engine re-runs its full gate chain (kill, cooldown, freeze,
           TW1-TW6, M5, pair-cap clamp) on it. Live real orders remain
           unreachable until P4b (adapter DRY_RUN refusals stay armed).

Artifacts -> download/data/desk/*.json (+ csv). Printed table mirrors JSON.

Math conventions (Part 22 §3):
  hourly = rate / interval_h ; apr = hourly*24*365
  f_apr  = apr(short leg) - apr(long leg)      # shorts receive positive rate
  f8h    = (h_short - h_long) * 8
  rt_tt  = TAKER[short] + TAKER[long]          # fraction, both legs taker
  net30  = gross_apr - rt_tt * 365/30          # our fees on their gross
"""
import argparse
import asyncio
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp  # noqa: E402

import depth_sampler as ds  # noqa: E402
import depth_newvenue as nv  # noqa: E402
import paper_ledger as pl  # noqa: E402
import v3_connectors as vc  # noqa: E402

DESK_DIR = "/home/z/my-project/download/data/desk"
HOLD_DAYS = 30.0
OUR_VENUES = set(pl.TAKER.keys())
UA = {"User-Agent": "Mozilla/5.0"}

# ----------------------------------------------------------- P5b: open gate
PIN_PATH = os.path.join(DESK_DIR, "pin.json")
PIN_TTL_H = 24.0               # stale pins are ignored by the engine
DESK_DEFAULT_SIZE = 250.0
DESK_MAX_SIZE = 500.0          # hard desk cap; engine M5 still gates on top

# ------------------------------------------------------------- P5c: basis
# Spot long + perp short (cash-and-carry). Spot side is CEX-only v1 (key-free
# public endpoints); perp side must be an OUR_VENUES perp (executable fees).
# Spot fees: conservative flat 10 bps taker (no VIP/BNB discounts assumed).
SPOT_VENUES = ["binance", "bybit", "okx", "bitget"]
SPOT_TAKER = {v: 0.0010 for v in SPOT_VENUES}
PX_DIV_MAX = 0.02              # |perp/spot - 1| beyond this -> suspect flag


# ------------------------------------------------------------- pure math
def hourly_rate(rate, ivl_h):
    """Per-interval funding rate -> hourly fraction. None if interval bad."""
    try:
        rate = float(rate)
        ivl_h = float(ivl_h)
    except (TypeError, ValueError):
        return None
    if ivl_h <= 0 or ivl_h > 48:
        return None
    return rate / ivl_h


def apr_of(h):
    return None if h is None else h * 24.0 * 365.0


def combo_math(h_s, h_l):
    """(hourly_short, hourly_long) -> dict(apr, f8h) or Nones."""
    if h_s is None or h_l is None:
        return {"apr": None, "f8h": None}
    return {"apr": apr_of(h_s - h_l), "f8h": (h_s - h_l) * 8.0}


def fee_drag_apr(rt_frac, hold_days=HOLD_DAYS):
    """Entry RT fraction amortized over hold -> APR drag."""
    if rt_frac is None:
        return None
    return rt_frac * 365.0 / hold_days


def net30_of(gross_apr, rt_frac):
    if gross_apr is None or rt_frac is None:
        return None
    return gross_apr - fee_drag_apr(rt_frac)


def pspread(mid_bid_s, mid_ask_l):
    """Sell-high/buy-low spread fraction: (bid_s - ask_l)/ask_l."""
    if not mid_bid_s or not mid_ask_l or mid_ask_l <= 0:
        return None
    return (mid_bid_s - mid_ask_l) / mid_ask_l


def max_spread_from_series(series_a, series_b, window_h):
    """Max (h_a - h_b) over joined 5-min buckets within window_h.

    Series: [(ts_ms, hourly)] sorted. Returns max spread or None."""
    if not series_a or not series_b:
        return None
    cut = (series_a[-1][0] + series_b[-1][0]) / 2.0 - window_h * 3600_000
    b = {}
    for ts, h in series_b:
        if ts >= cut:
            b[int(ts // 300_000)] = h
    best = None
    for ts, h in series_a:
        if ts < cut:
            continue
        other = b.get(int(ts // 300_000))
        if other is None:
            continue
        sp = h - other
        if best is None or sp > best:
            best = sp
    return best


def rt_for_style(sv, lv, style="tt"):
    """Entry RT fraction for style: tt=taker/taker, mm=maker/maker."""
    t = pl.TAKER.get(sv)
    l = pl.TAKER.get(lv)
    if t is None or l is None:
        return None
    if style == "tt":
        return t + l
    if style == "mm":
        return pl.MAKER.get(sv, t) + pl.MAKER.get(lv, l)
    return None


# ------------------------------------------------- P5b: open (pure halves)
def validate_pin(pin, taker_map, now_epoch, ttl_h=PIN_TTL_H,
                 max_usd=DESK_MAX_SIZE):
    """Structural + policy validation of a desk pin. Returns (ok, reason).

    Engine calls this on every tick while pin mode is on - a rejected pin
    must never crash the window, only demote it to static pairs."""
    if not isinstance(pin, dict):
        return False, "pin is not an object"
    if pin.get("schema") != 1:
        return False, f"bad schema {pin.get('schema')!r}"
    coin = str(pin.get("coin") or "").strip().upper()
    sv = str(pin.get("short") or "").strip().lower()
    lv = str(pin.get("long") or "").strip().lower()
    if not coin or not sv or not lv:
        return False, "missing coin/short/long"
    if sv == lv:
        return False, "short == long"
    for v in (sv, lv):
        if v not in taker_map:
            return False, f"venue {v} outside our fee/book coverage"
    if pin.get("short_style") not in ("taker", "maker"):
        return False, f"bad short_style {pin.get('short_style')!r}"
    try:
        sz = float(pin.get("size_usd"))
    except (TypeError, ValueError):
        return False, "size_usd not a number"
    if not (0.0 < sz <= max_usd):
        return False, f"size_usd {sz} outside (0, {max_usd:.0f}]"
    try:
        created = datetime.strptime(str(pin.get("created")),
                                    "%Y-%m-%dT%H:%M:%SZ")
        created = created.replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return False, f"created not ISO Z ({pin.get('created')!r})"
    if now_epoch - created > ttl_h * 3600.0:
        return False, f"pin older than {ttl_h:.0f}h TTL"
    return True, "ok"


def pin_pair_dict(pin):
    """Validated pin -> engine_pairs() candidate dict shape."""
    sv = str(pin["short"]).lower()
    lv = str(pin["long"]).lower()
    return {
        "pair": f"{str(pin['coin']).upper()} {sv}->{lv}",
        "coin": str(pin["coin"]).upper(),
        "short": sv, "long": lv,
        "size_usd": float(pin["size_usd"]),
        "seq": "tt" if pin["short_style"] == "taker" else "sm_lt",
        "median_apr": pin.get("median_apr"),
        "src": "desk_pin",
    }


def resolve_combo(combos, short=None, long=None):
    """Pick the desk combo to pin from show_combo_rows output.

    Explicit short+long -> exact match (else None). Otherwise the best
    EXECUTABLE legs_ours combo by net30_tt: positive apr, known RT, and
    live books on both legs (pspread is only computed when both books
    exist - the executability proxy). None if the desk has nothing
    executable (engine gates stay the real authority)."""
    if short and long:
        s, l = str(short).lower(), str(long).lower()
        for c in combos:
            if c["short_venue"] == s and c["long_venue"] == l:
                return c
        return None
    cands = [c for c in combos
             if c.get("legs_ours") and c.get("apr") is not None
             and c["apr"] > 0 and c.get("net30_tt") is not None
             and c.get("pspread") is not None]
    if not cands:
        return None
    cands.sort(key=lambda c: c["net30_tt"], reverse=True)
    return cands[0]


def preflight_refusals(combo, ctx):
    """Desk-side fast-fail checks. The engine chain remains the authority;
    these only give the operator a fast, readable verdict BEFORE a window.

    ctx: kill(bool), kill_src, frozen(set of venues), active(bool),
    cooldown(bool), funding_ok(bool), books_ok(bool).
    Returns list of refusal reasons (empty = proceed to pin)."""
    out = []
    if ctx.get("kill"):
        out.append(f"kill-switch HALT ({ctx.get('kill_src')})")
    hit = (ctx.get("frozen") or set()) & \
          {combo["short_venue"], combo["long_venue"]}
    if hit:
        out.append(f"venue frozen: {sorted(hit)}")
    if ctx.get("active"):
        out.append("pair already has an active position")
    if ctx.get("cooldown"):
        out.append("pair on post-close cooldown")
    if not ctx.get("funding_ok"):
        out.append("live funding missing on one/both legs")
    if not ctx.get("books_ok"):
        out.append("live book missing on one/both legs")
    if combo.get("apr") is None or combo["apr"] <= 0:
        out.append(f"apr not positive ({combo.get('apr')})")
    return out


# ------------------------------------------------------------- row builders
def norm_venue(v):
    return str(v or "").lower().replace(".", "_").strip()


def scan_row_from_sharpe(r, medians):
    """Sharpe arb-table row -> desk row (Part 22 §3.1)."""
    coin = str(r.get("symbol", "") or r.get("base_coin", "")).upper()
    sv = norm_venue(r.get("shortExchange") or r.get("short_venue"))
    lv = norm_venue(r.get("longExchange") or r.get("long_venue"))
    if not coin or not sv or not lv:
        return None
    try:
        gross = float(r.get("apr") or 0) / 100.0
        net_sharpe = float(r.get("netApr") or 0) / 100.0
    except (TypeError, ValueError):
        gross = net_sharpe = None
    ois = r.get("oiShort")
    oil = r.get("oiLong")
    try:
        oi = min(float(ois), float(oil)) if (ois and oil) else None
    except (TypeError, ValueError):
        oi = None
    rt = rt_for_style(sv, lv, "tt")
    med = medians.get((coin, sv, lv))
    return {
        "coin": coin, "short_venue": sv, "long_venue": lv,
        "gross_apr": gross, "net_sharpe": net_sharpe,
        "net_ours_30d": net30_of(gross, rt) if gross is not None else None,
        "rt_tt_frac": rt,
        "oi_min_usd": oi,
        "depth_usd": _f(r.get("executableDepthUsd")),
        "exec_status": r.get("executionStatus"),
        "legs_ours": sv in OUR_VENUES and lv in OUR_VENUES,
        "median_apr_30d": med,
        "vs_median": (gross - med) if (gross is not None and med is not None)
                     else None,
    }


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def apply_filters(rows, args_like):
    """Common scan filters. args_like: min_apr, min_oi, venues, exclude,
    ours_only, top. Filter on the better of net_ours/net_sharpe."""
    venues = args_like.get("venues") or []
    if isinstance(venues, str):
        venues = [v.strip() for v in venues.split(",") if v.strip()]
    venues = set(venues)
    excl = args_like.get("exclude") or []
    if isinstance(excl, str):
        excl = [c.strip() for c in excl.split(",") if c.strip()]
    excl = {c.strip().upper() for c in excl if c}
    min_apr = args_like.get("min_apr")
    min_oi = args_like.get("min_oi")
    out = []
    for r in rows:
        if r is None:
            continue
        if excl and r["coin"] in excl:
            continue
        if venues and not (r["short_venue"] in venues
                           and r["long_venue"] in venues):
            continue
        if args_like.get("ours_only") and not r["legs_ours"]:
            continue
        if min_apr is not None:
            best = max(x for x in (r["net_ours_30d"], r["net_sharpe"])
                       if x is not None) if (
                r["net_ours_30d"] is not None
                or r["net_sharpe"] is not None) else None
            if best is None or best < min_apr:
                continue
        if min_oi is not None and (r["oi_min_usd"] is None
                                   or r["oi_min_usd"] < min_oi):
            continue
        out.append(r)
    out.sort(key=lambda r: max((x for x in (r["net_ours_30d"],
                                            r["net_sharpe"])
                                if x is not None), default=-9), reverse=True)
    top = args_like.get("top")
    return out[:top] if top else out


def show_combo_rows(fund_map, book_map, hist_map, rt_hold=HOLD_DAYS):
    """Build all ordered long/short combos for one coin.

    fund_map: {venue: {"rate": fr/ivl, "ivl_h": x, "next_time": ..,
                       "source": ..}}
    book_map: {venue: {"bid": px, "ask": px} | None}
    hist_map: {venue: [(ts_ms, hourly)]}
    Returns list of combo dicts sorted by apr desc."""
    venues = sorted(fund_map.keys())
    out = []
    for sv in venues:
        for lv in venues:
            if sv == lv:
                continue
            h_s = hourly_rate(fund_map[sv]["rate"], fund_map[sv]["ivl_h"])
            h_l = hourly_rate(fund_map[lv]["rate"], fund_map[lv]["ivl_h"])
            m = combo_math(h_s, h_l)
            rt_tt = rt_for_style(sv, lv, "tt")
            rt_mm = rt_for_style(sv, lv, "mm")
            bs = book_map.get(sv) or {}
            bl = book_map.get(lv) or {}
            row = {
                "short_venue": sv, "long_venue": lv,
                "apr": m["apr"], "f8h": m["f8h"],
                "rate_short": fund_map[sv]["rate"],
                "ivl_short": fund_map[sv]["ivl_h"],
                "rate_long": fund_map[lv]["rate"],
                "ivl_long": fund_map[lv]["ivl_h"],
                "next_short": fund_map[sv].get("next_time"),
                "next_long": fund_map[lv].get("next_time"),
                "src_short": fund_map[sv].get("source"),
                "src_long": fund_map[lv].get("source"),
                "rt_tt_frac": rt_tt, "rt_mm_frac": rt_mm,
                "net30_tt": net30_of(m["apr"], rt_tt),
                "net30_mm": net30_of(m["apr"], rt_mm),
                "max_f24h": max_spread_from_series(hist_map.get(sv, []),
                                                   hist_map.get(lv, []), 24),
                "max_f1h": max_spread_from_series(hist_map.get(sv, []),
                                                  hist_map.get(lv, []), 1),
                "pspread": pspread(bs.get("bid"), bl.get("ask")),
                "pspread_at_size": None, "slip_short_bps": None,
                "slip_long_bps": None, "legs_ours":
                    sv in OUR_VENUES and lv in OUR_VENUES,
            }
            out.append(row)
    out.sort(key=lambda r: (r["apr"] if r["apr"] is not None else -9),
             reverse=True)
    return out


def walk_size(book, side, size_usd, mid):
    """book=(bids,asks); side 'sell' walks bids, 'buy' walks asks.
    Returns (vwap, slip_bps) or (None, None)."""
    if not book:
        return None, None
    bids, asks = book
    levels = bids if side == "sell" else asks
    if not levels:
        return None, None
    r = ds.walk(levels, mid, size_usd)
    if r is None:
        return None, None
    slip, filled, vwap = r
    return vwap, slip


# ------------------------------------------------------------- P5c: basis
def spot_base_candidates(coin):
    """Venue-symbol base candidates for a Sharpe coin name.

    Exchanges prefix/rename low-price bases (1000PEPE) and Sharpe may use
    the other convention - try both directions, exact-match only."""
    c = str(coin).upper()
    out = [c]
    if c.startswith("1000") and len(c) > 4:
        out.append(c[4:])
    else:
        out.append("1000" + c)
    return out


def basis_math(h_p, spot_taker, perp_taker, basis_entry,
               hold_days=HOLD_DAYS):
    """Spot-long / perp-short carry math (P5c).

    h_p         hourly funding of the SHORT perp leg (fraction per hour)
    basis_entry (perp_bid - spot_ask) / spot_ask, locked at entry, one-time
    rt          round trip = 2 * (spot taker + perp taker), both legs taker
    net30       funding_apr - rt amortized over hold (basis NOT included)
    basis_apr   one-time basis annualized over hold (convergence assumption)
    breakeven_d funding days needed to cover the entry RT (basis excluded)
    """
    f_apr = apr_of(h_p)
    rt = None
    if spot_taker is not None and perp_taker is not None:
        rt = 2.0 * (spot_taker + perp_taker)
    net30 = net30_of(f_apr, rt)
    basis_apr = (basis_entry * 365.0 / hold_days
                 if basis_entry is not None else None)
    net30_incl = (net30 + basis_apr
                  if net30 is not None and basis_apr is not None else None)
    be = (rt / (h_p * 24.0)
          if (rt is not None and h_p is not None and h_p > 0) else None)
    return {"funding_apr": f_apr, "f8h": (h_p * 8.0 if h_p is not None
                                          else None),
            "rt_frac": rt, "net30": net30, "basis_apr": basis_apr,
            "net30_incl_basis": net30_incl, "breakeven_days": be}


def basis_row(coin, spot_v, perp_v, spot_ask, perp_bid, h_p,
              spot_taker=None, perp_taker=None):
    """One spot->perp combo row with honest Nones and risk flags.

    Returns None if either price is missing (never guessed)."""
    if not spot_ask or not perp_bid or spot_ask <= 0 or perp_bid <= 0:
        return None
    basis = (perp_bid - spot_ask) / spot_ask
    st = SPOT_TAKER.get(spot_v) if spot_taker is None else spot_taker
    pt = pl.TAKER.get(perp_v) if perp_taker is None else perp_taker
    m = basis_math(h_p, st, pt, basis)
    flags = []
    if h_p is None or h_p <= 0:
        flags.append("funding_not_positive")
    if basis < 0:
        flags.append("negative_basis")
    if m["rt_frac"] is None:
        flags.append("fee_unknown")
    div = abs(perp_bid / spot_ask - 1.0)
    if div > PX_DIV_MAX:
        flags.append(f"px_divergence_{div*100:.1f}pct")
    return {"coin": coin, "spot_venue": spot_v, "perp_venue": perp_v,
            "spot_ask": spot_ask, "perp_bid": perp_bid,
            "basis_entry": basis, "px_div": div,
            "h_perp": h_p, **m, "flags": flags,
            "basis_at_size": None, "slip_spot_bps": None,
            "slip_perp_bps": None, "min_f1h": None, "min_f24h": None,
            "pp_best_net30": None}


def min_hourly_from_series(series, window_h):
    """Min hourly funding in the last window_h of [(ts_ms, h)] sorted.

    For basis rows: the SHORT perp leg wants funding to STAY positive, so
    the flip-risk statistic is the MIN (mirror of max_f for perp-perp)."""
    if not series:
        return None
    cut = series[-1][0] - window_h * 3600_000
    vals = [h for ts, h in series if ts >= cut]
    return min(vals) if vals else None


def apply_basis_filters(rows, args_like):
    """Basis scan filters: min_fapr, min_basis, venues (perp),
    spot_venues, exclude, top. Sort by net30_incl_basis desc."""
    def _split(x):
        if isinstance(x, str):
            return [v.strip() for v in x.split(",") if v.strip()]
        return list(x or [])

    venues = set(_split(args_like.get("venues")))
    svenues = set(_split(args_like.get("spot_venues")))
    excl = {c.strip().upper() for c in _split(args_like.get("exclude")) if c}
    min_fapr = args_like.get("min_fapr")
    min_basis = args_like.get("min_basis")
    out = []
    for r in rows:
        if r is None:
            continue
        if excl and r["coin"] in excl:
            continue
        if venues and r["perp_venue"] not in venues:
            continue
        if svenues and r["spot_venue"] not in svenues:
            continue
        if min_fapr is not None and (r["funding_apr"] is None
                                     or r["funding_apr"] < min_fapr):
            continue
        if min_basis is not None and (r["basis_entry"] is None
                                      or r["basis_entry"] < min_basis):
            continue
        out.append(r)
    out.sort(key=lambda r: (r["net30_incl_basis"]
                            if r["net30_incl_basis"] is not None else -9),
             reverse=True)
    top = args_like.get("top")
    return out[:top] if top else out


# ------------------------------------------- P5c: spot feeds (key-free, 1 call
async def _get_json(url, params=None, timeout=20):
    async with ds._session.get(
            url, params=params,
            timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.json(content_type=None)


def _parse_spot_tickers(venue, j):
    """Venue spot ticker payload -> {base_upper: {"bid": px, "ask": px}}.
    USDT-quote symbols only. Junk/unknown payloads parse to {} (honest)."""
    out = {}
    if not isinstance(j, (dict, list)):
        return out
    if venue == "binance":
        rows = j if isinstance(j, list) else (j.get("data") or [])
        for r in rows:
            s = str(r.get("symbol") or "")
            if not s.endswith("USDT") or len(s) <= 4:
                continue
            try:
                out[s[:-4]] = {"bid": float(r["bidPrice"]),
                               "ask": float(r["askPrice"])}
            except (KeyError, TypeError, ValueError):
                continue
    elif venue == "bybit":
        rows = ((j.get("result") or {}).get("list") or []) \
            if isinstance(j, dict) else []
        for r in rows:
            s = str(r.get("symbol") or "")
            if not s.endswith("USDT") or len(s) <= 4:
                continue
            try:
                out[s[:-4]] = {"bid": float(r["bid1Price"]),
                               "ask": float(r["ask1Price"])}
            except (KeyError, TypeError, ValueError):
                continue
    elif venue == "okx":
        rows = (j.get("data") or []) if isinstance(j, dict) else []
        for r in rows:
            inst = str(r.get("instId") or "")
            if not inst.endswith("-USDT"):
                continue
            base = inst[:-5]
            if not base:
                continue
            try:
                out[base] = {"bid": float(r["bidPx"]),
                             "ask": float(r["askPx"])}
            except (KeyError, TypeError, ValueError):
                continue
    elif venue == "bitget":
        rows = (j.get("data") or []) if isinstance(j, dict) else []
        for r in rows:
            s = str(r.get("symbol") or "")
            if not s.endswith("USDT") or len(s) <= 4:
                continue
            try:
                out[s[:-4]] = {"bid": float(r["bidPr"]),
                               "ask": float(r["askPr"])}
            except (KeyError, TypeError, ValueError):
                continue
    return out


async def spot_ticker_map(venue):
    """Full-venue spot bookTicker in ONE call. {} on any failure (honest)."""
    urls = {
        "binance": "https://api.binance.com/api/v3/ticker/bookTicker",
        "bybit": "https://api.bybit.com/v5/market/tickers",
        "okx": "https://www.okx.com/api/v5/market/tickers",
        "bitget": "https://api.bitget.com/api/v2/spot/market/tickers",
    }
    params = {"category": "spot"} if venue == "bybit" else \
        ({"instType": "SPOT"} if venue == "okx" else None)
    if venue not in urls:
        return {}
    try:
        return _parse_spot_tickers(venue, await _get_json(urls[venue], params))
    except Exception:
        return {}


def _parse_spot_book(venue, j):
    """Venue spot depth payload -> (bids, asks) levels [(px, qty)]."""
    if not isinstance(j, dict):
        return None, None
    if venue == "binance":
        d = j
        lvl = lambda xs: [(float(p), float(q)) for p, q in
                          (xs or [])[:20] if float(q)]
        return lvl(d.get("bids")), lvl(d.get("asks"))
    if venue == "bybit":
        d = (j.get("result") or {}) if isinstance(j, dict) else {}
        lvl = lambda xs: [(float(p), float(q)) for p, q in
                          (xs or [])[:20] if float(q)]
        return lvl(d.get("b")), lvl(d.get("a"))
    if venue == "okx":
        rows = (j.get("data") or [{}]) if isinstance(j, dict) else [{}]
        d = rows[0]
        lvl = lambda xs: [(float(x[0]), float(x[1])) for x in
                          (xs or [])[:20] if len(x) >= 2 and float(x[1])]
        return lvl(d.get("bids")), lvl(d.get("asks"))
    if venue == "bitget":
        d = (j.get("data") or {}) if isinstance(j, dict) else {}
        lvl = lambda xs: [(float(p), float(q)) for p, q in
                          (xs or [])[:20] if float(q)]
        return lvl(d.get("bids")), lvl(d.get("asks"))
    return None, None


async def spot_book(venue, base):
    """Spot depth (bids, asks) for basis@size. None on any failure."""
    cands = spot_base_candidates(base)
    for c in cands:
        try:
            if venue == "binance":
                j = await _get_json(
                    "https://api.binance.com/api/v3/depth",
                    {"symbol": c + "USDT", "limit": 20})
            elif venue == "bybit":
                j = await _get_json(
                    "https://api.bybit.com/v5/market/orderbook",
                    {"category": "spot", "symbol": c + "USDT", "limit": 21})
            elif venue == "okx":
                j = await _get_json(
                    "https://www.okx.com/api/v5/market/books",
                    {"instId": c + "-USDT", "sz": 20})
            elif venue == "bitget":
                j = await _get_json(
                    "https://api.bitget.com/api/v2/spot/market/orderbook",
                    {"symbol": c + "USDT", "limit": 20})
            else:
                return None
            bids, asks = _parse_spot_book(venue, j)
            if bids and asks:
                return (bids, asks)
        except Exception:
            continue
    return None


async def pp_best_net30_by_coin():
    """Best executable our-venue perp-perp net30 per coin (arb feed).

    The 'does basis beat perp-perp' comparison column. {} on failure."""
    try:
        rows, _, _ = await vc.Sharpe.arb_table()
    except Exception:
        return {}
    medians = pl.load_medians()
    best = {}
    for r in rows:
        d = scan_row_from_sharpe(r, medians)
        if not d or not d["legs_ours"] or d["net_ours_30d"] is None:
            continue
        c = d["coin"]
        if c not in best or d["net_ours_30d"] > best[c]:
            best[c] = d["net_ours_30d"]
    return best


async def cmd_basis(args):
    """P5c: spot-perp basis scanner (Part 22 s.8).

    Two-pass, honest-data: pass 1 ranks combos from one Sharpe current call
    + one spot bookTicker call per spot venue; pass 2 re-derives survivors
    with NATIVE funding + real books (basis @ size) + history flip stats.
    Read-only: NO execution path - the engine has no spot leg yet; any
    future basis-execution ships with its own gate chain (scope doc rule)."""
    coin_rows = await vc.Sharpe.current()
    spot_maps = {v: await spot_ticker_map(v) for v in SPOT_VENUES}
    # pass 1: candidate (coin, perp venue) from our venues, ranked by f_apr
    cands = []
    for r in coin_rows:
        pv = norm_venue(r["venue"])
        if pv not in OUR_VENUES or not r.get("mark"):
            continue
        h_p = hourly_rate(r["rate"], r["interval_h"])
        f_apr = apr_of(h_p)
        if f_apr is None or f_apr < (args.min_fapr or 0.0):
            continue
        cands.append((r["coin"], pv, h_p, f_apr, float(r["mark"])))
    cands.sort(key=lambda x: x[3], reverse=True)
    cands = cands[:args.max_coins]

    rows = []
    for coin, pv, h_p, f_apr, perp_mark in cands:
        for sv in SPOT_VENUES:
            tmap = spot_maps.get(sv) or {}
            tick = None
            for base in spot_base_candidates(coin):
                if base in tmap and tmap[base]["ask"] > 0:
                    tick = tmap[base]
                    break
            if tick is None:
                continue
            row = basis_row(coin, sv, pv, tick["ask"], perp_mark, h_p)
            if row is None:
                continue
            rows.append(row)
    kept = apply_basis_filters(rows, {
        "min_fapr": None, "min_basis": args.min_basis,
        "venues": args.venues, "spot_venues": args.spot_venues,
        "exclude": args.exclude, "top": args.scan_keep})

    # pass 2: authoritative native funding + books + history for survivors.
    # Context is fetched ONCE per (coin, perp venue) - rows sharing a perp
    # leg reuse it (no duplicate API calls, no Sharpe/native mismatch
    # between spot venues on the same perp leg).
    pp_best = await pp_best_net30_by_coin()
    ctx = {}
    for r in kept:
        key = (r["coin"], r["perp_venue"])
        if key in ctx:
            continue
        h_native, pbook, ser = None, None, []
        try:
            nf = await vc.get_funding(r["perp_venue"], r["coin"])
            if nf and nf.get("rate") is not None:
                h_native = hourly_rate(float(nf["rate"]),
                                       float(nf.get("interval_h") or 8))
        except Exception:
            pass
        try:
            pbook = await pl.get_book(r["perp_venue"], r["coin"])
        except Exception:
            pbook = None
        try:
            h, _, _ = await vc.Sharpe.history(r["coin"], days=2)
            ser = sorted((t, hh) for t, rate, ivl in
                         h.get(r["perp_venue"], [])
                         if (hh := hourly_rate(rate, ivl)) is not None)
        except Exception:
            ser = []
        ctx[key] = {"h": h_native if h_native is not None else r["h_perp"],
                    "h_is_native": h_native is not None, "pbook": pbook,
                    "ser": ser}
    enriched = []
    for r in kept:
        c = ctx[(r["coin"], r["perp_venue"])]
        fund, pbook, ser = c["h"], c["pbook"], c["ser"]
        try:
            sbook = await spot_book(r["spot_venue"], r["coin"])
        except Exception:
            sbook = None
        row = basis_row(r["coin"], r["spot_venue"], r["perp_venue"],
                        r["spot_ask"], r["perp_bid"], fund)
        if row is None:
            continue
        row["px_src"] = "pass1"
        row["pp_best_net30"] = pp_best.get(r["coin"])
        if sbook and pbook:
            mid_s = (sbook[0][0][0] + sbook[1][0][0]) / 2.0
            mid_p = (pbook[0][0][0] + pbook[1][0][0]) / 2.0
            # executable prices win over the pass-1 mark proxy: recompute
            # the row from real best bid/ask (native-wins rule), then
            # walk @ size on top
            row2 = basis_row(r["coin"], r["spot_venue"], r["perp_venue"],
                             sbook[1][0][0], pbook[0][0][0], fund)
            if row2 is not None:
                row = row2
                row["px_src"] = "books"
                row["pp_best_net30"] = pp_best.get(r["coin"])
            vs, ss = walk_size(sbook, "buy", args.size_usd, mid_s)
            vp, sp = walk_size(pbook, "sell", args.size_usd, mid_p)
            if vs and vp and vs > 0:
                row["basis_at_size"] = (vp - vs) / vs
                row["slip_spot_bps"] = round(ss, 2) if ss is not None \
                    else None
                row["slip_perp_bps"] = round(sp, 2) if sp is not None \
                    else None
        if c["h_is_native"] and fund is not None:
            row["funding_apr"] = apr_of(fund)
            row["f8h"] = fund * 8.0
            m2 = basis_math(fund, SPOT_TAKER.get(r["spot_venue"]),
                            pl.TAKER.get(r["perp_venue"]),
                            row["basis_entry"])
            row.update({k: m2[k] for k in
                        ("rt_frac", "net30", "basis_apr",
                         "net30_incl_basis", "breakeven_days")})
        row["min_f1h"] = min_hourly_from_series(ser, 1)
        row["min_f24h"] = min_hourly_from_series(ser, 24)
        if row["min_f24h"] is not None and row["min_f24h"] <= 0:
            row["flags"].append("funding_flip_24h")
        enriched.append(row)
    kept2 = apply_basis_filters(enriched, {
        "min_fapr": args.min_fapr, "min_basis": args.min_basis,
        "venues": args.venues, "spot_venues": args.spot_venues,
        "exclude": args.exclude, "top": args.top})

    art = {"generated": nowiso(), "spot_venues": SPOT_VENUES,
           "spot_fee_taker": SPOT_TAKER, "size_usd": args.size_usd,
           "candidate_coins": len({c for c, _, _, _, _ in cands}),
           "pass1_rows": len(rows), "kept": len(kept2),
           "note": "read-only scanner; no execution path (engine has no "
                   "spot leg); net30_incl_basis assumes basis converges "
                   "over the 30d hold",
           "rows": kept2}
    print(f"\nDESK BASIS [pass1 rows={len(rows)} kept={len(kept2)} "
          f"coins={art['candidate_coins']}] size=${args.size_usd:.0f} "
          f"spot fee=10bps flat")
    hdr = (f"{'coin':10s} {'spot@venue':18s} {'perp@venue':10s} "
           f"{'f_apr':>7s} {'f8h':>7s} {'basis':>7s} {'b@size':>7s} "
           f"{'net30':>7s} {'n30+b':>7s} {'pp30':>7s} {'minF24h':>8s} "
           f"{'be_d':>5s} flags")
    print(hdr)
    for r in kept2[:args.print_rows]:
        pp = _pc(r["pp_best_net30"]) if r["pp_best_net30"] is not None \
            else "-"
        be_s = f"{r['breakeven_days']:.1f}" \
            if r["breakeven_days"] is not None else "-"
        print(f"{r['coin']:10s} {r['spot_venue']:18s} {r['perp_venue']:10s} "
              f"{_pc(r['funding_apr']):>7s} {_pc3(r['f8h']):>7s} "
              f"{_pc3(r['basis_entry']):>7s} {_pc3(r['basis_at_size']):>7s} "
              f"{_pc(r['net30']):>7s} {_pc(r['net30_incl_basis']):>7s} "
              f"{pp:>7s} {_pc3(r['min_f24h']):>8s} {be_s:>5s} "
              f"{','.join(r['flags']) or '-'}")
    if args.json:
        save_artifact(args.json, art)
    return art


# ------------------------------------------------------------- sessions
async def open_sessions():
    s = aiohttp.ClientSession(headers=UA)
    pl._session = s
    ds._session = s
    nv._session = s
    vc._session = None
    await vc.init()
    return s


def save_artifact(name, obj):
    os.makedirs(DESK_DIR, exist_ok=True)
    p = os.path.join(DESK_DIR, name)
    with open(p, "w") as fh:
        json.dump(obj, fh, indent=1, default=str)
    print(f"artifact: {p}")
    return p


def nowiso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------- cmd: scan
async def cmd_scan(args):
    rows, asof, stale = await vc.Sharpe.arb_table()
    medians = pl.load_medians()
    desk_rows = [scan_row_from_sharpe(r, medians) for r in rows]
    flt = {"min_apr": args.min_apr, "min_oi": args.min_oi,
           "venues": [v.strip() for v in args.venues.split(",")] if args.venues else None,
           "exclude": args.exclude.split(",") if args.exclude else None,
           "ours_only": args.ours_only, "top": args.top}
    kept = apply_filters(desk_rows, flt)
    art = {"asof": asof, "stale": bool(stale), "generated": nowiso(),
           "feed_rows": len(rows), "kept": len(kept), "filters": flt,
           "rows": kept}
    print(f"\nDESK SCAN [asof {asof} stale={stale}] feed={len(rows)} "
          f"kept={len(kept)}")
    hdr = (f"{'coin':12s} {'short->long':22s} {'net30ours':>9s} "
           f"{'netShrpe':>8s} {'OI$min':>10s} {'ours':>4s} {'med30':>7s}")
    print(hdr)
    for r in kept[:args.print_rows]:
        oi_s = f"{r['oi_min_usd']:.0f}" if r['oi_min_usd'] else '-'
        pair_s = r['short_venue'] + '->' + r['long_venue']
        print(f"{r['coin']:12s} {pair_s:22s} "
              f"{_pc(r['net_ours_30d']):>9s} {_pc(r['net_sharpe']):>8s} "
              f"{oi_s:>10s} "
              f"{'Y' if r['legs_ours'] else '-':>4s} {_pc(r['median_apr_30d']):>7s}")
    if args.json:
        save_artifact(args.json, art)
    return art


def _pc(x):
    return f"{x*100:.1f}%" if x is not None else "-"


def _pc3(x):
    """3-decimal percent for small fractions (f8h, maxF, pspread)."""
    return f"{x*100:.3f}%" if x is not None else "-"


# ------------------------------------------------------------- cmd: show
async def gather_show(coin, size_usd):
    """Live show-data bundle for one coin (shared by `show` and `open`).
    Returns the show artifact dict (combos sorted apr desc)."""
    coin = coin.upper()
    # 1) live funding: native our-venues first, Sharpe fill
    fund_map = {}
    for v in sorted(OUR_VENUES):
        try:
            r = await vc.get_funding(v, coin)
            if r and r.get("rate") is not None:
                fund_map[v] = {"rate": float(r["rate"]),
                               "ivl_h": float(r.get("interval_h") or 8),
                               "next_time": r.get("next_time"),
                               "source": r.get("source", "native")}
        except Exception:
            continue
    try:
        sh = await pl.sharpe_current([coin]) or {}
        for v, (rate, ivl) in sh.items():
            if v not in fund_map and rate is not None:
                fund_map[v] = {"rate": float(rate), "ivl_h": float(ivl or 8),
                               "next_time": None, "source": "sharpe"}
    except Exception:
        pass
    # 2) books (our venues with fetchers)
    book_map, raw_books = {}, {}
    for v in sorted(OUR_VENUES):
        try:
            bk = await pl.get_book(v, coin)
        except Exception:
            bk = None
        if bk and bk[0] and bk[1]:
            bids, asks = bk
            bid, ask = bids[0][0], asks[0][0]
            book_map[v] = {"bid": bid, "ask": ask,
                           "mid": (bid + ask) / 2.0}
            raw_books[v] = bk
        else:
            book_map[v] = None
    # 3) history series for Max F
    hist_map = {}
    try:
        h, hasof, hstale = await vc.Sharpe.history(coin, days=2)
        for v, rows in h.items():
            ser = [(t, hourly_rate(rate, ivl)) for t, rate, ivl in rows]
            ser = [(t, h_) for t, h_ in ser if h_ is not None]
            ser.sort()
            hist_map[v] = ser
        hist_meta = {"asof": hasof, "stale": bool(hstale), "days": 2}
    except Exception as e:
        hist_meta = {"error": str(e)[:100]}
    # 4) combos + P-spread @ size
    combos = show_combo_rows(fund_map, book_map, hist_map)
    for c in combos:
        if size_usd and book_map.get(c["short_venue"]) and \
                book_map.get(c["long_venue"]):
            bs = raw_books.get(c["short_venue"])
            bl = raw_books.get(c["long_venue"])
            mids = book_map[c["short_venue"]]["mid"], \
                book_map[c["long_venue"]]["mid"]
            vs, ss = walk_size(bs, "sell", size_usd, mids[0])
            vl, sl = walk_size(bl, "buy", size_usd, mids[1])
            if vs and vl and vl > 0:
                c["pspread_at_size"] = (vs - vl) / vl
                c["slip_short_bps"] = round(ss, 2) if ss is not None else None
                c["slip_long_bps"] = round(sl, 2) if sl is not None else None
                c["rt_at_size_frac"] = (c["rt_tt_frac"] or 0) + \
                    ((ss or 0) + (sl or 0)) / 1e4
                c["net30_tt_at_size"] = net30_of(c["apr"],
                                                 c["rt_at_size_frac"])
    art = {"coin": coin, "generated": nowiso(), "size_usd": size_usd,
           "fund_map": fund_map, "book_map": book_map,
           "hist_meta": hist_meta, "combos": combos}
    return art


async def cmd_show(args):
    art = await gather_show(args.coin.upper(), args.size_usd)
    coin, combos, fund_map, book_map = (art["coin"], art["combos"],
                                        art["fund_map"], art["book_map"])
    print(f"\nDESK SHOW {coin} [venues funded={len(fund_map)} "
          f"books={sum(1 for b in book_map.values() if b)}]")
    print(f"{'short->long':22s} {'apr':>8s} {'f8h':>8s} {'net30tt':>8s} "
          f"{'pSprd':>8s} {'pSprd@sz':>8s} {'maxF24h':>8s} {'src':>10s}")
    for c in combos[:args.print_rows]:
        src = "+".join(sorted({c['src_short'] or '?', c['src_long'] or '?'}))
        print(f"{c['short_venue'] + '->' + c['long_venue']:22s} "
              f"{_pc(c['apr']):>8s} {_pc3(c['f8h']):>8s} "
              f"{_pc(c['net30_tt']):>8s} {_pc3(c['pspread']):>8s} "
              f"{_pc3(c['pspread_at_size']):>8s} {_pc3(c['max_f24h']):>8s} "
              f"{src[:10]:>10s}")
    if args.json:
        save_artifact(f"show_{coin}_latest.json", art)
    return art


# ------------------------------------------------------------- cmd: open
def engine_state(con_path, pair_name):
    """Read-only engine DB peek (own connection, always closed).
    Returns dict(active, cooldown) for the pair."""
    import sqlite3

    import v3_exec as ex
    out = {"active": False, "cooldown": False}
    con = sqlite3.connect(con_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT 1 FROM exec_positions WHERE pair=? AND status IN "
            "('seq_open','open','unwinding') LIMIT 1", (pair_name,)).fetchone()
        out["active"] = row is not None
        r = con.execute(
            "SELECT ts_open FROM exec_positions WHERE pair=? AND status IN "
            "('cancelled','closed') ORDER BY ts_open DESC LIMIT 1",
            (pair_name,)).fetchone()
        out["cooldown"] = bool(r and (ex.nows() - ex.iso2ts(r["ts_open"])
                                      < ex.COOLDOWN_S))
    finally:
        con.close()
    return out


def write_pin(path, pin):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(pin, fh, indent=1, default=str)
    return path


async def cmd_open(args):
    """P5b: gated on-demand entry = preflight -> pin -> engine window.

    The pin only ADDS a candidate to the engine's entry checks; every gate
    (kill, cooldown, freeze, TW1-TW6, M5 sizing, pair-cap clamp) still runs
    in the engine exactly as for static pairs. Refusals here are fast
    operator feedback, not the safety mechanism."""
    import v3_exec as ex
    coin = args.coin.upper()
    art = await gather_show(coin, args.size_usd)
    combos = art["combos"]
    combo = resolve_combo(combos, args.short, args.long)
    if combo is None:
        if args.short and args.long:
            print(f"OPEN REFUSE {coin}: combo {args.short.lower()}->"
                  f"{args.long.lower()} not among live combos")
        else:
            print(f"OPEN REFUSE {coin}: no executable our-venue combo "
                  f"(positive apr + net30 + both books) in live data "
                  f"({len(combos)} combos seen)")
        return 2
    sv, lv = combo["short_venue"], combo["long_venue"]
    pair_name = f"{coin} {sv}->{lv}"

    # ---- engine-state context (read-only)
    import v3_portfolio as pf
    ctx = {"kill": False, "kill_src": None, "frozen": set()}
    halt, ksrc = pf.kill_halt()
    # kill_halt is fail-safe: stale/missing kill file also returns halt.
    # The ENGINE re-evaluates at window start (ensure_kill_fresh -> kill_eval
    # on live DB state), so desk only hard-refuses on an EVALUATED halt;
    # staleness becomes a warning (engine may still halt itself there).
    if halt and str(ksrc).startswith("evaluated"):
        ctx["kill"], ctx["kill_src"] = True, ksrc
    elif halt:
        ctx["kill_warn"] = f"kill state not fresh ({ksrc}); engine " \
                           f"re-evaluates at window start"
    try:
        st_state = engine_state(ex.V3DB, pair_name)
        ctx["active"] = st_state["active"]
        ctx["cooldown"] = st_state["cooldown"]
    except Exception as e:
        print(f"OPEN REFUSE {coin}: engine DB unreadable ({e!r})")
        return 2
    import v3_store as _st
    con = None
    try:
        import sqlite3 as _sq
        con = _sq.connect(ex.V3DB)
        con.row_factory = _sq.Row
        frozen = {v for v in (sv, lv) if _st.kv_get(con, f"venue_freeze_{v}")}
        ctx["frozen"] = frozen
    finally:
        if con:
            con.close()

    # data presence feeds the same ctx the pure preflight reads
    ctx["funding_ok"] = (sv in art["fund_map"]) and (lv in art["fund_map"])
    ctx["books_ok"] = bool(art["book_map"].get(sv)) and \
        bool(art["book_map"].get(lv))
    refusals = preflight_refusals(combo, ctx)

    med = pl.load_medians().get((coin, sv, lv))
    warn = []
    if med is None:
        warn.append("no 30d median for this combo - engine TW2 will FAIL "
                    "the pre-entry chain (conservative block)")
    if ctx.get("kill_warn"):
        warn.append(ctx["kill_warn"])

    print(f"\nDESK OPEN {pair_name} size=${args.size_usd:.0f}/leg "
          f"style={args.style} mode={args.mode}")
    print(f"  combo live: apr={_pc(combo.get('apr'))} "
          f"f8h={_pc3(combo.get('f8h'))} net30tt={_pc(combo.get('net30_tt'))} "
          f"psprd@sz={_pc3(combo.get('pspread_at_size'))} "
          f"med30={_pc(med)}")
    for w in warn:
        print(f"  WARN {w}")
    if refusals:
        print("  REFUSED by preflight:")
        for r in refusals:
            print(f"    - {r}")
        return 2

    pin = {"schema": 1, "created": nowiso(), "ttl_h": PIN_TTL_H,
           "coin": coin, "short": sv, "long": lv,
           "size_usd": float(args.size_usd), "short_style": args.style,
           "median_apr": med,
           "provenance": {
               "apr": combo.get("apr"), "f8h": combo.get("f8h"),
               "net30_tt": combo.get("net30_tt"), "pspread": combo.get("pspread"),
               "pspread_at_size": combo.get("pspread_at_size"),
               "max_f24h": combo.get("max_f24h"),
               "show_generated": art["generated"]},
           "preflight": {"refusals": [], "warnings": warn}}
    p = write_pin(PIN_PATH, pin)
    print(f"  pin written: {p} (TTL {PIN_TTL_H:.0f}h; engine gates unchanged)")
    ok, why = validate_pin(pin, pl.TAKER,
                           datetime.now(timezone.utc).timestamp())
    if not ok:                                   # defensive: never ship a bad pin
        print(f"  self-validation FAILED ({why}); pin removed")
        os.remove(p)
        return 2

    if args.no_run:
        print(f"  --no-run: start the window with:\n    python3 "
              f"scripts/v3_exec.py run --minutes <m> --every 20 --pin-desk "
              f"[--mode live]")
        return 0
    exe_dir = os.path.dirname(os.path.abspath(__file__))
    cmdp = [sys.executable, os.path.join(exe_dir, "v3_exec.py"),
            "run", "--minutes", str(args.minutes), "--every", str(args.every),
            "--pin-desk", "--mode", args.mode]
    print(f"  engine window: {' '.join(cmdp)}\n")
    rc = subprocess.call(cmdp)
    print(f"\n  engine window exit={rc}; status: python3 scripts/v3_exec.py "
          f"status")
    return rc


# ------------------------------------------------------------- cmd: history
async def cmd_history(args):
    coin = args.coin.upper()
    h, asof, stale = await vc.Sharpe.history(coin, days=args.days)
    out = {"coin": coin, "asof": asof, "stale": bool(stale),
           "days": args.days, "generated": nowiso(), "venues": {}}
    series_by_venue = {}          # always kept, for CSV; JSON only if flagged
    for v, rows in h.items():
        ser = [(t, hourly_rate(rate, ivl)) for t, rate, ivl in rows]
        ser = sorted((t, x) for t, x in ser if x is not None)
        if not ser:
            continue
        series_by_venue[v] = ser
        vals = sorted(x for _, x in ser)
        n = len(vals)
        out["venues"][v] = {
            "n": n,
            "first": datetime.fromtimestamp(ser[0][0] / 1000,
                                            tz=timezone.utc).isoformat(),
            "last": datetime.fromtimestamp(ser[-1][0] / 1000,
                                           tz=timezone.utc).isoformat(),
            "mean_hourly": sum(vals) / n,
            "p05": vals[int(0.05 * n)], "p50": vals[n // 2],
            "p95": vals[min(n - 1, int(0.95 * n))],
            "min": vals[0], "max": vals[-1],
            "series": ser if args.include_series else None,
        }
    print(f"\nDESK HISTORY {coin} [{args.days}d, asof {asof} stale={stale}]")
    print(f"{'venue':10s} {'n':>6s} {'mean_h':>9s} {'p50':>9s} {'p95':>9s} "
          f"{'min':>9s} {'max':>9s} last")
    for v, d in sorted(out["venues"].items()):
        print(f"{v:10s} {d['n']:>6d} {_pc3(d['mean_hourly']):>9s} "
              f"{_pc3(d['p50']):>9s} {_pc3(d['p95']):>9s} "
              f"{_pc3(d['min']):>9s} {_pc3(d['max']):>9s} {d['last'][:16]}")
    if args.csv:
        os.makedirs(DESK_DIR, exist_ok=True)
        p = os.path.join(DESK_DIR, f"history_{coin}.csv")
        with open(p, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["ts_iso", "venue", "rate_hourly"])
            for v, ser in series_by_venue.items():
                for t, x in ser:
                    w.writerow([datetime.fromtimestamp(
                        t / 1000, tz=timezone.utc).isoformat(), v, x])
        print(f"csv: {p}")
    if args.json:
        save_artifact(f"history_{coin}.json", out)
    return out


# ------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(description="P5a headless arbitrage desk")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="cross-venue strategy table")
    s.add_argument("--top", type=int, default=25)
    s.add_argument("--print-rows", type=int, default=20)
    s.add_argument("--min-apr", type=float, default=None)
    s.add_argument("--min-oi", type=float, default=None)
    s.add_argument("--venues", type=str, default=None)
    s.add_argument("--exclude", type=str, default=None)
    s.add_argument("--ours-only", action="store_true")
    s.add_argument("--json", type=str, default="scan_latest.json")
    s.set_defaults(fn=cmd_scan)

    d = sub.add_parser("show", help="per-coin deep dive")
    d.add_argument("coin")
    d.add_argument("--size-usd", type=float, default=250.0)
    d.add_argument("--print-rows", type=int, default=12)
    d.add_argument("--json", type=str, default=None)
    d.set_defaults(fn=cmd_show)

    h = sub.add_parser("history", help="funding series export")
    h.add_argument("coin")
    h.add_argument("--days", type=int, default=30)
    h.add_argument("--csv", action="store_true")
    h.add_argument("--include-series", action="store_true")
    h.add_argument("--json", type=str, default=None)
    h.set_defaults(fn=cmd_history)

    b = sub.add_parser("basis", help="P5c spot-perp basis scanner (spot long "
                                     "+ perp short; read-only)")
    b.add_argument("--min-fapr", type=float, default=0.10,
                   help="min funding APR of the short perp leg (default 0.10)")
    b.add_argument("--min-basis", type=float, default=None,
                   help="min entry basis (perp_bid - spot_ask)/spot_ask")
    b.add_argument("--venues", type=str, default=None,
                   help="perp venue filter (comma-separated)")
    b.add_argument("--spot-venues", type=str, default=None,
                   help="spot venue filter (comma-separated)")
    b.add_argument("--exclude", type=str, default=None)
    b.add_argument("--size-usd", type=float, default=250.0)
    b.add_argument("--max-coins", type=int, default=25,
                   help="pass-1 candidate (coin,venue) cap by funding APR")
    b.add_argument("--scan-keep", type=int, default=12,
                   help="rows carried into pass 2 (native + books + history)")
    b.add_argument("--top", type=int, default=20)
    b.add_argument("--print-rows", type=int, default=15)
    b.add_argument("--json", type=str, default="basis_latest.json")
    b.set_defaults(fn=cmd_basis)

    o = sub.add_parser("open", help="P5b gated on-demand entry: preflight "
                                    "-> pin -> engine window")
    o.add_argument("coin")
    o.add_argument("--short", type=str, default=None,
                   help="pin a specific short venue (default: best "
                        "net30tt our-venue combo)")
    o.add_argument("--long", type=str, default=None,
                   help="pin a specific long venue")
    o.add_argument("--size-usd", type=float, default=DESK_DEFAULT_SIZE)
    o.add_argument("--style", choices=["taker", "maker"], default="taker",
                   help="short-leg entry style (default taker; maker is "
                        "the sm_lt sequence)")
    o.add_argument("--minutes", type=float, default=8.0)
    o.add_argument("--every", type=float, default=20.0)
    o.add_argument("--mode", choices=["paper", "live"], default="paper",
                   help="engine adapter mode (live = P4a DRY_RUN)")
    o.add_argument("--no-run", action="store_true",
                   help="preflight + pin only; print the engine command")
    o.set_defaults(fn=cmd_open)

    args = ap.parse_args()

    async def run():
        ses = await open_sessions()
        try:
            await args.fn(args)
        finally:
            await ses.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
