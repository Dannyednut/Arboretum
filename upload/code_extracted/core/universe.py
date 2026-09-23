import logging
from typing import Dict, Iterable, List, Optional

from hyperliquid.info import Info
from hyperliquid.utils import constants

from config.settings import TradingPair
from core.legacy_models import BasisPair, MarketSymbol

logger = logging.getLogger("Universe")


def api_url(network: str) -> str:
    return constants.MAINNET_API_URL if network == "mainnet" else constants.TESTNET_API_URL


def ws_url(network: str) -> str:
    return "wss://api.hyperliquid.xyz/ws" if network == "mainnet" else "wss://api.hyperliquid-testnet.xyz/ws"


class UniverseBuilder:
    def __init__(self, network: str, info: Optional[Info] = None):
        self.network = network
        self.info = info or Info(api_url(network), skip_ws=True)
        self.symbols: Dict[str, MarketSymbol] = {}
        self.ws_to_name: Dict[str, str] = {}

    def refresh_metadata(self) -> Dict[str, MarketSymbol]:
        self.symbols.clear()
        self.ws_to_name.clear()

        perp_meta = self.info.meta()
        for asset in perp_meta["universe"]:
            symbol = MarketSymbol(
                name=asset["name"],
                ws_coin=asset["name"],
                sz_decimals=int(asset["szDecimals"]),
                px_decimals=6,
                is_spot=False,
            )
            self._add_symbol(symbol)

        spot_meta = self.info.spot_meta()
        tokens_by_index = {token["index"]: token for token in spot_meta["tokens"]}
        for spot in spot_meta["universe"]:
            base = tokens_by_index[spot["tokens"][0]]
            quote = tokens_by_index[spot["tokens"][1]]
            alias = f'{base["name"]}/{quote["name"]}'
            exchange_name = spot["name"]
            symbol = MarketSymbol(
                name=alias,
                ws_coin=exchange_name,
                sz_decimals=int(base["szDecimals"]),
                px_decimals=8,
                is_spot=True,
            )
            self._add_symbol(symbol)
            if exchange_name not in self.symbols:
                self.symbols[exchange_name] = symbol

        logger.info("Loaded %s Hyperliquid symbols", len(self.symbols))
        return self.symbols

    def resolve_pairs(self, configured_pairs: Iterable[TradingPair]) -> List[BasisPair]:
        if not self.symbols:
            self.refresh_metadata()

        pairs: List[BasisPair] = []
        for configured in configured_pairs:
            if not configured.enabled:
                continue
            perp = self._resolve_symbol(configured.perp, is_spot=False)
            spot = self._resolve_symbol(configured.spot, is_spot=True)
            pairs.append(BasisPair(perp=perp, spot=spot))
        return pairs

    def _add_symbol(self, symbol: MarketSymbol) -> None:
        self.symbols[symbol.name] = symbol
        self.ws_to_name[symbol.ws_coin] = symbol.name

    def _resolve_symbol(self, name: str, is_spot: bool) -> MarketSymbol:
        symbol = self.symbols.get(name)
        if symbol is None:
            raise ValueError(f"Unknown Hyperliquid symbol: {name}")
        if symbol.is_spot != is_spot:
            market = "spot" if is_spot else "perp"
            raise ValueError(f"{name} is not a {market} symbol")
        return symbol
