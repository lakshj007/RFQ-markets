import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from kalshi_mm.maker_paper import MakerPaperRecorder
from kalshi_mm.scanner import Discrepancy


def snapshot() -> Discrepancy:
    return Discrepancy(
        ticker="MARKET",
        event_ticker="EVENT",
        outcome="Tie",
        odds_event_id="odds-event",
        fair_probability=Decimal("0.265"),
        yes_bid=Decimal("0.24"),
        yes_ask=Decimal("0.30"),
        midpoint=Decimal("0.27"),
        action="NONE",
        edge=Decimal("-0.035"),
        bookmaker_count=3,
        match_score=0.98,
        yes_bid_size=Decimal("20"),
        yes_ask_size=Decimal("30"),
    )


class FakeTrades:
    def __init__(self, items: list[dict] | None = None) -> None:
        self.items = items or []

    def get_trades(self, ticker: str, *, limit: int = 100) -> list[dict]:
        assert ticker == "MARKET"
        assert limit == 100
        return self.items


def trade(
    trade_id: str,
    created_at: datetime,
    *,
    taker_book_side: str,
    price: str,
    count: str = "1",
) -> dict:
    return {
        "trade_id": trade_id,
        "created_time": created_at.isoformat(),
        "taker_book_side": taker_book_side,
        "yes_price_dollars": price,
        "count_fp": count,
        "is_block_trade": False,
    }


def test_maker_paper_persists_quote_and_records_both_fills(tmp_path) -> None:
    output = tmp_path / "maker.jsonl"
    state = tmp_path / "maker-state.json"
    started = datetime(2026, 7, 10, 20, tzinfo=UTC)
    recorder = MakerPaperRecorder(
        output,
        state,
        markout_horizons_seconds=(20,),
    )

    first = recorder.update([snapshot()], trades=FakeTrades(), now=started)

    assert first.quotes_opened == 1
    assert recorder.quotes[0].bid_price == Decimal("0.25")
    assert recorder.quotes[0].ask_price == Decimal("0.29")

    recorder = MakerPaperRecorder(
        output,
        state,
        markout_horizons_seconds=(20,),
    )
    fills = FakeTrades(
        [
            trade(
                "bid-fill",
                started + timedelta(seconds=30),
                taker_book_side="ask",
                price="0.25",
            ),
            trade(
                "ask-fill",
                started + timedelta(seconds=40),
                taker_book_side="bid",
                price="0.29",
            ),
        ]
    )
    second = recorder.update(
        [replace(snapshot(), midpoint=Decimal("0.28"))],
        trades=fills,
        now=started + timedelta(seconds=61),
    )

    records = [json.loads(line) for line in output.read_text().splitlines()]
    completed = next(item for item in records if item["record_type"] == "quote_completed")
    assert second.fills_recorded == 2
    assert second.quotes_completed == 1
    assert second.markouts_recorded == 2
    assert second.quotes_opened == 1
    assert second.active_quotes == 1
    assert Decimal(completed["gross_profit_before_fees"]) == Decimal("0.04")


def test_maker_paper_expires_one_sided_fill_as_inventory(tmp_path) -> None:
    output = tmp_path / "maker.jsonl"
    state = tmp_path / "maker-state.json"
    started = datetime(2026, 7, 10, 20, tzinfo=UTC)
    recorder = MakerPaperRecorder(
        output,
        state,
        quote_lifetime_seconds=60,
        markout_horizons_seconds=(30,),
    )
    recorder.update([snapshot()], trades=FakeTrades(), now=started)

    one_fill = FakeTrades(
        [
            trade(
                "bid-fill",
                started + timedelta(seconds=10),
                taker_book_side="ask",
                price="0.24",
            )
        ]
    )
    update = recorder.update(
        [
            replace(
                snapshot(),
                yes_bid=Decimal("0.22"),
                yes_ask=Decimal("0.24"),
                midpoint=Decimal("0.23"),
            )
        ],
        trades=one_fill,
        now=started + timedelta(seconds=61),
    )

    records = [json.loads(line) for line in output.read_text().splitlines()]
    cancelled = next(item for item in records if item["record_type"] == "quote_cancelled")
    assert update.fills_recorded == 1
    assert update.quotes_completed == 0
    assert update.quotes_cancelled == 1
    assert cancelled["reason"] == "expired"
    assert Decimal(cancelled["inventory"]) == Decimal("1")
    assert Decimal(cancelled["inventory_exit_price"]) == Decimal("0.22")
    assert Decimal(cancelled["inventory_exit_profit"]) == Decimal("-0.03")


def test_maker_paper_rejects_placeholder_wide_spread(tmp_path) -> None:
    recorder = MakerPaperRecorder(
        tmp_path / "maker.jsonl",
        tmp_path / "maker-state.json",
        max_spread=Decimal("0.15"),
    )
    placeholder = replace(
        snapshot(),
        yes_bid=Decimal("0.05"),
        yes_ask=Decimal("0.81"),
        midpoint=Decimal("0.43"),
    )

    update = recorder.update([placeholder], trades=FakeTrades())

    assert update.quotes_opened == 0
    assert update.active_quotes == 0


def test_maker_paper_opens_only_one_complementary_contract_per_event(tmp_path) -> None:
    recorder = MakerPaperRecorder(
        tmp_path / "maker.jsonl",
        tmp_path / "maker-state.json",
        max_open_quotes=5,
    )
    complement = replace(snapshot(), ticker="OTHER", outcome="Other team")

    update = recorder.update([snapshot(), complement], trades=FakeTrades())

    assert update.quotes_opened == 1
    assert update.active_quotes == 1
