from data.local_book import LocalOrderBook


def test_quote_walks_asks_for_buy_vwap():
    book = LocalOrderBook()
    book.process_l2_update(
        {
            "coin": "BTC",
            "time": 1_700_000_000_000,
            "levels": [
                [{"px": "99", "sz": "10"}],
                [{"px": "100", "sz": "1"}, {"px": "110", "sz": "10"}],
            ],
        }
    )

    quote = book.get_quote("BTC", "buy", 155.0)

    assert quote.is_complete
    assert round(quote.base_size, 6) == round(1 + 55 / 110, 6)
    assert round(quote.vwap, 6) == round(155 / 1.5, 6)
    assert quote.best_px == 100
    assert quote.worst_px == 110


def test_quote_returns_incomplete_when_depth_is_thin():
    book = LocalOrderBook()
    book.process_l2_update(
        {
            "coin": "ETH",
            "levels": [
                [{"px": "100", "sz": "0.1"}],
                [{"px": "101", "sz": "0.1"}],
            ],
        }
    )

    quote = book.get_quote("ETH", "sell", 100.0)

    assert not quote.is_complete
    assert quote.vwap == 0
