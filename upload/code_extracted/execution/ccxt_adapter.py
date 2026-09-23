import asyncio
import logging
from typing import Any, Dict, Optional

import ccxt.pro as ccxtpro

from config.settings import EngineSettings
from core.interfaces.exchange import BaseExecutionClient
from core.models import HedgeLeg, OrderLegResult

logger = logging.getLogger("CcxtAdapter")

class CcxtExecutionAdapter(BaseExecutionClient):
    def __init__(self, exchange_id: str, settings: EngineSettings):
        self.exchange_id = exchange_id
        self.settings = settings
        self._dry_run = settings.DRY_RUN
        
        exchange_class = getattr(ccxtpro, exchange_id)
        
        api_key = getattr(settings, f"{exchange_id.upper()}_API_KEY", "")
        api_secret = getattr(settings, f"{exchange_id.upper()}_SECRET", "")
        
        self.exchange = exchange_class({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
        })
        
        if settings.NETWORK == "testnet":
            self.exchange.set_sandbox_mode(True)
            
    @property
    def account_address(self) -> Optional[str]:
        return self.exchange.apiKey if self.exchange.apiKey else "unauthenticated"

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    async def get_top_of_book(self, symbol: str, asset_type: str = "perp") -> Optional[Dict[str, float]]:
        """Best bid/ask snapshot, or None. Works unauthenticated (public data)."""
        try:
            ob = await self.exchange.fetch_order_book(symbol, limit=5)
            bids, asks = ob.get("bids") or [], ob.get("asks") or []
            if bids and asks:
                return {"bid": float(bids[0][0]), "ask": float(asks[0][0])}
        except Exception as e:
            logger.debug(f"Top-of-book fetch failed on {self.exchange_id} {symbol}: {e}")
        return None

    async def execute_leg(self, leg: HedgeLeg, size: float, price: float) -> OrderLegResult:
        if self._dry_run:
            logger.info(f"[DRY RUN] Would execute {leg.side} on {self.exchange_id} for {leg.symbol} size {size} px {price}")
            return OrderLegResult(
                exchange=self.exchange_id, symbol=leg.symbol, requested_size=size, requested_px=price, filled_size=size, avg_px=price, status="filled"
            )

        # Bug #1 fix: a live limit order at price<=0 is always rejected (or worse,
        # a fat-finger crossing order). Refuse instead of sending it.
        if not price or price <= 0:
            logger.error(f"Refusing live limit order with invalid price {price} on {self.exchange_id} {leg.symbol}")
            return OrderLegResult(exchange=self.exchange_id, symbol=leg.symbol, requested_size=size, requested_px=price, filled_size=0.0, avg_px=0.0, status="error", error="invalid_price")

        try:
            ccxt_type = "swap" if leg.asset_type == "perp" else leg.asset_type
            params = {"type": ccxt_type}
            res = await self.exchange.create_order(leg.symbol, "limit", leg.side, size, price, params)
            filled = float(res.get("filled") or 0.0)
            # Bug #1 fix (companion): a resting/zero-fill order must NOT be booked
            # as "filled" - that phantom-filled bookkeeping is what leaves
            # unhedged naked legs behind after "Successfully Harvested" lines.
            if filled > 0:
                return OrderLegResult(
                    exchange=self.exchange_id, symbol=leg.symbol, requested_size=size, requested_px=price, filled_size=filled, avg_px=float(res.get("average") or res.get("price") or price), status="filled"
                )
            return OrderLegResult(exchange=self.exchange_id, symbol=leg.symbol, requested_size=size, requested_px=price, filled_size=0.0, avg_px=0.0, status="error", error="zero_fill")
        except Exception as e:
            logger.error(f"Execution error on {self.exchange_id} for {leg.symbol}: {e}")
            return OrderLegResult(exchange=self.exchange_id, symbol=leg.symbol, requested_size=size, requested_px=price, filled_size=0.0, avg_px=0.0, status="error", error=str(e))

    async def fetch_balances(self) -> Dict[str, Any]:
        if self._dry_run:
            return {"USDT": 1000.0, "USDC": 1000.0}
        
        try:
            return await self.exchange.fetch_balance()
        except Exception as e:
            logger.error(f"Error fetching balance on {self.exchange_id}: {e}")
            return {}

    async def get_active_positions(self) -> Dict[str, float]:
        if self._dry_run:
            return {}
            
        try:
            positions = await self.exchange.fetch_positions()
            active = {}
            for pos in positions:
                if float(pos.get("contracts", 0)) > 0:
                    symbol = pos.get("symbol")
                    side = pos.get("side") # 'long' or 'short'
                    size = float(pos.get("contracts"))
                    if side == "short":
                        size = -size
                    active[symbol] = size
            return active
        except Exception as e:
            logger.error(f"Error fetching positions on {self.exchange_id}: {e}")
            return {}

    async def execute_unwind_leg(self, leg: HedgeLeg, size: float, price: float) -> OrderLegResult:
        if self._dry_run:
            logger.info(f"[DRY RUN] Would unwind {leg.side} on {self.exchange_id} for {leg.symbol}")
            return OrderLegResult(exchange=self.exchange_id, symbol=leg.symbol, requested_size=size, requested_px=price, filled_size=size, avg_px=price, status="filled")
        
        if not price or price <= 0:
            logger.error(f"Refusing live unwind with invalid price {price} on {self.exchange_id} {leg.symbol}")
            return OrderLegResult(exchange=self.exchange_id, symbol=leg.symbol, requested_size=size, requested_px=price, filled_size=0.0, avg_px=0.0, status="error", error="invalid_price")

        try:
            ccxt_type = "swap" if leg.asset_type == "perp" else leg.asset_type
            params = {"type": ccxt_type}
            res = await self.exchange.create_order(leg.symbol, "limit", leg.side, size, price, params)
            filled = float(res.get("filled") or 0.0)
            if filled > 0:
                return OrderLegResult(exchange=self.exchange_id, symbol=leg.symbol, requested_size=size, requested_px=price, filled_size=filled, avg_px=float(res.get("average") or res.get("price") or price), status="filled")
            return OrderLegResult(exchange=self.exchange_id, symbol=leg.symbol, requested_size=size, requested_px=price, filled_size=0.0, avg_px=0.0, status="error", error="zero_fill")
        except Exception as e:
            logger.error(f"Unwind error on {self.exchange_id} for {leg.symbol}: {e}")
            return OrderLegResult(exchange=self.exchange_id, symbol=leg.symbol, requested_size=size, requested_px=price, filled_size=0.0, avg_px=0.0, status="error", error=str(e))

    async def get_net_delta_usd(self, asset: str) -> float:
        if self._dry_run:
            return 0.0
            
        try:
            positions = await self.exchange.fetch_positions([asset])
            delta = 0.0
            for pos in positions:
                delta += float(pos.get("notional", 0))
            return delta
        except Exception as e:
            logger.error(f"Delta fetch error: {e}")
            return 0.0
