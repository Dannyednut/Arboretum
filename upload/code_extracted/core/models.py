from dataclasses import dataclass, field
from enum import Enum
from time import time
from typing import Any, Dict, List, Optional


class OpportunityType(str, Enum):
    SPOT_PERP = "spot-perp"
    CROSS_EXCHANGE = "cross-exchange"
    DATED_FUTURES_BASIS = "dated-futures-basis"
    CALENDAR_SPREAD = "futures-calendar-spread"
    PERP_DATED_CARRY = "perp-dated-carry"
    CEX_SPOT_TRANSFER = "cex-spot-transfer"
    CEX_DEX = "cex_dex"
    DEX_DEX = "dex_dex"


@dataclass(frozen=True)
class HedgeLeg:
    exchange: str
    symbol: str
    side: str  # "buy" / "sell"
    asset_type: str  # "spot", "perp", "future"
    leverage: float = 1.0
    expiry: Optional[str] = None
    strike: Optional[float] = None


@dataclass(frozen=True)
class ArbitrageOpportunity:
    id: str  # unique identifier derived from attributes
    type: OpportunityType
    coin: str
    legs: List[HedgeLeg]
    net_apr_pct: float
    raw_data: Dict[str, Any]
    next_event_time: Optional[str] = None


@dataclass(frozen=True)
class EventArbitrageOpportunity(ArbitrageOpportunity):
    event_name: str = ""
    event_type: str = "unknown"
    buffer_hours_before_event: float = 0.0


@dataclass(frozen=True)
class PortfolioState:
    global_total_usd: float
    global_free_usd: float
    balances_by_exchange: Dict[str, Dict[str, float]]
    positions_by_exchange: Dict[str, Dict[str, float]]
    margin_utilization_pct: float


@dataclass(frozen=True)
class OrderLegResult:
    exchange: str
    symbol: str
    requested_size: float
    requested_px: float
    filled_size: float = 0.0
    avg_px: float = 0.0
    oid: Optional[int] = None
    status: str = "unknown"
    error: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def filled_notional(self) -> float:
        return self.filled_size * self.avg_px


@dataclass(frozen=True)
class ExecutionReport:
    opportunity: ArbitrageOpportunity
    accepted: bool
    dry_run: bool
    legs: List[OrderLegResult]
    raw_response: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def fully_filled(self) -> bool:
        return self.accepted and all(leg.status == "filled" for leg in self.legs)


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
