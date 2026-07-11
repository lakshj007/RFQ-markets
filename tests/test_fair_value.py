import json
from decimal import Decimal

import pytest

from kalshi_mm.fair_value import JsonFileFairValue, OddsConsensusFairValue, StaticFairValue
from kalshi_mm.odds import DEFAULT_SHARP_BOOKMAKERS
from tests.test_matching import kalshi_event
from tests.test_scanner import _two_book_event


def test_json_fair_value_reloads_file(tmp_path) -> None:
    path = tmp_path / "fair.json"
    path.write_text(json.dumps({"MARKET": 0.54}))
    source = JsonFileFairValue(path)

    assert source.get("MARKET") == Decimal("0.54")
    path.write_text(json.dumps({"MARKET": "0.57"}))
    assert source.get("MARKET") == Decimal("0.57")


def test_fair_value_must_be_probability() -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        StaticFairValue(Decimal("1.1")).get("MARKET")


class FairFakeKalshi:
    def get_market(self, ticker: str) -> dict:
        return {
            "ticker": ticker,
            "event_ticker": "KXMLBGAME-TEST",
            "yes_sub_title": "Toronto",
        }

    def get_event(self, event_ticker: str) -> dict:
        assert event_ticker == "KXMLBGAME-TEST"
        return kalshi_event()


class FairFakeOdds:
    def __init__(self) -> None:
        self.calls = 0
        self.bookmakers: str | None = None

    def get_odds(self, *args, **kwargs) -> list:
        self.calls += 1
        self.bookmakers = kwargs["bookmakers"]
        return [_two_book_event()]


def test_odds_consensus_fair_value_is_cached_between_book_updates() -> None:
    odds = FairFakeOdds()
    source = OddsConsensusFairValue(
        kalshi=FairFakeKalshi(),  # type: ignore[arg-type]
        odds=odds,  # type: ignore[arg-type]
        market_ticker="MARKET",
        sport="baseball_mlb",
        max_age_seconds=10**9,
        refresh_seconds=60,
        include_live=True,
    )

    first = source.get("MARKET")
    second = source.get("MARKET")

    assert Decimal("0.5") < first < Decimal("0.55")
    assert second == first
    assert odds.calls == 1
    assert odds.bookmakers == DEFAULT_SHARP_BOOKMAKERS
