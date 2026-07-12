from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import requests

from .models import as_decimal

ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"
SHARP_BOOKMAKER_KEYS = ("pinnacle", "circasports", "bookmaker", "fanduel")
DEFAULT_SHARP_BOOKMAKERS = ",".join(SHARP_BOOKMAKER_KEYS)


class OddsAPIError(RuntimeError):
    def __init__(self, status_code: int, path: str, detail: str) -> None:
        super().__init__(f"Odds API GET {path} returned {status_code}: {detail}")
        self.status_code = status_code
        self.path = path
        self.detail = detail


@dataclass(frozen=True, slots=True)
class OddsQuota:
    remaining: int | None = None
    used: int | None = None
    last_cost: int | None = None


@dataclass(frozen=True, slots=True)
class OddsOutcome:
    name: str
    price: Decimal
    point: Decimal | None = None


@dataclass(frozen=True, slots=True)
class OddsMarket:
    key: str
    last_update: datetime | None
    outcomes: tuple[OddsOutcome, ...]


@dataclass(frozen=True, slots=True)
class BookmakerOdds:
    key: str
    title: str
    last_update: datetime | None
    markets: tuple[OddsMarket, ...]


@dataclass(frozen=True, slots=True)
class OddsEvent:
    event_id: str
    sport_key: str
    sport_title: str
    commence_time: datetime
    home_team: str
    away_team: str
    bookmakers: tuple[BookmakerOdds, ...]


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _optional_decimal(value: str | int | float | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return as_decimal(value)


def parse_event(payload: dict[str, Any]) -> OddsEvent:
    bookmakers: list[BookmakerOdds] = []
    for bookmaker in payload.get("bookmakers", []):
        markets: list[OddsMarket] = []
        for market in bookmaker.get("markets", []):
            outcomes = tuple(
                OddsOutcome(
                    name=str(outcome["name"]),
                    price=as_decimal(outcome["price"]),
                    point=_optional_decimal(outcome.get("point")),
                )
                for outcome in market.get("outcomes", [])
            )
            markets.append(
                OddsMarket(
                    key=str(market["key"]),
                    last_update=parse_datetime(market.get("last_update")),
                    outcomes=outcomes,
                )
            )
        bookmakers.append(
            BookmakerOdds(
                key=str(bookmaker["key"]),
                title=str(bookmaker.get("title", bookmaker["key"])),
                last_update=parse_datetime(bookmaker.get("last_update")),
                markets=tuple(markets),
            )
        )
    commence_time = parse_datetime(payload.get("commence_time"))
    if commence_time is None:
        raise ValueError("Odds API event is missing commence_time")
    return OddsEvent(
        event_id=str(payload["id"]),
        sport_key=str(payload["sport_key"]),
        sport_title=str(payload.get("sport_title", payload["sport_key"])),
        commence_time=commence_time,
        home_team=str(payload["home_team"]),
        away_team=str(payload["away_team"]),
        bookmakers=tuple(bookmakers),
    )


class OddsClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = ODDS_API_BASE_URL,
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("ODDS_API_KEY is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.quota = OddsQuota()

    @classmethod
    def from_env(cls) -> OddsClient:
        return cls(api_key=os.getenv("ODDS_API_KEY", ""))

    @staticmethod
    def _header_int(headers: Any, name: str) -> int | None:
        raw = headers.get(name)
        return int(raw) if raw is not None else None

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        query = dict(params or {})
        query["apiKey"] = self.api_key
        try:
            response = self.session.get(
                self.base_url + path,
                params=query,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise OddsAPIError(0, path, "network request failed") from exc
        self.quota = OddsQuota(
            remaining=self._header_int(response.headers, "x-requests-remaining"),
            used=self._header_int(response.headers, "x-requests-used"),
            last_cost=self._header_int(response.headers, "x-requests-last"),
        )
        if not response.ok:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            safe_detail = str(detail).replace(self.api_key, "[redacted]")
            raise OddsAPIError(response.status_code, path, safe_detail)
        return response.json()

    def get_sports(self, *, include_all: bool = False) -> list[dict[str, Any]]:
        payload = self._get("/sports", params={"all": str(include_all).lower()})
        if not isinstance(payload, list):
            raise OddsAPIError(200, "/sports", "expected a JSON array")
        return payload

    def get_odds(
        self,
        sport: str,
        *,
        regions: str = "us",
        markets: str = "h2h",
        bookmakers: str | None = None,
    ) -> list[OddsEvent]:
        params: dict[str, Any] = {
            "regions": regions,
            "markets": markets,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        if bookmakers:
            params["bookmakers"] = bookmakers
        payload = self._get(f"/sports/{sport}/odds", params=params)
        if not isinstance(payload, list):
            raise OddsAPIError(200, f"/sports/{sport}/odds", "expected a JSON array")
        return [parse_event(item) for item in payload]

    def get_event_odds(
        self,
        sport: str,
        event_id: str,
        *,
        regions: str = "us",
        markets: str = "h2h",
        bookmakers: str | None = None,
    ) -> OddsEvent:
        """Fetch one already-matched event instead of repeatedly scanning a league."""
        params: dict[str, Any] = {
            "regions": regions,
            "markets": markets,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        if bookmakers:
            params["bookmakers"] = bookmakers
        path = f"/sports/{sport}/events/{event_id}/odds"
        payload = self._get(path, params=params)
        if not isinstance(payload, dict):
            raise OddsAPIError(200, path, "expected a JSON object")
        return parse_event(payload)
