#!/usr/bin/env python3
"""Native (venue-direct) verification of Sharpe-derived claims:
  1. Kraken perps: persistent negative funding on COTI/KAITO/BIO/MEW/KAIA/MINA/CKB
  2. Crypto.com perps: negative on CELR/LINEA/CRV/ICP, positive on INJ/NVDA
  3. Gate.io: LTC deeply negative (Sharpe-only leg -> needs corroboration)
  4. Backpack floor: LTC/BNB pinned ~+11% APR (reachable, guess-url)
Live point-in-time check only; history check where an endpoint exists."""
import json
import urllib.request

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def apr(rate, ivl_h):
    return rate * 8760.0 / ivl_h * 100.0 if rate and ivl_h else None


print("=== 1. Kraken futures live tickers (funding) ===")
try:
    d = get("https://futures.kraken.com/derivatives/api/v3/tickers")
    tk = d.get("tickers", [])
    perps = [t for t in tk if t.get("type") == "funding" or "pf_" in
             (t.get("symbol") or "")]
    want = ["COTI", "KAITO", "BIO", "MEW", "KAIA", "MINA", "CKB", "LTC",
            "ALGO", "XMR"]
    found = 0
    for t in perps:
        sym = (t.get("symbol") or "").replace("pf_", "").split("USD")[0]
        if sym in want:
            fr = t.get("fundingRate")
            fb = t.get("fundingRatePrediction")
            ivl = 1.0  # kraken perps settle hourly
            if fr is not None:
                found += 1
                print(f"  {sym:8s} rate {float(fr)*100:+.4f}%/1h -> "
                      f"{apr(float(fr), ivl):+8.1f}% APR "
                      f"(pred {float(fb)*100:+.4f}%)" if fb else
                      f"  {sym:8s} rate {float(fr)*100:+.4f}%/1h -> "
                      f"{apr(float(fr), ivl):+8.1f}% APR")
    print(f"  matched {found} of {len(want)} wanted")
except Exception as e:
    print("  !!", repr(e))

print("=== 2. Crypto.com live funding ===")
try:
    d = get("https://api.crypto.com/exchange/v1/public/get-tickers")
    rows = d.get("result", {}).get("data", [])
    want = {"CELR-USDT", "LINEA-USDT", "CRV-USDT", "ICP-USDT", "INJ-USDT",
            "NVDA-USDT", "LTC-USDT", "COTI-USDT"}
    n = 0
    for t in rows:
        s = t.get("i") or t.get("instrument_name") or ""
        if s in want:
            fr = t.get("funding_rate_pct") or t.get("f") or None
            if fr is not None:
                n += 1
                fr = float(fr) / 100.0 if abs(float(fr)) > 0.01 else float(fr)
                print(f"  {s:12s} funding {fr*100:+.4f}% (cadence? assume 1h)"
                      f" -> ~{apr(fr, 1):+.1f}% APR")
    if n == 0:
        print("  tickers lack funding fields; trying funding-rate-history")
        for inst in ("CELR_USDT", "INJ_USDT"):
            try:
                d2 = get("https://api.crypto.com/exchange/v1/public/"
                         f"get-funding-rate-history?instrument_name={inst}"
                         "&page_size=5")
                rr = d2.get("result", {}).get("data", [])
                for x in rr[:3]:
                    print(f"  {inst}: {x}")
            except Exception as e2:
                print(f"  !! {inst}: {e2!r}")
                break
except Exception as e:
    print("  !!", repr(e))

print("=== 3. Gate.io LTC live (public v4 futures) ===")
try:
    d = get("https://api.gateio.ws/api/v4/futures/usdt/contracts/LTC_USDT")
    c = d if isinstance(d, dict) else {}
    fr = c.get("funding_rate")
    ivl = c.get("funding_interval") or 8
    print(f"  LTC_USDT funding_rate {fr} per {ivl}h -> "
          f"{apr(float(fr), float(ivl)):+.1f}% APR" if fr else
          f"  LTC_USDT raw: {str(c)[:200]}")
except Exception as e:
    print("  !!", repr(e))

print("=== 4. Backpack live funding (guess endpoint) ===")
try:
    d = get("https://api.backpack.exchange/api/v1/markets")
    mk = d if isinstance(d, list) else d.get("data", [])
    names = [m.get("symbol") for m in mk][:8]
    print(f"  markets endpoint OK ({len(mk)} markets), sample: {names}")
except Exception as e:
    print("  !! markets:", repr(e))
try:
    d = get("https://api.backpack.exchange/api/v1/fundingRates?symbol=LTC_PERP")
    print(f"  LTC_PERP funding: {str(d)[:200]}")
except Exception as e:
    print("  !! fundingRates:", repr(e))
