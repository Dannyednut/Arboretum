import asyncio
import logging
from typing import Dict, List

from core.interfaces.exchange import BaseExecutionClient
from core.models import ArbitrageOpportunity, ExecutionReport, OrderLegResult

logger = logging.getLogger("ExecutionRouter")

class ExecutionRouter:
    def __init__(self, clients: Dict[str, BaseExecutionClient]):
        """
        clients: Dict mapping exchange_id to its initialized BaseExecutionClient
        """
        self.clients = clients

    async def execute_opportunity(self, opp: ArbitrageOpportunity, sizes: Dict[str, float], prices: Dict[str, float]) -> ExecutionReport:
        """
        Executes all legs of an opportunity concurrently across the relevant exchanges.
        Implements basic Legging Risk Mitigation (if one fails, closes the other).
        """
        tasks = []
        for leg in opp.legs:
            client = self.clients.get(leg.exchange.lower())
            if not client:
                logger.error(f"Router: No client found for exchange {leg.exchange}")
                return ExecutionReport(opportunity=opp, accepted=False, dry_run=False, legs=[], error="Missing client")

            size = sizes.get(f"{leg.exchange}_{leg.symbol}", 0.0)
            price = prices.get(f"{leg.exchange}_{leg.symbol}", 0.0)
            
            # Start execution task for this leg
            tasks.append(client.execute_leg(leg, size, price))

        logger.info(f"Router executing {opp.id} legs concurrently...")
        results: List[OrderLegResult] = await asyncio.gather(*tasks, return_exceptions=True)

        legs = []
        all_success = True
        dry_run = False
        
        for idx, res in enumerate(results):
            if isinstance(res, Exception):
                logger.error(f"Leg execution threw exception: {res}")
                legs.append(OrderLegResult(exchange=opp.legs[idx].exchange, symbol=opp.legs[idx].symbol, requested_size=0, requested_px=0, status="error", error=str(res)))
                all_success = False
            else:
                legs.append(res)
                if res.status != "filled":
                    all_success = False
                # Just take dry_run from any valid client property
                client = self.clients.get(opp.legs[idx].exchange.lower())
                if client and client.dry_run:
                    dry_run = True

        report = ExecutionReport(
            opportunity=opp,
            accepted=True,
            dry_run=dry_run,
            legs=legs,
            error=None if all_success else "Legging mismatch detected"
        )

        if not all_success and not dry_run:
            await self._mitigate_legging_risk(opp, legs)

        return report

    async def _mitigate_legging_risk(self, opp: ArbitrageOpportunity, legs: List[OrderLegResult]):
        """
        Legging Recovery Protocol: If Leg A fills but Leg B fails, immediately close Leg A at market.
        """
        logger.warning(f"Router: Legging mismatch on {opp.id}! Triggering Recovery Protocol.")
        unwind_tasks = []
        for leg_req, leg_res in zip(opp.legs, legs):
            if leg_res.status == "filled" and leg_res.filled_size > 0:
                client = self.clients.get(leg_req.exchange.lower())
                if client:
                    # Invert side to close
                    close_side = "sell" if leg_req.side == "buy" else "buy"
                    logger.warning(f"Emergency closing filled leg: {close_side} {leg_res.filled_size} {leg_req.symbol} on {leg_req.exchange}")
                    
                    # Create temporary close leg
                    from core.models import HedgeLeg
                    close_leg = HedgeLeg(exchange=leg_req.exchange, symbol=leg_req.symbol, side=close_side, asset_type=leg_req.asset_type)
                    # Use a market price equivalent or very loose limit for emergency close
                    price = leg_res.avg_px * 1.05 if close_side == "buy" else leg_res.avg_px * 0.95
                    
                    unwind_tasks.append(client.execute_unwind_leg(close_leg, leg_res.filled_size, price))

        if unwind_tasks:
            await asyncio.gather(*unwind_tasks, return_exceptions=True)
            logger.info("Recovery Protocol complete.")
