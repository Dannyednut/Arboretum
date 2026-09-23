#!/usr/bin/env python3
"""v3 M3 execution engine - paper mode (spec Part 12 §3.3, build P3 / Part 15).

Fully automated simulated execution driven by LIVE books. No real orders ever.

What it automates (per spec §3.3):
  entry sequences   default short(floor) leg maker first, long leg taker on
                    short fill; both-taker for taker shorts. Venue styles:
                    maker-first on nado/aster, taker on CEX legs.
  90s legging cap   unfilled side must fill within 90s of the first leg or
                    it converts to taker.
  unwind ladder     exits in tranches sized to the 10/25/50bps depth rungs
                    (recomputed at execution time); maker-first on
                    nado/aster, taker on CEX legs; event exits (floor-param
                    change / delisting) may cross full size at market.
  triggers          spread compression (live < 20% of median, 3 checks
                    >=10min apart), floor-param change (hourly native
                    probe), delisting flag (daily Sharpe listings),
                    funding decay (3d OLS slope < -2 APR pts/day on 2
                    consecutive daily checks).
  pre-entry gate    full M2 tripwire chain (v3_tripwires) must PASS.

Ledger: v3.db tables exec_positions / exec_orders / paper_fills /
exec_drift (+ events). paper_fills follows the spec §3.3 schema
{ts, pair, leg, venue, side, style, target_qty, sim_fill_px,
sim_slip_bps, fee_bps, funding_interval, notes}.

Gap safety (sessions are ping-driven; the engine must survive gaps):
  - a resting maker order is NEVER assumed filled across a gap; fills are
    only recognized when a tick observes the book crossed through the
    order price (conservative: understates fill quality, never inflates)
  - a resting order that is no longer at the touch is cancelled and
    re-posted at the new touch (repost counter capped)
  - every stage transition is persisted before the tick returns

CLI:
  tick                          one engine cycle
  run --minutes M --every S     tick loop (in-session continuous mode)
  status                        dump engine state
  drift                         realized-vs-assumed RT cost report
  unwind --pos N --reason T     force unwind ladder (logged override)
"""
import argparse
import asyncio
import json
import os
import re
import sqlite3
import statistics as stx
import sys
import time
from datetime import datetime, timezone

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import depth_sampler as ds            # noqa: E402
import depth_newvenue as nv           # noqa: E402
import v3_connectors as vc            # noqa: E402
import v3_store as st                 # noqa: E402
import v3_tripwires as tw             # noqa: E402
import v3_portfolio as pf             # noqa: E402  (M5 gate/kill/trickle)
import paper_ledger as pl             # noqa: E402  (fees, medians, apr, books)
import v3_exec_adapter as exa         # noqa: E402  (P4a adapter seam)

V3DB = st.DB
TAKER = pl.TAKER
MAKER = pl.MAKER

ADAPTER = exa.make_adapter("paper")   # default; main --mode live swaps it


def set_adapter(a):
    global ADAPTER
    ADAPTER = a
    return a

# ----------------------------------------------------------------- tuning
MAKER_PATIENCE_S = 600        # entry maker leg: cancel+defer if unfilled
LEGGING_CAP_S = 90            # spec: unfilled side converts to taker
UNWIND_MAKER_PATIENCE_S = 900  # maker unwind tranche -> taker conversion
RUNGS = (10, 25, 50)          # bps depth rungs for unwind tranches
COMPRESSION_FRAC = 0.20       # live spread < 20% of median ...
COMPRESSION_GAP_S = 600       # ... checks spaced >= 10 min ...
COMPRESSION_N = 3             # ... 3 consecutive -> unwind
INVERTED_N = 2                # live spread < 0 on 2 consecutive checks
DECAY_SLOPE = -2.0            # APR pts/day, 3d window (spec)
COOLDOWN_S = 3600             # after cancel/defer, pair re-arms after 1h
REPOST_MAX = 25               # max reposts per order before give-up
MAKER_VENUES = {"nado", "aster"}   # maker-first unwind legs
FREEZE_TTL_S = 86400          # fix3: venue freeze auto-expires after 24h
EMERGENCY_ASSUME_RT_PCT = 0.30  # C2: assumed-RT floor for immediate exits

# P5b desk pin (Part 22 s.6): desk-open candidate, validated every tick.
PIN_PATH = "/home/z/my-project/download/data/desk/pin.json"
DESK_PIN_MAX_USD = 500.0      # engine-side hard cap on pinned size/leg
DESK_PIN_TTL_H = 24.0         # stale pins are ignored, never crash a window
DESK_PIN_MODE = False         # --pin-desk; set via set_pin_mode()

PENDING = ("seq_open", "open", "unwinding")


def nowiso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def nows():
    return time.time()


