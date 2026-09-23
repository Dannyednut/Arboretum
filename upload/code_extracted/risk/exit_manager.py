import logging
from typing import Dict, List, Optional
from config.settings import EngineSettings
from core.legacy_models import BasisPair, BasisSignal, TradeDirection
from core.models import ExecutionReport, OrderLegResult
from data.local_book import LocalOrderBook

logger = logging.getLogger("ExitManager")


class ExitManager:
    def __init__(self, settings: EngineSettings, execution_adapter, order_book: LocalOrderBook):
        self.settings = settings
        self.execution = execution_adapter
        self.order_book = order_book

    async def get_active_positions(self, pairs: List[BasisPair]) -> Dict[str, float]:
        """
        Determines the net position (perp size) for each active pair.
        Returns a dict of {perp_coin: signed_size_float}.
        Positive indicates Long Perp (Backwardation), negative indicates Short Perp (Contango).
        """
        return await self.execution.get_active_positions()

    async def check_and_unwind(self, pair: BasisPair, perp_position_size: float) -> Optional[ExecutionReport]:
        """
        Calculates convergence of spot-perp spread, and unwinds if spread has converged.
        """
        notional = self.settings.ALLOCATION_PER_TRADE_USD
        spot_buy = self.order_book.get_quote(pair.spot.name, "buy", notional)
        spot_sell = self.order_book.get_quote(pair.spot.name, "sell", notional)
        perp_buy = self.order_book.get_quote(pair.perp.name, "buy", notional)
        perp_sell = self.order_book.get_quote(pair.perp.name, "sell", notional)

        if not (spot_buy.is_complete and spot_sell.is_complete and perp_buy.is_complete and perp_sell.is_complete):
            return None

        # Compute spot/perp current spreads
        # Contango entry: Spot Buy / Perp Sell. Unwind: Spot Sell / Perp Buy
        # Backwardation entry: Spot Sell / Perp Buy. Unwind: Spot Buy / Perp Sell

        is_contango_position = perp_position_size < 0.0  # Perp is short
        abs_size = abs(perp_position_size)

        if is_contango_position:
            # We bought Spot and shorted Perp. Unwind is Spot Sell and Perp Buy.
            # Spread = (perp_buy - spot_sell) / spot_sell
            current_spread = (perp_buy.vwap - spot_sell.vwap) / spot_sell.vwap
            unwind_direction = "Unwind Contango"
        else:
            # We sold Spot and went Long Perp. Unwind is Spot Buy and Perp Sell.
            # Spread = (perp_sell - spot_buy) / spot_buy
            current_spread = (perp_sell.vwap - spot_buy.vwap) / spot_buy.vwap
            unwind_direction = "Unwind Backwardation"

        logger.debug(
            "%s exit scan: pair=%s, size=%.6f, current_spread=%.4f%% (threshold=%.4f%%)",
            unwind_direction, pair.key, abs_size, current_spread * 100,
            self.settings.MIN_EXIT_SPREAD_CONVERGENCE_PCT * 100
        )

        # Trigger exit if spread is compressed below target convergence threshold
        if abs(current_spread) <= self.settings.MIN_EXIT_SPREAD_CONVERGENCE_PCT:
            logger.info(
                "Triggering unwinding exit for %s: current_spread=%.4f%% <= convergence_pct=%.4f%%",
                pair.key, current_spread * 100, self.settings.MIN_EXIT_SPREAD_CONVERGENCE_PCT * 100
            )
            spot_px = spot_sell.vwap if is_contango_position else spot_buy.vwap
            perp_px = perp_buy.vwap if is_contango_position else perp_sell.vwap
            return await self.execution.execute_unwind(pair, is_contango_position, abs_size, spot_px, perp_px)

        return None
