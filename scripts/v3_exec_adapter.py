#!/usr/bin/env python3
"""v3_exec_adapter.py - P4a execution adapter seam (Part 19 section 3).

One protocol, two implementations, selected by --mode paper|live:

  ExecutionAdapter        the contract the engine calls at exactly the
                          three primitives (taker fill / maker post /
                          maker-fill confirmation).
  PaperAdapter            current sim behavior: walk-the-book for takers,
                          touched-price maker rule stays engine-side.
                          This is the parity baseline.
  LiveAdapter             signed-REST implementation for P4b.  In P4a it
                          ships DRY_RUN-only: it performs the SAME walk
                          (via an internal PaperAdapter), applies
                          exchange-rule checks (qty step / min qty /
                          min notional / price sanity), records the
                          exchange-reported fee table (vc.FEES) next to
                          the paper fee, appends an INTENT row to a JSONL
                          log, and returns the paper result UNCHANGED so
                          engine behavior is bit-identical.  Real order
                          placement refuses with RuntimeError until P4b.

Engine behavior in P4a is unchanged by construction: paper fills remain
authoritative; the intent log is the shadow used by the parity harness
(scripts/parity_report.py).

Intent log: JSONL, one row per adapter call:
  {ts, mode, dry_run, op, venue, coin, side, usd|qty, px, slip_bps,
   fee_paper_bps, fee_live_bps, checks:[...], note}
Path: download/data/exec_intents.jsonl (override: env EXA_INTENT_LOG).
"""
import json
import os
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import depth_sampler as ds            # noqa: E402
import v3_connectors as vc            # noqa: E402
import paper_ledger as pl             # noqa: E402  (TAKER/MAKER fee tables)

DEFAULT_INTENT_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "download", "data", "exec_intents.jsonl")


def _nowiso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def book_mid(book):
    """Same math as v3_exec.mid_of (kept local to avoid circular import)."""
    if not book:
        return None
    b, a = book
    if not b or not a:
        return None
    return (b[0][0] + a[0][0]) / 2.0


# ------------------------------------------------------------- venue rules
# P4a: conservative static subset for the 5 basket venues; P4b replaces
# this with live exchangeInfo/contracts fetch + cache.  Unknown venues or
# coins degrade to a "rules:unknown" note, never a hard failure.
VENUE_RULES = {
    "binance": {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 5.0},
    "aster":   {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 5.0},
    "okx":     {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 5.0},
    "bingx":   {"qty_step": 0.1,   "min_qty": 0.1,   "min_notional": 5.0},
    "nado":    {"qty_step": 0.01,  "min_qty": 0.01,  "min_notional": 1.0},
}
PX_SANITY_FRAC = 0.02        # fill VWAP must sit within +/-2% of mid


def _rules_for(venue, coin):
    r = dict(VENUE_RULES.get(venue) or {})
    if not r:
        return None, ["rules:unknown-venue"]
    # per-coin overrides (none shipped in P4a -> hook point for P4b cache)
    return r, []


def _fee_bps(table_frac, venue):
    """paper_ledger fee tables are fractions; convert to bps."""
    return table_frac.get(venue, 0.0005) * 1e4


class ExecutionAdapter(ABC):
    """Contract the engine calls at the three primitives (Part 19 s.2/3)."""

    mode = "abstract"
    dry_run = True

    @abstractmethod
    def place_taker(self, venue, coin, side, usd, book, note=""):
        """Market/IOC order sized to `usd` notional.

        Returns {ok, reason, qty, px, filled, slip_bps, fee_bps}.
        Paper parity baseline: qty = filled/vwap from walk-the-book.
        """

    @abstractmethod
    def place_maker(self, venue, coin, side, qty, px, notional, note=""):
        """Post limit order.  Returns {ok, oid_external, checks}."""

    @abstractmethod
    def on_maker_fill(self, venue, coin, side, qty, px, oid_external,
                      note=""):
        """Confirm a maker fill observed by the engine polling layer."""

    # P4b surface (protocol-complete per Part 19 s.3; stubs in P4a)
    def order_status(self, venue, oid_external):
        raise NotImplementedError("P4b: signed order-status query")

    def cancel(self, venue, oid_external):
        raise NotImplementedError("P4b: signed cancel")

    def position_reconcile(self, venue):
        raise NotImplementedError("P4b: signed position fetch")

    def balance_reconcile(self, venue):
        raise NotImplementedError("P4b: signed balance fetch")


