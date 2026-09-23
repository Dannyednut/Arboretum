#!/usr/bin/env python3
"""PnL report for the $1k paper capital: both books, realized + accrual."""
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, "/home/z/my-project/scripts")

ENG = "/home/z/my-project/download/data/v3.db"
P0 = "/home/z/my-project/download/data/paper_v3.db"


def iso2ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def engine_positions():
    con = sqlite3.connect(ENG)
    con.row_factory = sqlite3.Row
    cols = [c[1] for c in con.execute(
        "PRAGMA table_info(exec_positions)").fetchall()]
    out = []
    for r in con.execute("SELECT * FROM exec_positions ORDER BY pos_id"):
        out.append(dict(zip(cols, r)))
    con.close()
    return out


def engine_fills(pair=None):
    con = sqlite3.connect(ENG)
    con.row_factory = sqlite3.Row
    cols = [c[1] for c in con.execute(
        "PRAGMA table_info(paper_fills)").fetchall()]
    q = "SELECT * FROM paper_fills"
    args = ()
    if pair:
        q += " WHERE pair=?"
        args = (pair,)
    q += " ORDER BY ts"
    out = [dict(zip(cols, r)) for r in con.execute(q, args)]
    con.close()
    return out


def fill_cash(f):
    """Signed cash flow of a fill (sell = +, buy = -), net of fees.
    sim_fill_px is the slip-walked price; fee_bps on notional."""
    notional = float(f["target_qty"]) * float(f["sim_fill_px"])
    cash = notional if f["side"] == "sell" else -notional
    fee = notional * float(f["fee_bps"] or 0) / 10000.0
    return cash - fee, fee


