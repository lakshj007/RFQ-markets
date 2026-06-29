from decimal import Decimal

from kalshi_mm.display import quote_cycle, table
from kalshi_mm.models import OrderBook
from kalshi_mm.strategy import MarketMakerStrategy, StrategyConfig


def test_table_is_human_readable() -> None:
    output = table(
        ("SIDE", "PRICE", "COUNT"),
        (("BID", "$0.5300", "1"), ("ASK", "$0.5700", "1")),
        right_align={1, 2},
    )

    assert "SIDE" in output
    assert "BID   $0.5300" in output
    assert "ASK   $0.5700" in output
    assert "{" not in output


def test_quote_cycle_labels_dry_run_and_sections() -> None:
    book = OrderBook.from_api(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "100"]],
                "no_dollars": [["0.40", "100"]],
            }
        }
    )
    strategy = MarketMakerStrategy(
        StrategyConfig(
            book_imbalance_weight=Decimal("0"),
            trade_imbalance_weight=Decimal("0"),
        )
    )
    plan = strategy.quote(book=book, fair_probability=Decimal("0.55"))

    output = quote_cycle(
        cycle=1,
        ticker="KX-SAMPLE",
        book=book,
        plan=plan,
        inventory=Decimal("0"),
        execute_demo=False,
    )

    assert "Quote cycle 1 [DRY RUN]" in output
    assert "Best YES bid" in output
    assert "External fair" in output
    assert "Proposed orders" in output

