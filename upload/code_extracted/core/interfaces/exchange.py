from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Iterable, Optional

from core.models import ArbitrageOpportunity, ExecutionReport, HedgeLeg, OrderLegResult


class BaseWsClient(ABC):
    @abstractmethod
    async def connect(self) -> None:
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        pass

    @abstractmethod
    async def subscribe_l2_book(
        self, coins: Iterable[str], callback: Callable[[Dict], Any]
    ) -> None:
        pass

    @abstractmethod
    async def subscribe_funding_rates(
        self, coins: Iterable[str], callback: Callable[[Dict], Any]
    ) -> None:
        pass


class BaseExecutionClient(ABC):
    @property
    @abstractmethod
    def account_address(self) -> Optional[str]:
        pass

    @property
    @abstractmethod
    def dry_run(self) -> bool:
        pass

    @abstractmethod
    async def execute_leg(self, leg: HedgeLeg, size: float, price: float) -> OrderLegResult:
        pass

    @abstractmethod
    async def fetch_balances(self) -> Dict[str, Any]:
        pass

    @abstractmethod
    async def get_active_positions(self) -> Dict[str, float]:
        pass

    @abstractmethod
    async def execute_unwind_leg(self, leg: HedgeLeg, size: float, price: float) -> OrderLegResult:
        pass

    @abstractmethod
    async def get_net_delta_usd(self, asset: str) -> float:
        pass

    async def get_top_of_book(self, symbol: str, asset_type: str = "perp") -> Optional[Dict[str, float]]:
        """Best bid/ask snapshot {'bid': float, 'ask': float}, or None if unavailable.

        Non-abstract default: adapters without book access return None so callers
        fall back to collar pricing around a reference fill price (Bug #1 fix)."""
        return None
