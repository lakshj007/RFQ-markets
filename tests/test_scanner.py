from datetime import UTC, datetime
from decimal import Decimal

from kalshi_mm.odds import DEFAULT_SHARP_BOOKMAKERS, parse_event
from kalshi_mm.scanner import consensus_probability, scan_moneyline_discrepancies
from tests.test_matching import kalshi_event
from tests.test_odds import odds_event_payload


def _two_book_event():
    payload = odds_event_payload()
    second = {
        **payload["bookmakers"][0],
        "key": "book-b",
        "title": "Book B",
        "markets": [
            {
                "key": "h2h",
                "last_update": "2026-07-04T19:59:20Z",
                "outcomes": [
                    {"name": "Toronto Blue Jays", "price": 1.9},
                    {"name": "New York Mets", "price": 2.0},
                ],
            }
        ],
    }
    payload["bookmakers"].append(second)
    return parse_event(payload)


def test_consensus_removes_each_bookmakers_vig() -> None:
    result = consensus_probability(
        _two_book_event(),
        "Toronto",
        now=datetime(2026, 7, 4, 20, tzinfo=UTC),
    )

    assert result is not None
    assert result.bookmaker_count == 2
    assert Decimal("0.5") < result.fair_probability < Decimal("0.55")


class FakeKalshi:
    def get_events(self, *, series_ticker: str, limit: int) -> list[dict]:
        assert series_ticker == "KXMLBGAME"
        assert limit == 50
        return [kalshi_event()]

    def get_orderbook(self, ticker: str, *, depth: int) -> dict:
        assert depth == 1
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "25"]],
                "no_dollars": [["0.50", "30"]],
            }
        }


class FakeOdds:
    def get_odds(
        self,
        sport: str,
        *,
        regions: str,
        markets: str,
        bookmakers: str | None,
    ) -> list:
        assert sport == "baseball_mlb"
        assert regions == "us"
        assert markets == "h2h"
        assert bookmakers == DEFAULT_SHARP_BOOKMAKERS
        return [_two_book_event()]


def test_scanner_compares_fair_to_executable_bid_and_ask() -> None:
    results = scan_moneyline_discrepancies(
        kalshi=FakeKalshi(),  # type: ignore[arg-type]
        odds=FakeOdds(),  # type: ignore[arg-type]
        series_ticker="KXMLBGAME",
        sport="baseball_mlb",
        min_edge=Decimal("0.01"),
        include_live=True,
        now=datetime(2026, 7, 4, 20, tzinfo=UTC),
    )

    assert len(results) == 2
    assert results[0].action in {"BUY YES", "SELL YES"}
    assert results[0].yes_bid == Decimal("0.40")
    assert results[0].yes_ask == Decimal("0.50")


def test_scanner_excludes_in_play_events_by_default() -> None:
    results = scan_moneyline_discrepancies(
        kalshi=FakeKalshi(),  # type: ignore[arg-type]
        odds=FakeOdds(),  # type: ignore[arg-type]
        series_ticker="KXMLBGAME",
        sport="baseball_mlb",
        now=datetime(2026, 7, 4, 20, tzinfo=UTC),
    )

    assert results == []
