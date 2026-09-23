import asyncio
import logging
from collections import defaultdict
from time import time
from typing import Dict, Optional

from config.settings import EngineSettings
from core.legacy_models import BasisSignal, TradeDirection
from core.models import RiskDecision

logger = logging.getLogger("Risk")


class RiskManager:
    def __init__(self, settings: EngineSettings, execution_adapter):
        self.settings = settings
        self.execution = execution_adapter
        self.last_signal_at: Dict[str, float] = defaultdict(float)
        self.daily_notional_usd = 0.0
        self.day_bucket = self._day_bucket()
        self.delta_threshold_usd = settings.ALLOCATION_PER_TRADE_USD * 0.05

    async def check_signal(self, signal: BasisSignal) -> RiskDecision:
        self._reset_daily_bucket_if_needed()

        perp_delta = await self.get_net_delta_usd(signal.pair.perp.name)
        if abs(perp_delta) > self.delta_threshold_usd:
            return RiskDecision(False, f"active position skew detected for {signal.pair.perp.name}: {perp_delta:.2f} USD")

        if signal.notional_usd + self.daily_notional_usd > self.settings.MAX_DAILY_NOTIONAL_USD:
            return RiskDecision(False, "daily notional limit exceeded")

        if signal.notional_usd > self.settings.MAX_POSITION_NOTIONAL_USD:
            return RiskDecision(False, "per-trade notional exceeds max position notional")

        if signal.spot_quote.age_seconds() > self.settings.MAX_BOOK_AGE_SECONDS:
            return RiskDecision(False, "spot book is stale")

        if signal.perp_quote.age_seconds() > self.settings.MAX_BOOK_AGE_SECONDS:
            return RiskDecision(False, "perp book is stale")

        if signal.spot_quote.filled_usd < signal.notional_usd:
            return RiskDecision(False, "insufficient spot depth")

        if signal.perp_quote.filled_usd < signal.notional_usd:
            return RiskDecision(False, "insufficient perp depth")

        now = time()
        if now - self.last_signal_at[signal.pair.key] < self.settings.SIGNAL_COOLDOWN_SECONDS:
            return RiskDecision(False, "pair cooldown active")

        slippage = self._quote_slippage(signal)
        if slippage > self.settings.MAX_SLIPPAGE_PCT:
            return RiskDecision(False, f"estimated slippage {slippage:.6f} exceeds limit")

        # Verify trading power (spot balance and perp margin)
        if self.settings.DRY_RUN:
            logger.debug("Dry run enabled: skipping balance and trading power validation checks.")
            return RiskDecision(True, "risk checks passed")

        try:
            balances = await self.execution.fetch_balances()
            perp_usdc = balances.get("PERP_USDC", 0.0)
            spot_usdc = balances.get("SPOT_USDC", 0.0)

            # Require at least 20% margin coverage on Perp
            perp_margin_required = signal.notional_usd * 0.20
            if perp_usdc < perp_margin_required:
                return RiskDecision(False, f"insufficient perp USDC margin: have {perp_usdc:.2f}, need {perp_margin_required:.2f}")

            if signal.direction == TradeDirection.CONTANGO:
                if spot_usdc < signal.notional_usd:
                    return RiskDecision(False, f"insufficient spot USDC balance: have {spot_usdc:.2f}, need {signal.notional_usd:.2f}")
            else:  # BACKWARDATION
                # Parse spot asset (base name from spot name)
                asset_name = signal.pair.spot.name.split("/")[0]
                spot_asset_bal = balances.get("assets", {}).get(asset_name, 0.0)
                if spot_asset_bal < signal.spot_quote.base_size:
                    return RiskDecision(False, f"insufficient spot {asset_name} balance: have {spot_asset_bal:.6f}, need {signal.spot_quote.base_size:.6f}")
        except Exception as exc:
            logger.error("Failed to verify trading power balances: %s", exc)
            return RiskDecision(False, f"failed to verify trading power balances: {exc}")

        return RiskDecision(True, "risk checks passed")

    def record_execution(self, signal: BasisSignal) -> None:
        self._reset_daily_bucket_if_needed()
        self.daily_notional_usd += signal.notional_usd
        self.last_signal_at[signal.pair.key] = time()

    async def get_net_delta_usd(self, asset: str) -> float:
        return await self.execution.get_net_delta_usd(asset)

    async def reconcile(self, asset: str) -> Optional[RiskDecision]:
        delta = await self.get_net_delta_usd(asset)
        if abs(delta) <= self.delta_threshold_usd:
            return None
        reason = f"skew detected for {asset}: {delta:.2f} USD; manual neutralization required"
        logger.warning(reason)
        return RiskDecision(False, reason)

    @staticmethod
    def _day_bucket() -> int:
        return int(time() // 86_400)

    def _reset_daily_bucket_if_needed(self) -> None:
        bucket = self._day_bucket()
        if bucket != self.day_bucket:
            self.day_bucket = bucket
            self.daily_notional_usd = 0.0

    @staticmethod
    def _quote_slippage(signal: BasisSignal) -> float:
        quotes = [signal.spot_quote, signal.perp_quote]
        slips = []
        for quote in quotes:
            if quote.best_px <= 0 or quote.vwap <= 0:
                slips.append(1.0)
            else:
                slips.append(abs(quote.vwap - quote.best_px) / quote.best_px)
        return max(slips)


AntiSkewModule = RiskManager
