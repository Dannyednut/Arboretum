#!/usr/bin/env python3
"""v3 M1 storage layer (spec Part 12 §3.1).

Single SQLite file: download/data/v3.db
Tables:
  funding_obs   (venue, base, ts_ms, rate, interval_h, source)
  book_samples  (same columns as the L2 CSVs, one row per venue/base/window)
  candidates    (discovery rows: sharpe arb table, scanner output)
  events        (E1r/E2/E3/floor_change/delisting)
  kv            (scan watermarks, metadata)

The P0 paper ledger stays in paper_v3.db (its own DB) until P3 consolidates.
All timestamps in funding_obs are epoch MILLISECONDS (native cache format).
"""
import os
import sqlite3

DB = "/home/z/my-project/download/data/v3.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS funding_obs(
  obs_id INTEGER PRIMARY KEY,
  venue TEXT NOT NULL, base TEXT NOT NULL, ts INTEGER NOT NULL,
  rate REAL NOT NULL, interval_h REAL, source TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_funding ON funding_obs(venue, base, ts, source);
CREATE INDEX IF NOT EXISTS ix_funding_pair ON funding_obs(base, venue);

CREATE TABLE IF NOT EXISTS book_samples(
  sid INTEGER PRIMARY KEY, ts TEXT, window TEXT, venue TEXT, base TEXT,
  pass_n INTEGER, mid REAL, spread_bps REAL, ok INTEGER,
  slip_sell_500 REAL, slip_sell_1000 REAL, slip_sell_2500 REAL,
  slip_sell_5000 REAL, slip_sell_10000 REAL, slip_sell_25000 REAL,
  slip_buy_500 REAL, slip_buy_1000 REAL, slip_buy_2500 REAL,
  slip_buy_5000 REAL, slip_buy_10000 REAL, slip_buy_25000 REAL,
  depth25_bid REAL, depth25_ask REAL, source TEXT);

CREATE TABLE IF NOT EXISTS candidates(
  cid INTEGER PRIMARY KEY, ts TEXT, coin TEXT, short_venue TEXT,
  long_venue TEXT, apr30_spread REAL, pos_day_frac REAL, net30 REAL,
  status TEXT, source TEXT, detail TEXT);

CREATE TABLE IF NOT EXISTS events(
  eid INTEGER PRIMARY KEY, ts TEXT, etype TEXT, coin TEXT, venue TEXT,
  detail TEXT);

CREATE TABLE IF NOT EXISTS tripwire_log(
  tid INTEGER PRIMARY KEY, ts TEXT, coin TEXT, short_venue TEXT,
  long_venue TEXT, size_usd REAL, mode TEXT, tw TEXT, result TEXT,
  measured TEXT);

CREATE TABLE IF NOT EXISTS kv(
  key TEXT PRIMARY KEY, value TEXT);
"""

# book_samples idempotency (window+leg+pass is one physical sample)
SCHEMA += """
CREATE UNIQUE INDEX IF NOT EXISTS ux_books
  ON book_samples(window, venue, base, pass_n, ts);
"""


def connect(db=DB):
    os.makedirs(os.path.dirname(db), exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def insert_funding(con, rows):
    """rows: iterable of (venue, base, ts_ms, rate, interval_h, source).
    Dedup by the unique index - silent skip of duplicates."""
    con.executemany(
        "INSERT OR IGNORE INTO funding_obs(venue,base,ts,rate,interval_h,"
        "source) VALUES (?,?,?,?,?,?)", rows)
    con.commit()


def funding_series(con, venue, base, source=None, asof_ms=None):
    """[(ts_ms, rate)] sorted, for one venue/base (optionally one source)."""
    q = ("SELECT ts, rate FROM funding_obs WHERE venue=? AND base=?")
    args = [venue, base]
    if source:
        q += " AND source=?"
        args.append(source)
    if asof_ms:
        q += " AND ts<=?"
        args.append(asof_ms)
    return con.execute(q + " ORDER BY ts", args).fetchall()


def series_index(con, source=None, asof_ms=None):
    """{(base, venue): [(ts_ms, rate), ...]} for full-DB scans."""
    q = ("SELECT base, venue, ts, rate FROM funding_obs")
    args = []
    if source:
        q += " WHERE source=?"
        args.append(source)
    if asof_ms:
        q += (" AND" if source else " WHERE") + " ts<=?"
        args.append(asof_ms)
    out = {}
    for b, v, t, r in con.execute(q, args):
        out.setdefault((b, v), []).append((t, r))
    return out


def kv_set(con, key, value):
    con.execute("INSERT OR REPLACE INTO kv(key,value) VALUES (?,?)",
                (key, str(value)))
    con.commit()


def kv_get(con, key, default=None):
    r = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def ev(con, etype, coin, venue, detail):
    """Shared event writer (exec, M4 floor watch, tripwires)."""
    from datetime import datetime, timezone
    con.execute("INSERT INTO events(ts,etype,coin,venue,detail) "
                "VALUES (?,?,?,?,?)",
                (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 etype, coin, venue, str(detail)[:500]))
    con.commit()
