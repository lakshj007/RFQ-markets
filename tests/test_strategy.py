from decimal import Decimal

import pytest

from kalshi_mm.models import OrderBook
from kalshi_mm.strategy import (
    MarketMakerStrategy,
    StrategyConfig,
    american_odds_to_probability,
    devig_two_way,
    trade_imbalance,
)


@pytest.fixture
def balanced_book() -> OrderBook:
    return OrderBook.from_api(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "100"]],
                "no_dollars": [["0.40", "100"]],
            }
        }
    )


def test_strategy_quotes_around_external_fair(balanced_book: OrderBook) -> None:
    strategy = MarketMakerStrategy(
        StrategyConfig(
            edge=Decimal("0.02"),
            inventory_skew=Decimal("0"),
            book_imbalance_weight=Decimal("0"),
            trade_imbalance_weight=Decimal("0"),
        )
    )

    plan = strategy.quote(book=balanced_book, fair_probability=Decimal("0.55"))

    assert plan.reservation_price == Decimal("0.55")
    assert plan.bid.price == Decimal("0.53")
    assert plan.ask.price == Decimal("0.57")


def test_positive_inventory_skews_quotes_lower(balanced_book: OrderBook) -> None:
    strategy = MarketMakerStrategy(
        StrategyConfig(
            edge=Decimal("0.02"),
            inventory_skew=Decimal("0.01"),
            book_imbalance_weight=Decimal("0"),
            trade_imbalance_weight=Decimal("0"),
        )
    )

    plan = strategy.quote(
        book=balanced_book,
        fair_probability=Decimal("0.55"),
        inventory=Decimal("2"),
    )

    assert plan.reservation_price == Decimal("0.53")
    assert plan.bid.price == Decimal("0.51")
    assert plan.ask.price == Decimal("0.55")


def test_inventory_limit_disables_risk_increasing_side(balanced_book: OrderBook) -> None:
    strategy = MarketMakerStrategy(StrategyConfig(max_inventory=Decimal("5")))

    long_plan = strategy.quote(
        book=balanced_book, fair_probability=Decimal("0.50"), inventory=Decimal("5")
    )
    short_plan = strategy.quote(
        book=balanced_book, fair_probability=Decimal("0.50"), inventory=Decimal("-5")
    )

    assert long_plan.bid is None
    assert long_plan.ask is not None
    assert short_plan.ask is None
    assert short_plan.bid is not None


def test_trade_imbalance_uses_canonical_book_side_and_deduplicates() -> None:
    trades = [
        {"trade_id": "1", "count_fp": "3.5", "taker_book_side": "bid"},
        {"trade_id": "2", "count_fp": "1.5", "taker_book_side": "ask"},
        {"trade_id": "1", "count_fp": "3.5", "taker_book_side": "bid"},
    ]

    assert trade_imbalance(trades) == Decimal("0.4")


def test_moneyline_conversion_and_devig() -> None:
    assert american_odds_to_probability(-110) == pytest.approx(Decimal("0.5238095238"))
    assert devig_two_way(-110, -110) == Decimal("0.5")


def test_invalid_probability_is_rejected(balanced_book: OrderBook) -> None:
    strategy = MarketMakerStrategy(StrategyConfig())
    with pytest.raises(ValueError, match="between 0 and 1"):
        strategy.quote(book=balanced_book, fair_probability=Decimal("1"))