class IntentLog:
    """Append-only JSONL intent log (never clobbers across instances)."""

    def __init__(self, path=None):
        self.path = path or os.environ.get("EXA_INTENT_LOG",
                                           DEFAULT_INTENT_LOG)

    def write(self, row):
        row = {"ts": _nowiso(), **row}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        return row


class PaperAdapter(ExecutionAdapter):
    """Exact current sim behavior.  Walk-the-book taker; fee pl.TAKER.

    The maker touched-price rule and fill polling stay engine-side in P4a
    (they are book-model decisions, not exchange decisions); this class
    confirms maker ops as accepted.
    """

    mode = "paper"
    dry_run = True

    def __init__(self, intent_log=None):
        self.ilog = intent_log  # None -> no logging (bit-identical legacy)

    def place_taker(self, venue, coin, side, usd, book, note=""):
        bids, asks = (book or (None, None))
        levels = bids if side == "sell" else asks
        mid = book_mid(book)
        if mid is None or not levels:
            return {"ok": False, "reason": "no book, deferring"}
        r = ds.walk(levels, mid, usd)
        if r is None:
            return {"ok": False, "reason": "empty book side"}
        slip, filled, vwap = r
        return {"ok": True, "qty": filled / vwap, "px": vwap,
                "filled": filled, "slip_bps": slip,
                "fee_bps": _fee_bps(pl.TAKER, venue)}

    def place_maker(self, venue, coin, side, qty, px, notional, note=""):
        if self.ilog:
            self.ilog.write({"mode": self.mode, "dry_run": True,
                             "op": "maker_post", "venue": venue,
                             "coin": coin, "side": side, "qty": qty,
                             "px": px, "checks": [], "note": note})
        return {"ok": True, "oid_external": None, "checks": []}

    def on_maker_fill(self, venue, coin, side, qty, px, oid_external,
                      note=""):
        if self.ilog:
            self.ilog.write({"mode": self.mode, "dry_run": True,
                             "op": "maker_fill", "venue": venue,
                             "coin": coin, "side": side, "qty": qty,
                             "px": px, "checks": [], "note": note})
        return {"ok": True, "checks": []}


