#!/usr/bin/env python3
"""Task 47: reconcile ledger after rollback #7 (sandbox revert resurrected the
original pre-rollback-#5 v3.db; the Sep-23 restored lineage survives only in
git-tracked dumps v3_state_20260923T0445.sql.gz).

Current live DB: original lineage + fresh pos5 (id 5, opened 2026-11-02,
INJ bingx->okx @34.54/7.237) + positions 1-4 + 16 old fills + 2 new.

Merge from the 0445 dump (read-only):
  - old reconstructed pos5 (Sep-16 economics, was open) -> re-insert as
    pos_id 8, status 'archived' (phantom: reconstructed in T40, superseded
    by the fresh entry; engine active-position filter excludes it)
  - pos6 (closed -$0.43) -> re-insert as pos_id 7, status 'closed'
  - their 6 fills re-noted pos:5->pos:8, pos:6->pos:7 (insert if missing)

Idempotent: skips rows that already exist (by pos_id or fill fingerprint).
"""
import gzip
import re
import sqlite3
import sys

DB = "/home/z/my-project/download/data/v3.db"
DUMP = "/home/z/my-project/download/db_backup/v3_state_20260923T0445.sql.gz"


def parse_dump_rows(gz_path, table):
    """Yield value-tuples from INSERT INTO "<table>" VALUES (...) lines
    (plain .dump style, no column lists - table order applies)."""
    rows = []
    pat = re.compile(r'^INSERT INTO "?' + re.escape(table) + r'"? VALUES\s*(\(.+\));?$')
    with gzip.open(gz_path, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("INSERT INTO"):
                continue
            m = pat.match(line)
            if not m:
                continue
            # split top-level commas of the VALUES tuple
            body = m.group(1)
            # statements are one-row-per-INSERT (db_backup style) or multi
            for tup in re.findall(r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)", body):
                vals, cur, q = [], "", None
                for ch in tup:
                    if q:
                        cur += ch
                        if ch == q:
                            q = None
                    elif ch in ("'", '"'):
                        q = ch
                        cur += ch
                    elif ch == ",":
                        vals.append(cur.strip())
                        cur = ""
                    else:
                        cur += ch
                vals.append(cur.strip())
                rows.append(vals)
    return rows


def unquote(v):
    if v == "NULL" or v is None:
        return None
    if v.startswith("'") and v.endswith("'"):
        return v[1:-1].replace("''", "'")
    return float(v) if ("." in v or "e" in v.lower()) else int(v)


def main():
    src = sqlite3.connect(":memory:")
    pos_rows = parse_dump_rows(DUMP, "exec_positions")
    fill_rows = parse_dump_rows(DUMP, "paper_fills")
    print(f"dump parse: {len(pos_rows)} position rows, {len(fill_rows)} fill rows")
    cols_p = [c[1] for c in sqlite3.connect(DB).execute(
        "PRAGMA table_info(exec_positions)")]
    con = sqlite3.connect(DB)
    have_p = {r[0] for r in con.execute("SELECT pos_id FROM exec_positions")}
    fill_fp = {(r[0], r[1], r[2], r[3]) for r in con.execute(
        "SELECT ts, notes, venue, side FROM paper_fills")}

    def up(vals, i):
        return unquote(vals[i]) if i < len(vals) else None

    inserted = skipped = 0
    ts_archived = None
    # map old ids: 5 -> 8 (archived), 6 -> 7 (closed)
    idmap = {5: 8, 6: 7}
    for vals in pos_rows:
        if not vals or not vals[0].isdigit():
            continue
        old_id = int(vals[0])
        if old_id in idmap:
            print(f"  debug pos{old_id}: cols={cols_p}")
            print(f"  debug vals={vals}")
        if old_id not in idmap:
            continue
        new_id = idmap[old_id]
        if new_id in have_p:
            skipped += 1
            continue
        rec = {c: up(vals, j) for j, c in enumerate(cols_p)}
        rec["pos_id"] = new_id
        rec["ts_close"] = rec.get("ts_close") or ("2026-11-02T00:00:00Z" if old_id == 6 else None)
        if old_id == 5:
            rec["status"] = "archived"
            ts_archived = rec["ts_open"]
            flags = rec.get("flags") or "{}"
            rec["flags"] = (
                '{"archived": "rollback #7 reconcile 2026-11-02: original DB '
                'resurrected by sandbox revert; Sep-16 reconstructed position '
                'superseded by fresh entry pos5 @7.237; kept for ledger '
                'history, excluded from active set"}')
        con.execute(
            f"INSERT INTO exec_positions({','.join(cols_p)}) VALUES ("
            + ",".join(f":{c}" for c in cols_p) + ")", rec)
        inserted += 1
        print(f"  + pos {old_id} -> pos_id {new_id} "
              f"({rec['status']}) {rec['pair']} qty={rec['qty']}")

    if ts_archived is None:
        row = con.execute("SELECT ts_open FROM exec_positions "
                          "WHERE pos_id=8").fetchone()
        ts_archived = row[0] if row else None
    fcols = [c[1] for c in con.execute("PRAGMA table_info(paper_fills)")]
    print("paper_fills cols:", fcols, "| ts_archived:", ts_archived)
    for vals in fill_rows:
        rec = {c: up(vals, j) for j, c in enumerate(fcols)}
        note = rec.get("notes") or ""
        m = re.search(r"pos:(\d+)", note)
        if m:
            if int(m.group(1)) not in idmap:
                continue
            rec["notes"] = note.replace(f"pos:{m.group(1)}",
                                        f"pos:{idmap[int(m.group(1))]}")
        elif ("reconstructed after rollback #3" in note
              and rec.get("ts") == ts_archived):
            # T40-reconstructed entry fills of old pos5 -> archived pos8
            rec["notes"] = "pos:8;" + note
        else:
            continue
        rec.pop("fid", None)
        fp = (rec.get("ts"), rec["notes"], rec.get("venue"), rec.get("side"))
        if fp in fill_fp:
            continue
        keys = ",".join(rec.keys())
        marks = ",".join(f":{k}" for k in rec)
        con.execute(f"INSERT INTO paper_fills({keys}) VALUES ({marks})", rec)
        fill_fp.add(fp)
        inserted += 1
        print(f"  + fill {rec.get('ts')} {rec['notes'][:40]}")

    con.commit()
    print(f"inserted={inserted} skipped={skipped}")
    for r in con.execute("SELECT pos_id, pair, status, qty FROM exec_positions ORDER BY pos_id"):
        print(" ", r)
    print("fills:", con.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0])
    con.close()


if __name__ == "__main__":
    sys.exit(main())
