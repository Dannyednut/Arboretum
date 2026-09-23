#!/usr/bin/env python3
"""P5d doc probes round 2: aster README (master), extended API section, paradex auth, lighter api-keys."""
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (research probe)"}

def get(url, cap=None):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            txt = r.read().decode("utf-8", "replace")
        return txt[:cap] if cap else txt
    except Exception as e:
        return f"<<ERROR {e}>>"

print("=" * 30, "ASTER README (master branch)")
print(get("https://raw.githubusercontent.com/asterdex/api-docs/master/README.md", 2600))

print("=" * 30, "EXTENDED llms.txt - API lines")
txt = get("https://docs.extended.exchange/llms.txt")
if not txt.startswith("<<ERROR"):
    lines = [l.strip() for l in txt.splitlines() if "api" in l.lower()]
    for l in lines[:28]:
        print(" ", l[:150])
else:
    print(txt)

print("=" * 30, "PARADEX api-authentication.md")
print(get("https://docs.paradex.trade/api/general-information/api-authentication.md", 3200))

print("=" * 30, "LIGHTER api-keys.md")
print(get("https://apidocs.lighter.xyz/docs/api-keys.md", 3200))
