#!/usr/bin/env python3
"""parity_report.py - P4a parity harness (Part 19 s.7 P4a acceptance).

Joins paper_fills rows (what the engine DID, authoritative) against
LiveAdapter DRY_RUN intent rows (what the live adapter WOULD have done)
and reports 1:1 coverage plus every rule-check flag seen.

Generic tool:
  python3 parity_report.py --db PATH --intents PATH [--since-fid N]

Join semantics (P4a, no real exchange):
  taker fill   <-> intent op=taker        on (venue, side, qty~=, px~=)
  maker fill   <-> intent op=maker_fill   on (venue, side, qty~=, px~=)
  (maker_post intents are paired 1:1 with maker fills only loosely: a
   resting order can be cancelled unfilled, so they are reported but not
   required to match.)
Exit 0 iff every new fill has a matching intent and no unmatched intents
of op=taker/maker_fill exist.  With zero rows on both sides the report is
vacuously 1:1 but prints an explicit "no seam traffic" banner so callers
can distinguish coverage from silence.
"""
import argparse
import json
import os
import sqlite3
import sys

TOL_QTY_REL = 1e-4
TOL_PX_REL = 1e-3

OPS_FILL = {"taker": "taker", "maker": "maker_fill"}


def load_intents(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def load_fills(db, since_fid=0):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT fid,ts,pair,leg,venue,side,style,target_qty,sim_fill_px,"
        "sim_slip_bps,fee_bps,notes FROM paper_fills WHERE fid>? "
        "ORDER BY fid", (since_fid,)).fetchall()]
    con.close()
    return rows


def _match(fill, intents, op):
    """Find first unused intent matching venue+side+op and qty/px within
    tolerance.  Returns (intent|None, remaining_intents)."""
    for i, it in enumerate(intents):
        if it.get("op") != op or it.get("venue") != fill["venue"] \
                or it.get("side") != fill["side"]:
            continue
        q, pq = it.get("qty"), fill["target_qty"]
        px, ppx = it.get("px"), fill["sim_fill_px"]
        if q is None or px is None:
            continue
        if abs(q - pq) > TOL_QTY_REL * max(abs(pq), 1e-12):
            continue
        if abs(px - ppx) > TOL_PX_REL * max(abs(ppx), 1e-12):
            continue
        return it, intents[:i] + intents[i + 1:]
    return None, intents


def join_parity(fills, intents):
    """Returns (report_dict).  Consumes matched taker/maker_fill intents."""
    pending = [dict(it) for it in intents]
    matched, unmatched_fills = [], []
    for f in fills:
        op = OPS_FILL.get(f["style"])
        it, pending = (_match(f, pending, op) if op else (None, pending))
        if it:
            matched.append((f, it))
        else:
            unmatched_fills.append(f)
    leftover = [it for it in pending
                if it.get("op") in ("taker", "maker_fill")]
    posts = [it for it in pending if it.get("op") == "maker_post"]
    nofills = [it for it in pending if it.get("op", "").endswith("nofill")]
    flags = sorted({c for _, it in matched for c in it.get("checks", [])})
    return {
        "fills_total": len(fills),
        "matched": len(matched),
        "unmatched_fills": unmatched_fills,
        "leftover_intents": leftover,
        "maker_posts_unfilled": len(posts),
        "taker_nofill_intents": len(nofills),
        "rule_flags": flags,
        "pairs": matched,
    }


def render(rep):
    lines = []
    lines.append(f"fills={rep['fills_total']} matched={rep['matched']} "
                 f"unmatched_fills={len(rep['unmatched_fills'])} "
                 f"leftover_intents={len(rep['leftover_intents'])} "
                 f"maker_posts_unfilled={rep['maker_posts_unfilled']} "
                 f"taker_nofill_intents={rep['taker_nofill_intents']}")
    for f, it in rep["pairs"]:
        lines.append(f"  PAIR fid={f['fid']} {f['pair']} {f['leg']}/"
                     f"{f['venue']} {f['side']} {f['style']} "
                     f"qty={f['target_qty']:.6g} px={f['sim_fill_px']:.6g}"
                     f"  <-> intent {it['ts']} checks={it.get('checks')}")
    for f in rep["unmatched_fills"]:
        lines.append(f"  UNMATCHED_FILL fid={f['fid']} {f['pair']} "
                     f"{f['leg']}/{f['venue']} {f['side']} {f['style']} "
                     f"qty={f['target_qty']:.6g}")
    for it in rep["leftover_intents"]:
        lines.append(f"  LEFTOVER_INTENT {it['ts']} {it['op']} "
                     f"{it.get('venue')}/{it.get('coin')} "
                     f"qty={it.get('qty')}")
    if rep["rule_flags"]:
        lines.append(f"  rule_flags: {'; '.join(rep['rule_flags'])}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--intents", required=True)
    ap.add_argument("--since-fid", type=int, default=0)
    a = ap.parse_args()
    rep = join_parity(load_fills(a.db, a.since_fid),
                      load_intents(a.intents))
    print(render(rep))
    vacuous = rep["fills_total"] == 0 and rep["matched"] == 0
    if vacuous:
        print("NOTE: no seam traffic in this dataset (vacuous parity)")
    good = not rep["unmatched_fills"] and not rep["leftover_intents"]
    print("PARITY:", "PASS" if good else "FAIL")
    sys.exit(0 if good else 1)


if __name__ == "__main__":
    main()
