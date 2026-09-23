#!/usr/bin/env python3
"""v3 M4 event monitors (spec Part 12 §3.4, build P4 / Part 16).

Monitors (all log-only; the engine consumes their outputs):
  e1r       daily cap-slam sweep, SIGN-ONLY + NAMES-ONLY: negative live
            funding on E1r venues (kraken bulk verified; htx / extended /
            crypto_com / variational best-effort graceful-down) for bases
            we could short elsewhere. Magnitude unverified -> names only;
            position only after native history resolves the unit question
            (spec open item 4).
  listings  daily E2/E3 (Sharpe): new-listing diff (floor-pin watch
            candidate, the BTW pattern) + suspension/delist lifecycle
            flags on any HELD leg -> alert (engine trigger_listings
            performs the unwind).
  floor     hourly floor-parameter watch: standalone TW5-style probes
            across ALL native param venues x their listed basket bases;
            param change -> venue_freeze_{venue} kv + FLOOR_PARAM_CHANGE
            event (= kill source #1 evidence).
  health    daily system health: tripwire fail rates split held vs
            pre-entry, settlement top10 (kv), funding staleness (36h),
            Sharpe-vs-native disagreement baseline (kill source #3 ref).

CLI: e1r | listings | floor | health | all
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

import aiohttp
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import v3_store as st                 # noqa: E402
import v3_connectors as vc            # noqa: E402
import v3_tripwires as tw             # noqa: E402
import depth_sampler as ds            # noqa: E402
import depth_newvenue as nv           # noqa: E402
import paper_ledger as pl             # noqa: E402  (P0 DB path)

E1R_VENUES = ("kraken", "htx", "extended", "crypto_com", "variational")
STALENESS_H = 36
DISAGREE_ABS_CAP = 0.50   # abs APR gap treated as outright spike, fraction
GUARD_S = {"e1r": 86400, "listings": 86400, "floor": 3600, "health": 86400}


def nows():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ev(con, etype, coin, venue, detail):
    con.execute("INSERT INTO events(ts, etype, coin, venue, detail) "
                "VALUES(?,?,?,?,?)", (nows(), etype, coin, venue, detail))
    con.commit()


def guard(con, key, seconds):
    """True (and set timestamp) if `seconds` elapsed since last run."""
    k = f"m4_{key}"
    last = st.kv_get(con, k)
    if last is not None:
        try:
            if time.time() - float(last) < seconds:
                return False
        except (TypeError, ValueError):
            pass
    st.kv_set(con, k, str(time.time()))
    return True


def held_positions(con):
    """Open/unwinding engine positions + open P0 positions."""
    out = []
    for r in con.execute("SELECT pos_id, coin, short_venue, long_venue, "
                         "status FROM exec_positions "
                         "WHERE status IN ('open','unwinding')").fetchall():
        out.append(dict(zip(["pos_id", "coin", "short_venue", "long_venue",
                             "status"], r)))
    try:
        p0 = sqlite3.connect(pl.DB)
        for r in p0.execute("SELECT pos_id, coin, short_venue, long_venue "
                            "FROM positions WHERE status='open'").fetchall():
            out.append(dict(zip(["pos_id", "coin", "short_venue",
                                 "long_venue", "status"],
                                list(r) + ["p0_open"])))
        p0.close()
    except Exception:
        pass
    return out


def basket_bases(con):
    rows = con.execute("SELECT DISTINCT base FROM funding_obs").fetchall()
    return sorted({r[0] for r in rows if r[0]})


# ------------------------------------------------------------------ E1r
async def _kraken_all(session):
    """Bulk verified: futures.kraken.com tickers carry fundingRate."""
    async with session.get("https://futures.kraken.com/derivatives/api/v3/"
                           "tickers", timeout=aiohttp.ClientTimeout(total=20))\
            as r:
        d = await r.json(content_type=None)
    out = {}
    for t in d.get("tickers", []):
        sym = t.get("symbol", "")            # e.g. "PF_XMRUSD"
        if not sym.startswith("PF_"):
            continue
        base = sym[3:].split("USD")[0].upper()
        try:
            fr = float(t.get("fundingRate", 0))
        except (TypeError, ValueError):
            continue
        if base:
            out[base] = fr                    # per 1h, sign-only use
    return out


async def _htx_coins(session, bases):
    out = {}
    for b in sorted(bases):
        try:
            u = ("https://api.hbdm.com/linear-swap-api/v1/swap_funding_rate"
                 f"?contract_code={b}-USDT")
            async with session.get(u, timeout=aiohttp.ClientTimeout(
                    total=8)) as r:
                d = await r.json(content_type=None)
            if isinstance(d.get("data"), list) and d["data"]:
                out[b] = float(d["data"][0].get("funding_rate", 0) or 0)
        except Exception:
            pass
    return out


async def _extended_coins(session, bases):
    out = {}
    try:
        u = "https://api.extended.exchange/api/v1/perps/summary"
        async with session.get(u, timeout=aiohttp.ClientTimeout(total=15))\
                as r:
            d = await r.json(content_type=None)
        rows = d.get("data", []) or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        for it in rows:
            m = (it.get("marketInfo", {}) or {}).get("name", "") or ""
            b = m.split("-")[0].split("USD")[0].upper()
            if b in bases:
                try:
                    out[b] = float((it.get("stats", {}) or {})
                                   .get("fundingRate", 0) or 0)
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    return out


async def _crypto_com_coins(session, bases):
    out = {}
    for b in sorted(bases):
        try:
            u = ("https://api.crypto.com/exchange/v1/public/get-funding-"
                 f"history?instrument_name={b}_USD-PERP&page_size=1")
            async with session.get(u, timeout=aiohttp.ClientTimeout(
                    total=8)) as r:
                d = await r.json(content_type=None)
            rows = ((d.get("result") or {}).get("data")) or []
            if rows:
                out[b] = float(rows[0].get("rate", 0) or 0)
        except Exception:
            pass
    return out


async def _variational_coins(session, bases):
    return {}   # endpoint still probe-only; graceful-down per spec


E1R_FETCHERS = {"kraken": _kraken_all,
                "htx": _htx_coins,
                "extended": _extended_coins,
                "crypto_com": _crypto_com_coins,
                "variational": _variational_coins}


async def run_e1r(con, verbose=True, force=False):
    if not force and not guard(con, "e1r", GUARD_S["e1r"]):
        if verbose:
            print("e1r: cadence guard, skip")
        return []
    bases = set(basket_bases(con))
    negatives, venue_status = [], {}
    async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0"}) as session:
        for v in E1R_VENUES:
            try:
                res = await E1R_FETCHERS[v](session, bases) \
                    if v != "kraken" else await E1R_FETCHERS[v](session)
                venue_status[v] = f"{len(res)} symbols"
                for b, fr in res.items():
                    if fr < 0 and b in bases:
                        negatives.append({"venue": v, "base": b})
            except Exception as e:
                venue_status[v] = f"down: {str(e)[:60]}"
    # names-only: dedup bases, note which venues flagged them
    by_base = {}
    for n in negatives:
        by_base.setdefault(n["base"], []).append(n["venue"])
    catch = [{"base": b, "negative_on": vs} for b, vs in sorted(by_base.items())]
    st.kv_set(con, "m4_e1r_last", json.dumps(
        {"ts": nows(), "catch": catch, "venues": venue_status}))
    if catch:
        ev(con, "E1R_CATCH", ",".join(c["base"] for c in catch), "e1r",
           json.dumps(catch))
    if verbose:
        print(f"e1r: {len(catch)} negative-funding basket names "
              f"(sign-only, magnitude pending open item 4)")
        for c in catch:
            print(f"  {c['base']}: negative on {','.join(c['negative_on'])}")
        for v, s in venue_status.items():
            print(f"  [{v}] {s}")
    return catch


# ------------------------------------------------------------------ E2/E3
async def run_listings(con, verbose=True, force=False):
    if not force and not guard(con, "listings", GUARD_S["listings"]):
        if verbose:
            print("listings: cadence guard, skip")
        return [], []
    rows, asof, stale = await vc.Sharpe.listings()
    # shape: {"rows": [{token_symbol, exchange, listing_date, ...}]}
    if isinstance(rows, dict):
        rows = rows.get("rows") or rows.get("data") or []
    if stale or not isinstance(rows, list):
        ev(con, "M4_LISTINGS_STALE", "-", "sharpe",
           f"stale={stale} rows={len(rows) if isinstance(rows, list) else 0}")
        if verbose:
            print(f"listings: STALE feed (asof {asof}) - alert only")
        return [], []
    live = {}
    for r in rows:
        b = str(r.get("token_symbol", r.get("base_coin", "")) or "").upper()
        if b:
            live[b] = {"exchange": r.get("exchange"),
                       "listing_date": r.get("listing_date")}
    base_kv = st.kv_get(con, "m4_listing_baseline")
    new_names, delist_alerts = [], []
    if base_kv is None or not json.loads(base_kv).get("names"):
        # first run (or empty prior baseline): set silently, no E2 spam
        st.kv_set(con, "m4_listing_baseline", json.dumps(
            {"ts": nows(), "names": sorted(live)}))
        if verbose:
            print(f"listings: baseline set ({len(live)} names)")
    else:
        old = set(json.loads(base_kv).get("names", []))
        cur = set(live)
        for b in sorted(cur - old):
            new_names.append(b)
            ev(con, "E2_NEW_LISTING", b, str(live[b].get("exchange")),
               json.dumps(live[b]))
        for b in sorted(old - cur):
            ev(con, "E3_DELIST_GONE", b, "sharpe", "name absent from feed")
        st.kv_set(con, "m4_listing_baseline", json.dumps(
            {"ts": nows(), "names": sorted(live)}))
    # E3 lifecycle flags on HELD legs via sharpe funding coverage
    held = held_positions(con)
    seen_coins = {}
    for p in held:
        c = p["coin"]
        if c not in seen_coins:
            try:
                cur_rows = await vc.Sharpe.current(c)
                seen_coins[c] = {str(r.get("venue", "")).lower()
                                 for r in cur_rows}
            except Exception:
                seen_coins[c] = None   # probe error: stay silent, retry next day
        venues = seen_coins[c]
        if venues is None:
            continue
        if not venues:
            delist_alerts.append({"coin": c, "flag": "no sharpe coverage"})
            ev(con, "E3_HELD_ALERT", c, "sharpe",
               "held coin has zero sharpe funding coverage")
        else:
            miss = {p["short_venue"], p["long_venue"]} - venues
            if miss == {p["short_venue"], p["long_venue"]}:
                delist_alerts.append({"coin": c, "flag": f"held venues absent {sorted(miss)}"})
                ev(con, "E3_HELD_ALERT", c, "sharpe",
                   f"held venues missing from sharpe: {sorted(miss)}")
    if verbose:
        print(f"listings: feed {len(live)} names, +{len(new_names)} new, "
              f"{len(delist_alerts)} held alerts")
        for n in new_names:
            print(f"  NEW {n} on {live[n].get('exchange')} "
                  f"{live[n].get('listing_date')}")
        for a in delist_alerts:
            print(f"  HELD ALERT {a['coin']}: {a['flag']}")
    return new_names, delist_alerts


# ------------------------------------------------------------------ floor
async def run_floor_watch(con, verbose=True, force=False):
    if not force and not guard(con, "floor", GUARD_S["floor"]):
        if verbose:
            print("floor: cadence guard, skip")
        return []
    held = held_positions(con)
    held_pairs = {(p["short_venue"], p["coin"]) for p in held} | \
                 {(p["long_venue"], p["coin"]) for p in held}
    changed, probed = [], 0
    async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0"}) as session:
        for v, fn in tw.PARAM_FETCHERS.items():
            # bases to probe: held legs on this venue + a per-venue sample
            # of basket bases listed there (keep hourly call budget sane)
            bases = {c for (vv, c) in held_pairs if vv == v}
            try:
                syms = await ds_exchangeInfo_symbols(v)
            except Exception:
                syms = set()
            if syms:
                for b in basket_bases(con):
                    if b in syms and len(bases) < 40:
                        bases.add(b)
            for b in sorted(bases):
                try:
                    p = await fn(b)
                except Exception as e:
                    if verbose:
                        print(f"  floor {v}/{b}: ERR {str(e)[:60]}")
                    continue
                if p is None:
                    continue
                probed += 1
                key = f"floor_params_{v}_{b}"
                base = st.kv_get(con, key)
                if base is None:
                    st.kv_set(con, key, json.dumps(p))
                    continue
                if json.loads(base) != p:
                    changed.append({"venue": v, "coin": b,
                                    "old": json.loads(base), "new": p})
                    st.kv_set(con, key, json.dumps(p))
                    st.kv_set(con, f"venue_freeze_{v}", nows())
                    ev(con, "FLOOR_PARAM_CHANGE", b, v, json.dumps(
                        {"old": json.loads(base), "new": p,
                         "path": "m4-floor-watch"}))
    if verbose:
        print(f"floor: {probed} probes, {len(changed)} changes")
        for c in changed:
            print(f"  CHANGE {c['venue']}/{c['coin']}: "
                  f"{c['old']} -> {c['new']} (venue frozen)")
    return changed


async def ds_exchangeInfo_symbols(v):
    """Lightweight per-venue symbol list for probe selection."""
    urls = {"aster": "https://fapi.asterdex.com/fapi/v1/exchangeInfo",
            "binance": "https://fapi.binance.com/fapi/v1/exchangeInfo",
            "dydx": None, "hl": None, "backpack": None}
    u = urls.get(v)
    if not u:
        return set()
    async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0"}) as session:
        async with session.get(u, timeout=aiohttp.ClientTimeout(total=15))\
                as r:
            d = await r.json(content_type=None)
    out = set()
    for s in d.get("symbols", []):
        if str(s.get("contractType", s.get("type", ""))).upper() in \
                ("PERPETUAL", "PERP") and \
                str(s.get("status", "TRADING")).upper() == "TRADING":
            b = str(s.get("baseAsset", s.get("symbol", "")))
            out.add(b.upper())
    return out


# ------------------------------------------------------------------ health
def run_health(con, verbose=True, force=False):
    if not force and not guard(con, "health", GUARD_S["health"]):
        if verbose:
            print("health: cadence guard, skip")
        return {}
    out = {}
    # 1. tripwire fail rates, held vs pre-entry split, last 24h
    # (tripwire_log.ts is an ISO string - lexicographic compare is safe)
    lo = (datetime.now(timezone.utc).timestamp() - 86400)
    lo_iso = datetime.fromtimestamp(lo, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    fails = {}
    for mode in ("held_check", "pre_entry"):
        rows = con.execute(
            "SELECT tw, result, COUNT(*) FROM tripwire_log "
            "WHERE ts > ? AND mode = ? AND tw != 'CHAIN' "
            "GROUP BY tw, result", (lo_iso, mode)).fetchall()
        agg = {}
        for t, res, n in rows:
            d = agg.setdefault(t, {})
            d[res] = d.get(res, 0) + n
        fails[mode] = agg
    out["tripwire_fails_24h"] = fails
    # 2. settlement top10 (from scanner kv census)
    for (k,) in con.execute("SELECT key FROM kv WHERE key LIKE "
                            "'settlement_top_%'").fetchall():
        out["settlement_top"] = json.loads(st.kv_get(con, k))
        out["settlement_top_asof"] = k
    # 3. funding staleness on held legs
    stale = []
    for p in held_positions(con):
        for v in {p["short_venue"], p["long_venue"]}:
            r = con.execute("SELECT MAX(ts) FROM funding_obs "
                            "WHERE venue=? AND base=?",
                            (v, p["coin"])).fetchone()
            if r and r[0]:
                age_h = (time.time() * 1000 - r[0]) / 3600000
                if age_h > STALENESS_H:
                    stale.append({"venue": v, "coin": p["coin"],
                                  "age_h": round(age_h, 1)})
    out["stale_legs"] = stale
    # 4. Sharpe-vs-native disagreement baseline (held pairs)
    disagree = []
    for p in held_positions(con):
        sv, lv, c = p["short_venue"], p["long_venue"], p["coin"]
        aprs = {}
        for v in (sv, lv):
            for src_like, tag in (("native_live", "native"),
                                  ("sharpe_live", "sharpe")):
                r = con.execute(
                    "SELECT rate, interval_h FROM funding_obs WHERE "
                    "venue=? AND base=? AND source LIKE ? "
                    "ORDER BY ts DESC LIMIT 1", (v, c, src_like)).fetchone()
                if r and r[1]:
                    ivl = float(r[1]) or 1.0
                    aprs.setdefault(tag, {})[v] = \
                        float(r[0]) / ivl * 24 * 365
        if "native" in aprs and "sharpe" in aprs and \
                set(aprs["native"]) == set(aprs["sharpe"]):
            gap = abs(sum(aprs["native"].values()) -
                      sum(aprs["sharpe"].values()))
            disagree.append({"coin": c, "gap_apr": round(gap, 4)})
    out["disagreement"] = disagree
    st.kv_set(con, "m4_disagreement_baseline", json.dumps(
        {"ts": nows(), "rows": disagree}))
    if verbose:
        print("health: tripwire fails 24h "
              f"(held/pre-entry split): {json.dumps(fails)}")
        print(f"health: stale legs (> {STALENESS_H}h): "
              f"{stale if stale else 'none'}")
        print(f"health: sharpe-vs-native disagreement (held): {disagree}")
    return out


# ------------------------------------------------------------------ main
async def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    force = "--force" in sys.argv
    con = st.connect()
    async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0"}) as session:
        pl._session = session
        ds._session = session
        nv._session = session
        await vc.init()
        if cmd in ("e1r", "all"):
            await run_e1r(con, force=force)
        if cmd in ("listings", "all"):
            await run_listings(con, force=force)
        if cmd in ("floor", "all"):
            await run_floor_watch(con, force=force)
        if cmd in ("health", "all"):
            run_health(con, force=force)
    con.close()


if __name__ == "__main__":
    asyncio.run(main())
