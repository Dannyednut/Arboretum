#!/usr/bin/env python3
"""Accrual rebuild from Sharpe settled funding history (survives the
Sep-4 rollback). Per position: carry = size x (rate_short - rate_long)
per settlement, integrated over the holding window."""
import asyncio
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/home/z/my-project/scripts")
import v3_connectors as vc          # noqa: E402
import aiohttp                      # noqa: E402

ENG = "/home/z/my-project/download/data/v3.db"
P0 = "/home/z/my-project/download/data/paper_v3.db"

WINDOWS = [
    ("eng#1", "INJ", "bingx", "okx", "2026-09-03T10:26:23Z",
     "2026-09-08T09:57:55Z", 250.0),
    ("eng#2", "UAI", "aster", "binance", "2026-09-03T21:03:29Z",
     "2026-09-08T10:02:23Z", 400.0),
    ("eng#3", "INJ", "bingx", "okx", "2026-09-08T09:58:20Z",
     "2026-09-08T10:09:37Z", 250.0),
    ("eng#4", "UAI", "aster", "binance", "2026-09-08T10:02:49Z",
     None, 400.0),
    ("p0#1", "INJ", "bingx", "okx", "2026-09-02T02:39:28Z", None, 250.0),
    ("p0#2", "UAI", "aster", "binance", "2026-09-02T15:54:04Z", None,
     400.0),
]


def iso2ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


async def hist(coin, days=8):
    return await vc.Sharpe.history(coin, days=days)


async def main():
    async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0"}) as session:
        vc._session = session
        coins = {"INJ", "UAI"}
        hists = {}
        for c in coins:
            try:
                res = await hist(c)
                hists[c] = res[0] if isinstance(res, tuple) else res
            except Exception as e:
                print(f"history {c}: ERR {e}")
                hists[c] = {}
        now = datetime.now(timezone.utc).timestamp()
        print(f"{'pos':6s} {'window':24s} {'settl':>5s} {'carry_usd':>10s}"
              f"  {'short_apr':>9s} {'long_apr':>9s}")
        tot_real = {}
        for name, coin, sv, lv, t0s, t1s, size in WINDOWS:
            t0 = iso2ts(t0s)
            t1 = iso2ts(t1s) if t1s else now
            h = hists.get(coin, {})
            sa = h.get(sv) or []
            la = h.get(lv) or []
            if not sa or not la:
                print(f"{name:6s} {sv}->{lv}: history missing "
                      f"({len(sa)}/{len(la)} rows)")
                continue
            # build per-leg cumulative settled rate within window
            def carry_series(rows):
                out = []
                for ts, rate, ivh in rows:
                    t = ts / 1000 if ts > 1e11 else ts
                    if t0 <= t <= t1:
                        out.append((t, float(rate), float(ivh or 1)))
                out.sort()
                return out
            ss, ls = carry_series(sa), carry_series(la)
            # pair by nearest settlement timestamps (both legs settle on
            # their own calendars; integrate each leg independently)
            def leg_carry(rows, sign):
                tot = 0.0
                for t, rate, ivh in rows:
                    tot += sign * rate * size
                return tot, len(rows)
            sc, sn = leg_carry(ss, +1)     # short receives
            lc, ln = leg_carry(ls, -1)     # long pays
            carry = sc + lc
            days = (t1 - t0) / 86400
            apr_s = (sc / size / days * 365) if days > 0.02 else 0
            apr_l = (-lc / size / days * 365) if days > 0.02 else 0
            print(f"{name:6s} {sv}->{lv} {t0s[:16]}+{days:4.1f}d "
                  f"{sn + ln:5d} {carry:+10.2f}  {apr_s:8.1%} "
                  f"{apr_l:8.1%}")
            tot_real[name] = carry
        print("\nsummary:")
        eng = sum(v for k, v in tot_real.items() if k.startswith("eng"))
        p0 = sum(v for k, v in tot_real.items() if k.startswith("p0"))
        print(f"  engine book accrual (4 pos): ${eng:+.2f}")
        print(f"  p0 book accrual (2 pos):     ${p0:+.2f}")


if __name__ == "__main__":
    asyncio.run(main())
