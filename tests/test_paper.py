import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from kalshi_mm.paper import PaperRecorder
from kalshi_mm.scanner import Discrepancy


def opportunity() -> Discrepancy:
    return Discrepancy(
        ticker="MARKET",
        event_ticker="EVENT",
        outcome="Team A",
        odds_event_id="odds-event",
        fair_probability=Decimal("0.60"),
        yes_bid=Decimal("0.52"),
        yes_ask=Decimal("0.54"),
        midpoint=Decimal("0.53"),
        action="BUY YES",
        edge=Decimal("0.06"),
        bookmaker_count=5,
        match_score=0.95,
    )


def test_paper_recorder_logs_signal_then_markout(tmp_path) -> None:
    path = tmp_path / "paper.jsonl"
    recorder = PaperRecorder(
        path,
        markout_horizons_seconds=(60,),
        signal_cooldown_seconds=300,
    )
    started = datetime(2026, 7, 4, 20, tzinfo=UTC)

    first = recorder.update([opportunity()], now=started)
    second = recorder.update(
        [replace(opportunity(), midpoint=Decimal("0.57"))],
        now=started + timedelta(seconds=61),
    )

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert first.signals_recorded == 1
    assert second.signals_recorded == 0
    assert second.markouts_recorded == 1
    assert records[0]["record_type"] == "signal"
    assert records[1]["record_type"] == "markout"
    assert Decimal(records[1]["markout"]) == Decimal("0.03")


def test_paper_recorder_marks_make_bid_as_long_entry(tmp_path) -> None:
    path = tmp_path / "paper.jsonl"
    recorder = PaperRecorder(path, markout_horizons_seconds=(60,))
    started = datetime(2026, 7, 4, 20, tzinfo=UTC)
    make_bid = replace(
        opportunity(),
        action="MAKE BID",
        edge=Decimal("0.015"),
    )

    recorder.update([make_bid], now=started)
    update = recorder.update(
        [replace(make_bid, midpoint=Decimal("0.55"))],
        now=started + timedelta(seconds=61),
    )

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert update.markouts_recorded == 1
    assert records[0]["entry_price"] == "0.52"
    assert Decimal(records[1]["markout"]) == Decimal("0.03")
