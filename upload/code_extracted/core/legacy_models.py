"""
Legacy models kept for backward compatibility with Hyperliquid-native scanner components.
New code should use core.models (ArbitrageOpportunity, HedgeLeg, etc.) instead.
"""
from dataclasses import dataclass, field
from enum import Enum
from time import time
from typing import Any, Dict, Optional


class TradeDirection(str, Enum):
    CONTANGO = "contango"
    BACKWARDATION = "backwardation"


@dataclass(frozen=True)
class MarketSymbol:
    name: str
    ws_coin: str
    sz_decimals: int
    px_decimals: int
    is_spot: bool


@dataclass(frozen=True)
class BasisPair:
    perp: MarketSymbol
    spot: MarketSymbol

    @property
    def key(self) -> str:
        return f"{self.spot.name}/{self.perp.ws_coin}/{'SPOT' if self.spot.is_spot else 'PERP'}"


@dataclass(frozen=True)
class BookQuote:
    symbol: str
    side: str
    quote_usd: float
    filled_usd: float
    fill_rate: float
    vwap: float
    best_px: float
    base_size: float
    timestamp: float
    worst_px: float = 0.0

    @property
    def is_complete(self) -> bool:
        return self.fill_rate >= 1.0

    def age_seconds(self) -> float:
        return time() - self.timestamp


@dataclass(frozen=True)
class BasisSignal:
    pair: BasisPair
    direction: TradeDirection
    basis_pct: float
    funding_rate_pct: float
    net_yield_pct: float
    spot_quote: Optional[BookQuote]
    perp_quote: Optional[BookQuote]
    notional_usd: float
    reason: str
