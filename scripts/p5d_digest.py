#!/usr/bin/env python3
"""Compact digest of p5d_*.json search caches -> stdout (context-cheap reading)."""
import glob, json, os

DIR = os.path.join(os.path.dirname(__file__), "search_results")
for path in sorted(glob.glob(os.path.join(DIR, "p5d_*.json"))):
    slug = os.path.basename(path)[4:-5]
    try:
        with open(path) as f:
            rows = json.load(f)
    except Exception as e:
        print(f"## {slug}: ERROR {e}")
        continue
    if not isinstance(rows, list):
        rows = rows.get("results", rows) if isinstance(rows, dict) else []
    print(f"## {slug}")
    seen = set()
    for r in (rows or [])[:4]:
        name = (r.get("name") or "").strip()[:90]
        host = r.get("host_name") or ""
        snip = " ".join(((r.get("snippet") or "").replace("\n", " ")).split())[:230]
        url = (r.get("url") or "").strip()
        key = snip[:80]
        if key in seen:
            continue
        seen.add(key)
        print(f"- {name} [{host}]")
        print(f"  {snip}")
        print(f"  {url}")
    print()
