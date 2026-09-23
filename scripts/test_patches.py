#!/usr/bin/env python3
"""Verification suite for the 4 live-blocking bug patches (Option 3).

Pure-logic tests; external exchange SDKs are stubbed so this runs anywhere.
Run: python3 /home/z/my-project/scripts/test_patches.py
"""
import asyncio
import sys
import types
from time import time

CODE = "/home/z/my-project/upload/code_extracted"
sys.path.insert(0, CODE)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}: {name}" + (f"  [{detail}]" if detail else ""))


# ---------------------------------------------------------------- SDK stubbing
def _stub(name):
    if name in sys.modules:
        return sys.modules[name]
    m = _DummyModule(name)
    sys.modules[name] = m
    return m


class _Dummy:  # never called in tested paths
    def __init__(self, *a, **k): pass
    def __getattr__(self, item): return _Dummy()


class _DummyModule(types.ModuleType):
    def __getattr__(self, item):
        if item.startswith("__"):
            raise AttributeError(item)
        return _Dummy


for mod in ("ccxt", "ccxt.pro", "hyperliquid", "hyperliquid.exchange",
            "hyperliquid.info", "hyperliquid.utils", "hyperliquid.utils.types",
            "eth_account", "eth_account.signers", "eth_account.signers.local"):
    _stub(mod)
sys.modules["ccxt"].pro = sys.modules["ccxt.pro"]
sys.modules["hyperliquid"].exchange = sys.modules["hyperliquid.exchange"]
sys.modules["hyperliquid"].info = sys.modules["hyperliquid.info"]
sys.modules["eth_account"].Account = _Dummy()
sys.modules["eth_account"].signers = sys.modules["eth_account.signers"]
sys.modules["hyperliquid"].utils = sys.modules["hyperliquid.utils"]

# ---------------------------------------------------------------- 1) venues.py
print("\n[1] Bug #2 - venue canonicalization")
from core.venues import canonical_venue, sharpe_display_name

check("Gate.io -> gate", canonical_venue("Gate.io") == "gate")
check("Binance -> binance", canonical_venue("Binance") == "binance")
check("OKX -> okx", canonical_venue("OKX") == "okx")
check("Hyperliquid -> hyperliquid", canonical_venue("Hyperliquid") == "hyperliquid")
check("display: gate -> Gate.io (not 'Gate.Io')", sharpe_display_name("gate") == "Gate.io")
check("display: okx -> OKX", sharpe_display_name("okx") == "OKX")

# ------------------------------------------------------- 2) APR parse + mappers
print("\n[2] Bug #4 - strict APR parsing (regression: the 1122.67% harvest)")
from data.opportunity_feed import SharpeOpportunityIngestor

ing = SharpeOpportunityIngestor(api_key="")

# THE regression: netAprPct is already percent and >= 10 - old code gave 1122.67%
opp = ing._map_futures_carry({"coin": "BTC", "spotVenue": "Binance",
                              "expiry": "2026-06-26", "netAprPct": 11.2267})
check("netAprPct=11.2267 -> 11.2267% (was 1122.67%)",
      opp is not None and abs(opp.net_apr_pct - 11.2267) < 1e-9,
      f"got {opp.net_apr_pct if opp else None}")

# percent field < 10 (old code x100'd it)
opp = ing._map_calendar_spread({"symbol": "ETH", "exchange": "Bybit",
                                "frontExpiry": "2026-09-26", "backExpiry": "2026-12-26",
                                "netRollApyPct": -3.2})
check("netRollApyPct=-3.2 -> -3.2% (was -320%)",
      opp is not None and abs(opp.net_apr_pct + 3.2) < 1e-9,
      f"got {opp.net_apr_pct if opp else None}")

# decimal field scales by 100
opp = ing._map_cross_exchange({"symbol": "DOGE", "longExchange": "Gate.io",
                               "shortExchange": "Binance", "netApr": 0.657})
check("netApr=0.657 -> 65.7%", opp is not None and abs(opp.net_apr_pct - 65.7) < 1e-9,
      f"got {opp.net_apr_pct if opp else None}")
check("and 'Gate.io' leg canonicalized to 'gate'", opp is not None and opp.legs[0].exchange == "gate")
check("negative decimal -0.005 -> -0.5%",
      abs(ing._parse_apr_pct({"netApr": -0.005}, ("netApr",)) + 0.5) < 1e-9)

# guardrail: implausible magnitude rejects the row
check("netApr=11.2267 (decimal -> 1122.67%) rejected",
      ing._map_cross_exchange({"symbol": "X", "longExchange": "A", "shortExchange": "B", "netApr": 11.2267}) is None)
check("netAprPct=1500 rejected",
      ing._map_futures_carry({"coin": "X", "spotVenue": "Binance", "expiry": "e", "netAprPct": 1500}) is None)
check("missing fields -> 0.0", ing._parse_apr_pct({}, ("netApr", "apr")) == 0.0)

