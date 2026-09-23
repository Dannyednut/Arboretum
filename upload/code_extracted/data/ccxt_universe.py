import logging
from typing import Dict, Iterable, List
import ccxt.pro as ccxtpro

from config.settings import TradingPair
from core.legacy_models import BasisPair, MarketSymbol

logger = logging.getLogger("CcxtUniverse")

class CcxtUniverseBuilder:
    def __init__(self, exchange: ccxtpro.Exchange):
        self.exchange = exchange
        self.symbols: Dict[str, MarketSymbol] = {}
        self.ws_to_name: Dict[str, str] = {}

    async def initialize(self):
        await self.exchange.load_markets()
        logger.info(f"Loaded {len(self.exchange.markets)} markets from {self.exchange.name}")
        logger.info(f"Markets: {list(self.exchange.markets.keys())}")
        
    def resolve_pairs(self, configured_pairs: Iterable[TradingPair]) -> List[BasisPair]:
        pairs: List[BasisPair] = []
        for configured in configured_pairs:
            if not configured.enabled:
                continue
            
            # Use provided configured names as the symbol names in CCXT
            perp_name = configured.perp
            spot_name = configured.spot
            
            perp_market = self.exchange.market(perp_name) if perp_name in self.exchange.markets else None
            spot_market = self.exchange.market(spot_name) if spot_name in self.exchange.markets else None
            
            if not perp_market or not spot_market:
                logger.warning(f"Could not resolve CCXT markets for {perp_name} or {spot_name}")
                continue

            perp = MarketSymbol(
                name=perp_name,
                ws_coin=perp_name, # CCXT uses the same symbol for ws
                sz_decimals=perp_market.get("precision", {}).get("amount", 3),
                px_decimals=perp_market.get("precision", {}).get("price", 2),
                is_spot=False,
            )
            spot = MarketSymbol(
                name=spot_name,
                ws_coin=spot_name,
                sz_decimals=spot_market.get("precision", {}).get("amount", 3),
                px_decimals=spot_market.get("precision", {}).get("price", 2),
                is_spot=True,
            )
            
            self.symbols[perp.name] = perp
            self.ws_to_name[perp.ws_coin] = perp.name
            self.symbols[spot.name] = spot
            self.ws_to_name[spot.ws_coin] = spot.name
            
            pairs.append(BasisPair(perp=perp, spot=spot))
            
        return pairs