def accrual_between(venue_a, venue_b, coin, t0, t1):
    """Integrated funding spread APR over [t0, t1] from funding_obs
    (native preferred per hour bucket, sharpe fallback), fraction."""
    con = sqlite3.connect(ENG)
    rows = con.execute(
        "SELECT ts, venue, rate, interval_h, source FROM funding_obs "
        "WHERE base=? AND ts > ? AND ts <= ? ORDER BY ts",
        (coin, int(t0 * 1000) - 3600_000, int(t1 * 1000) + 3600_000)
    ).fetchall()
    con.close()
    series = {}
    for ts, v, rate, ivl, src in rows:
        if v not in (venue_a, venue_b) or not ivl:
            continue
        h = int(ts // 3_600_000)
        apr = float(rate) / float(ivl) * 24 * 365
        key = (h, v)
        prev = series.get(key)
        if prev is None or (src.startswith("native")
                            and not prev[1].startswith("native")):
            series[key] = (apr, src)
    total = 0.0
    covered = 0.0
    t = t0
    while t < t1:
        h = int(t // 3600)
        aa = series.get((h, venue_a))
        bb = series.get((h, venue_b))
        if aa and bb:
            spread = aa[0] - bb[0]
            total += spread / (24 * 365)
            covered += 1
        t += 3600
    return total, covered, (t1 - t0) / 3600


def main():
    print("=" * 68)
    print("ENGINE BOOK (automated, P3)")
    print("=" * 68)
    for p in engine_positions():
        fills = [f for f in engine_fills(p["pair"])]
        # attach fills by time window of this position
        t_open = iso2ts(p["ts_open"])
        t_close = iso2ts(p["ts_close"]) if p["ts_close"] else time.time()
        mine = [f for f in fills
                if t_open - 5 <= iso2ts(f["ts"]) <= t_close + 5]
        cash, fees = 0.0, 0.0
        legq = {}
        for f in mine:
            c, fee = fill_cash(f)
            cash += c
            fees += fee
            k = (f["leg"], f["venue"], f["side"])
            legq[k] = legq.get(k, 0) + float(f["target_qty"])
        status = p["status"]
        # accrual only while open
        acc, hrs_cov, hrs_tot = accrual_between(
            p["short_venue"], p["long_venue"], p["coin"], t_open,
            min(t_close, time.time()))
        size = float(p["size_usd"] or 0)
        acc_usd = acc * size
        print(f"\n#{p['pos_id']} {p['pair']}  ${size:.0f}/leg  "
              f"{p['ts_open'][:16]} -> "
              f"{(p['ts_close'] or 'now')[:16]}  [{status}]"
              f"  reason={p['reason_close'] or '-'}")
        for k, q in sorted(legq.items()):
            print(f"   {k[0]:5s} {k[1]:8s} {k[2]:4s} qty {q:.2f}")
        print(f"   price PnL (net of ${fees:.2f} fees): "
              f"${cash:+.2f}")
        print(f"   funding accrual: {acc * 100:+.2f}% over "
              f"{hrs_cov:.0f}/{hrs_tot:.0f}h covered -> ${acc_usd:+.2f}")
        if status == "open":
            print(f"   unrealized total (price+accrual): "
                  f"${cash + acc_usd:+.2f}")
        else:
            print(f"   REALIZED: ${cash + acc_usd:+.2f}")

    print()
    print("=" * 68)
    print("RESIDUAL EXPOSURE (unwind sizing bug: legs sized by")
    print("size_usd/px instead of position qty -> residuals on closes)")
    print("=" * 68)
    con = sqlite3.connect(ENG)
    marks = {}
    for v, c in (("bingx", "INJ"), ("okx", "INJ"), ("aster", "UAI"),
                 ("binance", "UAI")):
        r = con.execute(
            "SELECT rate FROM funding_obs WHERE venue=? AND base=? "
            "AND source LIKE 'native%' ORDER BY ts DESC LIMIT 1",
            (v, c)).fetchone()
        # approximate mark via latest book sample mid
        m = con.execute(
            "SELECT value FROM kv WHERE key=? ", (f"bs_{v}_{c}",)).fetchone()
        marks[(v, c)] = m[0] if m else None
    # price marks from fills (last known fill px per venue/coin)
    for f in engine_fills():
        key = (f["venue"], f["pair"].split()[0])
        marks[key] = float(f["sim_fill_px"])
    for pid, pair, qty_col in ((1, "INJ bingx->okx", "short"),
                               (2, "UAI aster->binance", "short")):
        pos = [p for p in engine_positions() if p["pos_id"] == pid][0]
        coin = pos["coin"]
        t_open = iso2ts(pos["ts_open"])
        t_close = iso2ts(pos["ts_close"])
        mine = [f for f in engine_fills(pair)
                if t_open - 5 <= iso2ts(f["ts"]) <= t_close + 5]
        net = {}
        for f in mine:
            sign = -1 if f["side"] == "sell" else 1   # buy reduces short
            if f["leg"] == "short":
                net.setdefault("short", 0.0)
                net["short"] += (-float(f["target_qty"])
                                 if f["side"] == "sell"
                                 else float(f["target_qty"]))
            else:
                net.setdefault("long", 0.0)
                net["long"] += (float(f["target_qty"])
                                if f["side"] == "buy"
                                else -float(f["target_qty"]))
        # net signed remaining: short<0 open short, long>0 open long
        res_s = -net.get("short", 0.0)    # negative = still short
        res_l = net.get("long", 0.0)      # positive = still long
        px_s = marks.get((pos["short_venue"], coin), 0.0)
        px_l = marks.get((pos["long_venue"], coin), 0.0)
        print(f"\n#{pid} {pair} residual after close:")
        print(f"   short {pos['short_venue']}: {res_s:+.2f} units "
              f"(~${abs(res_s * px_s):.0f} @ {px_s})")
        print(f"   long  {pos['long_venue']}: {res_l:+.2f} units "
              f"(~${abs(res_l * px_l):.0f} @ {px_l})")
        print(f"   -> hedged residual pair, UNMONITORED (position closed)")
    con.close()

    print()
    print("=" * 68)
    print("P0 BOOK (paper ledger, manual-mode validation)")
    print("=" * 68)
    p0 = sqlite3.connect(P0)
    p0.row_factory = sqlite3.Row
    acols = [c[1] for c in p0.execute(
        "PRAGMA table_info(accruals)").fetchall()]
    for r in p0.execute("SELECT * FROM positions WHERE status='open'"):
        d = dict(r)
        t_open = iso2ts(d["ts_open"])
        rows = p0.execute(
            "SELECT carry_hourly_usd, ts FROM accruals WHERE pos_id=? "
            "ORDER BY ts", (d["pos_id"],)).fetchall()
        acc_usd = sum(float(x["carry_hourly_usd"] or 0) for x in rows)
        fee = 0.0
        for f in p0.execute(
                "SELECT notional_usd, fee_bps, slip_bps FROM fills "
                "WHERE pos_id=?", (d["pos_id"],)).fetchall():
            fee += float(f["notional_usd"]) * \
                (float(f["fee_bps"] or 0) + float(f["slip_bps"] or 0)) \
                / 10000
        print(f"\n#{d['pos_id']} {d['pair']}  ${d['size_usd']:.0f}/leg  "
              f"open {d['ts_open'][:16]} ({(time.time()-t_open)/86400:.1f}d)")
        print(f"   entry RT cost: {d['entry_cost_pct']:.3f}%  "
              f"fees+slip paid ~${fee:.2f}")
        print(f"   funding accrual (tracked): ${acc_usd:+.2f} "
              f"({len(rows)} hourly rows, {rows[0]['ts'][:10]}.."
              f"{rows[-1]['ts'][:10]})" if rows else
              f"   funding accrual (tracked): $0.00 (no rows)")
        # engine-window share (for engine accrual estimate)
        if d["coin"] == "INJ":
            ew0, ew1 = iso2ts("2026-09-03T10:26:23Z"), \
                iso2ts("2026-09-08T09:57:55Z")
        else:
            ew0, ew1 = iso2ts("2026-09-03T21:03:29Z"), \
                iso2ts("2026-09-08T10:02:23Z")
        eng_acc = sum(
            float(x["carry_hourly_usd"] or 0) for x in rows
            if ew0 <= iso2ts(x["ts"]) <= ew1)
        print(f"   engine-window share ({d['coin']}): ${eng_acc:+.2f}"
              f"  [engine accrual estimate for #"
              f"{'1' if d['coin'] == 'INJ' else '2'}]")
    p0.close()

    print()
    print("=" * 68)
    print("TOTAL vs $1k")
    print("=" * 68)


if __name__ == "__main__":
    main()
