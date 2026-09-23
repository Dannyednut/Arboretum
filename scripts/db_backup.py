#!/usr/bin/env python3
"""State-only DB backup -> git-tracked gzip dumps (rollback hardening).

Why: sandbox rollbacks have wiped download/data/*.db three times (untracked
files). Git is our only durability guarantee, but the raw v3.db is ~170MB
because funding_obs/book_samples hold ~1.6M history rows that are REBUILDABLE
from venue APIs (v3_backfill.py) and live engine runs. State tables (positions,
fills, orders, kv, tripwire_log, events, drift, pnl, candidates, portfolio
snap) are small and NOT re-derivable - those are what we back up.

Usage:
  python3 scripts/db_backup.py                 # backup v3.db + paper_v3.db
  python3 scripts/db_backup.py --keep 8        # keep 8 most recent per db
  python3 scripts/db_backup.py --restore download/db_backup/v3_X.sql.gz out.db

Output: download/db_backup/<name>_state_<ts>.sql.gz  (git-tracked dir)
Schema for excluded tables IS kept (CREATE TABLE), so a restored DB runs the
engine immediately; excluded tables just start empty and repopulate.

Run at the end of every engine window (runbook) and before any risky op.
"""
import argparse
import gzip
import os
import sqlite3
from datetime import datetime, timezone

# tables whose rows are rebuildable data caches (schema kept, rows skipped)
CACHE_TABLES = {"funding_obs", "book_samples"}

DEFAULT_DBS = [
    "/home/z/my-project/download/data/v3.db",
    "/home/z/my-project/download/data/paper_v3.db",
]
OUTDIR = "/home/z/my-project/download/db_backup"


def dump_state(db_path: str, out_gz: str) -> tuple[int, list]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")]
    skipped_rows = 0
    n_lines = 0
    with gzip.open(out_gz, "wt", compresslevel=9) as fh:
        for line in con.iterdump():
            low = line.lower()
            if low.startswith('insert into "') and \
                    any(f'insert into "{t}"' in low for t in CACHE_TABLES):
                skipped_rows += 1
                continue
            fh.write(line + "\n")
            n_lines += 1
    con.close()
    return n_lines, tables


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=5)
    ap.add_argument("--outdir", default=OUTDIR)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")

    for db_path in DEFAULT_DBS:
        if not os.path.exists(db_path):
            print(f"SKIP (missing): {db_path}")
            continue
        name = os.path.splitext(os.path.basename(db_path))[0]
        out = os.path.join(args.outdir, f"{name}_state_{ts}.sql.gz")
        n_lines, tables = dump_state(db_path, out)
        sz = os.path.getsize(out)
        print(f"BACKUP {name}: {n_lines} lines, {sz/1024:.1f} KB "
              f"-> {out} (tables: {len(tables)}, caches row-skipped)")
        # prune old snapshots, keep newest N
        olds = sorted(f for f in os.listdir(args.outdir)
                      if f.startswith(name + "_state_") and f != os.path.basename(out))
        for old in olds[:-args.keep] if args.keep > 0 else []:
            os.remove(os.path.join(args.outdir, old))
            print(f"  pruned {old}")


def restore(gz_path: str, out_db: str):
    assert not os.path.exists(out_db), f"refusing to overwrite {out_db}"
    con = sqlite3.connect(out_db)
    with gzip.open(gz_path, "rt") as fh:
        con.executescript(fh.read())
    con.commit()
    con.close()
    print(f"RESTORED {gz_path} -> {out_db} "
          f"(funding_obs/book_samples empty; rebuild via v3_backfill.py)")


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 4 and sys.argv[1] == "--restore":
        restore(sys.argv[2], sys.argv[3])
    else:
        main()
