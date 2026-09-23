#!/usr/bin/env python3
"""Multi-session depth re-sampler (Part-5 §6.1 next-step 1).

Re-runs the Part-5 L2 sweep at different trading sessions to build a per-leg
slippage STABILITY distribution across time-of-day windows.

Modes:
  now    one immediate sweep (3 passes ~45s apart), window tag W1now
  daemon sweeps at fixed UTC hours [16, 20, 0, 4, 8, 12] -> windows W02..W07
         (US afternoon, US late, Asia x2, EU open, EU mid), then exits

Rows append to download/data/l2_depth_samples_ms.csv (resume-safe) with extra
columns `window` and `session` (asia 00-07 / eu 08-13 / us 14-23 UTC).
Reuses depth_sampler.py fetchers, legs and metrics unchanged.
"""
import asyncio
import csv
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import depth_sampler as ds  # noqa: E402

OUT = "/home/z/my-project/download/data/l2_depth_samples_ms.csv"
PASSES = 3
PASS_GAP_S = 45
DAEMON_HOURS_UTC = [16, 20, 0, 4, 8, 12]
FLD = ds.COLS + ["window", "session", "err"]


def session_label(h):
    return "asia" if h < 8 else ("eu" if h < 14 else "us")


async def one_sweep(http, wid):
    total_ok = total = 0
    for p in range(1, PASSES + 1):
        t0 = time.time()
        results = []
        await ds.sample_pass(http, p, results)
        ok = sum(r["ok"] for r in results)
        total += len(results)
        total_ok += ok
        write_header = not os.path.exists(OUT)
        with open(OUT, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FLD, extrasaction="ignore")
            if write_header:
                w.writeheader()
            for r in results:
                r2 = dict(r)
                r2["window"] = wid
                r2["session"] = session_label(datetime.now(timezone.utc).hour)
                w.writerow(r2)
        print(f"{datetime.now(timezone.utc):%H:%M:%S} {wid} pass {p}: "
              f"{ok}/{len(results)} ok ({time.time()-t0:.0f}s)", flush=True)
        if p < PASSES:
            await asyncio.sleep(PASS_GAP_S)
    return total_ok, total


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "now"
    ds.load_legs()
    async with aiohttp.ClientSession() as http:
        ds._session = http
        info = await ds._get("https://fapi.asterdex.com/fapi/v1/exchangeInfo")
        for x in info.get("symbols", []):
            if x.get("status") == "TRADING":
                ds.ASTER_SYMS[x["baseAsset"].upper()] = x["symbol"]
        print(f"aster symbol map {len(ds.ASTER_SYMS)}", flush=True)

        if mode == "now":
            wid = sys.argv[2] if len(sys.argv) > 2 else "W1now"
            await one_sweep(http, wid)
            return

        start_i = int(sys.argv[2]) if len(sys.argv) > 2 else 2
        hours = ([int(x) for x in sys.argv[3].split(",")]
                 if len(sys.argv) > 3 else DAEMON_HOURS_UTC)
        now = datetime.now(timezone.utc)
        targets = []
        for d in (0, 1, 2):
            for h in hours:
                t = now.replace(hour=h, minute=1, second=0, microsecond=0) \
                    + timedelta(days=d)
                if t > now + timedelta(minutes=2):
                    targets.append(t)
        targets = sorted(targets)[:len(hours)]
        print("daemon plan:", [t.isoformat() for t in targets], flush=True)
        for i, t in enumerate(targets, start=start_i):
            wait = (t - datetime.now(timezone.utc)).total_seconds()
            if wait > 0:
                print(f"sleeping {wait/3600:.2f}h until {t.isoformat()}",
                      flush=True)
                await asyncio.sleep(wait)
            wid = f"W{i:02d}_{t.hour:02d}u_{session_label(t.hour)}"
            try:
                ok, tot = await one_sweep(http, wid)
                print(f"== {wid} done: {ok}/{tot}", flush=True)
            except Exception as e:            # keep the daemon alive
                print(f"!! {wid} failed: {e!r}", flush=True)
        print("daemon complete", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
