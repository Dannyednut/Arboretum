#!/usr/bin/env python3
"""Sharpe AI free-tier probe: which /api/* endpoints answer without a key,
and what the payloads actually contain (mapped to our research needs)."""
import json
import urllib.parse
import urllib.request

BASE = "https://www.sharpe.ai"

CALLS = [
    ("funding/current", "/api/funding/rates?type=current&limit=1000"),
    ("funding/coins", "/api/funding/coins"),
    ("funding/history3y", "/api/funding/rates?type=history&coin=XMR&days=1095&limit=5"),
    ("funding/equity", "/api/funding/rates?type=current&asset_class=equity&limit=40"),
    ("funding/settlement", "/api/funding/settlement?window=1d&limit=10"),
    ("arb/cross-ex", "/api/arbitrage/cross-exchange?assetClass=all"),
    ("rwa-perps", "/api/rwa-perps/rates?type=current"),
    ("meta/coverage", "/api/meta/coverage"),
    ("meta/datasets", "/api/meta/datasets"),
    ("futures/crowding", "/api/futures/data?chart=crowding-risk&coin=XMR&timeframe=1M"),
    ("listings/recent", "/api/listings/recent"),
    ("derivs-overview", "/api/market/derivatives-overview"),
    ("global/overview", "/api/global/overview"),
]


def get(path):
    url = BASE + path
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except Exception as e:
        return -1, {}, str(e).encode()


def summarize(name, status, body):
    print(f"\n=== {name} -> HTTP {status}")
    if status != 200:
        print("  ", body[:220].decode(errors="replace"))
        return None
    try:
        d = json.loads(body)
    except Exception:
        print("  non-json:", body[:200])
        return None
    rows = d if isinstance(d, list) else (d.get("data") or d)
    if isinstance(rows, dict):
        keys = list(rows.keys())
        print("  dict keys:", keys[:14])
        return d
    if isinstance(rows, list):
        print(f"  list rows: {len(rows)}")
        if rows:
            print("  row0:", json.dumps(rows[0])[:400])
        return d
    return d


def main():
    out = {}
    for name, path in CALLS:
        status, headers, body = get(path)
        d = summarize(name, status, body)
        if d is not None:
            out[name] = {"path": path, "payload": d}
        interesting = {k: v for k, v in headers.items()
                       if k.lower().startswith(("x-data", "x-freshness",
                                                "x-coverage", "x-rate",
                                                "x-requested"))}
        if interesting:
            print("  headers:", interesting)
    json.dump(out, open("/home/z/my-project/scripts/sharpe_free_probe.json", "w"),
              default=str)


if __name__ == "__main__":
    main()
