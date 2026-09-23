import asyncio
import logging
import os
from unittest.mock import AsyncMock

from config.settings import settings
from core.models import ArbitrageOpportunity, HedgeLeg, OpportunityType, PortfolioState
from data.opportunity_feed import SharpeOpportunityIngestor
from main import YieldHarvesterEngine


async def run_simulation():
    print("Starting YieldHarvesterEngine Simulation...")
    
    # 1. Force DRY_RUN
    settings.DRY_RUN = True
    settings.POSITION_CHECK_INTERVAL_SECONDS = 2
    
    # Clean previous logs to verify new ones
    if os.path.exists("logs/harvester.log"):
        os.remove("logs/harvester.log")
        
    engine = YieldHarvesterEngine()
    
    # 2. Mock Portfolio Manager to return a healthy portfolio
    engine.portfolio.get_state = AsyncMock(return_value=PortfolioState(
        global_total_usd=10000.0,
        global_free_usd=8000.0,
        balances_by_exchange={"hyperliquid": {"USDC": {"free": 8000.0, "total": 10000.0}}},
        positions_by_exchange={"hyperliquid": {}},
        margin_utilization_pct=0.20
    ))
    
    # 3. Mock Sharpe API to return one highly profitable opportunity
    mock_opp = ArbitrageOpportunity(
        id="spot_perp_hyperliquid_BTC",
        type=OpportunityType.SPOT_PERP,
        coin="BTC",
        legs=[
            HedgeLeg(exchange="hyperliquid", symbol="BTC/USDC", side="buy", asset_type="spot"),
            HedgeLeg(exchange="hyperliquid", symbol="BTC/USDC:USDC", side="sell", asset_type="perp"),
        ],
        net_apr_pct=45.0,
        raw_data={"fundingRate": 0.0005, "apr": 0.45}
    )
    
    engine.ingestor.fetch_spot_perp = AsyncMock(return_value=[mock_opp])
    
    # 4. Mock execution client execution to simply return "filled" without doing network calls 
    # (Though in pure DRY_RUN, the router or the client logs and fakes it anyway, 
    # but we mock it here to ensure we don't hit real APIs by accident during tests)
    for client in engine.clients.values():
        client.execute_leg = AsyncMock(side_effect=lambda leg, sz, px: __import__('core.models', fromlist=['OrderLegResult']).OrderLegResult(
            exchange=leg.exchange, symbol=leg.symbol, requested_size=sz, requested_px=px,
            filled_size=sz, avg_px=px, status="filled"
        ))
        client.execute_unwind_leg = AsyncMock(side_effect=lambda leg, sz, px: __import__('core.models', fromlist=['OrderLegResult']).OrderLegResult(
            exchange=leg.exchange, symbol=leg.symbol, requested_size=sz, requested_px=px,
            filled_size=sz, avg_px=px, status="filled"
        ))

    # 5. Run the engine for a short duration
    engine.is_running = True
    harvester_task = asyncio.create_task(engine.harvester_loop())
    monitor_task = asyncio.create_task(engine.monitor_loop())
    
    # Wait for it to process the mock opportunity
    await asyncio.sleep(2)
    
    # Now alter the portfolio state to trigger the Triple Factor Gate (e.g. margin squeeze)
    engine.portfolio.get_state.return_value = PortfolioState(
        global_total_usd=10000.0,
        global_free_usd=500.0, # Plunged
        balances_by_exchange={"hyperliquid": {"USDC": {"free": 500.0, "total": 10000.0}}},
        positions_by_exchange={"hyperliquid": {"BTC/USDC:USDC": -0.5}},
        margin_utilization_pct=0.95 # Triggers gate Unwind!
    )
    
    print("\nSimulating Margin Squeeze to trigger Triple Factor Gate Unwind...")
    await asyncio.sleep(3)
    
    # Stop engine
    engine.stop()
    harvester_task.cancel()
    monitor_task.cancel()
    
    print("\nSimulation complete. Checking Logs:")
    with open("logs/harvester.log", "r") as f:
        print(f.read())


if __name__ == "__main__":
    asyncio.run(run_simulation())
