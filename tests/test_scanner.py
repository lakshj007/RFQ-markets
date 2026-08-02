from datetime import UTC, datetime
from decimal import Decimal

from kalshi_mm.odds import DEFAULT_SHARP_BOOKMAKERS, parse_event
from kalshi_mm.scanner import (
    combine_complementary_books,
    consensus_probability,
    consensus_total_probability,
    parse_full_game_total_line,
    scan_moneyline_discrepancies,
    scan_total_discrepancies,
)
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


def _two_book_totals_event():
    payload = odds_event_payload()
    payload["bookmakers"] = []
    for key, over, under in (
        ("book-a", 1.91, 1.91),
        ("book-b", 1.95, 1.87),
    ):
        payload["bookmakers"].append(
            {
                "key": key,
                "title": key,
                "last_update": "2026-07-04T19:59:30Z",
                "markets": [
                    {
                        "key": "totals",
                        "last_update": "2026-07-04T19:59:30Z",
                        "outcomes": [
                            {"name": "Over", "price": over, "point": 8.5},
                            {"name": "Under", "price": under, "point": 8.5},
                            {"name": "Over", "price": 2.5, "point": 9.5},
                            {"name": "Under", "price": 1.5, "point": 9.5},
                        ],
                    }
                ],
            }
        )
    return parse_event(payload)


def _two_book_three_way_event():
    payload = odds_event_payload()
    payload["bookmakers"] = []
    for key in ("book-a", "book-b"):
        payload["bookmakers"].append(
            {
                "key": key,
                "title": key,
                "last_update": "2026-07-04T19:59:30Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-07-04T19:59:30Z",
                        "outcomes": [
                            {"name": "Toronto Blue Jays", "price": 2.1},
                            {"name": "New York Mets", "price": 2.5},
                            {"name": "Draw", "price": 3.2},
                        ],
                    }
                ],
            }
        )
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


def test_total_consensus_uses_only_the_exact_line() -> None:
    result = consensus_total_probability(
        _two_book_totals_event(),
        Decimal("8.5"),
        now=datetime(2026, 7, 4, 20, tzinfo=UTC),
    )

    assert result is not None
    assert result.bookmaker_count == 2
    assert Decimal("0.49") < result.fair_probability < Decimal("0.51")


def test_full_game_total_parser_rejects_team_and_period_totals() -> None:
    assert parse_full_game_total_line("Reg Time: Over 2.5 goals scored") == Decimal("2.5")
    assert parse_full_game_total_line("Over 8.5 runs scored") == Decimal("8.5")
    assert parse_full_game_total_line("Reg Time: France over 1.5 goals") is None
    assert (
        parse_full_game_total_line(
            "Over 1.5 goals scored",
            event_title="France vs Spain: First Half Total",
        )
        is None
    )


class FakeKalshi:
    def get_events(self, *, series_ticker: str, limit: int) -> list[dict]:
        assert series_ticker == "KXMLBGAME"
        assert limit == 50
        return [kalshi_event()]

    def get_orderbook(self, ticker: str, *, depth: int) -> dict:
        assert depth == 1
        if ticker.endswith("-TOR"):
            return {
                "orderbook_fp": {
                    "yes_dollars": [["0.50", "35"]],
                    "no_dollars": [["0.40", "40"]],
                }
            }
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "25"]],
                "no_dollars": [["0.50", "30"]],
            }
        }


class FakeSyntheticKalshi(FakeKalshi):
    def get_orderbook(self, ticker: str, *, depth: int) -> dict:
        assert depth == 1
        if ticker.endswith("-TOR"):
            return {
                "orderbook_fp": {
                    "yes_dollars": [["0.55", "12"]],
                    "no_dollars": [["0.40", "13"]],
                }
            }
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "25"]],
                "no_dollars": [["0.20", "30"]],
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


class FakeTotalKalshi:
    def get_events(self, *, series_ticker: str, limit: int) -> list[dict]:
        assert series_ticker == "KXMLBTOTAL"
        assert limit == 50
        return [
            {
                "event_ticker": "KXMLBTOTAL-TEST",
                "title": "New York M vs Toronto: Total Runs",
                "markets": [
                    {
                        "ticker": "KXMLBTOTAL-TEST-9",
                        "title": "Toronto vs New York Total Runs?",
                        "yes_sub_title": "Over 8.5 runs scored",
                        "occurrence_datetime": "2026-07-04T20:05:00Z",
                    },
                    {
                        "ticker": "KXMLBTEAMTOTAL-TEST-TOR5",
                        "title": "Will Toronto score over 4.5 runs?",
                        "yes_sub_title": "Toronto over 4.5 runs",
                        "occurrence_datetime": "2026-07-04T20:05:00Z",
                    },
                ],
            }
        ]

    def get_orderbook(self, ticker: str, *, depth: int) -> dict:
        assert ticker == "KXMLBTOTAL-TEST-9"
        assert depth == 1
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "25"]],
                "no_dollars": [["0.55", "30"]],
            }
        }


class FakeTotalOdds:
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
        assert markets == "totals"
        assert bookmakers == DEFAULT_SHARP_BOOKMAKERS
        return [_two_book_totals_event()]


