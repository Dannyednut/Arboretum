#!/usr/bin/env python3
"""v3_settings.py - P4b key/config scaffold (Part 19 s.4 settings pattern).

pydantic-settings + dotenv, ported from the reference implementation
(upload/code_extracted/config/settings.py) with v3-specific hardening:

  * keys live in <project root>/.env (gitignored) or process env;
    env vars win over .env (12-factor precedence)
  * secrets are pydantic SecretStr -> repr/log-safe by construction
  * per-venue credential helpers: unset -> None, incomplete -> error,
    okx carries a passphrase (3 fields)
  * mode-consistency validators: YES_REAL requires live + DRY_RUN=false
    (P4a keeps real placement unreachable regardless of keys)
  * credentials_status() feeds ops readiness checks; nothing here ever
    prints a secret

Usage:
    from v3_settings import get_settings, require_credentials, mask
    s = get_settings()               # cached
    creds = s.credentials("binance") # CredentialSet | None
"""
import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
EXAMPLE_FILE = ROOT / ".env.example"

VENUES = ("binance", "aster", "okx", "bingx", "nado", "bybit",
          "bitget", "hl", "dydx", "backpack", "orderly", "gate",
          "kucoin", "bitmex", "deribit", "htx", "paradex", "mexc",
          "lighter", "extended", "edgex", "apex", "drift", "kraken",
          "blofin", "whitebit", "toobit", "weex", "cbintl")
# fields per venue; 3-field venues carry a key-generated passphrase;
# DEX scoped credentials use their native names (Part 24 guide)
VENUE_FIELDS = {
    "binance": ("BINANCE_API_KEY", "BINANCE_API_SECRET"),
    "aster": ("ASTER_API_KEY", "ASTER_API_SECRET"),
    "okx": ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"),
    "bingx": ("BINGX_API_KEY", "BINGX_API_SECRET"),
    "nado": ("NADO_API_KEY", "NADO_API_SECRET"),
    "bybit": ("BYBIT_API_KEY", "BYBIT_API_SECRET"),
    "bitget": ("BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_PASSPHRASE"),
    "hl": ("HL_AGENT_ADDRESS", "HL_AGENT_PRIVATE_KEY"),
    "dydx": ("DYDX_MNEMONIC",),
    "backpack": ("BACKPACK_API_KEY", "BACKPACK_API_SECRET"),
    "orderly": ("ORDERLY_KEY", "ORDERLY_SECRET"),
    "gate": ("GATE_API_KEY", "GATE_API_SECRET"),
    "kucoin": ("KUCOIN_API_KEY", "KUCOIN_API_SECRET", "KUCOIN_PASSPHRASE"),
    "bitmex": ("BITMEX_API_KEY", "BITMEX_API_SECRET"),
    "deribit": ("DERIBIT_CLIENT_ID", "DERIBIT_CLIENT_SECRET"),
    "htx": ("HTX_ACCESS_KEY", "HTX_SECRET_KEY"),
    "paradex": ("PARADEX_PRIVATE_KEY",),
    "mexc": ("MEXC_API_KEY", "MEXC_API_SECRET"),
    "lighter": ("LIGHTER_API_PRIVATE_KEY", "LIGHTER_ACCOUNT_INDEX",
                "LIGHTER_API_KEY_INDEX"),
    "extended": ("EXTENDED_API_KEY", "EXTENDED_API_SECRET"),
    "edgex": ("EDGEX_L2_PRIVATE_KEY",),
    "apex": ("APEX_API_KEY", "APEX_API_SECRET"),
    "drift": ("DRIFT_KEYPAIR_B58",),
    "kraken": ("KRAKEN_API_KEY", "KRAKEN_API_SECRET"),
    "blofin": ("BLOFIN_API_KEY", "BLOFIN_API_SECRET", "BLOFIN_PASSPHRASE"),
    "whitebit": ("WHITEBIT_API_KEY", "WHITEBIT_API_SECRET"),
    "toobit": ("TOOBIT_API_KEY", "TOOBIT_API_SECRET"),
    "weex": ("WEEX_API_KEY", "WEEX_API_SECRET"),
    "cbintl": ("CBINTL_API_KEY", "CBINTL_API_SECRET"),
}