class LiveAdapter(ExecutionAdapter):
    """Signed-REST adapter.  P4a ships DRY_RUN-only (parity shadow).

    DRY_RUN semantics: same walk result as PaperAdapter (engine behavior
    identical), plus rule checks + fee-disagreement + intent row.  When
    `dry_run` is False the adapter refuses (P4b ships real placement).
    """

    mode = "live"

    def __init__(self, dry_run=True, intent_log=None, settings=None):
        if not dry_run:
            raise RuntimeError(
                "LiveAdapter real placement is P4b: not shipped. "
                "Run --mode live (DRY_RUN) or --mode paper.")
        self.dry_run = True
        self._paper = PaperAdapter()          # parity baseline walk
        self.ilog = intent_log or IntentLog()
        if settings is None:                  # lazy: keeps module standalone
            from v3_settings import get_settings
            settings = get_settings()
        self.settings = settings

    def credentials_ready(self, venue: str) -> bool:
        """P4b readiness surface: True iff venue credentials complete."""
        return self.settings.credentials_status().get(venue, False)

    # -- internals -------------------------------------------------------
    def _check_rules(self, venue, coin, side, qty, px, book, slip_bps,
                     fee_paper_bps, note, fee_side="taker"):
        checks = list(_rules_for(venue, coin)[1])
        rules = VENUE_RULES.get(venue)
        if rules:
            step, mnq, mnn = (rules["qty_step"], rules["min_qty"],
                              rules["min_notional"])
            units = qty / step
            off = abs(units - round(units)) * step
            if off > 1e-9:
                checks.append(f"qty_step:{side}:would_round_to_"
                              f"{round(units) * step:.6g}")
            if qty < mnq:
                checks.append(f"min_qty:REJECT({qty:.6g}<{mnq})")
            if qty * px < mnn:
                checks.append(f"min_notional:REJECT({qty * px:.2f}<{mnn})")
        mid = book_mid(book)
        if mid and px and abs(px / mid - 1.0) > PX_SANITY_FRAC:
            checks.append(f"px_sanity:vwap_off_mid_"
                          f"{(px / mid - 1.0) * 100:.2f}pct")
        pair_ = vc.FEES.get(venue)
        if pair_:
            fee_live_bps = pair_[0] if fee_side == "taker" else pair_[1]
        else:
            fee_live_bps = fee_paper_bps
        if abs(fee_live_bps - fee_paper_bps) > 0.05:
            checks.append(f"fee_disagree:paper{fee_paper_bps:.2f}_vs_"
                          f"live{fee_live_bps:.2f}bps")
        return checks, fee_live_bps

    def _intent(self, op, venue, coin, side, checks, note, **extra):
        self.ilog.write({"mode": self.mode, "dry_run": self.dry_run,
                         "op": op, "venue": venue, "coin": coin,
                         "side": side, "checks": checks, "note": note,
                         **extra})

    # -- ExecutionAdapter -------------------------------------------------
    def place_taker(self, venue, coin, side, usd, book, note=""):
        r = self._paper.place_taker(venue, coin, side, usd, book, note)
        if not r.get("ok"):
            self._intent("taker_nofill", venue, coin, side, [],
                         note, usd=usd, reason=r.get("reason"))
            return r
        checks, fee_live_bps = self._check_rules(
            venue, coin, side, r["qty"], r["px"], book, r["slip_bps"],
            r["fee_bps"], note)
        self._intent("taker", venue, coin, side, checks, note, usd=usd,
                     qty=r["qty"], px=r["px"], slip_bps=r["slip_bps"],
                     fee_paper_bps=r["fee_bps"], fee_live_bps=fee_live_bps)
        return r                    # parity: paper result passes through

    def place_maker(self, venue, coin, side, qty, px, notional, note=""):
        checks, fee_live_bps = self._check_rules(
            venue, coin, side, qty, px, None, 0.0,
            _fee_bps(pl.MAKER, venue), note, fee_side="maker")
        self._intent("maker_post", venue, coin, side, checks, note,
                     qty=qty, px=px, notional=notional,
                     fee_paper_bps=_fee_bps(pl.MAKER, venue),
                     fee_live_bps=fee_live_bps)
        return {"ok": True, "oid_external": None, "checks": checks}

    def on_maker_fill(self, venue, coin, side, qty, px, oid_external,
                      note=""):
        checks, _ = self._check_rules(venue, coin, side, qty, px, None,
                                      0.0, _fee_bps(pl.MAKER, venue),
                                      note, fee_side="maker")
        self._intent("maker_fill", venue, coin, side, checks, note,
                     qty=qty, px=px, oid_external=oid_external)
        return {"ok": True, "checks": checks}


# ------------------------------------------------------------ selection
_CURRENT = None


def make_adapter(mode, intent_log=None):
    if mode == "paper":
        return PaperAdapter(intent_log=intent_log)
    if mode == "live":
        return LiveAdapter(dry_run=True, intent_log=intent_log)
    raise ValueError(f"unknown adapter mode: {mode!r} "
                     f"(expected 'paper' or 'live')")


def set_adapter(a):
    global _CURRENT
    _CURRENT = a
    return a


def get_adapter():
    if _CURRENT is None:
        set_adapter(make_adapter("paper"))
    return _CURRENT


def _selftest():
    """Smoke: parity invariant on a synthetic book + intent log round-trip."""
    import tempfile
    book = ([(99.5, 2), (99.4, 3)], [(100.5, 2), (100.6, 3)])
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "i.jsonl")
        pa = make_adapter("paper", intent_log=IntentLog(p))
        la = make_adapter("live", intent_log=IntentLog(p))
        rp = pa.place_taker("okx", "INJ", "buy", 500.0, book, "t")
        rl = la.place_taker("okx", "INJ", "buy", 500.0, book, "t")
        assert rp == rl, (rp, rl)
        assert rl["ok"] and rl["px"] > 100.0 and rl["qty"] > 0
        n = sum(1 for _ in open(p, encoding="utf-8"))
        assert n == 1, n                       # paper silent, live 1 row
        assert isinstance(la.credentials_ready("binance"), bool)
    print("v3_exec_adapter selftest OK")


if __name__ == "__main__":
    _selftest()