# ------------------------------------------------- 3) engine protected pricing
print("\n[3] Bug #1 - protected pricing (no more price=0.0)")
from core.models import ArbitrageOpportunity, ExecutionReport, HedgeLeg, OpportunityType, OrderLegResult


class FakeClient:
    def __init__(self, tob):
        self.tob, self.calls = tob, []

    async def get_top_of_book(self, symbol, asset_type="perp"):
        self.calls.append(symbol)
        return self.tob


from main import YieldHarvesterEngine, HeldPosition

eng = object.__new__(YieldHarvesterEngine)  # skip __init__ (no adapters/network)


class _S:
    MAX_SLIPPAGE_PCT = 0.0025
    ALLOCATION_PER_TRADE_USD = 100.0
    FORCE_UNWIND_IF_STALE_SECONDS = 900.0


eng.settings = _S()

leg = HedgeLeg(exchange="binance", symbol="ETH/USDT:USDT", side="buy", asset_type="perp")
px = asyncio.run(eng._protected_price(FakeClient({"bid": 99.0, "ask": 101.0}), leg, "buy"))
check("tob ask=101 buy -> collared 101*1.0025", abs(px - 101 * 1.0025) < 1e-9, f"got {px}")
px = asyncio.run(eng._protected_price(FakeClient({"bid": 99.0, "ask": 101.0}), leg, "sell"))
check("tob bid=99 sell -> collared 99*0.9975", abs(px - 99 * 0.9975) < 1e-9, f"got {px}")
px = asyncio.run(eng._protected_price(None, leg, "sell", ref_px=100.0))
check("no book -> entry-fill collar 100*0.9975 (never 0.0)", abs(px - 100 * 0.9975) < 1e-9, f"got {px}")
px = asyncio.run(eng._protected_price(FakeClient(None), leg, "buy", ref_px=None))
check("no book + no ref -> None (order skipped, not sent at 0)", px is None)

# ------------------------------------------- 4) unwind sizes/prices from entry
print("\n[4] Bug #1/#3 - unwind uses ACTUAL fills + protected prices")


class RecordingGate:
    def __init__(self):
        self.last_sizes, self.last_prices = None, None

    async def execute_unwind(self, opp, sizes, prices):
        self.last_sizes, self.last_prices = sizes, prices

        class _R:
            fully_filled = True
            error = None

        return _R()


entry_leg_res = OrderLegResult(exchange="binance", symbol="ETH/USDT:USDT",
                               requested_size=100.0, requested_px=2000.0,
                               filled_size=55.0, avg_px=2000.0, status="filled")
report = ExecutionReport(opportunity=None, accepted=True, dry_run=False, legs=[entry_leg_res])
held = HeldPosition(opp=ArbitrageOpportunity(
    id="x", type=OpportunityType.CROSS_EXCHANGE, coin="ETH",
    legs=[HedgeLeg(exchange="binance", symbol="ETH/USDT:USDT", side="buy", asset_type="perp")],
    net_apr_pct=10.0, raw_data={}), entry_report=report, captured_at_ts=time(), refreshed_at_ts=time())

eng.clients = {"binance": FakeClient({"bid": 1980.0, "ask": 1982.0})}
eng.gate = RecordingGate()
asyncio.run(eng._unwind_position(held))
check("unwind size = actual fill 55 (was naive 100)",
      eng.gate.last_sizes == {"binance_ETH/USDT:USDT": 55.0}, str(eng.gate.last_sizes))
check("unwind price = live-bid collar 1980*0.9975 (was 0.0)",
      abs(eng.gate.last_prices["binance_ETH/USDT:USDT"] - 1980 * 0.9975) < 1e-6,
      str(eng.gate.last_prices))
check("no zero prices anywhere", all(v > 0 for v in eng.gate.last_prices.values()))

# ---------------------------------------------- 5) adapter live guards (stubbed)
print("\n[5] Bug #1 - adapter-level invalid-price / zero-fill guards")
from execution.ccxt_adapter import CcxtExecutionAdapter

ad = object.__new__(CcxtExecutionAdapter)
ad.exchange_id = "binance"
ad._dry_run = False
ad.settings = _S()


class _ExchNoCall:
    def __getattr__(self, item):
        raise AssertionError(f"exchange.{item} must not be called")


ad.exchange = _ExchNoCall()
res = asyncio.run(ad.execute_leg(leg, 1.0, 0.0))
check("live execute_leg(price=0) refused", res.status == "error" and res.error == "invalid_price")


class _ExchZeroFill:
    async def create_order(self, *a, **k):
        return {"filled": 0.0, "average": 0.0}


ad.exchange = _ExchZeroFill()
res = asyncio.run(ad.execute_leg(leg, 1.0, 2000.0))
check("zero-fill NOT booked as filled", res.status == "error" and res.error == "zero_fill")

# ------------------------------------------------------------- summary
print(f"\n{'=' * 60}\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL PATCH VERIFICATIONS PASSED")
