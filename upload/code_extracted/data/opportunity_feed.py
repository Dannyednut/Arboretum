import asyncio
import logging
from typing import Any, Dict, List, Optional
import aiohttp

from core.models import ArbitrageOpportunity, HedgeLeg, OpportunityType
from core.venues import canonical_venue, sharpe_display_name

logger = logging.getLogger("OpportunityFeed")

# Guardrail: real net-carry APRs above this magnitude are data errors, not alpha
# (Part-1 study: live differentials run 87-340% at the extreme; this rejects
# mis-scaled fields instead of trading on them).
MAX_PLAUSIBLE_APR_PCT = 1000.0


class SharpeOpportunityIngestor:
    BASE_URL = "https://www.sharpe.ai/api/v1/arbitrage"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"Authorization": f"Bearer {api_key}"}

    async def fetch_spot_perp(self, exchanges: List[str]) -> List[ArbitrageOpportunity]:
        url = f"{self.BASE_URL}/spot-perp"
        all_opps = []
        for ex in exchanges:
            params = {"exchange": sharpe_display_name(ex)}
            opps = await self._fetch_and_map(url, params, OpportunityType.SPOT_PERP, self._map_spot_perp)
            all_opps.extend(opps)
        return all_opps

    async def fetch_cross_exchange(self, exchanges: List[str]) -> List[ArbitrageOpportunity]:
        url = f"{self.BASE_URL}/cross-exchange"
        # all_opps = []
        # for ex in exchanges:
        params = {"exchanges": ",".join([sharpe_display_name(ex) for ex in exchanges])}
        opps = await self._fetch_and_map(url, params, OpportunityType.CROSS_EXCHANGE, self._map_cross_exchange)
        # all_opps.extend(opps)
        return opps

    async def _fetch_and_map(self, url: str, params: Dict[str, str], opp_type: OpportunityType, mapper) -> List[ArbitrageOpportunity]:
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, params=params, timeout=10) as resp:
                    if resp.status != 200:
                        logger.error(f"Failed to fetch {opp_type.value}: {resp.status} {await resp.text()}")
                        return []
                    data = await resp.json()
                    # Sharpe API sometimes wraps rows in data.rows, sometimes just data
                    rows = data.get("data", [])
                    if isinstance(rows, dict) and "rows" in rows:
                        rows = rows["rows"]
                    
                    opps = []
                    for row in rows:
                        try:
                            opp = mapper(row)
                            if opp:
                                opps.append(opp)
                        except Exception as e:
                            logger.error(f"Error parsing {opp_type.value} row: {e}")
                    return opps
        except Exception as e:
            logger.error(f"Request error for {opp_type.value}: {e}")
            return []

    @staticmethod
    def _parse_apr_pct(row: Dict[str, Any], keys: tuple) -> Optional[float]:
        """Normalize an APR-ish field into PERCENT units, strictly.

        Bug #4 fix. Rules:
        - a field whose name ends with 'pct' is already percent  -> use as-is
        - bare 'apr'/'netApr' fields are decimals                -> x100
        - magnitude guardrail: |pct| > MAX_PLAUSIBLE_APR_PCT      -> reject row (None)

        The old heuristic `net_apr * 100 if net_apr < 10 else net_apr` mis-scaled
        percent-denominated fields < 10 (netAprPct 11.2267 -> '1122.67% Net APR'
        harvest lines seen in logs) and always mis-scaled negatives.
        """
        for key in keys:
            if key in row and row[key] is not None:
                try:
                    val = float(row[key])
                except (TypeError, ValueError):
                    continue
                pct = val if key.lower().endswith("pct") else val * 100.0
                if abs(pct) > MAX_PLAUSIBLE_APR_PCT:
                    logger.error(
                        f"Implausible APR {pct:.2f}% from field '{key}' (raw={row[key]}) - row rejected"
                    )
                    return None
                return pct
        return 0.0

    def _map_spot_perp(self, row: Dict[str, Any]) -> Optional[ArbitrageOpportunity]:
        # {"symbol": "DOGE", "exchange": "Binance", "portfolio": {"spotSide": "buy", "perpSide": "short"}, "netApr": 0.3464, ...}
        coin = row["symbol"]
        exchange = canonical_venue(row["exchange"])
        portfolio = row.get("portfolio", {})
        
        spot_side = portfolio.get("spotSide", "buy")
        perp_side = "sell" if portfolio.get("perpSide", "short") == "short" else "buy"

        legs = [
            HedgeLeg(exchange=exchange, symbol=f"{coin}/USDT", side=spot_side, asset_type="spot", leverage=1.0),
            HedgeLeg(exchange=exchange, symbol=f"{coin}/USDT:USDT", side=perp_side, asset_type="perp", leverage=1.0)
        ]
        
        net_apr_pct = self._parse_apr_pct(row, ("netApr",))
        if net_apr_pct is None:
            return None

        return ArbitrageOpportunity(
            id=f"spot_perp_{exchange}_{coin}",
            type=OpportunityType.SPOT_PERP,
            coin=coin,
            legs=legs,
            net_apr_pct=net_apr_pct,
            raw_data=row,
            next_event_time=row.get("nextFundingTime")
        )

    def _map_cross_exchange(self, row: Dict[str, Any]) -> Optional[ArbitrageOpportunity]:
        # {"symbol": "DOGE", "longExchange": "Gate.io", "shortExchange": "Binance", "netApr": 0.657, ...}
        coin = row["symbol"]
        long_ex = canonical_venue(row["longExchange"])
        short_ex = canonical_venue(row["shortExchange"])

        legs = [
            HedgeLeg(exchange=long_ex, symbol=f"{coin}/USDT:USDT", side="buy", asset_type="perp", leverage=1.0),
            HedgeLeg(exchange=short_ex, symbol=f"{coin}/USDT:USDT", side="sell", asset_type="perp", leverage=1.0)
        ]

        # Strict APR normalization (Bug #4 fix) - no decimal/percent guessing
        net_apr_pct = self._parse_apr_pct(row, ("netApr", "apr"))
        if net_apr_pct is None:
            return None

        return ArbitrageOpportunity(
            id=f"cross_perp_{long_ex}_{short_ex}_{coin}",
            type=OpportunityType.CROSS_EXCHANGE,
            coin=coin,
            legs=legs,
            net_apr_pct=net_apr_pct,
            raw_data=row,
            next_event_time=row.get("nextSettlement")
        )

    async def fetch_dex_scanner_preview(self, pool_url: str, exchanges: List[str], min_profit_pct: float = 1.0, mode: str = "cex_dex", second_pool_url: Optional[str] = None) -> List[ArbitrageOpportunity]:
        url = f"{self.BASE_URL}/dex-scanner/preview"
        params = {
            "poolUrl": pool_url,
            "exchanges": ",".join([sharpe_display_name(ex) for ex in exchanges]),
            "minProfitPct": str(min_profit_pct),
            "mode": mode,
        }
        if mode == "dex_dex" and second_pool_url:
            params["secondPoolUrl"] = second_pool_url

        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, params=params, timeout=10) as resp:
                    if resp.status != 200:
                        logger.error(f"Failed to fetch dex-scanner preview: {resp.status} {await resp.text()}")
                        return []
                    data = await resp.json()
                    
                    data_dict = data.get("data", {})
                    opportunities = data_dict.get("opportunities", [])
                    
                    opps = []
                    for row in opportunities:
                        try:
                            opp = self._map_dex_scanner(row, mode)
                            if opp:
                                opps.append(opp)
                        except Exception as e:
                            logger.error(f"Error parsing dex scanner row: {e}")
                    return opps
        except Exception as e:
            logger.error(f"Request error for dex-scanner preview: {e}")
            return []

    def _map_dex_scanner(self, row: Dict[str, Any], mode: str) -> Optional[ArbitrageOpportunity]:
        coin = row.get("token", "UNKNOWN")
        buy_venue = canonical_venue(row.get("buyVenue", ""))
        sell_venue = canonical_venue(row.get("sellVenue", ""))
        
        legs = [
            HedgeLeg(exchange=buy_venue, symbol=f"{coin}/USDT", side="buy", asset_type="spot", leverage=1.0),
            HedgeLeg(exchange=sell_venue, symbol=f"{coin}/USDT", side="sell", asset_type="spot", leverage=1.0)
        ]
        
        opp_type = OpportunityType.CEX_DEX if mode == "cex_dex" else OpportunityType.DEX_DEX
        
        net_apr_pct = self._parse_apr_pct(row, ("spreadPct",))
        if net_apr_pct is None:
            return None

        return ArbitrageOpportunity(
            id=f"{opp_type.value}_{buy_venue}_{sell_venue}_{coin}",
            type=opp_type,
            coin=coin,
            legs=legs,
            net_apr_pct=net_apr_pct,
            raw_data=row
        )

    async def fetch_cross_spot(self, exchanges: List[str]) -> List[ArbitrageOpportunity]:
        url = f"{self.BASE_URL}/cex-spot-transfer"
        # all_opps = []
        # for ex in exchanges:
        params = {"exchange": ",".join([sharpe_display_name(ex) for ex in exchanges])}
        opps = await self._fetch_and_map(url, params, OpportunityType.CEX_SPOT_TRANSFER, self._map_cross_spot)
        # all_opps.extend(opps)
        return opps

    def _map_cross_spot(self, row: Dict[str, Any]) -> Optional[ArbitrageOpportunity]:
        coin = row.get("symbol", "UNKNOWN")
        buy_ex = canonical_venue(row.get("buyExchange", "UNKNOWN"))
        sell_ex = canonical_venue(row.get("sellExchange", "UNKNOWN"))

        legs = [
            HedgeLeg(exchange=buy_ex, symbol=f"{coin}/USDT", side="buy", asset_type="spot", leverage=1.0),
            HedgeLeg(exchange=sell_ex, symbol=f"{coin}/USDT", side="sell", asset_type="spot", leverage=1.0)
        ]

        net_apr_pct = self._parse_apr_pct(row, ("netApr", "apr"))
        if net_apr_pct is None:
            return None
        return ArbitrageOpportunity(
            id=f"cross_spot_{buy_ex}_{sell_ex}_{coin}",
            type=OpportunityType.CEX_SPOT_TRANSFER,
            coin=coin,
            legs=legs,
            net_apr_pct=net_apr_pct,
            raw_data=row
        )

    async def fetch_dated_futures_carry(self, exchanges: List[str]) -> List[ArbitrageOpportunity]:
        url = f"{self.BASE_URL}/dated-futures-basis"
        # all_opps = []
        # for ex in exchanges:
        params = {"exchanges": ",".join([sharpe_display_name(ex) for ex in exchanges])}
        opps = await self._fetch_and_map(url, params, OpportunityType.DATED_FUTURES_BASIS, self._map_futures_carry)
        # all_opps.extend(opps)
        return opps

    def _map_futures_carry(self, row: Dict[str, Any]) -> Optional[ArbitrageOpportunity]:
        coin = row.get("coin", "UNKNOWN")
        exchange = canonical_venue(row.get("spotVenue", "UNKNOWN"))
        expiry = row.get("expiry", "") # e.g., "2026-06-26"
        
        legs = [
            HedgeLeg(exchange=exchange, symbol=f"{coin}/USDT", side="buy", asset_type="spot", leverage=1.0),
            HedgeLeg(exchange=exchange, symbol=f"{coin}/USDT-{expiry}", side="sell", asset_type="future", leverage=1.0, expiry=expiry)
        ]

        # netAprPct is ALREADY percent (root cause of the logged '1122.67% Net APR');
        # _parse_apr_pct respects the Pct suffix instead of re-multiplying.
        net_apr_pct = self._parse_apr_pct(row, ("netAprPct", "apr"))
        if net_apr_pct is None:
            return None
        return ArbitrageOpportunity(
            id=f"futures_carry_{exchange}_{coin}_{expiry}",
            type=OpportunityType.DATED_FUTURES_BASIS,
            coin=coin,
            legs=legs,
            net_apr_pct=net_apr_pct,
            raw_data=row,
            next_event_time=expiry
        )

    async def fetch_calendar_spreads(self, exchanges: List[str]) -> List[ArbitrageOpportunity]:
        url = f"{self.BASE_URL}/futures-calendar-spread"
        # all_opps = []
        # for ex in exchanges:
        params = {"exchanges": ",".join([sharpe_display_name(ex) for ex in exchanges])}
        opps = await self._fetch_and_map(url, params, OpportunityType.CALENDAR_SPREAD, self._map_calendar_spread)
        # all_opps.extend(opps)
        return opps

    def _map_calendar_spread(self, row: Dict[str, Any]) -> Optional[ArbitrageOpportunity]:
        coin = row.get("symbol", "UNKNOWN")
        exchange = canonical_venue(row.get("exchange", "UNKNOWN"))
        front_expiry = row.get("frontExpiry", "")
        back_expiry = row.get("backExpiry", "")
        
        legs = [
            HedgeLeg(exchange=exchange, symbol=f"{coin}/USDT-{front_expiry}", side="buy", asset_type="future", leverage=1.0, expiry=front_expiry),
            HedgeLeg(exchange=exchange, symbol=f"{coin}/USDT-{back_expiry}", side="sell", asset_type="future", leverage=1.0, expiry=back_expiry)
        ]

        net_apr_pct = self._parse_apr_pct(row, ("netRollApyPct", "apr"))
        if net_apr_pct is None:
            return None
        return ArbitrageOpportunity(
            id=f"calendar_{exchange}_{coin}_{front_expiry}_{back_expiry}",
            type=OpportunityType.CALENDAR_SPREAD,
            coin=coin,
            legs=legs,
            net_apr_pct=net_apr_pct,
            raw_data=row,
            next_event_time=front_expiry # Front expiry is the first critical date
        )
