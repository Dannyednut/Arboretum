import threading
from time import time
from typing import Any, Dict, Mapping, Optional, Tuple

from core.legacy_models import BookQuote


class LocalOrderBook:
    def __init__(self, symbol_aliases: Optional[Mapping[str, str]] = None):
        self.books: Dict[str, Dict[str, Any]] = {}
        self.symbol_aliases = dict(symbol_aliases or {})
        self._lock = threading.Lock()

    def update_aliases(self, symbol_aliases: Mapping[str, str]) -> None:
        with self._lock:
            self.symbol_aliases.update(symbol_aliases)

    def process_l2_update(self, ws_data: Dict[str, Any]) -> None:
        coin = ws_data.get("coin")
        levels = ws_data.get("levels")
        if not coin or not levels or len(levels) < 2:
            return

        symbol = self.symbol_aliases.get(coin, coin)
        timestamp = float(ws_data.get("time") or int(time() * 1000)) / 1000.0

        with self._lock:
            self.books[symbol] = {
                "bids": {float(level["px"]): float(level["sz"]) for level in levels[0]},
                "asks": {float(level["px"]): float(level["sz"]) for level in levels[1]},
                "timestamp": timestamp,
            }

    def get_quote(self, symbol: str, side: str, size_usd: float) -> BookQuote:
        with self._lock:
            book = self.books.get(symbol)
            if not book:
                return self._empty_quote(symbol, side, size_usd)

            book_side = book["asks"] if side == "buy" else book["bids"]
            sorted_prices = sorted(book_side.keys(), reverse=(side == "sell"))
            timestamp = float(book["timestamp"])

            accumulated_usd = 0.0
            accumulated_coin = 0.0
            best_px = sorted_prices[0] if sorted_prices else 0.0
            worst_px = 0.0

            for px in sorted_prices:
                sz = book_side[px]
                level_usd = px * sz
                worst_px = px
                if accumulated_usd + level_usd >= size_usd:
                    remaining = size_usd - accumulated_usd
                    accumulated_coin += remaining / px
                    accumulated_usd += remaining
                    break
                accumulated_coin += sz
                accumulated_usd += level_usd

            fill_rate = accumulated_usd / size_usd if size_usd > 0 else 0.0
            vwap = size_usd / accumulated_coin if accumulated_usd >= size_usd and accumulated_coin else 0.0
            return BookQuote(
                symbol=symbol,
                side=side,
                quote_usd=size_usd,
                filled_usd=accumulated_usd,
                fill_rate=fill_rate,
                base_size=accumulated_coin,
                vwap=vwap,
                best_px=best_px,
                worst_px=worst_px,
                timestamp=timestamp,
            )

    def get_vwap(self, coin: str, side: str, size_usd: float) -> Tuple[float, float]:
        quote = self.get_quote(coin, side, size_usd)
        return quote.vwap, quote.base_size

    def best_bid_ask(self, symbol: str) -> Tuple[float, float]:
        with self._lock:
            book = self.books.get(symbol)
            if not book:
                return 0.0, 0.0
            bids = book["bids"]
            asks = book["asks"]
            return (max(bids) if bids else 0.0, min(asks) if asks else 0.0)

    @staticmethod
    def _empty_quote(symbol: str, side: str, size_usd: float) -> BookQuote:
        return BookQuote(
            symbol=symbol,
            side=side,
            quote_usd=size_usd,
            filled_usd=0.0,
            fill_rate=0.0,
            base_size=0.0,
            vwap=0.0,
            best_px=0.0,
            worst_px=0.0,
            timestamp=0.0,
        )
