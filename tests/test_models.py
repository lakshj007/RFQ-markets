from decimal import Decimal

import pytest

from kalshi_mm.models import OrderBook, PriceGrid, PriceRange


def test_orderbook_converts_no_bids_to_yes_asks() -> None:
    book = OrderBook.from_api(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.10", "10"], ["0.27", "150"]],
                "no_dollars": [["0.02", "100"], ["0.35", "30"]],
            }
        }
    )

    assert book.best_bid.price == Decimal("0.27")
    assert book.best_bid.size == Decimal("150")
    assert book.best_ask.price == Decimal("0.65")
    assert book.best_ask.size == Decimal("30")
    assert book.spread == Decimal("0.38")
    assert book.midpoint == Decimal("0.46")
    assert book.microprice == pytest.approx(Decimal("0.5866666667"))


def test_orderbook_rejects_wrong_shape() -> None:
    with pytest.raises(ValueError, match="orderbook_fp"):
        OrderBook.from_api({"markets": []})


def test_price_grid_uses_market_price_ranges() -> None:
    grid = PriceGrid.from_market(
        {
            "price_ranges": [
                {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
                {"start": "0.1000", "end": "1.0000", "step": "0.0100"},
            ]
        }
    )

    assert grid.floor(Decimal("0.0559")) == Decimal("0.0550")
    assert grid.ceil(Decimal("0.0551")) == Decimal("0.0560")
    assert grid.floor(Decimal("0.567")) == Decimal("0.5600")
    assert grid.ceil(Decimal("0.567")) == Decimal("0.5700")


def test_price_grid_rounds_in_the_requested_direction_across_a_range_gap() -> None:
    grid = PriceGrid(
        (
            PriceRange(Decimal("0"), Decimal("0.50"), Decimal("0.01")),
            PriceRange(Decimal("0.52"), Decimal("1"), Decimal("0.01")),
        )
    )

    assert grid.floor(Decimal("0.51")) == Decimal("0.50")
    assert grid.ceil(Decimal("0.51")) == Decimal("0.52")
