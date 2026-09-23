"""
Tests for the new execution layer: ExecutionRouter (Phase 3).
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
from execution.router import ExecutionRouter
from risk.portfolio_manager import PortfolioManager


def _make_spot_perp_opp(coin="BTC", exchange="binance") -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        id=f"spot_perp_{exchange}_{coin}",
        type=OpportunityType.SPOT_PERP,
        coin=coin,
        legs=[
            HedgeLeg(exchange=exchange, symbol=f"{coin}/USDT", side="buy", asset_type="spot"),
            HedgeLeg(exchange=exchange, symbol=f"{coin}/USDT:USDT", side="sell", asset_type="perp"),
        ],
        net_apr_pct=35.0,
        raw_data={"fundingRate": 0.00032},
    )


def _filled_leg(exchange: str, symbol: str) -> OrderLegResult:
    return OrderLegResult(
        exchange=exchange, symbol=symbol, requested_size=1.0, requested_px=100.0,
        filled_size=1.0, avg_px=100.0, status="filled"
    )


def _error_leg(exchange: str, symbol: str) -> OrderLegResult:
    return OrderLegResult(
        exchange=exchange, symbol=symbol, requested_size=1.0, requested_px=100.0,
        filled_size=0.0, avg_px=0.0, status="error", error="Margin too low"
    )


@pytest.mark.asyncio
async def test_router_executes_both_legs_concurrently():
    """Router should call execute_leg on the correct client for each leg and return a filled report."""
    opp = _make_spot_perp_opp()
    
    mock_client = MagicMock()
    mock_client.dry_run = True
    mock_client.execute_leg = AsyncMock(side_effect=[
        _filled_leg("binance", "BTC/USDT"),
        _filled_leg("binance", "BTC/USDT:USDT"),
    ])
    
    router = ExecutionRouter(clients={"binance": mock_client})
    sizes = {"binance_BTC/USDT": 1.0, "binance_BTC/USDT:USDT": 1.0}
    prices = {"binance_BTC/USDT": 100.0, "binance_BTC/USDT:USDT": 100.0}
    
    report = await router.execute_opportunity(opp, sizes, prices)
    
    assert report.accepted is True
    assert report.fully_filled is True
    assert len(report.legs) == 2
    assert mock_client.execute_leg.call_count == 2


@pytest.mark.asyncio
async def test_router_triggers_recovery_on_partial_fill():
    """If Leg A fills but Leg B fails, router should trigger legging recovery (close Leg A)."""
    opp = _make_spot_perp_opp()
    
    mock_client = MagicMock()
    mock_client.dry_run = False
    mock_client.execute_leg = AsyncMock(side_effect=[
        _filled_leg("binance", "BTC/USDT"),   # Leg A fills
        _error_leg("binance", "BTC/USDT:USDT"),  # Leg B fails
    ])
    mock_client.execute_unwind_leg = AsyncMock(return_value=_filled_leg("binance", "BTC/USDT"))
    
    router = ExecutionRouter(clients={"binance": mock_client})
    sizes = {"binance_BTC/USDT": 1.0, "binance_BTC/USDT:USDT": 1.0}
    prices = {"binance_BTC/USDT": 100.0, "binance_BTC/USDT:USDT": 100.0}
    
    report = await router.execute_opportunity(opp, sizes, prices)
    
    # Report should acknowledge the failure
    assert report.fully_filled is False
    assert "Legging mismatch" in (report.error or "")
    # Recovery should have been triggered (execute_unwind_leg called for Leg A)
    mock_client.execute_unwind_leg.assert_called_once()


@pytest.mark.asyncio
async def test_router_returns_error_on_missing_client():
    """Router should immediately reject an opportunity if no client is configured for the exchange."""
    opp = _make_spot_perp_opp(exchange="okx")  # No OKX client registered
    
    router = ExecutionRouter(clients={"binance": MagicMock()})
    report = await router.execute_opportunity(opp, {}, {})
    
    assert report.accepted is False
    assert "Missing client" in (report.error or "")


@pytest.mark.asyncio
async def test_portfolio_manager_aggregates_balances():
    """PortfolioManager should sum USDT/USDC across all exchange clients."""
    mock_hl = MagicMock()
    mock_hl.fetch_balances = AsyncMock(return_value={"USDT": {"free": 500.0, "total": 500.0}})
    mock_hl.get_active_positions = AsyncMock(return_value={})
    
    mock_binance = MagicMock()
    mock_binance.fetch_balances = AsyncMock(return_value={"USDT": {"free": 300.0, "total": 300.0}})
    mock_binance.get_active_positions = AsyncMock(return_value={"BTC/USDT:USDT": -0.5})
    
    pm = PortfolioManager(
        execution_clients={"hyperliquid": mock_hl, "binance": mock_binance},
        global_liquidity_buffer_pct=0.20
    )
    state = await pm.get_state()
    
    assert state.global_free_usd == pytest.approx(800.0)
    assert state.global_total_usd == pytest.approx(800.0)
    assert "hyperliquid" in state.balances_by_exchange
    assert "binance" in state.balances_by_exchange


@pytest.mark.asyncio
async def test_portfolio_manager_can_allocate():
    """PortfolioManager.can_allocate should enforce the 20% liquidity buffer."""
    mock_client = MagicMock()
    mock_client.fetch_balances = AsyncMock(return_value={"USDT": {"free": 1000.0, "total": 1000.0}})
    mock_client.get_active_positions = AsyncMock(return_value={})
    
    pm = PortfolioManager({"exchange": mock_client}, global_liquidity_buffer_pct=0.20)
    state = await pm.get_state()
    
    # Buffer = 20% of 1000 = 200. Available = 800.
    assert pm.can_allocate(state, 700.0) is True   # 700 < 800 → allowed
    assert pm.can_allocate(state, 900.0) is False  # 900 > 800 → blocked


@pytest.mark.asyncio
async def test_portfolio_manager_handles_exchange_error_gracefully():
    """PortfolioManager should not crash if one exchange's balance fetch fails."""
    mock_ok = MagicMock()
    mock_ok.fetch_balances = AsyncMock(return_value={"USDT": {"free": 500.0, "total": 500.0}})
    mock_ok.get_active_positions = AsyncMock(return_value={})
    
    mock_bad = MagicMock()
    mock_bad.fetch_balances = AsyncMock(side_effect=ConnectionError("timeout"))
    mock_bad.get_active_positions = AsyncMock(return_value={})
    
    pm = PortfolioManager({"ok": mock_ok, "bad": mock_bad}, global_liquidity_buffer_pct=0.20)
    state = await pm.get_state()  # Should not raise
    
    assert state.global_free_usd == pytest.approx(500.0)
