#!/usr/bin/env python3
"""Probe the two un-probed Sharpe endpoints for P1 finalization:
funding/settlement (dollar-weighted ranking) + confirm rwa-perps/rates."""
import json
import urllib.request

BASE = "https://www.sharpe.ai/api"


def get(path):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"__err__": repr(e)}


for path in ("/funding/settlement?limit=20",
             "/funding/settlements?limit=20",
             "/rwa-perps/rates?type=current&limit=5",
             "/arbitrage/cross-exchange?assetClass=all"):
    d = get(path)
    print(f"\n== {path}")
    if isinstance(d, dict) and "__err__" in d:
        print("  ERR:", d["__err__"])
        continue
    rows = d if isinstance(d, list) else (d.get("data") or [])
    print(f"  rows: {len(rows)}  type: {type(d).__name__}")
    if rows:
        print("  keys:", sorted(rows[0].keys()))
        print("  sample:", json.dumps(rows[0])[:300])
    elif isinstance(d, dict):
        print("  dict keys:", list(d.keys())[:12])
