#!/usr/bin/env python3
"""Layered v3.db restoration after sandbox rollback #4.

Problem: the surviving mirror v3.db is Sep-14 vintage (correct 1.58M-row
funding_obs history, STALE state: no pos5, 16 fills), while the newest
authoritative state lives in the git-tracked db_backup gzip
(v3_state_20260921T0502.sql.gz: pos5 repaired, pos6 unwound, 22 fills).

Recipe:
  1. db_backup.restore(gz) -> fresh DB with authoritative state, caches empty
  2. ATTACH the old mirror DB, copy funding_obs + book_samples rows across
     (schema compared first; skip if mismatch)
  3. sanity-check state tables, swap into place

Usage: python3 scripts/restore_state_merge.py
"""
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db_backup  # noqa: E402

BASE = "/home/z/my-project/download/data"
STATE_GZ = f"{BASE}/../db_backup/v3_state_20260921T0502.sql.gz"
OLD_DB = f"{BASE}/v3_old_sep14.db"      # renamed mirror copy (keeps history)
TMP_DB = f"{BASE}/v3_restored_tmp.db"   # built fresh, then swapped in
FINAL = f"{BASE}/v3.db"

CACHE_TABLES = ["funding_obs", "book_samples"]
STATE_CHECK = {
    "exec_positions": ("SELECT status, COUNT(*) FROM exec_positions GROUP BY status",),
    "paper_fills": ("SELECT COUNT(*) FROM paper_fills",),
    "tripwire_log": ("SELECT COUNT(*) FROM tripwire_log",),
    "kv": ("SELECT COUNT(*) FROM kv",),
}


def schema_of(con: sqlite3.Connection, table: str, master: str = "main") -> str:
    row = con.execute(
        f"SELECT sql FROM {master}.sqlite_master "
        "WHERE type='table' AND name=?", (table,)).fetchone()
    return (row[0] or "").strip().lower() if row else ""


def main() -> None:
    assert os.path.exists(STATE_GZ), f"missing state dump: {STATE_GZ}"
    assert os.path.exists(OLD_DB), f"missing history DB: {OLD_DB}"
    assert not os.path.exists(TMP_DB), f"leftover tmp: {TMP_DB}"

    # 1) authoritative state from git-tracked dump
    db_backup.restore(STATE_GZ, TMP_DB)

    # 2) re-attach history rows from the old mirror DB
    new = sqlite3.connect(TMP_DB)
    new.execute("PRAGMA journal_mode=WAL")
    new.execute("ATTACH ? AS old", (OLD_DB,))
    merged = {}
    for t in CACHE_TABLES:
        s_new, s_old = schema_of(new, t), schema_of(new, t, master="old")
        if not s_old:
            print(f"  {t}: not present in old DB, skip")
            continue
        if s_new != s_old:
            print(f"  {t}: SCHEMA MISMATCH - skip rows "
                  f"(new={s_new[:60]!r} old={s_old[:60]!r})")
            continue
        n_old = new.execute(f"SELECT COUNT(*) FROM old.{t}").fetchone()[0]
        new.execute(f"INSERT INTO {t} SELECT * FROM old.{t}")
        merged[t] = (n_old, new.execute(
            f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        print(f"  {t}: copied {merged[t][0]} rows -> {merged[t][1]}")
    new.commit()

    # 3) sanity-check restored state
    ok = True
    for t, (q,) in STATE_CHECK.items():
        rows = new.execute(q).fetchall()
        print(f"  state {t}: {rows}")
    statuses = dict(new.execute(
        "SELECT status, COUNT(*) FROM exec_positions GROUP BY status"))
    if not set(statuses) <= {"seq_open", "open", "unwinding", "cancelled",
                             "closed"}:
        print("  STATUS VOCABULARY VIOLATION - aborting swap")
        ok = False
    n_fills = new.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
    if n_fills < 22:
        print(f"  fills {n_fills} < 22 expected - check before swap")
    new.close()
    if not ok:
        sys.exit(1)

    # 4) swap into place (keep the old file as v3_old_sep14.db)
    os.replace(TMP_DB, FINAL)
    for ext in ("-wal", "-shm"):
        p = FINAL + ext
        if os.path.exists(p):
            os.remove(p)
    con = sqlite3.connect(f"file:{FINAL}?mode=ro", uri=True)
    print("final integrity:", con.execute("PRAGMA quick_check").fetchone()[0])
    print("final funding_obs:", con.execute(
        "SELECT COUNT(*) FROM funding_obs").fetchone()[0])
    print("final positions:", con.execute(
        "SELECT pos_id, coin, short_venue, long_venue, status, size_usd "
        "FROM exec_positions ORDER BY pos_id").fetchall())
    con.close()
    print("DONE ->", FINAL)


if __name__ == "__main__":
    main()
