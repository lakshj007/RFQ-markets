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
from .odds import DEFAULT_SHARP_BOOKMAKERS, OddsClient, OddsEvent
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
    event_id: str | None = None
    bookmaker_count: int = 0
    bookmaker_keys: tuple[str, ...] = ()
    oldest_update: datetime | None = None
    quota_remaining: int | None = None


class OddsFairValueUnavailable(ValueError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


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
    _event_id: str | None = None
    _outcome: str | None = None
    _last_snapshot: OddsFairSnapshot | None = None

    def _matched_event(self, market: dict[str, object]) -> OddsEvent:
        if self._event_id is not None:
            event = self.odds.get_event_odds(
                self.sport,
                self._event_id,
                regions=self.regions,
                markets="h2h",
                bookmakers=self.bookmakers,
            )
            if event.event_id != self._event_id:
                raise ValueError("Odds API single-event response ID changed unexpectedly")
            return event

        event_ticker = str(market.get("event_ticker", ""))
        if not event_ticker or not self._outcome:
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
        self._event_id = matches[0].odds_event.event_id
        return matches[0].odds_event

    def _refresh(self) -> Decimal:
        market = self.kalshi.get_market(self.market_ticker)
        self._outcome = str(market.get("yes_sub_title", ""))
        event = self._matched_event(market)
        self._event_commence_time = event.commence_time
        now = datetime.now(UTC)
        consensus = consensus_probability(
            event,
            self._outcome,
            min_bookmakers=self.min_bookmakers,
            max_age_seconds=self.max_age_seconds,
            now=now,
        )
        if consensus is None:
            unbounded = consensus_probability(
                event,
                self._outcome,
                min_bookmakers=self.min_bookmakers,
                max_age_seconds=float("inf"),
                now=now,
            )
            reason = "stale_odds" if unbounded is not None else "insufficient_bookmakers"
            raise OddsFairValueUnavailable(
                reason,
                f"not enough fresh sportsbook prices for {self.market_ticker}",
            )
        selected = set(consensus.bookmaker_keys)
        updates = [
            market.last_update or bookmaker.last_update
            for bookmaker in event.bookmakers
            if bookmaker.key in selected
            for market in bookmaker.markets
            if market.key == "h2h" and (market.last_update or bookmaker.last_update) is not None
        ]
        if len(updates) < consensus.bookmaker_count:
            raise OddsFairValueUnavailable(
                "stale_odds",
                f"sportsbook update timestamps are missing for {self.market_ticker}",
            )
        self._cached_probability = _validate_probability(consensus.fair_probability)
        self._observed_at = now
        self._last_refresh = time.monotonic()
        self._last_snapshot = OddsFairSnapshot(
            probability=self._cached_probability,
            event_commence_time=self._event_commence_time,
            observed_at=self._observed_at,
            event_id=self._event_id,
            bookmaker_count=consensus.bookmaker_count,
            bookmaker_keys=consensus.bookmaker_keys,
            oldest_update=min(updates) if updates else None,
            quota_remaining=getattr(getattr(self.odds, "quota", None), "remaining", None),
        )
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
        self.get(ticker)
        if self._last_snapshot is None:
            raise RuntimeError("odds fair-value snapshot is incomplete")
        return self._last_snapshot

    def refresh_snapshot(self, ticker: str) -> OddsFairSnapshot:
        if ticker != self.market_ticker:
            raise ValueError(
                f"fair-value source is configured for {self.market_ticker}, not {ticker}"
            )
        self._refresh()
        assert self._last_snapshot is not None
        return self._last_snapshot
