"""
Tests for the TripleFactorGate (Phase 3 exit logic).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest

from config.settings import EngineSettings
from core.models import (
    ArbitrageOpportunity,
    ExecutionReport,
    HedgeLeg,
    OpportunityType,
    OrderLegResult,
    PortfolioState,
)
from risk.triple_factor_gate import TripleFactorGate
from execution.router import ExecutionRouter


def _make_opp(net_apr_pct=35.0, funding_rate=0.00032, coin="BTC", exchange="binance") -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        id=f"spot_perp_{exchange}_{coin}",
        type=OpportunityType.SPOT_PERP,
        coin=coin,
        legs=[
            HedgeLeg(exchange=exchange, symbol=f"{coin}/USDT", side="buy", asset_type="spot"),
            HedgeLeg(exchange=exchange, symbol=f"{coin}/USDT:USDT", side="sell", asset_type="perp"),
        ],
        net_apr_pct=net_apr_pct,
        raw_data={"fundingRate": funding_rate},
    )


def _state(margin_util=0.50, total=10000.0, free=5000.0) -> PortfolioState:
    return PortfolioState(
        global_total_usd=total,
        global_free_usd=free,
        balances_by_exchange={},
        positions_by_exchange={},
        margin_utilization_pct=margin_util,
    )


def _filled_leg(exchange, symbol) -> OrderLegResult:
    return OrderLegResult(
        exchange=exchange, symbol=symbol, requested_size=1.0, requested_px=100.0,
        filled_size=1.0, avg_px=100.0, status="filled"
    )


@pytest.mark.asyncio
async def test_gate_allows_healthy_opportunity():
    """Gate should NOT trigger unwind when all factors are healthy."""
    opp = _make_opp(net_apr_pct=35.0, funding_rate=0.00032)
    state = _state(margin_util=0.50)

    settings = EngineSettings(MAX_MARGIN_UTILIZATION_PCT=0.85, MIN_EXIT_SPREAD_CONVERGENCE_PCT=0.0005)
    router = MagicMock()
    gate = TripleFactorGate(router, settings)

    should_unwind = await gate.evaluate_opportunity(opp, state, {})
    assert should_unwind is False


@pytest.mark.asyncio
async def test_gate_triggers_on_margin_breach():
    """Gate should trigger unwind when margin utilization exceeds the threshold."""
    opp = _make_opp(net_apr_pct=35.0)
    state = _state(margin_util=0.90)  # Above 85% limit

    settings = EngineSettings(MAX_MARGIN_UTILIZATION_PCT=0.85)
    gate = TripleFactorGate(MagicMock(), settings)

    should_unwind = await gate.evaluate_opportunity(opp, state, {})
    assert should_unwind is True


@pytest.mark.asyncio
async def test_gate_triggers_on_basis_convergence():
    """Gate should trigger unwind when net APR drops to/below the exit convergence threshold."""
    # Threshold is 0.0005 * 100 = 0.05%. APR = 0.02% — below threshold.
    opp = _make_opp(net_apr_pct=0.02)
    state = _state(margin_util=0.40)

    settings = EngineSettings(MIN_EXIT_SPREAD_CONVERGENCE_PCT=0.0005, MAX_MARGIN_UTILIZATION_PCT=0.85)
    gate = TripleFactorGate(MagicMock(), settings)

    should_unwind = await gate.evaluate_opportunity(opp, state, {})
    assert should_unwind is True


@pytest.mark.asyncio
async def test_gate_triggers_on_funding_squeeze():
    """Gate should trigger unwind when funding rate flips sharply negative."""
    opp = _make_opp(net_apr_pct=35.0, funding_rate=-0.01)  # -1% funding squeeze
    state = _state(margin_util=0.40)

    settings = EngineSettings(MIN_EXIT_SPREAD_CONVERGENCE_PCT=0.0005, MAX_MARGIN_UTILIZATION_PCT=0.85)
    gate = TripleFactorGate(MagicMock(), settings)

    should_unwind = await gate.evaluate_opportunity(opp, state, {})
    assert should_unwind is True


@pytest.mark.asyncio
async def test_gate_execute_unwind_inverts_leg_sides():
    """execute_unwind should reverse the sides of all legs and call the router."""
    opp = _make_opp()  # spot=buy, perp=sell

    mock_router = MagicMock()
    mock_report = MagicMock(spec=ExecutionReport)
    mock_report.fully_filled = True
    mock_router.execute_opportunity = AsyncMock(return_value=mock_report)

    settings = EngineSettings()
    gate = TripleFactorGate(mock_router, settings)

    sizes = {"binance_BTC/USDT": 1.0, "binance_BTC/USDT:USDT": 1.0}
    prices = {"binance_BTC/USDT": 100.0, "binance_BTC/USDT:USDT": 100.0}

    report = await gate.execute_unwind(opp, sizes, prices)

    # Verify the router was called with an inverted-side opportunity
    assert mock_router.execute_opportunity.call_count == 1
    called_opp: ArbitrageOpportunity = mock_router.execute_opportunity.call_args[0][0]
    assert called_opp.id.endswith("_unwind")
    # spot was buy → should now be sell
    assert called_opp.legs[0].side == "sell"
    # perp was sell → should now be buy
    assert called_opp.legs[1].side == "buy"
    assert report.fully_filled is True
