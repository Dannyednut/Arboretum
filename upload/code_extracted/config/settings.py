from functools import lru_cache
from typing import List

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class TradingPair(BaseModel):
    perp: str
    spot: str
    enabled: bool = True


class EngineSettings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore")

    NETWORK: str = Field(default="mainnet")
    DRY_RUN: bool = Field(default=True)

    ACTIVE_EXCHANGES: str = Field(default="Binance,Bybit,OKX")

    HL_AGENT_PRIVATE_KEY: str = Field(default="")
    HL_MAIN_ACCOUNT_ADDRESS: str = Field(default="")

    BINANCE_API_KEY: str = Field(default="")
    BINANCE_SECRET: str = Field(default="")
    
    BYBIT_API_KEY: str = Field(default="")
    BYBIT_SECRET: str = Field(default="")

    SHARPE_API_KEY: str = Field(default="")

    TRADING_PAIRS: str = Field(default="PURR:PURR/USDC")

    ALLOCATION_PER_TRADE_USD: float = Field(default=100.0, gt=0)
    MAX_POSITION_NOTIONAL_USD: float = Field(default=1_000.0, gt=0)
    MAX_DAILY_NOTIONAL_USD: float = Field(default=5_000.0, gt=0)
    MIN_NET_PROFIT_MARGIN_PCT: float = Field(default=0.000, ge=0)
    MIN_BOOK_DEPTH_MULTIPLIER: float = Field(default=1.25, gt=0)
    MAX_SLIPPAGE_PCT: float = Field(default=0.0025, ge=0)
    MAX_BOOK_AGE_SECONDS: float = Field(default=3.0, gt=0)
    SIGNAL_COOLDOWN_SECONDS: float = Field(default=60.0, ge=0)
    RECONCILE_INTERVAL_SECONDS: float = Field(default=10.0, gt=0)
    WS_CONNECT_TIMEOUT_SECONDS: float = Field(default=15.0, gt=0)
    HEARTBEAT_INTERVAL_SECONDS: float = Field(default=30.0, gt=0)

    TAKER_FEE_PCT: float = Field(default=0.00035, ge=0)
    EXIT_FEE_MULTIPLIER: float = Field(default=2.0, ge=0)
    HOLDING_TIME_HOURS: float = Field(default=1.0, gt=0)

    LOG_LEVEL: str = Field(default="INFO")
    DB_PATH: str = Field(default="bot_data.db")

    MIN_EXIT_SPREAD_CONVERGENCE_PCT: float = Field(default=0.0005, ge=0)
    POSITION_CHECK_INTERVAL_SECONDS: float = Field(default=5.0, gt=0)

    # Stale-opportunity protection (Bug #3 fix): harvest-cycle snapshots older
    # than MAX_OPP_AGE_SECONDS stop refreshing the gate; positions whose signal
    # has not been seen fresh for FORCE_UNWIND_IF_STALE_SECONDS are unwound.
    MAX_OPP_AGE_SECONDS: float = Field(default=90.0, gt=0)
    FORCE_UNWIND_IF_STALE_SECONDS: float = Field(default=900.0, gt=0)

    # Portfolio Manager Settings
    GLOBAL_LIQUIDITY_BUFFER_PCT: float = Field(default=0.20, ge=0.0, le=1.0)
    MAX_MARGIN_UTILIZATION_PCT: float = Field(default=0.85, ge=0.0, le=1.0)
    ALPHA_LEVERAGE: float = Field(default=7.0, ge=1.0)
    SATELLITE_LEVERAGE: float = Field(default=1.0, ge=1.0)

    @field_validator("NETWORK")
    @classmethod
    def validate_network(cls, value: str) -> str:
        normalized = value.lower().strip()
        if normalized not in {"mainnet", "testnet"}:
            raise ValueError("NETWORK must be 'mainnet' or 'testnet'")
        return normalized

    @property
    def trading_pairs(self) -> List[TradingPair]:
        text = self.TRADING_PAIRS.strip()
        if not text:
            return []
        pairs = []
        for raw in text.split(","):
            if not raw.strip():
                continue
            parts = raw.split(":", 1)
            if len(parts) == 2:
                pairs.append(TradingPair(perp=parts[0].strip(), spot=parts[1].strip()))
        return pairs

    @property
    def active_exchanges(self) -> List[str]:
        text = self.ACTIVE_EXCHANGES.strip()
        if not text:
            return []
        return [ex.strip().lower() for ex in text.split(",") if ex.strip()]


@lru_cache(maxsize=1)
def get_settings() -> EngineSettings:
    return EngineSettings()


settings = get_settings()
