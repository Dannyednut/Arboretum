import asyncio
import logging
import signal
from dataclasses import dataclass
from time import time
from typing import Dict, Optional, Tuple

from config.settings import settings
from core.interfaces.exchange import BaseExecutionClient
from core.models import ArbitrageOpportunity, ExecutionReport, OpportunityType, PortfolioState
from data.ccxt_ws_client import CcxtWsClient
from data.opportunity_feed import SharpeOpportunityIngestor
from execution.ccxt_adapter import CcxtExecutionAdapter
from execution.hl_adapter import HyperliquidExecutionAdapter
from execution.router import ExecutionRouter
from risk.portfolio_manager import PortfolioManager
from risk.triple_factor_gate import TripleFactorGate

logger = logging.getLogger("YieldHarvester")


@dataclass
class HeldPosition:
    """Bookkeeping for a live harvested position.

    Bug #3 fix: we no longer hold only the (immediately stale) opportunity
    snapshot - we track when it was last refreshed from the feed and keep the
    entry ExecutionReport so unwinds use ACTUAL filled sizes/prices, not the
    configured allocation."""
    opp: ArbitrageOpportunity
    entry_report: ExecutionReport
    captured_at_ts: float
    refreshed_at_ts: float


class YieldHarvesterEngine:
    def __init__(self):
        self._setup_logging()
        self.settings = settings
        self.is_running = False

        self.clients: Dict[str, BaseExecutionClient] = {}
        self.active_opps: Dict[str, HeldPosition] = {}
        # Latest feed snapshots {opp_id: (opp, captured_ts)} refreshed every harvest cycle
        self._latest_opps: Dict[str, Tuple[ArbitrageOpportunity, float]] = {}

        # Initialize configured adapters
        for exchange_id in settings.active_exchanges:
            if exchange_id == "hyperliquid":
                self.clients["hyperliquid"] = HyperliquidExecutionAdapter(settings)
            else:
                self.clients[exchange_id] = CcxtExecutionAdapter(exchange_id, settings)

        self.portfolio = PortfolioManager(self.clients, settings.GLOBAL_LIQUIDITY_BUFFER_PCT)
        self.router = ExecutionRouter(self.clients)
        self.ingestor = SharpeOpportunityIngestor(settings.SHARPE_API_KEY)
        self.gate = TripleFactorGate(self.router, settings)

    @staticmethod
    def _setup_logging() -> None:
        import os
        import logging.handlers
        level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")

        root = logging.getLogger()
        root.setLevel(level)
        root.handlers.clear()

        console = logging.StreamHandler()
        console.setFormatter(fmt)
        console.setLevel(level)
        root.addHandler(console)

        os.makedirs("logs", exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            "logs/harvester.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        file_handler.setLevel(level)
        root.addHandler(file_handler)

    # ------------------------------------------------------------------ pricing
    def _collar_price(self, ref_px: float, side: str) -> float:
        """Protective limit price around a reference: buy no higher than
        ref*(1+slip), sell no lower than ref*(1-slip)."""
        c = self.settings.MAX_SLIPPAGE_PCT
        return ref_px * (1 + c) if side == "buy" else ref_px * (1 - c)

    async def _protected_price(
        self,
        client: Optional[BaseExecutionClient],
        leg,
        side: str,
        ref_px: Optional[float] = None,
    ) -> Optional[float]:
        """Bug #1 fix: every order price is derived from a live top-of-book when
        available, else collared around the reference (entry fill) price.
        Returns None (never 0.0) when no sane price can be determined."""
        if client is not None:
            tob = await client.get_top_of_book(leg.symbol, leg.asset_type)
            if tob:
                px = tob.get("ask") if side == "buy" else tob.get("bid")
                if px and px > 0:
                    return self._collar_price(px, side)
        if ref_px and ref_px > 0:
            return self._collar_price(ref_px, side)
        return None

    # -------------------------------------------------------------------- loops
    async def run(self):
        self.is_running = True
        self._install_signal_handlers()
        logger.info(f"Starting Multi-Exchange Yield Harvester; dry_run={self.settings.DRY_RUN}")

        tasks = [
            asyncio.create_task(self.harvester_loop()),
            asyncio.create_task(self.monitor_loop()),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()

    async def monitor_loop(self):
        while self.is_running:
            try:
                state = await self.portfolio.get_state()
                now = time()

                to_remove = []
                for opp_id, held in self.active_opps.items():
                    # Bug #3 fix: evaluate the gate against the FRESH feed snapshot
                    # when one exists; fall back to the captured snapshot otherwise.
                    eval_opp = held.opp
                    live = self._latest_opps.get(opp_id)
                    if live is not None:
                        eval_opp, held.refreshed_at_ts = live[0], live[1]

                    should_unwind = await self.gate.evaluate_opportunity(eval_opp, state, {})

                    # Staleness guard: a position whose signal has not been seen
                    # fresh in the feed for FORCE_UNWIND_IF_STALE_SECONDS is a
                    # blind position - unwind it protectively. Dated/calendar
                    # positions are exempt (they legitimately leave the live feed).
                    event_types = {OpportunityType.DATED_FUTURES_BASIS, OpportunityType.CALENDAR_SPREAD}
                    stale_beyond_limit = (
                        (now - held.refreshed_at_ts) > self.settings.FORCE_UNWIND_IF_STALE_SECONDS
                        and held.opp.type not in event_types
                    )
                    if stale_beyond_limit:
                        logger.warning(
                            f"{opp_id}: signal stale for >{self.settings.FORCE_UNWIND_IF_STALE_SECONDS}s - protective unwind"
                        )
                        should_unwind = True

                    if should_unwind:
                        report = await self._unwind_position(held)
                        if report and report.fully_filled:
                            logger.info(f"Successfully unwound {opp_id}")
                            to_remove.append(opp_id)
                        else:
                            logger.warning(
                                f"Unwind incomplete for {opp_id} ({report.error if report else 'no report'}); will retry"
                            )

                for r in to_remove:
                    self.active_opps.pop(r, None)

            except Exception as e:
                logger.error(f"Monitor loop error: {e}")

            await asyncio.sleep(self.settings.POSITION_CHECK_INTERVAL_SECONDS)

    async def _unwind_position(self, held: HeldPosition) -> Optional[ExecutionReport]:
        """Bug #1 fix: unwind with ACTUAL filled sizes from the entry report and
        protected prices from the live book (collared around entry fill when the
        book is unavailable). Never sends price=0.0."""
        sizes, prices = {}, {}
        for leg_res, leg in zip(held.entry_report.legs, held.opp.legs):
            key = f"{leg.exchange}_{leg.symbol}"
            sizes[key] = leg_res.filled_size if leg_res.filled_size > 0 else self.settings.ALLOCATION_PER_TRADE_USD
            client = self.clients.get(leg.exchange.lower())
            close_side = "sell" if leg.side == "buy" else "buy"
            px = await self._protected_price(client, leg, close_side, ref_px=leg_res.avg_px)
            if px is None:
                logger.error(f"Cannot determine protective unwind price for {key}; aborting this attempt")
                return None
            prices[key] = px
        return await self.gate.execute_unwind(held.opp, sizes, prices)

    async def harvester_loop(self):
        while self.is_running:
            try:
                state = await self.portfolio.get_state()
                if not self.portfolio.can_allocate(state, self.settings.ALLOCATION_PER_TRADE_USD):
                    logger.info("Skipping harvest: Global Liquidity Buffer reached or insufficient margin.")
                    await asyncio.sleep(10)
                    continue

                # 1. Fetch Opportunities
                logger.debug("Fetching opportunities from Sharpe API...")

                # Gather opportunities concurrently
                fetch_tasks = [
                    self.ingestor.fetch_spot_perp(exchanges=self.settings.active_exchanges),
                    self.ingestor.fetch_cross_exchange(exchanges=self.settings.active_exchanges),
                    self.ingestor.fetch_cross_spot(exchanges=self.settings.active_exchanges),
                    self.ingestor.fetch_dated_futures_carry(exchanges=self.settings.active_exchanges),
                    self.ingestor.fetch_calendar_spreads(exchanges=self.settings.active_exchanges)
                ]

                results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

                all_opps = []
                for res in results:
                    if isinstance(res, list):
                        all_opps.extend(res)
                    else:
                        logger.error(f"Error in opportunity fetch task: {res}")

                # Bug #3 fix: publish fresh snapshots (with capture time) for the monitor loop
                now = time()
                self._latest_opps = {o.id: (o, now) for o in all_opps}

                # Filter opportunities
                valid_opps = [o for o in all_opps if o.net_apr_pct >= self.settings.MIN_NET_PROFIT_MARGIN_PCT * 100]
                valid_opps.sort(key=lambda x: x.net_apr_pct, reverse=True)

                if valid_opps:
                    best_opp = valid_opps[0]
                    if best_opp.id not in self.active_opps:
                        logger.info(f"Found Alpha Opportunity! {best_opp.id} with {best_opp.net_apr_pct:.2f}% Net APR")

                        # Bug #1 fix: sizes stay allocation-based by design at ENTRY,
                        # but prices are live-protected (top-of-book + slippage
                        # collar). An entry without a sane price is skipped, not
                        # sent at 0.0.
                        sizes = {f"{leg.exchange}_{leg.symbol}": self.settings.ALLOCATION_PER_TRADE_USD for leg in best_opp.legs}
                        prices = {}
                        price_ok = True
                        for leg in best_opp.legs:
                            client = self.clients.get(leg.exchange.lower())
                            px = await self._protected_price(client, leg, leg.side)
                            if px is None:
                                logger.warning(
                                    f"No protected price available for {leg.exchange} {leg.symbol}; skipping entry this cycle"
                                )
                                price_ok = False
                                break
                            prices[f"{leg.exchange}_{leg.symbol}"] = px

                        if not price_ok:
                            await asyncio.sleep(10)
                            continue

                        report = await self.router.execute_opportunity(best_opp, sizes, prices)
                        if report.fully_filled:
                            logger.info(f"Successfully Harvested {best_opp.id}")
                            self.active_opps[best_opp.id] = HeldPosition(
                                opp=best_opp,
                                entry_report=report,
                                captured_at_ts=now,
                                refreshed_at_ts=now,
                            )
                        else:
                            logger.warning(f"Failed to harvest {best_opp.id}: {report.error}")

            except Exception as e:
                logger.error(f"Harvester loop error: {e}")

            await asyncio.sleep(10)

    def stop(self):
        self.is_running = False

    def _install_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self.stop)
        except NotImplementedError:
            pass

if __name__ == "__main__":
    asyncio.run(YieldHarvesterEngine().run())
