from core.legacy_models import BasisPair, BookQuote, MarketSymbol, TradeDirection
from yld.net_calculator import NetYieldCalculator


def _quote(symbol, side, px):
    return BookQuote(symbol, side, 100, 100, 1, px, px, px, 4_102_444_800.0)


def test_builds_contango_signal_when_perp_is_rich():
    pair = BasisPair(
        perp=MarketSymbol("BTC", "BTC", 5, 6, False),
        spot=MarketSymbol("BTC/USDC", "BTC/USDC", 5, 8, True),
    )
    calc = NetYieldCalculator(taker_fee_pct=0.0001, exit_fee_multiplier=2)

    signal = calc.build_signal(
        pair,
        spot_buy=_quote("BTC/USDC", "buy", 100),
        spot_sell=_quote("BTC/USDC", "sell", 99),
        perp_buy=_quote("BTC", "buy", 104),
        perp_sell=_quote("BTC", "sell", 105),
        funding_rate_pct=0.0,
        holding_time_hours=1,
        notional_usd=100,
        threshold_pct=0.01,
    )

    assert signal is not None
    assert signal.direction == TradeDirection.CONTANGO
    assert signal.net_yield_pct > 0.01


def test_returns_none_below_threshold():
    pair = BasisPair(
        perp=MarketSymbol("BTC", "BTC", 5, 6, False),
        spot=MarketSymbol("BTC/USDC", "BTC/USDC", 5, 8, True),
    )
    calc = NetYieldCalculator(taker_fee_pct=0.001, exit_fee_multiplier=2)

    signal = calc.build_signal(
        pair,
        spot_buy=_quote("BTC/USDC", "buy", 100),
        spot_sell=_quote("BTC/USDC", "sell", 99.9),
        perp_buy=_quote("BTC", "buy", 100.1),
        perp_sell=_quote("BTC", "sell", 100.2),
        funding_rate_pct=0.0,
        holding_time_hours=1,
        notional_usd=100,
        threshold_pct=0.01,
    )

    assert signal is None