def iso2ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def ts2iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def econ():
    con = sqlite3.connect(V3DB)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS exec_positions(
      pos_id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts_open TEXT, pair TEXT, coin TEXT, short_venue TEXT, long_venue TEXT,
      size_usd REAL, qty REAL, status TEXT, entry_cost_pct REAL,
      median_apr REAL, pair_cap_usd REAL, flags TEXT,
      unwind_reason TEXT, ts_close TEXT, reason_close TEXT);
    CREATE TABLE IF NOT EXISTS exec_orders(
      oid INTEGER PRIMARY KEY AUTOINCREMENT,
      pos_id INT, ts_post TEXT, ts_done TEXT, leg TEXT, venue TEXT,
      side TEXT, style TEXT, target_qty REAL, limit_px REAL,
      notional_usd REAL, status TEXT, reposts INT DEFAULT 0, notes TEXT);
    CREATE TABLE IF NOT EXISTS paper_fills(
      fid INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, pair TEXT, leg TEXT,
      venue TEXT, side TEXT, style TEXT, target_qty REAL, sim_fill_px REAL,
      sim_slip_bps REAL, fee_bps REAL, funding_interval REAL, notes TEXT);
    CREATE TABLE IF NOT EXISTS exec_drift(
      did INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, pair TEXT,
      assumed_rt_pct REAL, realized_rt_pct REAL, ratio REAL, verdict TEXT,
      detail TEXT);
    """)
    return con


def jload(s, d=None):
    try:
        return json.loads(s) if s else (d if d is not None else {})
    except Exception:
        return d if d is not None else {}


def ev(con, etype, coin, venue, detail):
    con.execute("INSERT INTO events(ts,etype,coin,venue,detail) "
                "VALUES (?,?,?,?,?)",
                (nowiso(), etype, coin, venue, str(detail)[:500]))
    con.commit()


def set_flags(con, pos_id, flags):
    con.execute("UPDATE exec_positions SET flags=? WHERE pos_id=?",
                (json.dumps(flags, default=str), pos_id))
    con.commit()


# ------------------------------------------------------------------ pairs
def set_pin_mode(on):
    global DESK_PIN_MODE
    DESK_PIN_MODE = bool(on)


def load_pin(now=None):
    """P5b: read + validate the desk pin. Returns (pair_dict|None, reason).

    A rejected pin NEVER crashes the window - it only demotes the run to
    static pairs. Validation re-runs every tick (TTL, venue coverage,
    size cap), so an edited/stale pin cannot linger unnoticed."""
    if not os.path.exists(PIN_PATH):
        return None, "no pin file"
    try:
        with open(PIN_PATH) as fh:
            pin = json.load(fh)
    except (OSError, ValueError) as e:
        return None, f"pin unreadable: {e!r}"[:120]
    try:
        from v3_desk import pin_pair_dict, validate_pin
    except Exception as e:                      # pragma: no cover
        return None, f"pin validator unavailable: {e!r}"[:120]
    ok, why = validate_pin(pin, pl.TAKER,
                           nows() if now is None else now,
                           ttl_h=DESK_PIN_TTL_H, max_usd=DESK_PIN_MAX_USD)
    if not ok:
        return None, f"pin rejected: {why}"
    return pin_pair_dict(pin), "ok"


def engine_pairs():
    med = pl.load_medians()
    out = []
    pin_name = None
    if DESK_PIN_MODE:
        pp, why = load_pin()
        if pp is not None:
            if pp["median_apr"] is None:      # same source statics use
                pp["median_apr"] = med.get((pp["coin"], pp["short"],
                                            pp["long"]))
            out.append(pp)
            pin_name = pp["pair"]
            print(f"  desk pin ACTIVE: {pp['pair']} ${pp['size_usd']:.0f}/leg "
                  f"seq={pp['seq']} (adds a candidate; all gates unchanged)")
        else:
            print(f"  desk pin: {why} - static pairs only")
    for p in pl.PAIRS:
        if pin_name and p["pair"] == pin_name:
            print(f"  desk pin replaces static config for {pin_name} "
                  f"this window")
            continue
        out.append({
            "pair": p["pair"], "coin": p["coin"], "short": p["short"],
            "long": p["long"], "size_usd": p["size_usd"],
            "seq": "tt" if p["short_style"] == "taker" else "sm_lt",
            "median_apr": med.get((p["coin"], p["short"], p["long"])),
            "src": "static",
        })
    return out


# ------------------------------------------------------------------ books
async def fetch_books(need):
    """need: set of (venue, coin) -> {(venue,coin): (bids, asks) | None}"""
    out = {}
    for venue, coin in sorted(need):
        try:
            out[(venue, coin)] = await pl.get_book(venue, coin)
        except Exception as e:
            out[(venue, coin)] = None
            print(f"  book {venue}/{coin}: ERR {str(e)[:80]}")
    return out


def mid_of(book):
    if not book:
        return None
    b, a = book
    if not b or not a:
        return None
    return (b[0][0] + a[0][0]) / 2.0


def rung_notionals(levels, mid, sgn):
    """Cumulative USD walkable within each slip rung.
    sgn -1: bid side (sell into bids), +1: ask side (buy from asks)."""
    out = {}
    for rung in RUNGS:
        lim = mid * (1 + sgn * rung / 1e4)
        out[rung] = sum(p * q for p, q in levels
                        if (p >= lim if sgn < 0 else p <= lim))
    return out


async def funding_interval(venue, coin):
    try:
        r = await vc.get_funding(venue, coin)
        if r and len(r) >= 2 and r[1]:
            return float(r[1])
    except Exception:
        pass
    return None


def write_fill(con, pair, leg, venue, side, style, qty, px, slip_bps,
               fee_bps, ivl, note):
    con.execute(
        "INSERT INTO paper_fills(ts,pair,leg,venue,side,style,target_qty,"
        "sim_fill_px,sim_slip_bps,fee_bps,funding_interval,notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (nowiso(), pair, leg, venue, side, style, round(qty, 10),
         round(px, 10), round(slip_bps, 2) if slip_bps is not None else None,
         round(fee_bps, 3), ivl, note[:240]))
    con.commit()
    print(f"  FILL {pair} {leg}/{venue} {side} {style} qty={qty:.6g} "
          f"px={px:.6g} slip={slip_bps if slip_bps is not None else 0:.1f}bps "
          f"fee={fee_bps:.2f}bps")


# ----------------------------------------------------------- entry plumbing
def active_positions(con):
    return {r["pair"]: dict(r) for r in con.execute(
        "SELECT * FROM exec_positions WHERE status IN (?,?,?)",
        PENDING).fetchall()}


def pair_on_cooldown(con, pair):
    r = con.execute(
        "SELECT ts_open FROM exec_positions WHERE pair=? AND "
        "status IN ('cancelled','closed') ORDER BY ts_open DESC LIMIT 1",
        (pair,)).fetchone()
    if r and nows() - iso2ts(r["ts_open"]) < COOLDOWN_S:
        return True
    return False


def freeze_active(con, venue):
    """fix3: TTL-aware venue freeze check.

    Parses both KV formats (epoch float, 'frozen <ISO>: ...').  Past-TTL
    freezes are deleted + EXEC_FREEZE_EXPIRE emitted.  Unparseable value
    stays frozen (fail-safe).
    """
    key = f"venue_freeze_{venue}"
    val = st.kv_get(con, key)
    if not val:
        return False
    ts = None
    s = str(val)
    m = re.match(r"frozen (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", s)
    if m:
        try:
            ts = iso2ts(m.group(1))
        except Exception:
            ts = None
    elif re.match(r"^-?\d+(\.\d+)?$", s):
        ts = float(s)
    if ts is None:
        return True                       # unparseable -> fail-safe frozen
    if nows() - ts >= FREEZE_TTL_S:
        con.execute("DELETE FROM kv WHERE key=?", (key,))
        con.commit()
        ev(con, "EXEC_FREEZE_EXPIRE", "", venue,
           f"venue freeze expired after {FREEZE_TTL_S // 3600}h "
           f"(set {ts2iso(ts)})")
        print(f"  freeze {venue}: TTL {FREEZE_TTL_S // 3600}h reached, "
              f"cleared")
        return False
    return True


def pair_cap(con, books, p, mid_s, mid_l):
    """M5-lite: pair_cap = min of leg 25bps entry-side capacities."""
    caps = []
    sb, sa = books[(p["short"], p["coin"])] or (None, None)
    lb, la = books[(p["long"], p["coin"])] or (None, None)
    for levels, mid, sgn in ((sb, mid_s, -1), (la, mid_l, +1)):
        if not levels or not mid:
            return None
        caps.append(rung_notionals(levels, mid, sgn).get(25, 0.0))
    return min(caps) if caps else None


def new_position(con, p, med_apr, cap):
    qty = 0.0  # set properly once the short touch is known
    cur = con.execute(
        "INSERT INTO exec_positions(ts_open,pair,coin,short_venue,"
        "long_venue,size_usd,qty,status,entry_cost_pct,median_apr,"
        "pair_cap_usd,flags) VALUES (?,?,?,?,?,?,?, 'seq_open',0,?,?,?)",
        (nowiso(), p["pair"], p["coin"], p["short"], p["long"],
         p["size_usd"], qty, med_apr, cap,
         json.dumps({"seq": p["seq"], "stage": "init",
                     "src": p.get("src", "static"),
                     "cost_bps": {}, "comp_streak": 0,
                     "comp_last": 0.0, "inv_streak": 0})))
    con.commit()
    return cur.lastrowid


def post_maker(con, pos, leg, venue, side, qty, px, notional, note="",
               reposts=0):
    cur = con.execute(
        "INSERT INTO exec_orders(pos_id,ts_post,leg,venue,side,style,"
        "target_qty,limit_px,notional_usd,status,reposts,notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,'resting',?,?)",
        (pos["pos_id"], nowiso(), leg, venue, side, "maker", qty, px,
         notional, reposts, note[:200]))
    con.commit()
    print(f"  POST {pos['pair']} {leg}/{venue} maker {side} "
          f"qty={qty:.6g} @ {px:.6g}")
    ADAPTER.place_maker(venue, pos["coin"], side, qty, px, notional,
                        f"pos:{pos['pos_id']};{note};oid:{cur.lastrowid}")
    return cur.lastrowid


def cancel_order(con, oid, status, note):
    con.execute("UPDATE exec_orders SET status=?, ts_done=?, notes=? "
                "WHERE oid=?",
                (status, nowiso(), note[:240], oid))
    con.commit()


async def order_fill(con, pos, o, px, note=""):
    """Write the maker fill row + mark order filled."""
    cancel_order(con, o["oid"], "filled", note or "maker fill observed")
    ivl = await funding_interval(o["venue"], pos["coin"])
    write_fill(con, pos["pair"], o["leg"], o["venue"], o["side"], "maker",
               o["target_qty"], px, 0.0, MAKER.get(o["venue"],
                                                   TAKER[o["venue"]]) * 1e4,
               ivl, f"pos:{pos['pos_id']};{note}")
    ADAPTER.on_maker_fill(o["venue"], pos["coin"], o["side"],
                          o["target_qty"], px, o["oid"],
                          f"pos:{pos['pos_id']};{note}")


def entry_cost_update(con, pos):
    f = jload(pos["flags"])
    cb = f.get("cost_bps", {})
    pct = sum(cb.values()) / 1e4 * 100
    con.execute("UPDATE exec_positions SET entry_cost_pct=? WHERE pos_id=?",
                (pct, pos["pos_id"]))
    con.commit()
    return pct


async def take_taker(con, pos, leg, venue, side, usd, book, note=""):
    """Taker fill via execution adapter (P4a: paper sim authoritative)."""
    r = ADAPTER.place_taker(venue, pos["coin"], side, usd, book,
                            f"pos:{pos['pos_id']};{note}")
    if not r or not r.get("ok"):
        reason = (r or {}).get("reason", "no fill")
        print(f"  taker {venue}/{pos['coin']}: {reason}")
        return False
    slip, qty, vwap, fee = r["slip_bps"], r["qty"], r["px"], r["fee_bps"]
    ivl = await funding_interval(venue, pos["coin"])
    write_fill(con, pos["pair"], leg, venue, side, "taker", qty, vwap,
               slip, fee, ivl, f"pos:{pos['pos_id']};{note}")
    f = jload(pos["flags"])
    f.setdefault("cost_bps", {})[f"{leg}:{side}:entry"] = slip + fee
    set_flags(con, pos["pos_id"], f)
    pos["flags"] = json.dumps(f, default=str)
    return True


# ------------------------------------------------- sequence state machine
def stage_get(pos):
    return jload(pos["flags"]).get("stage", "init")


def stage_set(con, pos, stage, **extra):
    f = jload(pos["flags"])
    f["stage"] = stage
    f["stage_ts"] = nows()
    f.update(extra)
    set_flags(con, pos["pos_id"], f)
    pos["flags"] = json.dumps(f, default=str)


async def start_entry(con, p, books):
    """Chain already passed. Fire sequence step 1 per pair seq."""
    mid_s = mid_of(books[(p["short"], p["coin"])])
    mid_l = mid_of(books[(p["long"], p["coin"])])
    cap = pair_cap(con, books, p, mid_s, mid_l)
    size = p["size_usd"]
    if cap is not None and cap > 0:
        clamp = min(size, 0.7 * cap)
        if clamp < size * 0.8:
            print(f"  size clamp {p['pair']}: ${size:.0f} -> ${clamp:.0f} "
                  f"(0.7 x pair_cap ${cap:.0f})")
        size = clamp
    # M5 portfolio gate (kill / sizing / venue caps / correlation /
    # RWA bucket / leverage) - runs BEFORE new_position is created
    gok, size, gnotes = pf.m5_gate(con, p["coin"], p["short"], p["long"],
                                   size, cap)
    for gn in gnotes:
        print(f"  {gn}")
    if not gok:
        ev(con, "EXEC_GATE_DENY", p["coin"],
           f"{p['short']}->{p['long']}", "; ".join(gnotes))
        print(f"  {p['pair']}: M5 gate DENY, deferred")
        return None
    pos_id = new_position(con, p, p.get("median_apr"), cap)
    if p.get("src") == "desk_pin":          # P5b audit: pin-sourced entry
        ev(con, "DESK_PIN_ENTRY", p["coin"], f"{p['short']}->{p['long']}",
           f"pos{pos_id} src=desk_pin size={size:.0f} seq={p['seq']}")
    sb, sa = books.get((p["short"], p["coin"])) or (None, None)
    lb, la = books.get((p["long"], p["coin"])) or (None, None)
    if mid_of((sb, sa)) is None or mid_of((lb, la)) is None:
        # book died between tick snapshot and entry -> abort cleanly
        con.execute("UPDATE exec_positions SET status='cancelled', "
                    "reason_close='book missing at entry' WHERE pos_id=?",
                    (pos_id,))
        con.commit()
        print(f"  {p['pair']}: book missing at entry, deferred")
        return None
    pos = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                           (pos_id,)).fetchone())

    if p["seq"] == "tt":
        # both taker, short first, same tick
        ok_s = await take_taker(con, pos, "short", p["short"], "sell",
                                size, (sb, sa), "entry seq tt")
        if not ok_s:
            con.execute("UPDATE exec_positions SET status='cancelled',"
                        "reason_close='short taker no fill (book)' "
                        "WHERE pos_id=?", (pos_id,))
            con.commit()
            return None
        pos = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                               (pos_id,)).fetchone())
        qty = p["size_usd"] / mid_s
        con.execute("UPDATE exec_positions SET qty=? WHERE pos_id=?",
                    (qty, pos_id))
        con.commit()
        ok_l = await take_taker(con, pos, "long", p["long"], "buy",
                                size, (lb, la), "entry seq tt")
        if ok_l:
            qty_l = p["size_usd"] / mid_l
            f = jload(pos["flags"])
            f["long_qty"] = qty_l
            set_flags(con, pos_id, f)
            finish_open(con, pos)
        else:
            stage_set(con, pos, "long_taker",
                      pending_deadline=nows() + LEGGING_CAP_S,
                      long_qty=size / mid_l)
        return pos_id

    # sm_lt: short maker at touch first
    px = sa[0][0]
    qty = size / px
    con.execute("UPDATE exec_positions SET qty=? WHERE pos_id=?",
                (qty, pos_id))
    con.commit()
    post_maker(con, pos, "short", p["short"], "sell", qty, px, size,
               "entry seq sm_lt")
    stage_set(con, pos, "short_maker")
    return pos_id


def finish_open(con, pos):
    pct = entry_cost_update(con, pos)
    con.execute("UPDATE exec_positions SET status='open' WHERE pos_id=?",
                (pos["pos_id"],))
    con.commit()
    ev(con, "EXEC_OPEN", pos["coin"],
       f"{pos['short_venue']}->{pos['long_venue']}",
       f"pos {pos['pos_id']} open, entry cost {pct:.3f}% one-way")
    print(f"  OPEN pos #{pos['pos_id']} {pos['pair']} entry {pct:.3f}%")


async def advance_resting(con, pos, books):
    """Fill-check / repost / patience-cancel resting entry+unwind orders."""
    orders = con.execute(
        "SELECT * FROM exec_orders WHERE pos_id=? AND status='resting'",
        (pos["pos_id"],)).fetchall()
    for o in orders:
        book = books.get((o["venue"], pos["coin"]))
        if not book:
            continue
        bids, asks = book
        if o["side"] == "sell":
            crossed = bids and bids[0][0] >= o["limit_px"]
            stale = asks and asks[0][0] < o["limit_px"]
        else:
            crossed = asks and asks[0][0] <= o["limit_px"]
            stale = bids and bids[0][0] > o["limit_px"]
        if crossed:
            px = o["limit_px"]
            await order_fill(con, pos, o, px)
            await on_maker_filled(con, pos, o, books)
        elif stale and o["reposts"] < REPOST_MAX:
            ntouch = (asks if o["side"] == "sell" else bids)[0][0]
            if abs(ntouch - o["limit_px"]) / o["limit_px"] < 1e-6:
                continue
            cancel_order(con, o["oid"], "cancelled", "repost-at-touch")
            post_maker(con, pos, o["leg"], o["venue"], o["side"],
                       o["target_qty"], ntouch, o["notional_usd"],
                       f"repost #{o['reposts'] + 1}",
                       reposts=o["reposts"] + 1)
        elif pos["status"] == "unwinding" and \
                (nows() - iso2ts(o["ts_post"]) > UNWIND_MAKER_PATIENCE_S):
            cancel_order(con, o["oid"], "expired",
                         "maker patience expired -> taker convert")
            await maker_to_taker(con, pos, o, books)


async def on_maker_filled(con, pos, o, books):
    """Entry short-maker filled -> long taker now (90s cap)."""
    if pos["status"] != "seq_open" or o["leg"] != "short":
        return
    f = jload(pos["flags"])
    f.setdefault("cost_bps", {})[f"{o['leg']}:{o['side']}:entry"] = \
        MAKER.get(o["venue"], 0.0) * 1e4
    set_flags(con, pos["pos_id"], f)
    pos["flags"] = json.dumps(f, default=str)
    stage_set(con, pos, "long_taker",
              pending_deadline=nows() + LEGGING_CAP_S)
    p = engine_pair_of(pos)
    lb, la = books.get((pos["long_venue"], pos["coin"])) or (None, None)
    pos = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                           (pos["pos_id"],)).fetchone())
    ok = await take_taker(con, pos, "long", pos["long_venue"], "buy",
                          pos["size_usd"], (lb, la), "entry legging-cap seq")
    if ok:
        f = jload(pos["flags"])
        mid_l = mid_of((lb, la))
        f["long_qty"] = pos["size_usd"] / mid_l if mid_l else None
        f.pop("pending_deadline", None)
        set_flags(con, pos["pos_id"], f)
        pos = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                               (pos["pos_id"],)).fetchone())
        finish_open(con, pos)
    # else: deadline enforced in enforce_legging_cap next tick


def engine_pair_of(pos):
    for p in engine_pairs():
        if p["pair"] == pos["pair"]:
            return p
    return {"pair": pos["pair"], "coin": pos["coin"],
            "short": pos["short_venue"], "long": pos["long_venue"],
            "size_usd": pos["size_usd"], "seq": "sm_lt",
            "median_apr": pos["median_apr"]}


async def maker_to_taker(con, pos, o, books):
    """Patience expired: convert resting maker to taker at current book."""
    book = books.get((o["venue"], pos["coin"])) or (None, None)
    bids, asks = book
    mid = mid_of(book)
    if mid is None:
        return
    levels = bids if o["side"] == "sell" else asks
    if not levels:
        return
    r = ds.walk(levels, mid, o["notional_usd"])
    if r is None:
        return
    slip, filled, vwap = r
    qty = filled / vwap
    if pos["status"] == "unwinding":
        f0 = jload(pos["flags"])
        eq = (f0.get("entry_leg_qty") or {}).get(o["leg"])
        if eq:
            done_q = (f0.get("unwind_done_qty") or {}).get(o["leg"], 0.0)
            qty = min(qty, max(eq - done_q, 0.0))   # C1 clamp
            filled = qty * vwap
    fee = TAKER.get(o["venue"], 0.0005) * 1e4
    ivl = await funding_interval(o["venue"], pos["coin"])
    write_fill(con, pos["pair"], o["leg"], o["venue"], o["side"], "taker",
               qty, vwap, slip, fee, ivl,
               f"pos:{pos['pos_id']};maker->taker conversion")
    f = jload(pos["flags"])
    tag = "exit" if pos["status"] == "unwinding" else "entry"
    f.setdefault("cost_bps", {})[f"{o['leg']}:{o['side']}:{tag}"] = slip + fee
    set_flags(con, pos["pos_id"], f)
    if pos["status"] == "seq_open" and o["leg"] == "short":
        await on_maker_filled(con, pos, o, books)
    elif pos["status"] == "unwinding":
        add_unwind_done(con, pos, o["leg"], filled, qty)


async def enforce_legging_cap(con, pos, books):
    f = jload(pos["flags"])
    dl = f.get("pending_deadline")
    if not dl or pos["status"] != "seq_open":
        return
    if nows() <= dl:
        # retry the long leg now
        lb, la = books.get((pos["long_venue"], pos["coin"])) or (None, None)
        pos2 = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                                (pos["pos_id"],)).fetchone())
        ok = await take_taker(con, pos2, "long", pos2["long_venue"], "buy",
                              pos2["size_usd"], (lb, la), "entry legging-cap retry")
        if ok:
            f = jload(pos2["flags"])
            mid_l = mid_of((lb, la))
            f["long_qty"] = pos2["size_usd"] / mid_l if mid_l else None
            f.pop("pending_deadline", None)
            set_flags(con, pos2["pos_id"], f)
            pos2 = dict(con.execute(
                "SELECT * FROM exec_positions WHERE pos_id=?",
                (pos2["pos_id"],)).fetchone())
            finish_open(con, pos2)
        elif nows() > dl:
            ev(con, "EXEC_LEGGING", pos["coin"], pos["long_venue"],
               f"pos {pos['pos_id']} long leg past 90s cap")
        return
    ev(con, "EXEC_LEGGING", pos["coin"], pos["long_venue"],
       f"pos {pos['pos_id']} long leg past {LEGGING_CAP_S}s cap -> force taker")
    lb, la = books.get((pos["long_venue"], pos["coin"])) or (None, None)
    pos2 = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                            (pos["pos_id"],)).fetchone())
    ok = await take_taker(con, pos2, "long", pos2["long_venue"], "buy",
                          pos2["size_usd"], (lb, la), "LEGGING CAP force taker")
    f = jload(pos2["flags"])
    f.pop("pending_deadline", None)
    set_flags(con, pos2["pos_id"], f)
    if ok:
        pos2 = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                                (pos2["pos_id"],)).fetchone())
        finish_open(con, pos2)


async def check_entry_patience(con, pos, books):
    """Entry short maker never filled within patience -> defer."""
    orders = con.execute(
        "SELECT MIN(ts_post) t FROM exec_orders WHERE pos_id=? AND "
        "status='resting' AND leg='short'", (pos["pos_id"],)).fetchone()
    if not orders or not orders["t"]:
        return
    if nows() - iso2ts(orders["t"]) > MAKER_PATIENCE_S:
        for o in con.execute("SELECT * FROM exec_orders WHERE pos_id=? AND "
                             "status='resting'", (pos["pos_id"],)):
            cancel_order(con, o["oid"], "cancelled", "entry deferred")
        con.execute("UPDATE exec_positions SET status='cancelled', "
                    "reason_close='maker patience expired - entry deferred' "
                    "WHERE pos_id=?", (pos["pos_id"],))
        con.commit()
        ev(con, "EXEC_DEFER", pos["coin"], pos["short_venue"],
           f"pos {pos['pos_id']} entry deferred (maker patience)")
        print(f"  DEFER {pos['pair']} pos #{pos['pos_id']} (maker patience)")


# ------------------------------------------------------------ unwind ladder
def add_unwind_done(con, pos, leg, usd_filled, qty_filled=None):
    f = jload(pos["flags"])
    f.setdefault("unwind_done", {})[leg] = \
        f.get("unwind_done", {}).get(leg, 0.0) + usd_filled
    if qty_filled is not None:
        f.setdefault("unwind_done_qty", {})[leg] = \
            f.get("unwind_done_qty", {}).get(leg, 0.0) + qty_filled
    set_flags(con, pos["pos_id"], f)
    pos["flags"] = json.dumps(f, default=str)
    check_unwind_complete(con, pos)


def _px_covering(levels, qty):
    """C1: worst-case VWAP to fill `qty` units walking best-first."""
    remain, cost = qty, 0.0
    for px, q in levels:
        take = min(remain, q)
        cost += take * px
        remain -= take
        if remain <= 1e-15:
            return cost / qty
    if qty - remain > 1e-15:
        return cost / (qty - remain)
    return levels[-1][0] if levels else 0.0


def _entry_leg_qty(con, pos):
    """C1: per-leg entry qty snapshot from paper_fills (entry side only,
    pos-scoped via the notes prefix the engine stamps on every fill)."""
    q = {}
    for leg, side in (("short", "sell"), ("long", "buy")):
        rows = con.execute(
            "SELECT target_qty FROM paper_fills WHERE pair=? AND leg=? "
            "AND side=? AND notes LIKE ?",
            (pos["pair"], leg, side,
             f"pos:{pos['pos_id']};%")).fetchall()
        tot = sum(float(r[0] or 0) for r in rows)
        q[leg] = tot if tot > 0 else None
    return q


def check_unwind_complete(con, pos):
    f = jload(pos["flags"])
    done = f.get("unwind_done", {})
    done_q = f.get("unwind_done_qty", {})
    eq = f.get("entry_leg_qty") or {}
    size = pos["size_usd"]
    # C1: complete on unit qty (>=98% of entry leg qty, both legs);
    # legacy USD fallback (>=95% of size) when no qty snapshot exists
    if eq.get("short") and eq.get("long"):
        complete = all(done_q.get(l, 0.0) >= 0.98 * eq[l]
                       for l in ("short", "long"))
    else:
        complete = all(done.get(l, 0.0) >= 0.95 * size
                       for l in ("short", "long"))
    if complete:
        pct_in = pos["entry_cost_pct"] or 0.0
        realized = 0.0
        cb = f.get("cost_bps", {})
        for leg in ("short", "long"):
            for side in ("sell", "buy"):
                realized += cb.get(f"{leg}:{side}:entry", 0.0) / 1e4
                realized += cb.get(f"{leg}:{side}:exit", 0.0) / 1e4
        realized *= 100
        # C2: emergency (immediate) exits carry a conservative assumed-RT
        # floor; drift rows are class-tagged for the kill counter
        cls = "immediate" if f.get("unwind_immediate") else "planned"
        assumed = max(2 * pct_in, EMERGENCY_ASSUME_RT_PCT) \
            if cls == "immediate" else 2 * pct_in
        ratio = realized / assumed if assumed > 0 else None
        verdict = ("OK" if ratio is not None and ratio <= 1.3 else "OVER")
        detail = dict(cb)
        detail["class"] = cls
        con.execute("INSERT INTO exec_drift(ts,pair,assumed_rt_pct,"
                    "realized_rt_pct,ratio,verdict,detail) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (nowiso(), pos["pair"], round(assumed, 4),
                     round(realized, 4),
                     round(ratio, 3) if ratio else None, verdict,
                     json.dumps(detail, default=str)[:400]))
        con.execute("UPDATE exec_positions SET status='closed', ts_close=?, "
                    "reason_close=? WHERE pos_id=?",
                    (nowiso(), f.get("unwind_reason", "manual"),
                     pos["pos_id"]))
        con.commit()
        ev(con, "EXEC_CLOSE", pos["coin"], pos["short_venue"],
           f"pos {pos['pos_id']} closed [{cls}] RT realized {realized:.3f}% "
           f"vs assumed {assumed:.3f}% ratio {ratio if ratio else 'n/a'}")
        print(f"  CLOSE pos #{pos['pos_id']} {pos['pair']} [{cls}] RT "
              f"{realized:.3f}% vs assumed {assumed:.3f}% [{verdict}]")


def unwind_styles(pos, immediate):
    """per-leg exit style: maker-first on nado/aster, taker on CEX legs."""
    out = {}
    for leg, v in (("short", pos["short_venue"]), ("long", pos["long_venue"])):
        out[leg] = "taker" if (immediate or v not in MAKER_VENUES) else "maker"
    return out


async def unwind_tranche(con, pos, leg, venue, style, books, note="",
                         immediate=False):
    """Execute at most one tranche of the ladder for this leg this tick."""
    f = jload(pos["flags"])
    done = f.get("unwind_done", {}).get(leg, 0.0)
    book = books.get((venue, pos["coin"])) or (None, None)
    bids, asks = book
    mid = mid_of(book)
    if mid is None:
        return
    # exit side: short leg closes with BUY (asks), long leg with SELL (bids)
    side = "buy" if leg == "short" else "sell"
    levels = asks if side == "buy" else bids
    sgn = +1 if side == "buy" else -1
    # C1: qty-first remaining sizing - when an entry-qty snapshot exists,
    # the tranche notional is what remain_qty costs at the WORST covering
    # VWAP (may exceed size_usd if price rose); legacy USD rule otherwise
    eq = (f.get("entry_leg_qty") or {}).get(leg)
    done_q = (f.get("unwind_done_qty") or {}).get(leg, 0.0)
    remain_q = None
    remain = pos["size_usd"] - done
    if eq:
        remain_q = max(eq - done_q, 0.0)
        if remain_q <= 1e-12:
            return
        remain = remain_q * _px_covering(levels, remain_q)
    elif remain <= 0.05 * pos["size_usd"]:
        return
    rn = rung_notionals(levels, mid, sgn)
    stage = f.get("unwind_stage", {}).get(leg, 0)
    # immediate event exits may cross full size; ladder exits use rungs
    if immediate:
        tranche = remain
    else:
        # cumulative-slice ladder: each tranche may use everything still
        # unwound-able within progressively wider rungs (10 -> 25 -> 50bps)
        cands = [rn[10] - done, rn[25] - done, rn[50] - done]
        slice_usd = max(cands)
        if slice_usd <= 0:
            # thin book beyond 50bps: after 3 zero-progress ticks, allow a
            # full-remain tranche (logged) so the ladder cannot strand
            zs = f.get("zero_fill_streak", {}).get(leg, 0) + 1
            f.setdefault("zero_fill_streak", {})[leg] = zs
            set_flags(con, pos["pos_id"], f)
            if zs < 3:
                print(f"  ladder {pos['pair']} {leg}: book thin within "
                      f"50bps, waiting ({zs}/3)")
                return
            slice_usd = remain
            note += ";thin-book extended tranche"
        else:
            f.setdefault("zero_fill_streak", {})[leg] = 0
            set_flags(con, pos["pos_id"], f)
        tranche = min(remain, max(slice_usd, 0))
        if tranche <= 0:
            return
    f.setdefault("unwind_stage", {})[leg] = stage + 1
    set_flags(con, pos["pos_id"], f)

    if style == "maker":
        resting = con.execute(
            "SELECT 1 FROM exec_orders WHERE pos_id=? AND leg=? AND "
            "status='resting'", (pos["pos_id"], leg)).fetchone()
        if resting:
            return
        touch = (asks if side == "buy" else bids)[0][0]
        qty = tranche / touch
        if remain_q is not None and qty > remain_q:
            qty = remain_q          # C1 post-walk clamp (maker)
            tranche = qty * touch
        post_maker(con, pos, leg, venue, side, qty, touch, tranche,
                   f"unwind tranche {note}")
    else:
        r = ADAPTER.place_taker(venue, pos["coin"], side, tranche, book,
                                f"pos:{pos['pos_id']};unwind;{note}")
        if not r or not r.get("ok"):
            return
        slip, vwap, fee = r["slip_bps"], r["px"], r["fee_bps"]
        qty = r["qty"]
        if remain_q is not None and qty > remain_q:
            qty = remain_q          # C1 post-walk clamp (taker)
            filled = qty * vwap
        else:
            filled = r["filled"]
        ivl = await funding_interval(venue, pos["coin"])
        write_fill(con, pos["pair"], leg, venue, side, "taker", qty, vwap,
                   slip, fee, ivl, f"pos:{pos['pos_id']};unwind;{note}")
        f = jload(pos["flags"])
        f.setdefault("cost_bps", {})[f"{leg}:{side}:exit"] = slip + fee
        set_flags(con, pos["pos_id"], f)
        pos["flags"] = json.dumps(f, default=str)
        add_unwind_done(con, pos, leg, filled, qty)


async def start_unwind(con, pos, reason, immediate=False):
    if pos["status"] == "unwinding":
        return
    f = jload(pos["flags"])
    f["unwind_reason"] = reason
    f["unwind_started"] = nowiso()
    f["unwind_immediate"] = bool(immediate)
    f["entry_leg_qty"] = _entry_leg_qty(con, pos)   # C1 qty snapshot
    set_flags(con, pos["pos_id"], f)
    con.execute("UPDATE exec_positions SET status='unwinding', "
                "unwind_reason=? WHERE pos_id=?", (reason, pos["pos_id"]))
    con.commit()
    ev(con, "EXEC_UNWIND_START", pos["coin"], pos["short_venue"],
       f"pos {pos['pos_id']} unwind start: {reason}"
       f"{' (immediate)' if immediate else ''}")
    print(f"  UNWIND pos #{pos['pos_id']} {pos['pair']}: {reason}")


async def progress_unwind(con, pos, books):
    f = jload(pos["flags"])
    reason = f.get("unwind_reason", "manual")
    immediate = bool(f.get("unwind_immediate"))
    styles = unwind_styles(pos, immediate)
    # maker unwind fills + patience conversion
    for o in con.execute("SELECT * FROM exec_orders WHERE pos_id=? AND "
                         "status='resting'", (pos["pos_id"],)).fetchall():
        book = books.get((o["venue"], pos["coin"]))
        if not book:
            continue
        bids, asks = book
        if o["side"] == "sell":
            crossed = bids and bids[0][0] >= o["limit_px"]
        else:
            crossed = asks and asks[0][0] <= o["limit_px"]
        if crossed:
            await order_fill(con, pos, o, o["limit_px"], "unwind maker fill")
            fee = MAKER.get(o["venue"], 0.0) * 1e4
            f2 = jload(pos["flags"])
            f2.setdefault("cost_bps", {})[f"{o['leg']}:{o['side']}:exit"] = fee
            set_flags(con, pos["pos_id"], f2)
            pos["flags"] = json.dumps(f2, default=str)
            add_unwind_done(con, pos, o["leg"], o["notional_usd"],
                            o["target_qty"])
        elif nows() - iso2ts(o["ts_post"]) > UNWIND_MAKER_PATIENCE_S:
            cancel_order(con, o["oid"], "expired",
                         "unwind maker patience -> taker convert")
            await maker_to_taker(con, pos, o, books)
    pos = dict(con.execute("SELECT * FROM exec_positions WHERE pos_id=?",
                           (pos["pos_id"],)).fetchone())
    if pos["status"] != "unwinding":
        return
    for leg, venue in (("short", pos["short_venue"]),
                       ("long", pos["long_venue"])):
        await unwind_tranche(con, pos, leg, venue, styles[leg], books,
                             reason, immediate)


# ---------------------------------------------------------------- triggers
def guard(con, key, seconds):
    """Time-gated check: True if last run older than `seconds`."""
    last = st.kv_get(con, key)
    if last and nows() - float(last) < seconds:
        return False
    st.kv_set(con, key, str(nows()))
    return True


async def live_spreads(coins):
    m = await pl.sharpe_current(sorted(coins))
    return m


async def trigger_compression(con, pos, live):
    f = jload(pos["flags"])
    sl = live.get(pos["coin"], {})
    if pos["short_venue"] not in sl or pos["long_venue"] not in sl:
        return
    med = pos["median_apr"]
    if not med or med <= 0:
        return
    spr = pl.apr(*sl[pos["short_venue"]][:2]) - \
        pl.apr(*sl[pos["long_venue"]][:2])
    if not guard(con, f"exec_compchk_{pos['pos_id']}", COMPRESSION_GAP_S):
        return
    if spr < 0:
        f["inv_streak"] = f.get("inv_streak", 0) + 1
        f["comp_streak"] = 0
    elif spr < COMPRESSION_FRAC * med:
        f["comp_streak"] = f.get("comp_streak", 0) + 1
        f["inv_streak"] = 0
    else:
        f["comp_streak"] = 0
        f["inv_streak"] = 0
    f["comp_last"] = spr
    set_flags(con, pos["pos_id"], f)
    if f["comp_streak"] >= COMPRESSION_N:
        await start_unwind(con, pos, "spread_compression")
    elif f["inv_streak"] >= INVERTED_N:
        await start_unwind(con, pos, "spread_inverted")


async def trigger_floor(con, pos):
    if not guard(con, f"exec_floorchk_{pos['pos_id']}", 3600):
        return
    ok, meas = await tw.tw5_floor(con, pos["coin"], pos["short_venue"],
                                  pos["long_venue"], held=True)
    # fix5: persist the held probe (incl old/new params) to tripwire_log
    con.execute("INSERT INTO tripwire_log(ts,coin,short_venue,long_venue,"
                "size_usd,mode,tw,result,measured) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (nowiso(), pos["coin"], pos["short_venue"], pos["long_venue"],
                 pos["size_usd"], "held_check", "TW5_floor_monitor",
                 "pass" if ok else "fail",
                 json.dumps(meas, default=str)[:800]))
    con.commit()
    if not ok and meas.get("changes"):
        await start_unwind(con, pos, "floor_param_change", immediate=True)


async def trigger_listings(con, pos):
    if not guard(con, "exec_listchk", 86400):
        return
    rows, asof, stale = await vc.Sharpe.listings()
    for r in rows if isinstance(rows, list) else []:
        if str(r.get("base_coin", "")).upper() != pos["coin"]:
            continue
        stat = str(r.get("status", r.get("state", ""))).lower()
        if any(k in stat for k in ("suspend", "delist", "halt")):
            await start_unwind(con, pos, f"delisting_alert:{stat}",
                               immediate=True)
            return


def spread_slope_apr_per_day(con, pos, hours=72):
    """OLS slope of the interval-true pair spread, APR pts/day, from
    funding_obs (hourly grid, native preferred, sharpe fallback)."""
    lo = int(nows()) - hours * 3600
    series = {}
    for v in (pos["short_venue"], pos["long_venue"]):
        rows = con.execute(
            "SELECT ts, rate, interval_h, source FROM funding_obs "
            "WHERE venue=? AND base=? AND ts>? ORDER BY ts",
            (v, pos["coin"], lo)).fetchall()
        if not rows:
            continue
        bucket = {}
        for r in rows:
            h = r["ts"] // 3600
            ivl = r["interval_h"] or 1.0
            aprv = r["rate"] / ivl * 24 * 365
            pr = bucket.get(h)
            # native rows preferred over sharpe rows in the same hour
            if pr is None or (r["source"].startswith("native")
                              and not pr[1].startswith("native")):
                bucket[h] = (aprv, r["source"])
        series[v] = {h: a for h, (a, _) in bucket.items()}
    sv, lv = pos["short_venue"], pos["long_venue"]
    if sv not in series or lv not in series:
        return None
    common = sorted(set(series[sv]) & set(series[lv]))
    if len(common) < 24:
        return None
    xs = [h * 3600 for h in common]
    ys = [series[sv][h] - series[lv][h] for h in common]
    xbar, ybar = stx.mean(xs), stx.mean(ys)
    sxx = sum((x - xbar) ** 2 for x in xs)
    sxy = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys))
    if sxx <= 0:
        return None
    slope_h = sxy / sxx
    return slope_h * 24 / 100.0   # APR fraction/h -> APR pts/day


async def trigger_decay(con, pos):
    if not guard(con, f"exec_decaychk_{pos['pos_id']}", 86400):
        return
    s = spread_slope_apr_per_day(con, pos)
    if s is None:
        st.kv_set(con, f"exec_decay_note_{pos['pos_id']}", "no series")
        return
    hist = jload(st.kv_get(con, f"exec_decay_hist_{pos['pos_id']}") or "",
                 d=[])
    hist.append({"ts": nowiso(), "slope": round(s, 3)})
    st.kv_set(con, f"exec_decay_hist_{pos['pos_id']}",
              json.dumps(hist[-8:]))
    if len(hist) >= 2 and hist[-1]["slope"] < DECAY_SLOPE \
            and hist[-2]["slope"] < DECAY_SLOPE:
        await start_unwind(con, pos, "funding_decay_3d")


# ------------------------------------------------------------------ tick
def write_book_samples(con, books):
    for (venue, coin), book in sorted(books.items()):
        if not book:
            continue
        # throttle: one exec_tick sample per venue-coin per minute
        if not guard(con, f"bs_{venue}_{coin}", 60):
            continue
        m = ds.book_metrics(*book)
        if not m:
            continue
        try:
            con.execute(
                "INSERT INTO book_samples(ts,window,venue,base,pass_n,mid,"
                "spread_bps,ok,slip_sell_500,slip_sell_1000,slip_sell_2500,"
                "slip_sell_5000,slip_sell_10000,slip_sell_25000,"
                "slip_buy_500,slip_buy_1000,slip_buy_2500,slip_buy_5000,"
                "slip_buy_10000,slip_buy_25000,depth25_bid,depth25_ask,"
                "source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?)",
                (nowiso(), "exec", venue, coin, 1, m["mid"],
                 m["spread_bps"], 1,
                 *[m.get(f"slip_bid_{n}") or None for n in ds.LADDER],
                 *[m.get(f"slip_ask_{n}") or None for n in ds.LADDER],
                 m.get("depth_bid_25"), m.get("depth_ask_25"),
                 "exec_tick"))
        except Exception:
            continue
    con.commit()


async def tick(verbose=True):
    t0 = nows()
    con = econ()
    pairs = engine_pairs()
    session = aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"})
    pl._session = session
    ds._session = session
    nv._session = session
    try:
        vc._session = None   # drop stale pointer to last tick's session
        await vc.init()

        # -------- 1. restore state, fetch books for everything involved
        act = active_positions(con)
        need = {(p["short"], p["coin"]) for p in pairs} | \
               {(p["long"], p["coin"]) for p in pairs}
        for pos in act.values():
            need |= {(pos["short_venue"], pos["coin"]),
                     (pos["long_venue"], pos["coin"])}
        books = await fetch_books(need)
        write_book_samples(con, books)

        # -------- 2. advance existing state machines
        for pos in list(act.values()):
            if pos["status"] == "unwinding":
                await progress_unwind(con, pos, books)
            elif pos["status"] == "seq_open":
                await advance_resting(con, pos, books)
                pos = dict(con.execute(
                    "SELECT * FROM exec_positions WHERE pos_id=?",
                    (pos["pos_id"],)).fetchone())
                if pos["status"] == "seq_open":
                    await enforce_legging_cap(con, pos, books)
                    pos = dict(con.execute(
                        "SELECT * FROM exec_positions WHERE pos_id=?",
                        (pos["pos_id"],)).fetchone())
                    if pos["status"] == "seq_open":
                        await check_entry_patience(con, pos, books)

        # -------- 3. triggers on open positions (each isolated:
        # one broken monitor must never stall the engine)
        act = active_positions(con)
        open_pos = [p for p in act.values() if p["status"] == "open"]
        if open_pos:
            live = await live_spreads({p["coin"] for p in open_pos})
            for pos in open_pos:
                for tcor, needs_live in ((trigger_compression, True),
                                         (trigger_floor, False),
                                         (trigger_listings, False),
                                         (trigger_decay, False)):
                    try:
                        if needs_live:
                            await tcor(con, pos, live)
                        else:
                            await tcor(con, pos)
                    except Exception as e:
                        print(f"  trigger {tcor.__name__} "
                              f"pos{pos['pos_id']}: ERR {str(e)[:100]}")

        # -------- 4. new entries (M5 kill-switch + chain-gated)
        pf.ensure_kill_fresh(con)
        halt, ksrc = pf.kill_halt()
        if halt:
            print(f"  kill-switch HALT: new entries blocked ({ksrc})")
        act = active_positions(con)
        for p in pairs:
            if halt:
                break
            if p["pair"] in act:
                continue
            if pair_on_cooldown(con, p["pair"]):
                continue
            frz = [v for v in (p["short"], p["long"])
                   if freeze_active(con, v)]
            if frz:
                print(f"  {p['pair']}: venue frozen {frz}, skip")
                continue
            if verbose:
                print(f"\n== ENTRY CHECK {p['pair']} "
                      f"${p['size_usd']:.0f}/leg seq={p['seq']}")
            twcon = con
            try:
                verdict, _ = await tw.run_chain(
                    twcon, p["coin"], p["short"], p["long"], p["size_usd"],
                    mode="pre_entry", verbose=verbose)
            except Exception as e:
                verdict = "ERR"
                print(f"  chain err {e}")
            await vc.init()   # chain closes its adopted view; re-adopt
            if verdict != "PASS":
                continue
            await start_entry(con, p, books)

        st.kv_set(con, "exec_heartbeat", nowiso())
        if verbose:
            n_open = con.execute(
                "SELECT COUNT(*) c FROM exec_positions WHERE "
                "status IN ('seq_open','open','unwinding')").fetchone()["c"]
            print(f"\n[tick done {nows()-t0:.1f}s active={n_open}]")
    finally:
        await session.close()
        con.close()
    return books


# ------------------------------------------------------------------ CLI
def cmd_status():
    con = econ()
    hb = st.kv_get(con, "exec_heartbeat")
    print(f"engine heartbeat: {hb}")
    print("\npositions:")
    for r in con.execute("SELECT * FROM exec_positions ORDER BY pos_id"):
        f = jload(r["flags"])
        print(f"  #{r['pos_id']} {r['pair']:22s} {r['status']:10s} "
              f"stage={f.get('stage', f.get('unwind_reason', '-')):18s} "
              f"entry={r['entry_cost_pct'] or 0:.3f}% "
              f"open={r['ts_open']} "
              + (f"close={r['ts_close']}" if r["ts_close"] else ""))
    print("\nresting orders:")
    for r in con.execute("SELECT * FROM exec_orders WHERE "
                         "status='resting'"):
        print(f"  #{r['oid']} pos{r['pos_id']} {r['leg']}/{r['venue']} "
              f"{r['side']} {r['target_qty']:.6g} @ {r['limit_px']:.6g} "
              f"since {r['ts_post']} reposts={r['reposts']}")
    print("\nrecent fills:")
    for r in con.execute("SELECT * FROM paper_fills ORDER BY fid DESC "
                         "LIMIT 12"):
        print(f"  {r['ts']} {r['pair']:20s} {r['leg']:5s}/{r['venue']:8s} "
              f"{r['side']:4s} {r['style']:5s} qty={r['target_qty']:.6g} "
              f"px={r['sim_fill_px']:.6g} slip={r['sim_slip_bps']} "
              f"fee={r['fee_bps']}")
    con.close()


def cmd_drift():
    con = econ()
    print(f"{'pair':22s} {'assumed%':>9s} {'realized%':>10s} "
          f"{'ratio':>6s} {'verdict':>8s}")
    for r in con.execute("SELECT * FROM exec_drift ORDER BY did"):
        print(f"{r['pair']:22s} {r['assumed_rt_pct']:9.3f} "
              f"{r['realized_rt_pct']:10.3f} "
              f"{r['ratio'] if r['ratio'] else 0:6.2f} {r['verdict']:>8s}")
    n = con.execute("SELECT COUNT(*) c, SUM(verdict='OVER') o FROM "
                    "exec_drift").fetchone()
    if n["c"]:
        print(f"\n{n['c']} closed cycles, {n['o'] or 0} over 1.3x gate "
              f"({100*(1-(n['o'] or 0)/n['c']):.0f}% within gate; "
              f"acceptance >=80%)")


async def cmd_unwind(pos_id, reason):
    con = econ()
    r = con.execute("SELECT * FROM exec_positions WHERE pos_id=? AND "
                    "status='open'", (pos_id,)).fetchone()
    if not r:
        print(f"no open engine position {pos_id}")
        return
    await start_unwind(con, dict(r), reason or "manual override")
    con.close()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["tick", "run", "status", "drift",
                                    "unwind"])
    ap.add_argument("--minutes", type=float, default=15)
    ap.add_argument("--every", type=float, default=20)
    ap.add_argument("--pos", type=int)
    ap.add_argument("--reason", default="manual override")
    ap.add_argument("--mode", choices=["paper", "live"], default="paper",
                    help="P4a: execution adapter mode (live = DRY_RUN)")
    ap.add_argument("--pin-desk", action="store_true",
                    help="P5b: add the desk-open pinned candidate to entry "
                         "checks (all engine gates unchanged)")
    a = ap.parse_args()
    set_adapter(exa.make_adapter(a.mode))
    set_pin_mode(a.pin_desk)
    if a.pin_desk:
        print("P5b DESK PIN mode: pinned candidate ADDED to entry checks; "
              "kill/cooldown/freeze/TW1-6/M5 gates all unchanged")
    if a.mode == "live":
        print("P4a LIVE adapter DRY_RUN: paper fills remain authoritative; "
              "no real orders are placed")
    if a.cmd == "tick":
        await tick()
        cmd_status()
    elif a.cmd == "run":
        t_end = nows() + a.minutes * 60
        i = 0
        while nows() < t_end:
            i += 1
            print(f"\n===== tick {i} {nowiso()} =====")
            try:
                await tick()
            except Exception as e:
                print(f"TICK ERR {e!r}")
            remain = t_end - nows()
            if remain > 0:
                await asyncio.sleep(min(a.every, remain))
        print("\n===== run window ended =====")
        cmd_status()
    elif a.cmd == "status":
        cmd_status()
    elif a.cmd == "drift":
        cmd_drift()
    elif a.cmd == "unwind":
        await cmd_unwind(a.pos, a.reason)


if __name__ == "__main__":
    asyncio.run(main())
