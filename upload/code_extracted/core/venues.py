"""Canonical venue identity handling.

Fix for Bug #2 (exchange-name case mismatch): Sharpe returns display names like
"Gate.io" while the engine keys clients/config by lowercase ccxt-style ids
("gate", "binance", ...). Every venue string entering the engine from an external
feed must pass through canonical_venue(); every outbound API param that needs a
display name must use sharpe_display_name().
"""

# display/legacy names -> engine canonical id
VENUE_ALIASES = {
    "binance": "binance",
    "bybit": "bybit",
    "okx": "okx",
    "okex": "okx",
    "gate": "gate",
    "gate.io": "gate",
    "gateio": "gate",
    "hyperliquid": "hyperliquid",
    "bitget": "bitget",
    "kucoin": "kucoin",
    "kucoinfutures": "kucoinfutures",
    "mexc": "mexc",
    "htx": "htx",
    "huobi": "htx",
    "aster": "aster",
    "asterdex": "aster",
    "dydx": "dydx",
    "dydxv4": "dydx",
    "paradex": "paradex",
}


def canonical_venue(name: str) -> str:
    """Map any external venue spelling to the engine's canonical lowercase id.

    Unknown venues are returned lowercased with dots/dashes collapsed so the
    router's .lower() lookup remains predictable; lookups will simply miss and
    log 'No client found' instead of raising."""
    if not name:
        return ""
    key = str(name).strip().lower().replace(" ", "")
    if key in VENUE_ALIASES:
        return VENUE_ALIASES[key]
    return key.replace(".", "").replace("-", "")


def sharpe_display_name(engine_id: str) -> str:
    """Engine id -> Sharpe API display name (their API is case-sensitive:
    'Binance', 'OKX', 'Gate.io', ...). Note str.title() on 'gate.io' yields
    'Gate.Io' which the API does not recognize - hence this explicit map."""
    key = str(engine_id).strip().lower()
    display = {
        "binance": "Binance",
        "bybit": "Bybit",
        "okx": "OKX",
        "okex": "OKX",
        "gate": "Gate.io",
        "gateio": "Gate.io",
        "hyperliquid": "Hyperliquid",
        "bitget": "Bitget",
        "kucoin": "KuCoin",
        "mexc": "MEXC",
        "htx": "HTX",
        "huobi": "HTX",
        "aster": "Aster",
        "dydx": "dYdX",
        "paradex": "Paradex",
    }
    return display.get(key, engine_id.upper() if len(engine_id) <= 4 else engine_id.title())