class FakeThreeWayKalshi(FakeSyntheticKalshi):
    def get_events(self, *, series_ticker: str, limit: int) -> list[dict]:
        event = kalshi_event()
        event["markets"].append(
            {
                "ticker": "KXMLBGAME-TEST-TIE",
                "yes_sub_title": "Tie",
                "no_sub_title": "Not Tie",
                "occurrence_datetime": "2026-07-04T20:05:00Z",
            }
        )
        return [event]

    def get_orderbook(self, ticker: str, *, depth: int) -> dict:
        if ticker.endswith("-TIE"):
            return {
                "orderbook_fp": {
                    "yes_dollars": [["0.20", "10"]],
                    "no_dollars": [["0.65", "11"]],
                }
            }
        return super().get_orderbook(ticker, depth=depth)


class FakeThreeWayOdds(FakeOdds):
    def get_odds(
        self,
        sport: str,
        *,
        regions: str,
        markets: str,
        bookmakers: str | None,
    ) -> list:
        return [_two_book_three_way_event()]


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
    new_york = next(item for item in results if item.outcome == "New York M")
    assert new_york.yes_bid == Decimal("0.40")
    assert new_york.yes_ask == Decimal("0.50")


def test_scanner_flags_passive_bid_when_bid_is_below_fair() -> None:
    results = scan_moneyline_discrepancies(
        kalshi=FakeKalshi(),  # type: ignore[arg-type]
        odds=FakeOdds(),  # type: ignore[arg-type]
        series_ticker="KXMLBGAME",
        sport="baseball_mlb",
        min_edge=Decimal("0.01"),
        include_live=True,
        now=datetime(2026, 7, 4, 20, tzinfo=UTC),
    )

    new_york = next(item for item in results if item.outcome == "New York M")
    assert new_york.action == "MAKE BID"
    assert new_york.edge > Decimal("0.01")
    assert new_york.action_route == "KXMLBGAME-TEST-NYM:YES|KXMLBGAME-TEST-TOR:NO"


def test_scanner_merges_other_outcomes_no_book() -> None:
    results = scan_moneyline_discrepancies(
        kalshi=FakeSyntheticKalshi(),  # type: ignore[arg-type]
        odds=FakeOdds(),  # type: ignore[arg-type]
        series_ticker="KXMLBGAME",
        sport="baseball_mlb",
        min_edge=Decimal("0.01"),
        include_live=True,
        now=datetime(2026, 7, 4, 20, tzinfo=UTC),
    )

    new_york = next(item for item in results if item.outcome == "New York M")
    assert new_york.direct_yes_bid == Decimal("0.40")
    assert new_york.direct_yes_ask == Decimal("0.80")
    assert new_york.yes_bid == Decimal("0.40")
    assert new_york.yes_ask == Decimal("0.45")
    assert new_york.effective_ask_route == "KXMLBGAME-TEST-TOR:NO"
    assert new_york.complement_ticker == "KXMLBGAME-TEST-TOR"


def test_complementary_books_preserve_best_route_and_size() -> None:
    direct = FakeSyntheticKalshi().get_orderbook("KXMLBGAME-TEST-NYM", depth=1)
    complement = FakeSyntheticKalshi().get_orderbook("KXMLBGAME-TEST-TOR", depth=1)

    from kalshi_mm.models import OrderBook

    effective = combine_complementary_books(
        "NYM",
        OrderBook.from_api(direct),
        "TOR",
        OrderBook.from_api(complement),
    )

    assert effective is not None
    assert effective.ask == Decimal("0.45")
    assert effective.ask_size == Decimal("12")
    assert effective.ask_route == "TOR:NO"


def test_scanner_never_synthesizes_three_way_market() -> None:
    results = scan_moneyline_discrepancies(
        kalshi=FakeThreeWayKalshi(),  # type: ignore[arg-type]
        odds=FakeThreeWayOdds(),  # type: ignore[arg-type]
        series_ticker="KXMLBGAME",
        sport="baseball_mlb",
        min_edge=Decimal("0.01"),
        include_live=True,
        now=datetime(2026, 7, 4, 20, tzinfo=UTC),
    )

    new_york = next(item for item in results if item.outcome == "New York M")
    assert new_york.yes_ask == Decimal("0.80")
    assert new_york.complement_ticker is None
    assert new_york.effective_ask_route == "KXMLBGAME-TEST-NYM:YES"


def test_scanner_excludes_in_play_events_by_default() -> None:
    results = scan_moneyline_discrepancies(
        kalshi=FakeKalshi(),  # type: ignore[arg-type]
        odds=FakeOdds(),  # type: ignore[arg-type]
        series_ticker="KXMLBGAME",
        sport="baseball_mlb",
        now=datetime(2026, 7, 4, 20, tzinfo=UTC),
    )

    assert results == []


def test_total_scanner_matches_exact_full_game_line() -> None:
    results = scan_total_discrepancies(
        kalshi=FakeTotalKalshi(),  # type: ignore[arg-type]
        odds=FakeTotalOdds(),  # type: ignore[arg-type]
        series_ticker="KXMLBTOTAL",
        sport="baseball_mlb",
        min_edge=Decimal("0.01"),
        include_live=True,
        now=datetime(2026, 7, 4, 20, tzinfo=UTC),
    )

    assert len(results) == 1
    assert results[0].ticker == "KXMLBTOTAL-TEST-9"
    assert results[0].market_type == "totals"
    assert results[0].line == Decimal("8.5")
    assert results[0].action == "BUY YES"
