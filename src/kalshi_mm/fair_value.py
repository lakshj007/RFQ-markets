from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from .client import KalshiClient
from .matching import match_events
from .models import ONE, ZERO, as_decimal
from .odds import DEFAULT_SHARP_BOOKMAKERS, OddsClient
from .scanner import consensus_probability


class FairValueSource(Protocol):
    def get(self, ticker: str) -> Decimal: ...


def _validate_probability(value: Decimal) -> Decimal:
    if not ZERO < value < ONE:
        raise ValueError("fair probability must be strictly between 0 and 1")
    return value


@dataclass(frozen=True, slots=True)
class StaticFairValue:
    probability: Decimal

    def get(self, ticker: str) -> Decimal:
        del ticker
        return _validate_probability(as_decimal(self.probability))


@dataclass(frozen=True, slots=True)
class JsonFileFairValue:
    """Reload a ticker-to-probability JSON map on every quote cycle."""

    path: Path

    def get(self, ticker: str) -> Decimal:
        payload = json.loads(self.path.read_text())
        if not isinstance(payload, dict) or ticker not in payload:
            raise ValueError(f"{self.path} must contain a {ticker!r} probability")
        return _validate_probability(as_decimal(payload[ticker]))


@dataclass(frozen=True, slots=True)
class OddsFairSnapshot:
    probability: Decimal
    event_commence_time: datetime
    observed_at: datetime


@dataclass(slots=True)
class OddsConsensusFairValue:
    """Refresh a no-vig sportsbook consensus on a bounded polling interval."""

    kalshi: KalshiClient
    odds: OddsClient
    market_ticker: str
    sport: str
    regions: str = "us"
    bookmakers: str | None = DEFAULT_SHARP_BOOKMAKERS
    min_bookmakers: int = 2
    max_age_seconds: float = 180
    refresh_seconds: float = 60
    match_window_hours: float = 6
    include_live: bool = False
    _cached_probability: Decimal | None = None
    _last_refresh: float = 0
    _event_commence_time: datetime | None = None
    _observed_at: datetime | None = None

    def _refresh(self) -> Decimal:
        market = self.kalshi.get_market(self.market_ticker)
        event_ticker = str(market.get("event_ticker", ""))
        outcome = str(market.get("yes_sub_title", ""))
        if not event_ticker or not outcome:
            raise ValueError("Kalshi market is missing event or outcome metadata")
        kalshi_event = self.kalshi.get_event(event_ticker)
        odds_events = self.odds.get_odds(
            self.sport,
            regions=self.regions,
            markets="h2h",
            bookmakers=self.bookmakers,
        )
        now = datetime.now(UTC)
        if not self.include_live:
            odds_events = [event for event in odds_events if event.commence_time > now]
        matches = match_events(
            [kalshi_event],
            odds_events,
            max_time_difference_seconds=self.match_window_hours * 60 * 60,
        )
        if len(matches) != 1:
            raise ValueError(f"could not safely match {self.market_ticker} to one Odds API event")
        self._event_commence_time = matches[0].odds_event.commence_time
        consensus = consensus_probability(
            matches[0].odds_event,
            outcome,
            min_bookmakers=self.min_bookmakers,
            max_age_seconds=self.max_age_seconds,
            now=now,
        )
        if consensus is None:
            raise ValueError(f"not enough fresh sportsbook prices for {self.market_ticker}")
        self._cached_probability = _validate_probability(consensus.fair_probability)
        self._observed_at = now
        self._last_refresh = time.monotonic()
        return self._cached_probability

    def get(self, ticker: str) -> Decimal:
        if ticker != self.market_ticker:
            raise ValueError(
                f"fair-value source is configured for {self.market_ticker}, not {ticker}"
            )
        if (
            not self.include_live
            and self._event_commence_time is not None
            and datetime.now(UTC) >= self._event_commence_time
        ):
            raise ValueError(f"refusing stale in-play fair value for {self.market_ticker}")
        stale = time.monotonic() - self._last_refresh >= self.refresh_seconds
        if self._cached_probability is None or stale:
            return self._refresh()
        return self._cached_probability

    def snapshot(self, ticker: str) -> OddsFairSnapshot:
        probability = self.get(ticker)
        if self._event_commence_time is None or self._observed_at is None:
            raise RuntimeError("odds fair-value snapshot is incomplete")
        return OddsFairSnapshot(
            probability=probability,
            event_commence_time=self._event_commence_time,
            observed_at=self._observed_at,
        )
