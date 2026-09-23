#!/usr/bin/env python3
"""P5d doc probes: aster README + llms.txt indexes (auth/funding lines only)."""
import urllib.request, re

UA = {"User-Agent": "Mozilla/5.0 (research probe)"}

def get(url, cap=None):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            txt = r.read().decode("utf-8", "replace")
        return txt[:cap] if cap else txt
    except Exception as e:
        return f"<<ERROR {e}>>"

print("=" * 30, "ASTER api-docs README (head)")
print(get("https://raw.githubusercontent.com/asterdex/api-docs/main/README.md", 3000))

PAT = re.compile(r"auth|fund|api[ _-]?key|session|agent|sign|credential|permission|withdraw", re.I)
for name, base in [
    ("EXTENDED", "https://docs.extended.exchange/llms.txt"),
    ("PARADEX", "https://docs.paradex.trade/llms.txt"),
    ("LIGHTER", "https://apidocs.lighter.xyz/llms.txt"),
    ("NADO", "https://docs.nado.xyz/llms.txt"),
]:
    print("=" * 30, name, "llms.txt (filtered)")
    txt = get(base)
    if txt.startswith("<<ERROR"):
        print(txt)
        continue
    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    hits = [l for l in lines if PAT.search(l)]
    for l in (hits or lines[:20])[:22]:
        print(" ", l[:150])
