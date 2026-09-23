import asyncio
import logging
import secrets
from typing import Any, Dict, List, Optional

import eth_account
from eth_account.signers.local import LocalAccount
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.types import Cloid

from config.settings import EngineSettings
from core.interfaces.exchange import BaseExecutionClient
from core.models import HedgeLeg, OrderLegResult
from core.universe import api_url

logger = logging.getLogger("HyperliquidExecution")


class HyperliquidExecutionAdapter(BaseExecutionClient):
    def __init__(self, settings: EngineSettings, info: Optional[Info] = None):
        self.settings = settings
        self._dry_run = settings.DRY_RUN
        self.exchange_id = "hyperliquid"
        self.info: Optional[Info] = info
        self.wallet: Optional[LocalAccount] = None
        self.exchange: Optional[Exchange] = None

        if not self._dry_run:
            if not settings.HL_AGENT_PRIVATE_KEY:
                raise ValueError("HL_AGENT_PRIVATE_KEY is required when DRY_RUN=false")
            self.info = self.info or Info(api_url(settings.NETWORK), skip_ws=True)
            self.wallet = eth_account.Account.from_key(settings.HL_AGENT_PRIVATE_KEY)
            self.exchange = Exchange(
                wallet=self.wallet,
                base_url=api_url(settings.NETWORK),
                vault_address=settings.HL_MAIN_ACCOUNT_ADDRESS or None,
            )

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def account_address(self) -> Optional[str]:
        if self.settings.HL_MAIN_ACCOUNT_ADDRESS:
            return self.settings.HL_MAIN_ACCOUNT_ADDRESS
        if self.wallet:
            return self.wallet.address
        return None

    async def _call_with_retry(self, fn, *args, max_retries=3, initial_delay=0.5, backoff=2.0, **kwargs):
        delay = initial_delay
        last_exc = None
        for attempt in range(max_retries):
            try:
                return await asyncio.to_thread(fn, *args, **kwargs)
            except Exception as exc:
                last_exc = exc
                if isinstance(exc, (ValueError, TypeError, asyncio.CancelledError)):
                    raise
                logger.warning(
                    "SDK call failed on attempt %d: %s. Retrying in %.2fs...",
                    attempt + 1, exc, delay
                )
                await asyncio.sleep(delay)
                delay *= backoff
        raise last_exc

    async def fetch_balances(self) -> Dict[str, Any]:
        address = self.account_address
        if not address:
            return {"PERP_USDC": 0.0, "SPOT_USDC": 0.0, "assets": {}}
        info = self._info()

        def _fetch():
            perp_state = info.user_state(address)
            spot_state = info.spot_user_state(address)
            return {
                "PERP_USDC": float(perp_state.get("withdrawable", 0)),
                "SPOT_USDC": self._extract_spot_usdc(spot_state),
                "assets": self._extract_spot_balances(spot_state),
                "raw": {"perp": perp_state, "spot": spot_state},
                "USDT": float(perp_state.get("withdrawable", 0)), # Mock equivalent for cross-exchange
                "USDC": float(perp_state.get("withdrawable", 0))
            }

        return await self._call_with_retry(_fetch)

    def _info(self) -> Info:
        if self.info is None:
            self.info = Info(api_url(self.settings.NETWORK), skip_ws=True)
        return self.info

    async def get_top_of_book(self, symbol: str, asset_type: str = "perp") -> Optional[Dict[str, float]]:
        """Best bid/ask from the public L2 snapshot, or None (caller falls back
        to collar pricing around the entry fill)."""
        try:
            info = self._info()
            coin = symbol.split("/")[0] if "/" in symbol else symbol
            book = await self._call_with_retry(info.l2_snapshot, coin)
            levels = book.get("level") or [] if isinstance(book, dict) else []
            if len(levels) >= 2:
                bids, asks = levels[0], levels[1]
                if bids and asks:
                    return {"bid": float(bids[0]["px"]), "ask": float(asks[0]["px"])}
        except Exception as exc:
            logger.debug("HL top-of-book failed for %s: %s", symbol, exc)
        return None

    async def execute_leg(self, leg: HedgeLeg, size: float, price: float) -> OrderLegResult:
        is_buy = leg.side.lower() == "buy"
        
        # We need to map sz_decimals properly. Assuming standard for now or pulled from elsewhere
        # Normally would use self.pair.sz_decimals but we only have symbol here.
        sz = self._round_size(size, 4)
        px = self._round_price(price, 4)

        if self.dry_run:
            logger.info(f"[DRY RUN] Would execute {leg.side} on {self.exchange_id} for {leg.symbol} size {sz} px {px}")
            return OrderLegResult(exchange=self.exchange_id, symbol=leg.symbol, requested_size=sz, requested_px=px, filled_size=sz, avg_px=px, status="filled")

        if not self.exchange:
            return OrderLegResult(exchange=self.exchange_id, symbol=leg.symbol, requested_size=sz, requested_px=px, status="error", error="exchange not initialized")

        # Bug #1 fix: never submit a live order at a non-positive limit price
        if px is None or px <= 0:
            logger.error("Refusing live HL order with invalid price %s for %s", price, leg.symbol)
            return OrderLegResult(exchange=self.exchange_id, symbol=leg.symbol, requested_size=sz, requested_px=px, status="error", error="invalid_price")

        req = self._order_request(leg.symbol, is_buy, sz, px)
        
        def _execute():
            return self.exchange.bulk_orders([req])

        try:
            raw = await self._call_with_retry(_execute)
            parsed = self.parse_order_response(raw, [OrderLegResult(exchange=self.exchange_id, symbol=leg.symbol, requested_size=sz, requested_px=px)])
            return parsed[0]
        except Exception as exc:
            logger.exception("Hyperliquid order submission failed: %s", exc)
            return OrderLegResult(exchange=self.exchange_id, symbol=leg.symbol, requested_size=sz, requested_px=px, status="error", error=str(exc))

    async def execute_unwind_leg(self, leg: HedgeLeg, size: float, price: float) -> OrderLegResult:
        is_buy = leg.side.lower() == "buy"
        sz = self._round_size(size, 4)
        px = self._round_price(price, 4)

        if self.dry_run:
            logger.info(f"[DRY RUN] Would unwind {leg.side} on {self.exchange_id} for {leg.symbol} size {sz} px {px}")
            return OrderLegResult(exchange=self.exchange_id, symbol=leg.symbol, requested_size=sz, requested_px=px, filled_size=sz, avg_px=px, status="filled")

        if not self.exchange:
            return OrderLegResult(exchange=self.exchange_id, symbol=leg.symbol, requested_size=sz, requested_px=px, status="error", error="exchange not initialized")

        # Bug #1 fix: never submit a live unwind at a non-positive limit price
        if px is None or px <= 0:
            logger.error("Refusing live HL unwind with invalid price %s for %s", price, leg.symbol)
            return OrderLegResult(exchange=self.exchange_id, symbol=leg.symbol, requested_size=sz, requested_px=px, status="error", error="invalid_price")

        req = self._order_request(leg.symbol, is_buy, sz, px)
        # Assuming asset_type "perp" means reduce only
        if leg.asset_type == "perp":
            req["reduce_only"] = True

        def _execute():
            return self.exchange.bulk_orders([req])

        try:
            raw = await self._call_with_retry(_execute)
            parsed = self.parse_order_response(raw, [OrderLegResult(exchange=self.exchange_id, symbol=leg.symbol, requested_size=sz, requested_px=px)])
            return parsed[0]
        except Exception as exc:
            logger.exception("Hyperliquid unwind submission failed: %s", exc)
            return OrderLegResult(exchange=self.exchange_id, symbol=leg.symbol, requested_size=sz, requested_px=px, status="error", error=str(exc))

    def parse_order_response(
        self, raw: Dict[str, Any], requested_legs: List[OrderLegResult]
    ) -> List[OrderLegResult]:
        statuses = (
            raw.get("response", {})
            .get("data", {})
            .get("statuses", [])
        )
        parsed: List[OrderLegResult] = []
        for requested, status in zip(requested_legs, statuses):
            parsed.append(self._parse_leg_status(requested, status))
        if len(parsed) < len(requested_legs):
            parsed.extend(
                OrderLegResult(
                    exchange=leg.exchange,
                    symbol=leg.symbol,
                    requested_size=leg.requested_size,
                    requested_px=leg.requested_px,
                    status="missing",
                    error="missing order status in response",
                )
                for leg in requested_legs[len(parsed) :]
            )
        return parsed

    @staticmethod
    def _parse_leg_status(requested: OrderLegResult, status: Dict[str, Any]) -> OrderLegResult:
        if "filled" in status:
            filled = status["filled"]
            return OrderLegResult(
                exchange=requested.exchange,
                symbol=requested.symbol,
                requested_size=requested.requested_size,
                requested_px=requested.requested_px,
                filled_size=float(filled.get("totalSz", 0)),
                avg_px=float(filled.get("avgPx", 0)),
                oid=filled.get("oid"),
                status="filled",
                raw=status,
            )
        if "error" in status:
            return OrderLegResult(
                exchange=requested.exchange,
                symbol=requested.symbol,
                requested_size=requested.requested_size,
                requested_px=requested.requested_px,
                status="error",
                error=str(status["error"]),
                raw=status,
            )
        return OrderLegResult(
            exchange=requested.exchange,
            symbol=requested.symbol,
            requested_size=requested.requested_size,
            requested_px=requested.requested_px,
            status="unknown",
            error="unrecognized order status",
            raw=status,
        )

    @staticmethod
    def _round_size(size: float, decimals: int) -> float:
        return round(size, decimals)

    @staticmethod
    def _round_price(price: float, decimals: int) -> float:
        return round(float(f"{price:.5g}"), decimals)

    @staticmethod
    def _order_request(symbol: str, is_buy: bool, size: float, price: float) -> Dict[str, Any]:
        return {
            "coin": symbol,
            "is_buy": is_buy,
            "sz": size,
            "limit_px": price,
            "order_type": {"limit": {"tif": "Ioc"}},
            "reduce_only": False,
            "cloid": Cloid.from_int(secrets.randbits(128)),
        }

    @staticmethod
    def _extract_spot_usdc(spot_state: Dict[str, Any]) -> float:
        balances = spot_state.get("balances", [])
        for balance in balances:
            if balance.get("coin") == "USDC":
                return float(balance.get("total", balance.get("hold", 0)))
        return 0.0

    @staticmethod
    def _extract_spot_balances(spot_state: Dict[str, Any]) -> Dict[str, float]:
        balances = {}
        for balance in spot_state.get("balances", []):
            coin = balance.get("coin")
            if coin:
                balances[coin] = float(balance.get("total", 0))
        return balances

    async def get_active_positions(self) -> Dict[str, float]:
        address = self.account_address
        if not address:
            return {}

        if self.dry_run:
            return {}

        try:
            info = self._info()
            perp_state = await self._call_with_retry(info.user_state, address)
            positions = {}
            for pos in perp_state.get("assetPositions", []):
                item = pos.get("position", {})
                coin = item.get("coin")
                if coin:
                    szi = float(item.get("szi", 0.0))
                    if szi != 0.0:
                        positions[coin] = szi
            return positions
        except Exception as exc:
            logger.error("Failed to fetch active positions: %s", exc)
            return {}

    async def get_net_delta_usd(self, asset: str) -> float:
        address = self.account_address
        if not address:
            return 0.0

        if self.dry_run:
            return 0.0

        info = self._info()

        def _fetch():
            state = info.user_state(address)
            for position in state.get("assetPositions", []):
                item = position.get("position", {})
                if item.get("coin") == asset:
                    return float(item.get("positionValue", 0)) * (1 if float(item.get("szi", 0)) >= 0 else -1)
            return 0.0

        try:
            return await self._call_with_retry(_fetch)
        except Exception as exc:
            logger.error("Failed to fetch net delta for %s: %s", asset, exc)
            return 0.0
