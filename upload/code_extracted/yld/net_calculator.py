from typing import Optional
import logging

from core.legacy_models import BasisPair, BasisSignal, BookQuote, TradeDirection

logger = logging.getLogger("Calculator")


class NetYieldCalculator:
    def __init__(self, taker_fee_pct: float = 0.00035, exit_fee_multiplier: float = 2.0):
        self.taker_fee = taker_fee_pct
        self.exit_fee_multiplier = exit_fee_multiplier

    def calculate_net_yield(
        self,
        basis_pct: float,
        funding_rate_pct: float,
        holding_time_hours: float,
        direction: TradeDirection = TradeDirection.CONTANGO,
    ) -> float:
        fee_drag = self.exit_fee_multiplier * self.taker_fee
        funding = funding_rate_pct * holding_time_hours
        if direction == TradeDirection.BACKWARDATION:
            return -basis_pct - funding #- fee_drag
        return basis_pct + funding #- fee_drag

    def build_signal(
        self,
        pair: BasisPair,
        spot_buy: BookQuote,
        spot_sell: BookQuote,
        perp_buy: BookQuote,
        perp_sell: BookQuote,
        funding_rate_pct: float,
        holding_time_hours: float,
        notional_usd: float,
        threshold_pct: float,
    ) -> Optional[BasisSignal]:
        contango_basis = (perp_sell.vwap - spot_buy.vwap) / spot_buy.vwap
        contango_net = self.calculate_net_yield(
            contango_basis, funding_rate_pct, holding_time_hours, TradeDirection.CONTANGO
        )

        backwardation_basis = (perp_buy.vwap - spot_sell.vwap) / spot_sell.vwap
        backwardation_net = self.calculate_net_yield(
            backwardation_basis, funding_rate_pct, holding_time_hours, TradeDirection.BACKWARDATION
        )

        if not hasattr(self, "_last_logged_basis"):
            self._last_logged_basis = {}

        if contango_net >= threshold_pct and contango_net >= backwardation_net:
            last_basis = self._last_logged_basis.get(pair.key, 999.0)
            if abs(contango_basis - last_basis) >= 0.0001:
                self._last_logged_basis[pair.key] = contango_basis
                logger.info(
                    "%s VWAP basis: contango=%.4f%% "
                    "(spot_buy_vwap=%.4f, perp_sell_vwap=%.4f, funding_rate=%.4f%%, net_yield=%.4f%%)",
                    pair.key,
                    contango_basis * 100,
                    spot_buy.vwap, perp_sell.vwap, funding_rate_pct * 100, contango_net * 100,
                )
            return BasisSignal(
                pair=pair,
                direction=TradeDirection.CONTANGO,
                basis_pct=contango_basis,
                funding_rate_pct=funding_rate_pct,
                net_yield_pct=contango_net,
                spot_quote=spot_buy,
                perp_quote=perp_sell,
                notional_usd=notional_usd,
                reason="perp rich versus spot after fees and funding",
            )

        if backwardation_net >= threshold_pct:
            last_basis = self._last_logged_basis.get(pair.key, 999.0)
            if abs(backwardation_basis - last_basis) >= 0.00011:
                self._last_logged_basis[pair.key] = backwardation_basis
                logger.info(
                    "%s VWAP basis: backwardation=%.4f%% "
                    "(spot_sell_vwap=%.4f, perp_buy_vwap=%.4f, funding_rate=%.4f%%, net_yield=%.4f%%)",
                    pair.key,
                    backwardation_basis * 100,
                    spot_sell.vwap, perp_buy.vwap, funding_rate_pct * 100, backwardation_net * 100,
                )
            return BasisSignal(
                pair=pair,
                direction=TradeDirection.BACKWARDATION,
                basis_pct=backwardation_basis,
                funding_rate_pct=funding_rate_pct,
                net_yield_pct=backwardation_net,
                spot_quote=spot_sell,
                perp_quote=perp_buy,
                notional_usd=notional_usd,
                reason="spot rich versus perp after fees and funding",
            )

        return None

    def should_enter(self, net_yield: float, target_threshold: float) -> bool:
        return net_yield >= target_threshold
