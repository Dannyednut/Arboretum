import logging
from typing import Dict, List, Optional
from config.settings import EngineSettings
from core.models import ArbitrageOpportunity, ExecutionReport, PortfolioState
from execution.router import ExecutionRouter

logger = logging.getLogger("TripleFactorGate")


class TripleFactorGate:
    def __init__(self, router: ExecutionRouter, settings: EngineSettings):
        self.router = router
        self.settings = settings
        self.max_margin_util = getattr(settings, "MAX_MARGIN_UTILIZATION_PCT", 0.85)

    async def evaluate_opportunity(self, live_opp: ArbitrageOpportunity, state: PortfolioState, held_sizes: Dict[str, float]) -> bool:
        """
        Evaluates an active opportunity against the Triple-Factor Gate.
        Returns True if the opportunity should be UNWOUND.
        """
        # Factor 0: Strict Expiry and Event Check
        if live_opp.next_event_time:
            try:
                from datetime import datetime
                import time
                dt_str = live_opp.next_event_time.replace('Z', '+00:00')
                if len(dt_str) == 10: # e.g. "2026-06-26"
                    dt_str += "T00:00:00+00:00"
                event_dt = datetime.fromisoformat(dt_str)
                
                buffer_seconds = 0
                from core.models import EventArbitrageOpportunity
                if isinstance(live_opp, EventArbitrageOpportunity):
                    buffer_seconds = live_opp.buffer_hours_before_event * 3600
                    
                if time.time() >= (event_dt.timestamp() - buffer_seconds):
                    logger.info(f"TripleFactorGate [Expiry/Event]: {live_opp.id} reached maturity/event time.")
                    return True
            except Exception as e:
                logger.debug(f"Failed to parse event time {live_opp.next_event_time} for {live_opp.id}: {e}")

        # Factor 1: Risk Threshold
        if state.margin_utilization_pct >= self.max_margin_util:
            logger.warning(f"TripleFactorGate [Risk]: Margin util {state.margin_utilization_pct*100:.1f}% >= limit {self.max_margin_util*100:.1f}%")
            return True

        from core.models import OpportunityType
        if live_opp.type in [OpportunityType.DATED_FUTURES_BASIS, OpportunityType.CALENDAR_SPREAD]:
            # Hold strictly to expiry, skip early convergence and funding checks
            return False

        # Factor 2: Basis Convergence
        # If the API reports that the net APR has dropped to a convergence threshold (e.g., 0 or negative)
        if live_opp.net_apr_pct <= getattr(self.settings, "MIN_EXIT_SPREAD_CONVERGENCE_PCT", 0.0) * 100:
            logger.info(f"TripleFactorGate [Basis]: {live_opp.id} converged. Net APR is {live_opp.net_apr_pct:.2f}%")
            return True

        # Factor 3: Funding Squeeze
        # Funding is a squeeze only if we are the side PAYING it.
        # Positive funding: longs pay shorts. Negative funding: shorts pay longs.
        funding = live_opp.raw_data.get("fundingRate", 0.0)
        net_funding = live_opp.raw_data.get("netFundingRate", funding)
        
        perp_leg = next((leg for leg in live_opp.legs if leg.asset_type in ["perp", "swap"]), None)
        if perp_leg:
            is_short_perp = (perp_leg.side == "sell")
            squeeze_threshold = 0.005
            
            if is_short_perp and net_funding < -squeeze_threshold:
                logger.warning(f"TripleFactorGate [Funding Squeeze]: Short perp paying extreme negative funding ({net_funding:.4f})")
                return True
            elif not is_short_perp and net_funding > squeeze_threshold:
                logger.warning(f"TripleFactorGate [Funding Squeeze]: Long perp paying extreme positive funding ({net_funding:.4f})")
                return True

        return False

    async def execute_unwind(self, opp: ArbitrageOpportunity, held_sizes: Dict[str, float], current_prices: Dict[str, float]) -> ExecutionReport:
        """
        Performs the Atomic Unwind via the Router.
        """
        logger.warning(f"Triggering Atomic Unwind for {opp.id}")
        
        # To unwind, we just reverse the side of the legs
        from core.models import HedgeLeg
        unwind_legs = []
        for leg in opp.legs:
            unwind_side = "sell" if leg.side == "buy" else "buy"
            unwind_legs.append(HedgeLeg(
                exchange=leg.exchange,
                symbol=leg.symbol,
                side=unwind_side,
                asset_type=leg.asset_type,
                leverage=leg.leverage
            ))
        
        unwind_opp = ArbitrageOpportunity(
            id=opp.id + "_unwind",
            type=opp.type,
            coin=opp.coin,
            legs=unwind_legs,
            net_apr_pct=0.0,
            raw_data={},
        )
        
        return await self.router.execute_opportunity(unwind_opp, held_sizes, current_prices)
