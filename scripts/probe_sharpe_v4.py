#!/usr/bin/env python3
"""Probe Sharpe history endpoint: exchange filter, paging behavior, row density."""
import json
import urllib.request

BASE = "https://www.sharpe.ai"


def get(path):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"__err__": repr(e)}


def span(rows):
    if not rows:
        return "EMPTY"
    ts = sorted(r["settled_at"] for r in rows)
    return f"{len(rows):5d} rows  {ts[0][:16]} -> {ts[-1][:16]}"


print("=== A. baseline: XMR 90d, limit 5000 ===")
d = get("/api/funding/rates?type=history&coin=XMR&days=90&limit=5000")
rows = d if isinstance(d, list) else []
print(span(rows))
if rows:
    from collections import Counter
    print("  venues:", Counter(r["exchange"] for r in rows).most_common())

print("=== B. exchange filter test: XMR 90d exchange=Pacifica ===")
d = get("/api/funding/rates?type=history&coin=XMR&days=90&exchange=Pacifica&limit=5000")
rows = d if isinstance(d, list) else []
print(span(rows))

print("=== C. offset test: XMR 90d limit=10 offset=10 (same as first 10?) ===")
d0 = get("/api/funding/rates?type=history&coin=XMR&days=90&limit=10")
d1 = get("/api/funding/rates?type=history&coin=XMR&days=90&limit=10&offset=10")
r0 = d0 if isinstance(d0, list) else []
r1 = d1 if isinstance(d1, list) else []
print("  first-10 head:", r0[0]["settled_at"] if r0 else "-", "| offset-10 head:", r1[0]["settled_at"] if r1 else "-")

print("=== D. end_time cursor test: XMR 90d end_time=2026-08-25 ===")
d = get("/api/funding/rates?type=history&coin=XMR&days=90&end_time=2026-08-25T00:00:00Z&limit=5000")
rows = d if isinstance(d, list) else []
print(span(rows))

print("=== E. start_time test ===")
d = get("/api/funding/rates?type=history&coin=XMR&days=90&start_time=2026-08-25T00:00:00Z&limit=5000")
rows = d if isinstance(d, list) else []
print(span(rows))

print("=== F. days=3 sanity (should be ~3 days dense) ===")
d = get("/api/funding/rates?type=history&coin=XMR&days=3&limit=5000")
rows = d if isinstance(d, list) else []
print(span(rows))