def mask(secret: str, keep: int = 4) -> str:
    """Log-safe rendering: first `keep` chars + length hint, never the rest."""
    s = str(secret or "")
    if not s:
        return "<unset>"
    if len(s) <= keep:
        return "*" * len(s)
    return f"{s[:keep]}***({len(s)}ch)"


class CredentialSet(BaseModel):
    venue: str
    fields: dict           # name -> SecretStr

    def model_repr_safe(self) -> str:
        parts = ", ".join(f"{k}={mask(v.get_secret_value())}"
                          for k, v in self.fields.items())
        return f"CredentialSet({self.venue}: {parts})"

    def __repr__(self):     # hard guarantee: no secret ever in repr
        return self.model_repr_safe()

    __str__ = __repr__


class V3Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), case_sensitive=True, extra="ignore")

    # ---- mode / safety ---------------------------------------------------
    EXEC_MODE: str = Field(default="paper")
    DRY_RUN: bool = Field(default=True)
    YES_REAL: bool = Field(default=False)

    # ---- pilot sizing gates (Part 19 s.7/8) ------------------------------
    P4B_PILOT_USD_PER_LEG: float = Field(default=50.0, gt=0)
    MAX_NOTIONAL_USD: float = Field(default=1000.0, gt=0)

    # ---- venue credentials (trade-only; see .env.example contract) -------
    BINANCE_API_KEY: str = Field(default="")
    BINANCE_API_SECRET: str = Field(default="")
    ASTER_API_KEY: str = Field(default="")
    ASTER_API_SECRET: str = Field(default="")
    OKX_API_KEY: str = Field(default="")
    OKX_API_SECRET: str = Field(default="")
    OKX_PASSPHRASE: str = Field(default="")
    BINGX_API_KEY: str = Field(default="")
    BINGX_API_SECRET: str = Field(default="")
    NADO_API_KEY: str = Field(default="")
    NADO_API_SECRET: str = Field(default="")
    # ---- P5d venue expansion (Part 24; trade-only, see .env.example) ------
    BYBIT_API_KEY: str = Field(default="")
    BYBIT_API_SECRET: str = Field(default="")
    BITGET_API_KEY: str = Field(default="")
    BITGET_API_SECRET: str = Field(default="")
    BITGET_PASSPHRASE: str = Field(default="")
    HL_AGENT_ADDRESS: str = Field(default="")
    HL_AGENT_PRIVATE_KEY: str = Field(default="")
    DYDX_MNEMONIC: str = Field(default="")
    BACKPACK_API_KEY: str = Field(default="")
    BACKPACK_API_SECRET: str = Field(default="")
    ORDERLY_KEY: str = Field(default="")
    ORDERLY_SECRET: str = Field(default="")
    GATE_API_KEY: str = Field(default="")
    GATE_API_SECRET: str = Field(default="")
    KUCOIN_API_KEY: str = Field(default="")
    KUCOIN_API_SECRET: str = Field(default="")
    KUCOIN_PASSPHRASE: str = Field(default="")
    BITMEX_API_KEY: str = Field(default="")
    BITMEX_API_SECRET: str = Field(default="")
    DERIBIT_CLIENT_ID: str = Field(default="")
    DERIBIT_CLIENT_SECRET: str = Field(default="")
    HTX_ACCESS_KEY: str = Field(default="")
    HTX_SECRET_KEY: str = Field(default="")
    PARADEX_PRIVATE_KEY: str = Field(default="")
    MEXC_API_KEY: str = Field(default="")
    MEXC_API_SECRET: str = Field(default="")
    LIGHTER_API_PRIVATE_KEY: str = Field(default="")
    LIGHTER_ACCOUNT_INDEX: str = Field(default="")
    LIGHTER_API_KEY_INDEX: str = Field(default="")
    EXTENDED_API_KEY: str = Field(default="")
    EXTENDED_API_SECRET: str = Field(default="")
    EDGEX_L2_PRIVATE_KEY: str = Field(default="")
    APEX_API_KEY: str = Field(default="")
    APEX_API_SECRET: str = Field(default="")
    DRIFT_KEYPAIR_B58: str = Field(default="")
    KRAKEN_API_KEY: str = Field(default="")
    KRAKEN_API_SECRET: str = Field(default="")
    BLOFIN_API_KEY: str = Field(default="")
    BLOFIN_API_SECRET: str = Field(default="")
    BLOFIN_PASSPHRASE: str = Field(default="")
    WHITEBIT_API_KEY: str = Field(default="")
    WHITEBIT_API_SECRET: str = Field(default="")
    TOOBIT_API_KEY: str = Field(default="")
    TOOBIT_API_SECRET: str = Field(default="")
    WEEX_API_KEY: str = Field(default="")
    WEEX_API_SECRET: str = Field(default="")
    CBINTL_API_KEY: str = Field(default="")
    CBINTL_API_SECRET: str = Field(default="")

    @field_validator("EXEC_MODE")
    @classmethod
    def _mode(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("paper", "live"):
            raise ValueError("EXEC_MODE must be 'paper' or 'live'")
        return v

    @field_validator("YES_REAL")
    @classmethod
    def _real_consistency(cls, v: bool, info) -> bool:
        if v:
            data = info.data
            if data.get("EXEC_MODE") != "live" or data.get("DRY_RUN"):
                raise ValueError(
                    "YES_REAL=true requires EXEC_MODE=live AND DRY_RUN=false "
                    "(real orders are a P4b decision, never implicit)")
        return v

    # ---- credential access -----------------------------------------------
    def credentials(self, venue: str):
        """CredentialSet for a venue, or None when fully unset.
        Partially-filled venues raise (silent truncation is how legs get
        stranded - fail loud instead)."""
        names = VENUE_FIELDS.get(venue)
        if not names:
            raise ValueError(f"unknown venue {venue!r} (known: {VENUES})")
        vals = {n: (getattr(self, n) or "").strip() for n in names}
        if not any(vals.values()):
            return None
        missing = [n for n, v in vals.items() if not v]
        if missing:
            raise ValueError(
                f"{venue}: incomplete credentials - missing {missing}; "
                f"fill them in {ENV_FILE} (template: .env.example) or env")
        return CredentialSet(
            venue=venue,
            fields={n: _secret(v) for n, v in vals.items()})

    def require_credentials(self, venue: str) -> CredentialSet:
        """P4b real-mode gate: like credentials() but None is an error."""
        c = self.credentials(venue)
        if c is None:
            raise ValueError(
                f"{venue}: no credentials configured - fill {ENV_FILE} "
                f"(template: .env.example). Keys must be trade-only, "
                f"withdrawals DISABLED, IP-allowlisted.")
        return c

    def credentials_status(self) -> dict:
        """{venue: bool} readiness map for the status command."""
        out = {}
        for v in VENUES:
            try:
                out[v] = self.credentials(v) is not None
            except ValueError:
                out[v] = False          # incomplete counts as not ready
        return out


def _secret(v: str):
    from pydantic import SecretStr
    return SecretStr(v)


@lru_cache(maxsize=1)
def get_settings() -> V3Settings:
    return V3Settings()


def example_is_loadable() -> bool:
    """CI guard: the committed template must always parse."""
    vals = dotenv_values(EXAMPLE_FILE)
    V3Settings(**{k: v for k, v in vals.items() if v is not None},
               _env_file=None)
    return True


def missing_from_example() -> dict:
    """Fields present in VENUE_FIELDS/mode but absent from the template."""
    vals = dotenv_values(EXAMPLE_FILE)
    want = ["EXEC_MODE", "DRY_RUN", "YES_REAL", "P4B_PILOT_USD_PER_LEG",
            "MAX_NOTIONAL_USD"] + [n for ns in VENUE_FIELDS.values()
                                   for n in ns]
    return [w for w in want if w not in vals]


if __name__ == "__main__":
    s = get_settings()
    print(f"EXEC_MODE={s.EXEC_MODE} DRY_RUN={s.DRY_RUN} "
          f"YES_REAL={s.YES_REAL}")
    print(f"credentials_status: {s.credentials_status()}")
    print(f".env.example loadable: {example_is_loadable()}")
    gaps = missing_from_example()
    print(f"template gaps: {gaps or 'none'}")
