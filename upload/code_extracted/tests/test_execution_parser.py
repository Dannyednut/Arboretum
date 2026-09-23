"""
Tests for the Hyperliquid execution response parser.
"""
from config.settings import EngineSettings
from core.models import OrderLegResult
from execution.hl_adapter import HyperliquidExecutionAdapter


def test_parse_order_response_handles_filled_and_error():
    adapter = HyperliquidExecutionAdapter(EngineSettings(DRY_RUN=True))
    requested = [
        OrderLegResult(exchange="hyperliquid", symbol="BTC/USDC", requested_size=0.1, requested_px=100.0),
        OrderLegResult(exchange="hyperliquid", symbol="BTC", requested_size=0.1, requested_px=101.0),
    ]
    raw = {
        "status": "ok",
        "response": {
            "data": {
                "statuses": [
                    {"filled": {"totalSz": "0.1", "avgPx": "100", "oid": 1}},
                    {"error": "Insufficient margin"},
                ]
            }
        },
    }

    legs = adapter.parse_order_response(raw, requested)

    assert legs[0].status == "filled"
    assert legs[0].filled_size == 0.1
    assert legs[0].exchange == "hyperliquid"
    assert legs[1].status == "error"
    assert legs[1].error == "Insufficient margin"


def test_parse_order_response_handles_missing_statuses():
    """If the API returns fewer statuses than legs, missing ones get 'missing' status."""
    adapter = HyperliquidExecutionAdapter(EngineSettings(DRY_RUN=True))
    requested = [
        OrderLegResult(exchange="hyperliquid", symbol="BTC/USDC", requested_size=0.1, requested_px=100.0),
        OrderLegResult(exchange="hyperliquid", symbol="BTC", requested_size=0.1, requested_px=101.0),
    ]
    raw = {
        "status": "ok",
        "response": {"data": {"statuses": [
            {"filled": {"totalSz": "0.1", "avgPx": "100", "oid": 1}},
            # Second leg missing
        ]}},
    }

    legs = adapter.parse_order_response(raw, requested)

    assert len(legs) == 2
    assert legs[0].status == "filled"
    assert legs[1].status == "missing"
