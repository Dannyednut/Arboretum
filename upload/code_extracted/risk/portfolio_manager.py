import asyncio
import logging
from typing import Dict, List

from core.interfaces.exchange import BaseExecutionClient
from core.models import PortfolioState

logger = logging.getLogger("PortfolioManager")

class PortfolioManager:
    def __init__(self, execution_clients: Dict[str, BaseExecutionClient], global_liquidity_buffer_pct: float = 0.20):
        self.clients = execution_clients
        self.global_liquidity_buffer_pct = global_liquidity_buffer_pct

    async def get_state(self) -> PortfolioState:
        # Run balances and positions concurrently across all configured exchanges
        balance_tasks = []
        position_tasks = []
        exchange_names = list(self.clients.keys())

        for name in exchange_names:
            client = self.clients[name]
            balance_tasks.append(client.fetch_balances())
            position_tasks.append(client.get_active_positions())

        balances_res = await asyncio.gather(*balance_tasks, return_exceptions=True)
        positions_res = await asyncio.gather(*position_tasks, return_exceptions=True)

        balances_by_exchange = {}
        positions_by_exchange = {}
        global_total_usd = 0.0
        global_free_usd = 0.0

        for idx, name in enumerate(exchange_names):
            # Parse balances
            b_res = balances_res[idx]
            if isinstance(b_res, Exception):
                logger.error(f"Failed to fetch balances for {name}: {b_res}")
                balances_by_exchange[name] = {}
            else:
                balances_by_exchange[name] = b_res
                # Simplify: assume 'USDT' or 'USDC' are our collateral 
                usdt_free = b_res.get('USDT', {}).get('free', b_res.get('USDT', 0.0)) if isinstance(b_res.get('USDT'), dict) else b_res.get('USDT', 0.0)
                usdc_free = b_res.get('USDC', {}).get('free', b_res.get('USDC', 0.0)) if isinstance(b_res.get('USDC'), dict) else b_res.get('USDC', 0.0)
                
                # In CCXT, balance['total'] might be available. We approximate total for now.
                usdt_total = b_res.get('USDT', {}).get('total', usdt_free) if isinstance(b_res.get('USDT'), dict) else usdt_free
                usdc_total = b_res.get('USDC', {}).get('total', usdc_free) if isinstance(b_res.get('USDC'), dict) else usdc_free
                
                global_free_usd += (usdt_free + usdc_free)
                global_total_usd += (usdt_total + usdc_total)

            # Parse positions
            p_res = positions_res[idx]
            if isinstance(p_res, Exception):
                logger.error(f"Failed to fetch positions for {name}: {p_res}")
                positions_by_exchange[name] = {}
            else:
                positions_by_exchange[name] = p_res

        margin_util = 0.0
        if global_total_usd > 0:
            margin_util = 1.0 - (global_free_usd / global_total_usd)

        return PortfolioState(
            global_total_usd=global_total_usd,
            global_free_usd=global_free_usd,
            balances_by_exchange=balances_by_exchange,
            positions_by_exchange=positions_by_exchange,
            margin_utilization_pct=margin_util
        )

    def can_allocate(self, state: PortfolioState, required_margin_usd: float) -> bool:
        """Check if we can allocate capital without breaching the liquidity buffer"""
        buffer_usd = state.global_total_usd * self.global_liquidity_buffer_pct
        available = state.global_free_usd - buffer_usd
        return available >= required_margin_usd
