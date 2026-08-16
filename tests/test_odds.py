from datetime import UTC, datetime
from decimal import Decimal

import pytest

from kalshi_mm.odds import OddsAPIError, OddsClient, parse_event


def odds_event_payload() -> dict:
    return {
        "id": "event-1",
        "sport_key": "baseball_mlb",
        "sport_title": "MLB",
        "commence_time": "2026-07-04T20:00:00Z",
        "home_team": "Toronto Blue Jays",
        "away_team": "New York Mets",
        "bookmakers": [
            {
                "key": "book-a",
                "title": "Book A",
                "last_update": "2026-07-04T19:59:30Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-07-04T19:59:30Z",
                        "outcomes": [
                            {"name": "Toronto Blue Jays", "price": 1.8},
                            {"name": "New York Mets", "price": 2.1},
                        ],
                    }
                ],
            }
        ],
    }


def test_parse_event_preserves_decimal_prices_and_timestamps() -> None:
    event = parse_event(odds_event_payload())

    assert event.event_id == "event-1"
    assert event.commence_time == datetime(2026, 7, 4, 20, tzinfo=UTC)
    assert event.bookmakers[0].markets[0].outcomes[0].price == Decimal("1.8")


class FakeResponse:
    ok = True
    status_code = 200
    text = ""
    headers = {
        "x-requests-remaining": "498",
        "x-requests-used": "2",
        "x-requests-last": "1",
    }

    def json(self) -> list[dict]:
        return [odds_event_payload()]


class FakeSession:
    def __init__(self) -> None:
        self.params: dict | None = None

    def get(self, url: str, *, params: dict, timeout: float) -> FakeResponse:
        assert url.endswith("/sports/baseball_mlb/odds")
        assert timeout == 15
        self.params = params
        return FakeResponse()


def test_odds_client_sends_key_without_exposing_it_in_models() -> None:
    session = FakeSession()
    client = OddsClient(api_key="secret", session=session)  # type: ignore[arg-type]

    events = client.get_odds("baseball_mlb")

    assert len(events) == 1
    assert session.params["apiKey"] == "secret"
    assert client.quota.remaining == 498
    assert "secret" not in repr(events)


class FakeEventResponse(FakeResponse):
    def json(self) -> dict:
        return odds_event_payload()


class FakeEventSession(FakeSession):
    def get(self, url: str, *, params: dict, timeout: float) -> FakeEventResponse:
        assert url.endswith("/sports/baseball_mlb/events/event-1/odds")
        self.params = params
        return FakeEventResponse()


def test_odds_client_refreshes_one_matched_event() -> None:
    session = FakeEventSession()
    client = OddsClient(api_key="secret", session=session)  # type: ignore[arg-type]

    event = client.get_event_odds(
        "baseball_mlb",
        "event-1",
        bookmakers="book-a,book-b",
    )

    assert event.event_id == "event-1"
    assert session.params is not None
    assert session.params["markets"] == "h2h"
    assert session.params["bookmakers"] == "book-a,book-b"


class FakeErrorResponse(FakeResponse):
    ok = False
    status_code = 401

    def json(self) -> dict:
        return {"message": "bad key secret"}


class FakeErrorSession(FakeSession):
    def get(self, url: str, *, params: dict, timeout: float) -> FakeErrorResponse:
        return FakeErrorResponse()


def test_odds_client_redacts_key_from_api_errors() -> None:
    client = OddsClient(api_key="secret", session=FakeErrorSession())  # type: ignore[arg-type]

    with pytest.raises(OddsAPIError) as error:
        client.get_odds("baseball_mlb")

    assert "secret" not in str(error.value)
    assert "[redacted]" in str(error.value)
