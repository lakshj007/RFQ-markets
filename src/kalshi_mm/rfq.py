from __future__ import annotations

import asyncio
import json
import math
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from .client import KalshiAPIError, KalshiClient
from .matching import match_events, normalize_name
from .models import ONE, ZERO, PriceGrid, as_decimal
from .odds import DEFAULT_SHARP_BOOKMAKERS, OddsClient, OddsEvent
from .scanner import (
    consensus_probability,
    consensus_spread_probability,
    consensus_total_probability,
    parse_full_game_spread,
    parse_full_game_total_line,
)

RFQ_LIVE_ENABLE_TOKEN = "I_UNDERSTAND_RFQ_REAL_MONEY"
RFQ_LIVE_ACKNOWLEDGEMENT = "REAL_MONEY_RFQ_AUTOCONFIRM"
HARD_MIN_RFQ_EDGE_RATE = Decimal("0.0075")
KALSHI_MAKER_FEE_RATE = Decimal("0.0175")
KALSHI_FEE_INCREMENT = Decimal("0.0001")
KALSHI_RFQ_PRICE_INCREMENT = Decimal("0.0001")
LEAD_RFQ_PRICE_INCREMENT = Decimal("0.001")
UNSUPPORTED_AUDIT_BATCH_SIZE = 1_000
COVERAGE_AUDIT_BATCH_SIZE = 1_000
PREPARE_MARKET_BATCH_SIZE = 4
PREPARE_MARKET_BATCH_SECONDS = 1.0
HVM_RFQ_TERMINAL_GRACE_SECONDS = 5.0
STANDARD_RFQ_TERMINAL_GRACE_SECONDS = 46.0
AGGREGATED_RFQ_SKIP_REASONS = {
    "RFQ has no side within position and notional limits",
    "RFQ quote would exceed the session notional limit",
    "RFQ size exceeds the per-request contract limit",
    "RFQ target cost exceeds the configured limit",
    "RFQ session execution limit reached",
    "target-cost RFQs are disabled",
}


def _format_rfq_price(price: Decimal) -> str:
    price = as_decimal(price)
    fixed = price.quantize(KALSHI_RFQ_PRICE_INCREMENT)
    if fixed != price:
        raise ValueError("RFQ price exceeds Kalshi's four-decimal dollar precision")
    return format(fixed, "f")


def estimated_maker_fee(
    price: Decimal,
    contracts: Decimal,
    *,
    fee_rate: Decimal = KALSHI_MAKER_FEE_RATE,
    fee_multiplier: Decimal = ONE,
) -> Decimal:
    """Conservatively model Kalshi's quadratic maker fee and centicent rounding."""
    price = as_decimal(price)
    contracts = as_decimal(contracts)
    fee_rate = as_decimal(fee_rate)
    fee_multiplier = as_decimal(fee_multiplier)
    if price <= ZERO or contracts <= ZERO or fee_rate <= ZERO or fee_multiplier <= ZERO:
        return ZERO
    if price >= ONE:
        raise ValueError("maker-fee price must be in (0, 1)")
    position_cost = price * contracts
    raw_fee = fee_multiplier * fee_rate * contracts * price * (ONE - price)
    rounded_total = (position_cost + raw_fee).quantize(
        KALSHI_FEE_INCREMENT,
        rounding=ROUND_CEILING,
    )
    return rounded_total - position_cost


def modeled_rfq_edge(
    outcome_fair: Decimal,
    price: Decimal,
    contracts: Decimal,
    *,
    fee_rate: Decimal,
    fee_multiplier: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    expected_value = outcome_fair * contracts
    if expected_value <= ZERO:
        raise ValueError("RFQ expected value must be positive")
    fee = estimated_maker_fee(
        price,
        contracts,
        fee_rate=fee_rate,
        fee_multiplier=fee_multiplier,
    )
    gross_edge_rate = (outcome_fair - price) / outcome_fair
    net_edge_rate = (expected_value - price * contracts - fee) / expected_value
    return gross_edge_rate, fee, net_edge_rate


def _execution_fee(payload: dict[str, Any]) -> Decimal | None:
    for key in ("fee_cost", "fee_cost_dollars", "fees_paid_dollars"):
        value = payload.get(key)
        if value is not None and str(value).strip() != "":
            fee = as_decimal(value)
            if fee < ZERO:
                raise ValueError("execution fee cannot be negative")
            return fee
    return None


def _parse_time(value: object, *, field: str) -> datetime:
    if not value:
        raise ValueError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class MoneylineFair:
    ticker: str
    probability: Decimal
    observed_at: datetime
    event_start: datetime
    source: str
    event_ticker: str = ""
    participants: frozenset[str] = frozenset()
    market_type: str = "moneyline"
    sport: str = "mlb"

    def validate(self) -> None:
        if not ZERO < self.probability < ONE:
            raise ValueError(f"fair probability for {self.ticker} must be in (0, 1)")


class MoneylineFairBook(Protocol):
    refresh_seconds: float

    def refresh(self) -> tuple[str, ...]: ...

    def get(self, ticker: str) -> MoneylineFair | None: ...

    def tickers(self) -> tuple[str, ...]: ...


class JsonMoneylineFairBook:
    """Atomically cached fair values written by a separate low-latency feed handler."""

    def __init__(self, path: str | Path, *, refresh_seconds: float = 0.25) -> None:
        if refresh_seconds <= 0:
            raise ValueError("fair-file refresh interval must be positive")
        self.path = Path(path)
        self.refresh_seconds = refresh_seconds
        self._mtime_ns: int | None = None
        self._values: dict[str, MoneylineFair] = {}

    @staticmethod
    def _parse(ticker: str, raw: object) -> MoneylineFair:
        if not isinstance(raw, dict):
            raise ValueError(f"fair entry for {ticker} must be an object")
        market_type = str(raw.get("market_type", ""))
        if market_type != "moneyline":
            raise ValueError(f"fair entry for {ticker} is not a moneyline")
        participants_raw = raw.get("participants", [])
        if not isinstance(participants_raw, list):
            raise ValueError(f"{ticker}.participants must be a list")
        fair = MoneylineFair(
            ticker=ticker,
            probability=as_decimal(raw["probability"]),
            observed_at=_parse_time(raw.get("observed_at"), field=f"{ticker}.observed_at"),
            event_start=_parse_time(raw.get("event_start"), field=f"{ticker}.event_start"),
            source=str(raw.get("source", "json-file")),
            event_ticker=str(raw.get("event_ticker", "")).strip(),
            participants=frozenset(
                normalize_name(str(item))
                for item in participants_raw
                if normalize_name(str(item))
            ),
            market_type=market_type,
            sport=str(raw.get("sport", "mlb")).casefold().strip(),
        )
        fair.validate()
        return fair

    def refresh(self) -> tuple[str, ...]:
        stat = self.path.stat()
        if stat.st_mtime_ns == self._mtime_ns:
            return self.tickers()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("RFQ fair file must contain a JSON object")
        raw_markets = payload.get("markets", payload)
        if not isinstance(raw_markets, dict):
            raise ValueError("RFQ fair file 'markets' must be an object")
        parsed = {
            str(ticker): self._parse(str(ticker), raw)
            for ticker, raw in raw_markets.items()
            if not str(ticker).startswith("_")
        }
        if not parsed:
            raise ValueError("RFQ fair file contains no moneyline fair values")
        self._values = parsed
        self._mtime_ns = stat.st_mtime_ns
        return self.tickers()

    def get(self, ticker: str) -> MoneylineFair | None:
        return self._values.get(ticker)

    def tickers(self) -> tuple[str, ...]:
        return tuple(sorted(self._values))


class OddsMoneylineFairBook:
    """Periodically refresh one safely mapped sports-market lane."""

    def __init__(
        self,
        *,
        kalshi: KalshiClient,
        odds: OddsClient,
        series_ticker: str,
        sport: str,
        regions: str = "us",
        bookmakers: str | None = DEFAULT_SHARP_BOOKMAKERS,
        min_bookmakers: int = 2,
        max_source_age_seconds: float = 60,
        match_window_hours: float = 6,
        refresh_seconds: float = 30,
        market_type: str = "h2h",
        allow_three_way: bool = False,
    ) -> None:
        if min_bookmakers < 1:
            raise ValueError("RFQ odds pricing requires at least one bookmaker")
        if max_source_age_seconds <= 0 or refresh_seconds <= 0:
            raise ValueError("RFQ odds freshness intervals must be positive")
        self.kalshi = kalshi
        self.odds = odds
        self.series_ticker = series_ticker
        self.sport = sport
        self.regions = regions
        self.bookmakers = bookmakers
        self.min_bookmakers = min_bookmakers
        self.max_source_age_seconds = max_source_age_seconds
        self.match_window_hours = match_window_hours
        self.refresh_seconds = refresh_seconds
        if market_type not in {"h2h", "totals", "spreads"}:
            raise ValueError("RFQ odds market type must be h2h, totals, or spreads")
        self.market_type = market_type
        self.allow_three_way = allow_three_way
        self._values: dict[str, MoneylineFair] = {}

    @staticmethod
    def _is_two_way(event: OddsEvent) -> bool:
        found = False
        for bookmaker in event.bookmakers:
            market = next((item for item in bookmaker.markets if item.key == "h2h"), None)
            if market is None:
                continue
            outcomes = [item for item in market.outcomes if item.price > ONE]
            if len(outcomes) != 2:
                return False
            if any(normalize_name(item.name) in {"draw", "tie"} for item in outcomes):
                return False
            found = True
        return found

    def _oldest_selected_update(
        self,
        event: OddsEvent,
        selected: set[str],
    ) -> datetime | None:
        updates = [
            market.last_update or bookmaker.last_update
            for bookmaker in event.bookmakers
            if bookmaker.key in selected
            for market in bookmaker.markets
            if market.key == self.market_type
            and (market.last_update or bookmaker.last_update) is not None
        ]
        return min(updates) if len(updates) >= len(selected) else None

    def refresh(self) -> tuple[str, ...]:
        kalshi_events = self.kalshi.get_events(
            series_ticker=self.series_ticker,
            status="open",
            limit=200,
        )
        odds_events = self.odds.get_odds(
            self.sport,
            regions=self.regions,
            markets=self.market_type,
            bookmakers=self.bookmakers,
        )
        now = datetime.now(UTC)
        odds_events = [event for event in odds_events if event.commence_time > now]
        if self.market_type == "h2h" and not self.allow_three_way:
            odds_events = [event for event in odds_events if self._is_two_way(event)]
        matches = match_events(
            kalshi_events,
            odds_events,
            max_time_difference_seconds=self.match_window_hours * 60 * 60,
        )
        refreshed: dict[str, MoneylineFair] = {}
        for match in matches:
            event_ticker = str(match.kalshi_event.get("event_ticker", "")).strip()
            participants = frozenset(
                filter(
                    None,
                    (
                        normalize_name(match.odds_event.home_team),
                        normalize_name(match.odds_event.away_team),
                    ),
                )
            )
            for market in match.kalshi_event.get("markets", []):
                ticker = str(market.get("ticker", ""))
                outcome = str(market.get("yes_sub_title", ""))
                if not ticker or not outcome:
                    continue
                if self.market_type == "totals":
                    line = parse_full_game_total_line(
                        outcome,
                        event_title=str(match.kalshi_event.get("title", "")),
                        market_title=str(market.get("title", "")),
                    )
                    consensus = (
                        consensus_total_probability(
                            match.odds_event,
                            line,
                            min_bookmakers=self.min_bookmakers,
                            max_age_seconds=self.max_source_age_seconds,
                            now=now,
                        )
                        if line is not None
                        else None
                    )
                elif self.market_type == "spreads":
                    spread = parse_full_game_spread(outcome)
                    consensus = (
                        consensus_spread_probability(
                            match.odds_event,
                            spread[0],
                            spread[1],
                            min_bookmakers=self.min_bookmakers,
                            max_age_seconds=self.max_source_age_seconds,
                            now=now,
                        )
                        if spread is not None
                        else None
                    )
                else:
                    consensus = consensus_probability(
                        match.odds_event,
                        outcome,
                        min_bookmakers=self.min_bookmakers,
                        max_age_seconds=self.max_source_age_seconds,
                        now=now,
                    )
                if consensus is None:
                    continue
                source_update = self._oldest_selected_update(
                    match.odds_event, set(consensus.bookmaker_keys)
                )
                if source_update is None:
                    continue
                fair = MoneylineFair(
                    ticker=ticker,
                    probability=consensus.fair_probability,
                    observed_at=source_update,
                    event_start=match.odds_event.commence_time,
                    source="odds-consensus:" + ",".join(consensus.bookmaker_keys),
                    event_ticker=event_ticker,
                    participants=participants,
                    market_type={
                        "h2h": "moneyline",
                        "totals": "total",
                        "spreads": "spread",
                    }[self.market_type],
                    sport={
                        "baseball_mlb": "mlb",
                        "basketball_wnba": "wnba",
                        "soccer_usa_mls": "mls",
                        "soccer_mexico_ligamx": "liga_mx",
                        "mma_mixed_martial_arts": "ufc",
                    }.get(self.sport, self.sport.casefold().strip()),
                )
                fair.validate()
                refreshed[fair.ticker] = fair
        if not refreshed:
            raise ValueError("no fresh, safely matched two-way moneyline fairs were found")
        self._values = refreshed
        return self.tickers()

    def get(self, ticker: str) -> MoneylineFair | None:
        return self._values.get(ticker)

    def tickers(self) -> tuple[str, ...]:
        return tuple(sorted(self._values))


class CompositeMoneylineFairBook:
    """Merge independently refreshed sport/market lanes into one fail-closed cache."""

    def __init__(self, books: tuple[MoneylineFairBook, ...]) -> None:
        if not books:
            raise ValueError("composite RFQ fair book requires at least one lane")
        self.books = books
        self.refresh_seconds = min(book.refresh_seconds for book in books)

    def refresh(self) -> tuple[str, ...]:
        refreshed = False
        errors: list[str] = []
        for book in self.books:
            try:
                book.refresh()
                refreshed = True
            except ValueError as exc:
                errors.append(str(exc))
        if not refreshed and not self.tickers():
            raise ValueError("no configured RFQ lane refreshed: " + "; ".join(errors))
        return self.tickers()

    def get(self, ticker: str) -> MoneylineFair | None:
        matches = [book.get(ticker) for book in self.books]
        values = [item for item in matches if item is not None]
        if len(values) > 1:
            raise ValueError(f"multiple RFQ fair lanes produced {ticker}")
        return values[0] if values else None

    def tickers(self) -> tuple[str, ...]:
        return tuple(sorted({ticker for book in self.books for ticker in book.tickers()}))


@dataclass(frozen=True, slots=True)
class RFQLeg:
    market_ticker: str
    event_ticker: str
    side: str

    @classmethod
    def from_payload(cls, raw: object) -> RFQLeg:
        if not isinstance(raw, dict):
            raise ValueError("each MVE leg must be an object")
        leg = cls(
            market_ticker=str(raw.get("market_ticker", "")).strip(),
            event_ticker=str(raw.get("event_ticker", "")).strip(),
            side=str(raw.get("side", "")).casefold().strip(),
        )
        if not leg.market_ticker or not leg.event_ticker or leg.side not in {"yes", "no"}:
            raise ValueError("each MVE leg requires market_ticker, event_ticker, and YES/NO side")
        return leg

    def as_dict(self) -> dict[str, str]:
        return {
            "market_ticker": self.market_ticker,
            "event_ticker": self.event_ticker,
            "side": self.side,
        }


@dataclass(frozen=True, slots=True)
class RFQRequest:
    rfq_id: str
    ticker: str
    contracts: Decimal | None
    created_at: datetime
    target_cost: Decimal | None = None
    collection_ticker: str = ""
    legs: tuple[RFQLeg, ...] = ()
    creator_id: str = ""

    @property
    def is_combo(self) -> bool:
        return bool(self.collection_ticker or self.legs)

    @property
    def sizing_mode(self) -> str:
        return "contracts" if self.contracts is not None else "target_cost"

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> RFQRequest:
        if message.get("type") != "rfq_created":
            raise ValueError("message is not an RFQ creation")
        payload = message.get("msg")
        if not isinstance(payload, dict):
            raise ValueError("RFQ message payload is missing")
        contracts_raw = payload.get("contracts_fp")
        target_cost_raw = payload.get("target_cost_dollars")
        contracts = as_decimal(contracts_raw) if contracts_raw not in {None, ""} else None
        target_cost = (
            as_decimal(target_cost_raw)
            if target_cost_raw not in {None, "", "0", "0.0", "0.00"}
            else None
        )
        if (contracts is None) == (target_cost is None):
            raise ValueError("RFQ must specify exactly one positive sizing mode")
        legs_raw = payload.get("mve_selected_legs") or []
        if not isinstance(legs_raw, list):
            raise ValueError("mve_selected_legs must be a list")
        request = cls(
            rfq_id=str(payload.get("id", "")),
            ticker=str(payload.get("market_ticker", "")),
            contracts=contracts,
            created_at=_parse_time(payload.get("created_ts"), field="created_ts"),
            target_cost=target_cost,
            collection_ticker=str(payload.get("mve_collection_ticker", "")).strip(),
            legs=tuple(RFQLeg.from_payload(raw) for raw in legs_raw),
            creator_id=str(payload.get("creator_id", "")).strip(),
        )
        size = request.contracts if request.contracts is not None else request.target_cost
        if not request.rfq_id or not request.ticker or size is None or size <= ZERO:
            raise ValueError("RFQ ID, ticker, and positive size are required")
        if request.is_combo and (not request.collection_ticker or not request.legs):
            raise ValueError("combo RFQs require both a collection ticker and selected legs")
        return request


@dataclass(frozen=True, slots=True)
class RFQQuotePlan:
    request: RFQRequest
    fair: MoneylineFair
    yes_bid: Decimal
    no_bid: Decimal
    # These are net of the modeled maker fee. Gross edge is retained separately
    # so audits can distinguish price improvement from fee drag.
    yes_edge_rate: Decimal | None
    no_edge_rate: Decimal | None
    yes_gross_edge_rate: Decimal | None
    no_gross_edge_rate: Decimal | None
    yes_estimated_fee: Decimal
    no_estimated_fee: Decimal
    maker_fee_rate: Decimal
    maker_fee_multiplier: Decimal
    leg_fairs: tuple[MoneylineFair, ...] = ()

    @property
    def has_side(self) -> bool:
        return self.yes_bid > ZERO or self.no_bid > ZERO

    @property
    def maximum_cost(self) -> Decimal:
        return max(
            self.yes_bid * self.yes_contracts + self.yes_estimated_fee,
            self.no_bid * self.no_contracts + self.no_estimated_fee,
        )

    @staticmethod
    def _target_contracts(target_cost: Decimal, bid: Decimal) -> Decimal:
        if bid <= ZERO:
            return ZERO
        requester_price = ONE - bid
        if requester_price <= ZERO:
            raise ValueError("target-cost RFQ bid leaves no positive requester price")
        # The requester takes the outcome opposite our bid, paying (1 - bid)
        # plus a nonnegative requester fee. Ignoring that fee and rounding up
        # therefore bounds Kalshi's exchange-derived contract count from above.
        return (target_cost / requester_price).quantize(
            Decimal("0.01"),
            rounding=ROUND_CEILING,
        )

    @property
    def yes_contracts(self) -> Decimal:
        if self.yes_bid <= ZERO:
            return ZERO
        if self.request.contracts is not None:
            return self.request.contracts
        assert self.request.target_cost is not None
        return self._target_contracts(self.request.target_cost, self.yes_bid)

    @property
    def no_contracts(self) -> Decimal:
        if self.no_bid <= ZERO:
            return ZERO
        if self.request.contracts is not None:
            return self.request.contracts
        assert self.request.target_cost is not None
        return self._target_contracts(self.request.target_cost, self.no_bid)

    def contracts_for(self, side: str) -> Decimal:
        return self.yes_contracts if side == "yes" else self.no_contracts

    def as_dict(self) -> dict[str, object]:
        return {
            "rfq_id": self.request.rfq_id,
            "ticker": self.request.ticker,
            "sizing_mode": self.request.sizing_mode,
            "contracts_fp": (
                str(self.request.contracts) if self.request.contracts is not None else None
            ),
            "target_cost_dollars": (
                str(self.request.target_cost) if self.request.target_cost is not None else None
            ),
            "yes_contracts_reserved": str(self.yes_contracts),
            "no_contracts_reserved": str(self.no_contracts),
            "contracts_reservation_basis": (
                "fixed"
                if self.request.contracts is not None
                else "target_cost_complement_price_upper_bound"
            ),
            "collection_ticker": self.request.collection_ticker or None,
            "legs": [leg.as_dict() for leg in self.request.legs],
            "fair_probability": str(self.fair.probability),
            "fair_observed_at": self.fair.observed_at.isoformat(),
            "fair_source": self.fair.source,
            "yes_bid": str(self.yes_bid),
            "no_bid": str(self.no_bid),
            "yes_edge_rate": (str(self.yes_edge_rate) if self.yes_edge_rate is not None else None),
            "no_edge_rate": (str(self.no_edge_rate) if self.no_edge_rate is not None else None),
            "yes_net_edge_rate": (
                str(self.yes_edge_rate) if self.yes_edge_rate is not None else None
            ),
            "no_net_edge_rate": (
                str(self.no_edge_rate) if self.no_edge_rate is not None else None
            ),
            "yes_gross_edge_rate": (
                str(self.yes_gross_edge_rate)
                if self.yes_gross_edge_rate is not None
                else None
            ),
            "no_gross_edge_rate": (
                str(self.no_gross_edge_rate)
                if self.no_gross_edge_rate is not None
                else None
            ),
            "yes_estimated_maker_fee": str(self.yes_estimated_fee),
            "no_estimated_maker_fee": str(self.no_estimated_fee),
            "maker_fee_rate": str(self.maker_fee_rate),
            "maker_fee_multiplier": str(self.maker_fee_multiplier),
            "maximum_cost": str(self.maximum_cost),
        }


def price_moneyline_rfq(
    request: RFQRequest,
    fair: MoneylineFair,
    *,
    price_grid: PriceGrid,
    edge_rate: Decimal,
    maker_fee_rate: Decimal = KALSHI_MAKER_FEE_RATE,
    maker_fee_multiplier: Decimal = ZERO,
    leg_fairs: tuple[MoneylineFair, ...] = (),
) -> RFQQuotePlan:
    edge_rate = as_decimal(edge_rate)
    maker_fee_rate = as_decimal(maker_fee_rate)
    maker_fee_multiplier = as_decimal(maker_fee_multiplier)
    if not HARD_MIN_RFQ_EDGE_RATE <= edge_rate < ONE:
        raise ValueError("RFQ proportional edge must be in [0.75%, 100%)")
    if maker_fee_rate < ZERO or maker_fee_multiplier < ZERO:
        raise ValueError("RFQ maker-fee inputs cannot be negative")

    def contracts_for(bid: Decimal) -> Decimal:
        if request.contracts is not None:
            return request.contracts
        assert request.target_cost is not None
        return RFQQuotePlan._target_contracts(request.target_cost, bid)

    def previous_bid(bid: Decimal) -> Decimal:
        epsilon = min(item.step for item in price_grid.ranges) / Decimal("10")
        previous = price_grid.floor(bid - epsilon)
        return previous if previous < bid else ZERO

    def bid_for(
        outcome_fair: Decimal,
    ) -> tuple[Decimal, Decimal | None, Decimal | None, Decimal]:
        raw = outcome_fair * (ONE - edge_rate)
        if raw < price_grid.minimum_order_price:
            return ZERO, None, None, ZERO
        bid = price_grid.floor(raw)
        while bid >= price_grid.minimum_order_price:
            gross_edge_rate, fee, net_edge_rate = modeled_rfq_edge(
                outcome_fair,
                bid,
                contracts_for(bid),
                fee_rate=maker_fee_rate,
                fee_multiplier=maker_fee_multiplier,
            )
            if net_edge_rate >= edge_rate:
                return bid, net_edge_rate, gross_edge_rate, fee
            bid = previous_bid(bid)
        return ZERO, None, None, ZERO

    yes_bid, yes_edge_rate, yes_gross_edge_rate, yes_fee = bid_for(fair.probability)
    no_bid, no_edge_rate, no_gross_edge_rate, no_fee = bid_for(ONE - fair.probability)
    if yes_bid + no_bid > ONE:
        raise ValueError("RFQ quote prices cannot sum above $1")
    plan = RFQQuotePlan(
        request=request,
        fair=fair,
        yes_bid=yes_bid,
        no_bid=no_bid,
        yes_edge_rate=yes_edge_rate,
        no_edge_rate=no_edge_rate,
        yes_gross_edge_rate=yes_gross_edge_rate,
        no_gross_edge_rate=no_gross_edge_rate,
        yes_estimated_fee=yes_fee,
        no_estimated_fee=no_fee,
        maker_fee_rate=maker_fee_rate,
        maker_fee_multiplier=maker_fee_multiplier,
        leg_fairs=leg_fairs,
    )
    if not plan.has_side:
        raise ValueError("fair value is too close to a boundary to quote either side")
    return plan


def price_lead_style_rfq(
    request: RFQRequest,
    fair: MoneylineFair,
    *,
    price_grid: PriceGrid,
    margin: Decimal = Decimal("0.006"),
    minimum_cushion: Decimal = Decimal("0.007"),
    two_leg_premium: Decimal = Decimal("0.009"),
    player_prop_premium: Decimal = Decimal("0.005"),
    soccer_premium: Decimal = Decimal("0.005"),
    maker_fee_rate: Decimal = KALSHI_MAKER_FEE_RATE,
    maker_fee_multiplier: Decimal = ZERO,
    leg_fairs: tuple[MoneylineFair, ...] = (),
) -> RFQQuotePlan:
    """Quote only NO at de-vigged fair minus a fixed, boundary-capped margin."""
    inputs = tuple(
        as_decimal(item)
        for item in (
            margin,
            minimum_cushion,
            two_leg_premium,
            player_prop_premium,
            soccer_premium,
            maker_fee_rate,
            maker_fee_multiplier,
        )
    )
    (
        margin,
        minimum_cushion,
        two_leg_premium,
        player_prop_premium,
        soccer_premium,
        maker_fee_rate,
        maker_fee_multiplier,
    ) = inputs
    if any(item < ZERO for item in inputs):
        raise ValueError("lead-style RFQ pricing inputs cannot be negative")

    premium = ZERO
    if len(request.legs) == 2:
        premium += two_leg_premium
    if any(item.market_type == "player_prop" for item in leg_fairs):
        premium += player_prop_premium
    if any(item.sport in {"mls", "liga_mx"} for item in leg_fairs):
        premium += soccer_premium
    required_cushion = minimum_cushion + premium
    boundary_cushion = min(fair.probability, ONE - fair.probability)
    if boundary_cushion < required_cushion:
        raise ValueError("parlay fair-value cushion is below the configured floor")
    effective_margin = min(margin + premium, boundary_cushion / Decimal("2"))
    no_fair = ONE - fair.probability

    def contracts_for(bid: Decimal) -> Decimal:
        if request.contracts is not None:
            return request.contracts
        assert request.target_cost is not None
        return RFQQuotePlan._target_contracts(request.target_cost, bid)

    def lead_grid_floor(price: Decimal) -> Decimal:
        candidate = (
            price / LEAD_RFQ_PRICE_INCREMENT
        ).to_integral_value(rounding="ROUND_FLOOR") * LEAD_RFQ_PRICE_INCREMENT
        while candidate >= price_grid.minimum_order_price:
            if price_grid.floor(candidate) == candidate:
                return candidate
            candidate -= LEAD_RFQ_PRICE_INCREMENT
        return ZERO

    def previous_bid(bid: Decimal) -> Decimal:
        return lead_grid_floor(bid - LEAD_RFQ_PRICE_INCREMENT)

    raw = no_fair - effective_margin
    no_bid = lead_grid_floor(raw) if raw >= price_grid.minimum_order_price else ZERO
    no_edge_rate: Decimal | None = None
    no_gross_edge_rate: Decimal | None = None
    no_fee = ZERO
    while no_bid >= price_grid.minimum_order_price:
        contracts = contracts_for(no_bid)
        gross, fee, net = modeled_rfq_edge(
            no_fair,
            no_bid,
            contracts,
            fee_rate=maker_fee_rate,
            fee_multiplier=maker_fee_multiplier,
        )
        net_margin_per_contract = no_fair - no_bid - fee / contracts
        if net_margin_per_contract >= effective_margin:
            no_edge_rate = net
            no_gross_edge_rate = gross
            no_fee = fee
            break
        no_bid = previous_bid(no_bid)
    if no_bid < price_grid.minimum_order_price:
        raise ValueError("fair value is too close to a boundary to quote NO")
    return RFQQuotePlan(
        request=request,
        fair=fair,
        yes_bid=ZERO,
        no_bid=no_bid,
        yes_edge_rate=None,
        no_edge_rate=no_edge_rate,
        yes_gross_edge_rate=None,
        no_gross_edge_rate=no_gross_edge_rate,
        yes_estimated_fee=ZERO,
        no_estimated_fee=no_fee,
        maker_fee_rate=maker_fee_rate,
        maker_fee_multiplier=maker_fee_multiplier,
        leg_fairs=leg_fairs,
    )


@dataclass(frozen=True, slots=True)
class RFQMakerConfig:
    edge_rate: Decimal = Decimal("0.011")
    maker_fee_rate: Decimal = KALSHI_MAKER_FEE_RATE
    min_contracts: Decimal = Decimal("1")
    max_contracts: Decimal = Decimal("10")
    max_target_cost: Decimal | None = None
    max_abs_position: Decimal = Decimal("10")
    max_notional: Decimal = Decimal("10")
    max_session_notional: Decimal | None = None
    max_session_contracts: Decimal | None = None
    max_session_executions: int | None = None
    max_active_quotes: int = 20
    first_fill_wins: bool = False
    allow_existing_positions: bool = False
    max_unaccepted_quote_age_seconds: float | None = None
    max_fair_age_seconds: float = 60
    reconcile_seconds: float = 15
    subaccount: int = 0
    allow_live_games: bool = False
    min_legs: int = 2
    max_legs: int = 10
    max_inflight_rfqs: int = 32
    max_quote_latency_seconds: float = 1.0
    combo_only: bool = False
    contracts_only: bool = False
    coverage_shadow: bool = False
    require_subaccount_metadata: bool = False
    pricing_mode: str = "proportional"
    no_side_only: bool = False
    pinnacle_only: bool = False
    max_hours_to_start: float | None = None
    moneyline_yes_only: bool = False
    max_moneyline_fair: Decimal | None = None
    fixed_margin: Decimal = Decimal("0.006")
    minimum_cushion: Decimal = Decimal("0.007")
    two_leg_premium: Decimal = Decimal("0.009")
    player_prop_premium: Decimal = Decimal("0.005")
    soccer_premium: Decimal = Decimal("0.005")
    per_leg_outcome_notional_cap: Decimal | None = None
    per_combo_notional_cap: Decimal | None = None
    creator_rate_limit: bool = False
    creator_burst_limit: int = 2
    creator_burst_window_seconds: float = 10
    required_active_sports: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.pricing_mode not in {"proportional", "lead_fixed"}:
            raise ValueError("RFQ pricing mode must be proportional or lead_fixed")
        if (
            self.pricing_mode == "proportional"
            and not HARD_MIN_RFQ_EDGE_RATE <= self.edge_rate < ONE
        ):
            raise ValueError("RFQ proportional edge must be in [0.75%, 100%)")
        if self.maker_fee_rate < KALSHI_MAKER_FEE_RATE:
            raise ValueError("RFQ maker-fee rate cannot be below Kalshi's published 1.75% rate")
        if self.min_contracts <= ZERO or self.max_contracts < self.min_contracts:
            raise ValueError("RFQ contract limits must be positive and ordered")
        if self.max_target_cost is not None and self.max_target_cost <= ZERO:
            raise ValueError("RFQ target-cost limit must be positive when configured")
        if self.contracts_only and self.max_target_cost is not None:
            raise ValueError("contracts-only RFQ mode cannot configure a target-cost limit")
        if self.max_abs_position <= ZERO:
            raise ValueError("RFQ contract and position limits must be positive")
        if self.max_notional <= ZERO or self.max_active_quotes <= 0:
            raise ValueError("RFQ notional and active-quote limits must be positive")
        if self.max_session_notional is not None and self.max_session_notional <= ZERO:
            raise ValueError("RFQ session notional limit must be positive when configured")
        if self.max_unaccepted_quote_age_seconds is not None and (
            not math.isfinite(self.max_unaccepted_quote_age_seconds)
            or self.max_unaccepted_quote_age_seconds <= 0
        ):
            raise ValueError("RFQ unaccepted-quote lifetime must be finite and positive")
        if self.max_session_contracts is not None and self.max_session_contracts <= ZERO:
            raise ValueError("RFQ session contract limit must be positive when configured")
        if self.max_session_executions is not None and self.max_session_executions <= 0:
            raise ValueError("RFQ session execution limit must be positive when configured")
        if self.first_fill_wins and self.max_session_executions != 1:
            raise ValueError("first-fill-wins quoting requires exactly one session execution")
        if self.max_fair_age_seconds <= 0:
            raise ValueError("RFQ maximum fair age must be positive")
        if not 1 <= self.reconcile_seconds <= 60:
            raise ValueError("RFQ reconciliation interval must be between 1 and 60 seconds")
        if not 0 <= self.subaccount <= 32:
            raise ValueError("RFQ subaccount must be between 0 and 32")
        if self.min_legs < 2 or self.max_legs < self.min_legs:
            raise ValueError("RFQ parlay leg limits must start at two and be ordered")
        if self.max_inflight_rfqs <= 0:
            raise ValueError("RFQ in-flight handler limit must be positive")
        if not 0.1 <= self.max_quote_latency_seconds <= 5:
            raise ValueError("RFQ maximum quote latency must be between 0.1 and 5 seconds")
        if self.max_hours_to_start is not None and self.max_hours_to_start <= 0:
            raise ValueError("maximum hours to game start must be positive")
        if self.max_moneyline_fair is not None and not ZERO < self.max_moneyline_fair < ONE:
            raise ValueError("maximum moneyline fair must be in (0, 1)")
        fixed_inputs = (
            self.fixed_margin,
            self.minimum_cushion,
            self.two_leg_premium,
            self.player_prop_premium,
            self.soccer_premium,
        )
        if any(item < ZERO for item in fixed_inputs):
            raise ValueError("lead-style margin, floor, and premiums cannot be negative")
        for cap in (self.per_leg_outcome_notional_cap, self.per_combo_notional_cap):
            if cap is not None and cap <= ZERO:
                raise ValueError("RFQ exposure caps must be positive when configured")
        if self.creator_burst_limit <= 0 or self.creator_burst_window_seconds <= 0:
            raise ValueError("creator burst limits must be positive")


@dataclass(slots=True)
class QuoteReservation:
    plan: RFQQuotePlan
    quote_id: str
    submitted_at_monotonic: float | None = None
    next_ttl_cancel_attempt_monotonic: float = 0.0
    accepted_side: str | None = None
    accepted_contracts: Decimal = ZERO
    confirmed_outcome_fair: Decimal | None = None
    confirmed_edge_rate: Decimal | None = None


class MarkdownRFQFillLedger:
    """Append an idempotent, human-readable record for every observed RFQ execution."""

    HEADER = (
        "# RFQ Fill Ledger\n\n"
        "This file is updated when the maker observes an executed RFQ. Net edge includes the\n"
        "actual fill fee when the Fills API returns it, otherwise the conservative modeled fee.\n"
        "Parlay fair value is the product of independent selected-leg moneyline probabilities;\n"
        "the configured proportional edge is applied once to that complete parlay fair value.\n\n"
        "| Executed (UTC) | Structure | Event/game | Legs | Side | Contracts | Fair | Quote | "
        "Gross edge | Fee | Fee source | Net edge | Gross edge $ | Net edge $ | RFQ | Quote | "
        "Order | Source |\n"
        "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | "
        "---: | ---: | --- | --- | --- | --- |\n"
    )

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @staticmethod
    def _cell(value: object) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ").strip()

    def append(
        self,
        reservation: QuoteReservation,
        *,
        event_ticker: str,
        execution: dict[str, Any],
    ) -> bool:
        side = reservation.accepted_side
        contracts = reservation.accepted_contracts
        if side not in {"yes", "no"} or contracts <= ZERO:
            raise ValueError("executed RFQ is missing its accepted side or contract count")
        plan = reservation.plan
        quote_price = plan.yes_bid if side == "yes" else plan.no_bid
        quote_time_fair = plan.fair.probability if side == "yes" else ONE - plan.fair.probability
        outcome_fair = reservation.confirmed_outcome_fair or quote_time_fair
        gross_edge_rate = (outcome_fair - quote_price) / outcome_fair
        gross_edge = (outcome_fair - quote_price) * contracts
        estimated_fee = estimated_maker_fee(
            quote_price,
            contracts,
            fee_rate=plan.maker_fee_rate,
            fee_multiplier=plan.maker_fee_multiplier,
        )
        actual_fee = _execution_fee(execution)
        fee = actual_fee if actual_fee is not None else estimated_fee
        fee_source = str(
            execution.get("fee_source")
            or ("execution" if actual_fee is not None else "modeled")
        )
        net_edge = gross_edge - fee
        net_edge_rate = net_edge / (outcome_fair * contracts)
        quote_id = reservation.quote_id
        marker = f"<!-- kalshi-rfq-fill:{quote_id} -->"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            current = self.path.read_text(encoding="utf-8")
        else:
            current = self.HEADER
            self.path.write_text(current, encoding="utf-8")
        if marker in current:
            return False
        executed_at = execution.get("executed_ts") or datetime.now(UTC).isoformat()
        order_id = execution.get("order_id") or execution.get("creator_order_id") or "-"
        if plan.request.is_combo:
            structure = "Independent moneyline parlay"
            event = ", ".join(leg.event_ticker for leg in plan.request.legs)
            legs = "; ".join(
                f"`{leg.market_ticker}` {leg.side.upper()}" for leg in plan.request.legs
            )
        else:
            structure = "Single moneyline"
            event = event_ticker
            legs = f"`{plan.request.ticker}` {side.upper()}"
        row = (
            f"| {self._cell(executed_at)} | {structure} | "
            f"{self._cell(event)} | {legs} | {side.upper()} | {contracts} | "
            f"${outcome_fair:.4f} | ${quote_price:.4f} | {gross_edge_rate:.4%} | "
            f"${fee:.4f} | {self._cell(fee_source)} | {net_edge_rate:.4%} | "
            f"${gross_edge:.4f} | ${net_edge:.4f} | "
            f"{self._cell(plan.request.rfq_id)} | {self._cell(quote_id)} {marker} | "
            f"{self._cell(order_id)} | {self._cell(plan.fair.source)} |\n"
        )
        with self.path.open("a", encoding="utf-8") as output:
            output.write(row)
        return True


class RFQRiskLedger:
    def __init__(
        self,
        config: RFQMakerConfig,
        *,
        positions: dict[str, Decimal] | None = None,
        available_balance: Decimal = Decimal("0"),
    ) -> None:
        self.config = config
        self.positions = dict(positions or {})
        self.available_balance = available_balance
        self.reservations: dict[str, QuoteReservation] = {}
        self.executed_contracts = ZERO
        self.executed_notional = ZERO
        self.executed_quotes = 0
        self.executed_combo_notional: dict[str, Decimal] = {}
        self.executed_leg_outcome_notional: dict[str, Decimal] = {}

    @staticmethod
    def _leg_outcome_keys(plan: RFQQuotePlan) -> tuple[str, ...]:
        return tuple(
            f"{leg.market_ticker}:{leg.side}" for leg in plan.request.legs
        )

    @staticmethod
    def _side_cost(plan: RFQQuotePlan, side: str, contracts: Decimal | None = None) -> Decimal:
        bid = plan.yes_bid if side == "yes" else plan.no_bid
        count = contracts if contracts is not None else plan.contracts_for(side)
        fee = estimated_maker_fee(
            bid,
            count,
            fee_rate=plan.maker_fee_rate,
            fee_multiplier=plan.maker_fee_multiplier,
        )
        return bid * count + fee

    def _reserved_notional_for_combo(self, ticker: str) -> Decimal:
        values = [
            item.plan.maximum_cost
            for item in self.reservations.values()
            if item.plan.request.ticker == ticker
        ]
        return max(values, default=ZERO) if self.config.first_fill_wins else sum(values, ZERO)

    def _reserved_notional_for_leg(self, key: str) -> Decimal:
        values = [
            item.plan.maximum_cost
            for item in self.reservations.values()
            if key in self._leg_outcome_keys(item.plan)
        ]
        return max(values, default=ZERO) if self.config.first_fill_wins else sum(values, ZERO)

    def _ensure_exposure_caps(
        self,
        plan: RFQQuotePlan,
        *,
        cost: Decimal | None = None,
        exclude_rfq_id: str | None = None,
    ) -> None:
        cost = plan.maximum_cost if cost is None else cost
        reservations = self.reservations
        excluded = reservations.pop(exclude_rfq_id, None) if exclude_rfq_id else None
        try:
            combo_cap = self.config.per_combo_notional_cap
            if combo_cap is not None:
                used = self.executed_combo_notional.get(plan.request.ticker, ZERO)
                used += self._reserved_notional_for_combo(plan.request.ticker)
                if used + cost > combo_cap:
                    raise ValueError("RFQ exceeds the per-combo cumulative notional cap")
            leg_cap = self.config.per_leg_outcome_notional_cap
            if leg_cap is not None:
                for key in self._leg_outcome_keys(plan):
                    used = self.executed_leg_outcome_notional.get(key, ZERO)
                    used += self._reserved_notional_for_leg(key)
                    if used + cost > leg_cap:
                        raise ValueError(
                            "RFQ exceeds the per-leg-outcome cumulative notional cap"
                        )
        finally:
            if excluded is not None and exclude_rfq_id is not None:
                reservations[exclude_rfq_id] = excluded

    def _reserved_balance(self) -> Decimal:
        costs = [reservation.plan.maximum_cost for reservation in self.reservations.values()]
        if self.config.first_fill_wins:
            return max(costs, default=ZERO)
        return sum(costs, ZERO)

    def _projected_reserved_balance(self, plan: RFQQuotePlan) -> Decimal:
        if self.config.first_fill_wins:
            return max(self._reserved_balance(), plan.maximum_cost)
        return self._reserved_balance() + plan.maximum_cost

    def constrain(self, plan: RFQQuotePlan) -> RFQQuotePlan:
        if self.config.contracts_only and plan.request.contracts is None:
            raise ValueError("target-cost RFQs are disabled")
        if (
            plan.request.target_cost is not None
            and self.config.max_target_cost is not None
            and plan.request.target_cost > self.config.max_target_cost
        ):
            raise ValueError("RFQ target cost exceeds the configured limit")
        if (
            self.config.max_session_executions is not None
            and self.executed_quotes
            + sum(item.accepted_side is not None for item in self.reservations.values())
            >= self.config.max_session_executions
        ):
            raise ValueError("RFQ session execution limit reached")
        if plan.request.contracts is not None:
            if plan.request.contracts < self.config.min_contracts:
                raise ValueError("RFQ size is below the minimum contract limit")
            if plan.request.contracts > self.config.max_contracts:
                raise ValueError("RFQ size exceeds the per-request contract limit")
        if len(self.reservations) >= self.config.max_active_quotes:
            raise ValueError("active RFQ quote limit reached")
        self._ensure_exposure_caps(plan)
        position = self.positions.get(plan.request.ticker, ZERO)
        long_reserve = sum(
            (
                item.plan.yes_contracts
                for item in self.reservations.values()
                if item.plan.request.ticker == plan.request.ticker and item.plan.yes_bid > ZERO
            ),
            ZERO,
        )
        short_reserve = sum(
            (
                item.plan.no_contracts
                for item in self.reservations.values()
                if item.plan.request.ticker == plan.request.ticker and item.plan.no_bid > ZERO
            ),
            ZERO,
        )
        if self.config.first_fill_wins:
            # Outstanding quotes are conditional alternatives: the atomic
            # acceptance gate below permits at most one of them to confirm.
            long_reserve = ZERO
            short_reserve = ZERO
        yes_bid = plan.yes_bid
        no_bid = plan.no_bid
        yes_contracts = plan.yes_contracts
        no_contracts = plan.no_contracts
        if self.config.max_session_contracts is not None:
            reserved_contracts = sum(
                (
                    max(item.plan.yes_contracts, item.plan.no_contracts)
                    for item in self.reservations.values()
                ),
                ZERO,
            )
            if self.config.first_fill_wins:
                reserved_contracts = ZERO
            remaining_contracts = (
                self.config.max_session_contracts
                - self.executed_contracts
                - reserved_contracts
            )
            if yes_contracts > remaining_contracts:
                yes_bid = ZERO
            if no_contracts > remaining_contracts:
                no_bid = ZERO
        if (
            yes_contracts < self.config.min_contracts
            or yes_contracts > self.config.max_contracts
            or yes_bid * yes_contracts + plan.yes_estimated_fee > self.config.max_notional
            or position + long_reserve + yes_contracts > self.config.max_abs_position
        ):
            yes_bid = ZERO
        if (
            no_contracts < self.config.min_contracts
            or no_contracts > self.config.max_contracts
            or no_bid * no_contracts + plan.no_estimated_fee > self.config.max_notional
            or position - short_reserve - no_contracts < -self.config.max_abs_position
        ):
            no_bid = ZERO
        constrained = replace(
            plan,
            yes_bid=yes_bid,
            no_bid=no_bid,
            yes_edge_rate=plan.yes_edge_rate if yes_bid > ZERO else None,
            no_edge_rate=plan.no_edge_rate if no_bid > ZERO else None,
            yes_gross_edge_rate=plan.yes_gross_edge_rate if yes_bid > ZERO else None,
            no_gross_edge_rate=plan.no_gross_edge_rate if no_bid > ZERO else None,
            yes_estimated_fee=plan.yes_estimated_fee if yes_bid > ZERO else ZERO,
            no_estimated_fee=plan.no_estimated_fee if no_bid > ZERO else ZERO,
        )
        if not constrained.has_side:
            raise ValueError("RFQ has no side within position and notional limits")
        if (
            self.config.max_session_notional is not None
            and self.executed_notional
            + self._projected_reserved_balance(constrained)
            > self.config.max_session_notional
        ):
            raise ValueError("RFQ quote would exceed the session notional limit")
        if self._projected_reserved_balance(constrained) > self.available_balance:
            raise ValueError("RFQ quote would exceed unreserved available balance")
        return constrained

    def reserve(self, plan: RFQQuotePlan, quote_id: str) -> None:
        if plan.request.rfq_id in self.reservations:
            raise ValueError("RFQ is already reserved")
        self.reservations[plan.request.rfq_id] = QuoteReservation(plan, quote_id)

    def release(self, rfq_id: str) -> QuoteReservation | None:
        return self.reservations.pop(rfq_id, None)

    def by_quote(self, quote_id: str) -> QuoteReservation | None:
        return next(
            (item for item in self.reservations.values() if item.quote_id == quote_id),
            None,
        )

    def mark_acceptance(
        self,
        reservation: QuoteReservation,
        *,
        side: str,
        contracts: Decimal,
    ) -> None:
        if reservation.accepted_side is not None:
            raise ValueError("RFQ quote acceptance was already recorded")
        cost = self._side_cost(reservation.plan, side, contracts)
        self._ensure_exposure_caps(
            reservation.plan,
            cost=cost,
            exclude_rfq_id=reservation.plan.request.rfq_id,
        )
        if self.config.max_session_executions is not None:
            accepted_quotes = sum(
                item.accepted_side is not None
                for item in self.reservations.values()
                if item is not reservation
            )
            if (
                self.executed_quotes + accepted_quotes
                >= self.config.max_session_executions
            ):
                raise ValueError("RFQ session execution limit reached before confirmation")
        reservation.accepted_side = side
        reservation.accepted_contracts = contracts

    def record_execution(self, reservation: QuoteReservation) -> None:
        side = reservation.accepted_side
        count = reservation.accepted_contracts
        if side not in {"yes", "no"} or count <= ZERO:
            return
        ticker = reservation.plan.request.ticker
        self.positions[ticker] = self.positions.get(ticker, ZERO) + (
            count if side == "yes" else -count
        )
        self.executed_contracts += count
        self.executed_quotes += 1
        price = reservation.plan.yes_bid if side == "yes" else reservation.plan.no_bid
        fee = estimated_maker_fee(
            price,
            count,
            fee_rate=reservation.plan.maker_fee_rate,
            fee_multiplier=reservation.plan.maker_fee_multiplier,
        )
        self.executed_notional += price * count + fee
        cost = price * count + fee
        self.executed_combo_notional[ticker] = (
            self.executed_combo_notional.get(ticker, ZERO) + cost
        )
        for key in self._leg_outcome_keys(reservation.plan):
            self.executed_leg_outcome_notional[key] = (
                self.executed_leg_outcome_notional.get(key, ZERO) + cost
            )
        self.available_balance = max(self.available_balance - price * count - fee, ZERO)


class RFQMessageStream(Protocol):
    def events(self) -> AsyncIterator[dict[str, Any]]: ...


class AuditLog(Protocol):
    def append(self, event: str, payload: dict[str, object]) -> None: ...


class RFQMaker:
    def __init__(
        self,
        *,
        client: KalshiClient,
        stream: RFQMessageStream,
        fair_book: MoneylineFairBook,
        config: RFQMakerConfig,
        audit_log: AuditLog,
        execute: bool,
        fill_ledger: MarkdownRFQFillLedger | None = None,
        allowed_tickers: set[str] | None = None,
        allowed_collections: set[str] | None = None,
    ) -> None:
        config.validate()
        if execute and config.coverage_shadow:
            raise ValueError("RFQ coverage shadow cannot submit quotes")
        self.client = client
        self.stream = stream
        self.fair_book = fair_book
        self.config = config
        self.audit_log = audit_log
        self.fill_ledger = fill_ledger
        self.execute = execute
        self.allowed_tickers = set(allowed_tickers or ())
        self.allowed_collections = set(allowed_collections or ())
        self.price_grids: dict[str, PriceGrid] = {}
        self.maker_fee_multipliers: dict[str, Decimal] = {}
        self.series_maker_fee_multipliers: dict[str, Decimal] = {}
        self._series_fee_tasks: dict[str, asyncio.Task[Decimal]] = {}
        self.event_tickers: dict[str, str] = {}
        self.event_markets: dict[str, tuple[str, ...]] = {}
        self.ledger = RFQRiskLedger(config)
        self.closed_rfqs: set[str] = set()
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._rfq_tails: dict[str, asyncio.Task[None]] = {}
        self._unsupported_counts: dict[str, int] = {}
        self._coverage_counts: dict[str, dict[str, int]] = {}
        self._coverage_messages = 0
        self._existing_position_events: set[str] = set()
        self._existing_position_participants: set[str] = set()
        self._creator_arrivals: dict[str, deque[float]] = {}
        self._blocked_creators: set[str] = set()
        self._combo_request_counts: dict[str, int] = {}
        self._rfq_ids_seen: set[str] = set()

    def _audit(self, event: str, payload: dict[str, object]) -> None:
        self.audit_log.append(event, payload)

    def _flush_unsupported_summary(self) -> None:
        if not self._unsupported_counts:
            return
        counts = dict(sorted(self._unsupported_counts.items()))
        self._unsupported_counts.clear()
        self._audit(
            "rfq_unsupported_summary",
            {
                "messages": sum(counts.values()),
                "reasons": counts,
                "risk_reserved": False,
            },
        )

    def _record_unsupported(self, reason: str) -> None:
        self._unsupported_counts[reason] = self._unsupported_counts.get(reason, 0) + 1
        if sum(self._unsupported_counts.values()) >= UNSUPPORTED_AUDIT_BATCH_SIZE:
            self._flush_unsupported_summary()

    @staticmethod
    def _coverage_outcome(reason: str) -> str:
        if reason.startswith("fair value is stale"):
            return "fair value is stale"
        if reason.startswith("active RFQ exposure overlaps an event"):
            return "active RFQ exposure overlaps an event"
        if reason.startswith("active RFQ exposure overlaps a participant"):
            return "active RFQ exposure overlaps a participant"
        if "rfq_closed" in reason.casefold() or "RFQ closed" in reason:
            return "RFQ closed before quote submission"
        return reason

    def _flush_coverage_summary(self) -> None:
        if not self._coverage_messages:
            return
        sizing = {
            mode: dict(sorted(outcomes.items()))
            for mode, outcomes in sorted(self._coverage_counts.items())
        }
        messages = self._coverage_messages
        self._coverage_counts.clear()
        self._coverage_messages = 0
        self._audit(
            "rfq_coverage_summary",
            {
                "messages": messages,
                "sizing": sizing,
                "execute": self.execute,
                "coverage_shadow": self.config.coverage_shadow,
            },
        )
        repeated = sum(max(0, count - 1) for count in self._combo_request_counts.values())
        self._audit(
            "rfq_unique_combo_summary",
            {
                "unique_rfqs": len(self._rfq_ids_seen),
                "unique_combos": len(self._combo_request_counts),
                "repeat_rfqs": repeated,
                "top_combos": [
                    {"ticker": ticker, "requests": count}
                    for ticker, count in sorted(
                        self._combo_request_counts.items(),
                        key=lambda item: (-item[1], item[0]),
                    )[:10]
                ],
            },
        )

    def _record_coverage(self, sizing_mode: str, outcome: str) -> None:
        mode = sizing_mode if sizing_mode in {"contracts", "target_cost"} else "invalid"
        outcomes = self._coverage_counts.setdefault(mode, {})
        normalized = self._coverage_outcome(outcome)
        outcomes[normalized] = outcomes.get(normalized, 0) + 1
        self._coverage_messages += 1
        if self._coverage_messages >= COVERAGE_AUDIT_BATCH_SIZE:
            self._flush_coverage_summary()

    def _prefilter_created(self, message: dict[str, Any]) -> tuple[str | None, str]:
        payload = message.get("msg")
        if not isinstance(payload, dict):
            return None, "invalid"
        try:
            request = RFQRequest.from_message(message)
        except (InvalidOperation, TypeError, ValueError):
            return "invalid RFQ shape", "invalid"
        self._rfq_ids_seen.add(request.rfq_id)
        if request.is_combo:
            self._combo_request_counts[request.ticker] = (
                self._combo_request_counts.get(request.ticker, 0) + 1
            )
        if not request.is_combo:
            if self.config.combo_only:
                return "single-market RFQs are disabled", request.sizing_mode
            if not self._ticker_allowed(request.ticker):
                return "ticker is not on the moneyline allowlist", request.sizing_mode
            return None, request.sizing_mode
        if self.config.contracts_only and request.contracts is None:
            return "target-cost RFQs are disabled", request.sizing_mode
        if self.allowed_collections and request.collection_ticker not in self.allowed_collections:
            return "MVE collection is not on the allowlist", request.sizing_mode
        if not self.config.min_legs <= len(request.legs) <= self.config.max_legs:
            return "parlay leg count is outside configured limits", request.sizing_mode
        if any(not self._ticker_allowed(leg.market_ticker) for leg in request.legs):
            return "ticker is not on the moneyline allowlist", request.sizing_mode
        return None, request.sizing_mode

    async def _enforce_creator_rate(self, request: RFQRequest) -> RFQRequest:
        if not self.config.creator_rate_limit:
            return request
        creator_id = request.creator_id
        if not creator_id:
            raw = await asyncio.to_thread(self.client.get_rfq, request.rfq_id)
            creator_id = str(raw.get("creator_id", "")).strip()
        if not creator_id:
            raise ValueError("RFQ creator identity is unavailable")
        if creator_id in self._blocked_creators:
            raise ValueError("RFQ creator is blocked for burst posting")
        now = time.monotonic()
        arrivals = self._creator_arrivals.setdefault(creator_id, deque())
        cutoff = now - self.config.creator_burst_window_seconds
        while arrivals and arrivals[0] < cutoff:
            arrivals.popleft()
        if len(arrivals) >= self.config.creator_burst_limit:
            self._blocked_creators.add(creator_id)
            raise ValueError("RFQ creator exceeded the burst-posting limit")
        arrivals.append(now)
        return replace(request, creator_id=creator_id)

    def _record_fill(
        self,
        reservation: QuoteReservation,
        execution: dict[str, Any],
        *,
        reconciled: bool = False,
    ) -> None:
        plan = reservation.plan
        side = reservation.accepted_side
        outcome_fair = reservation.confirmed_outcome_fair
        if outcome_fair is None and side in {"yes", "no"}:
            outcome_fair = plan.fair.probability if side == "yes" else ONE - plan.fair.probability
        quoted_price = plan.yes_bid if side == "yes" else plan.no_bid if side == "no" else None
        edge_rate = reservation.confirmed_edge_rate
        if plan.request.is_combo:
            event_ticker = ",".join(leg.event_ticker for leg in plan.request.legs)
            structure = "independent_moneyline_parlay"
            legs = [f"{leg.market_ticker}:{leg.side}" for leg in plan.request.legs]
        else:
            event_ticker = self.event_tickers.get(plan.request.ticker, "")
            structure = "single_moneyline"
            legs = [f"{plan.request.ticker}:{side or 'unknown'}"]
        payload: dict[str, object] = {
            "rfq_id": plan.request.rfq_id,
            "quote_id": reservation.quote_id,
            "ticker": plan.request.ticker,
            "event_ticker": event_ticker,
            "structure": structure,
            "collection_ticker": plan.request.collection_ticker or None,
            "legs": legs,
            "accepted_side": side,
            "contracts_fp": str(reservation.accepted_contracts),
            "outcome_fair": str(outcome_fair) if outcome_fair is not None else None,
            "quoted_price": str(quoted_price) if quoted_price is not None else None,
            "edge_rate": str(edge_rate) if edge_rate is not None else None,
            "order_id": str(execution.get("order_id") or execution.get("creator_order_id") or ""),
            "executed_ts": str(execution.get("executed_ts") or ""),
            "reconciled": reconciled,
        }
        if outcome_fair is not None and quoted_price is not None and side in {"yes", "no"}:
            contracts = reservation.accepted_contracts
            gross_edge_rate = (outcome_fair - quoted_price) / outcome_fair
            gross_edge = (outcome_fair - quoted_price) * contracts
            estimated_fee = estimated_maker_fee(
                quoted_price,
                contracts,
                fee_rate=plan.maker_fee_rate,
                fee_multiplier=plan.maker_fee_multiplier,
            )
            actual_fee = _execution_fee(execution)
            fee = actual_fee if actual_fee is not None else estimated_fee
            net_edge = gross_edge - fee
            net_edge_rate = net_edge / (outcome_fair * contracts)
            payload.update(
                {
                    "gross_edge_rate": str(gross_edge_rate),
                    "gross_edge_dollars": str(gross_edge),
                    "estimated_maker_fee": str(estimated_fee),
                    "actual_fee": str(actual_fee) if actual_fee is not None else None,
                    "fee_source": str(
                        execution.get("fee_source")
                        or ("execution" if actual_fee is not None else "modeled")
                    ),
                    "net_edge_rate": str(net_edge_rate),
                    "net_edge_dollars": str(net_edge),
                    "edge_rate": str(net_edge_rate),
                    "fee_model_breach": net_edge_rate < self.config.edge_rate,
                }
            )
        if self.fill_ledger is not None:
            try:
                self.fill_ledger.append(
                    reservation,
                    event_ticker=event_ticker,
                    execution=execution,
                )
            except Exception as exc:
                self._audit(
                    "rfq_fill_ledger_failed",
                    {
                        "rfq_id": plan.request.rfq_id,
                        "quote_id": reservation.quote_id,
                        "reason": str(exc),
                    },
                )
        self._audit("rfq_quote_executed", payload)

    async def _execution_with_actual_fee(
        self,
        execution: dict[str, Any],
    ) -> dict[str, Any]:
        if _execution_fee(execution) is not None:
            return {**execution, "fee_source": execution.get("fee_source") or "execution"}
        order_id = str(execution.get("creator_order_id") or execution.get("order_id") or "")
        if not order_id:
            return execution
        for delay in (0.0, 0.1, 0.25):
            if delay:
                await asyncio.sleep(delay)
            try:
                fills = await asyncio.to_thread(
                    self.client.get_fills,
                    order_id=order_id,
                    subaccount=self.config.subaccount,
                    limit=1000,
                )
                fees = [_execution_fee(fill) for fill in fills]
                if fills and all(fee is not None for fee in fees):
                    total = sum((fee for fee in fees if fee is not None), ZERO)
                    return {**execution, "fee_cost": str(total), "fee_source": "fills_api"}
            except Exception as exc:
                self._audit(
                    "rfq_fill_fee_lookup_failed",
                    {"order_id": order_id, "reason": str(exc)},
                )
                break
        return execution

    def _require_expected_subaccount(self, payload: dict[str, Any]) -> None:
        if not self.config.require_subaccount_metadata:
            return
        raw = payload.get("subaccount")
        if raw is None:
            raw = payload.get("creator_subaccount")
        # Kalshi omits/nulls this field for the primary account and returns it
        # when a numbered subaccount is involved. The quote ID has already been
        # matched to a reservation created by this authenticated process.
        if raw is None and self.config.subaccount == 0:
            return
        if raw is None or int(raw) != self.config.subaccount:
            raise ValueError("RFQ message subaccount is missing or does not match the canary")

    @staticmethod
    def _accepted_contracts(payload: dict[str, Any], side: str) -> Decimal:
        for key in (
            "contracts_accepted_fp",
            f"{side}_contracts_offered_fp",
            f"{side}_contracts_fp",
            "contracts_fp",
        ):
            raw = payload.get(key)
            if raw is None or raw == "":
                continue
            count = as_decimal(raw)
            if count > ZERO:
                return count
        return ZERO

    @staticmethod
    def _recover_acceptance(reservation: QuoteReservation, quote: dict[str, Any]) -> None:
        side = str(quote.get("accepted_side", "")).casefold()
        if side not in {"yes", "no"}:
            raise ValueError("executed quote is missing its accepted side")
        count = RFQMaker._accepted_contracts(quote, side)
        if count <= ZERO or count > reservation.plan.contracts_for(side):
            raise ValueError("executed quote has an invalid accepted size")
        reservation.accepted_side = side
        reservation.accepted_contracts = count

    def _fair(self, ticker: str, *, now: datetime | None = None) -> MoneylineFair:
        fair = self.fair_book.get(ticker)
        if fair is None:
            raise ValueError("no safely matched moneyline fair is cached")
        now = now or datetime.now(UTC)
        age = (now - fair.observed_at).total_seconds()
        if age < -5 or age > self.config.max_fair_age_seconds:
            raise ValueError(f"fair value is stale ({age:.3f}s old)")
        if not self.config.allow_live_games and now >= fair.event_start:
            raise ValueError("in-play moneyline RFQs are disabled")
        if (
            self.config.max_hours_to_start is not None
            and (fair.event_start - now).total_seconds()
            > float(self.config.max_hours_to_start) * 60 * 60
        ):
            raise ValueError("event is beyond the maximum pregame horizon")
        if self.config.pinnacle_only and fair.source != "odds-consensus:pinnacle":
            raise ValueError("fair value is not sourced exclusively from Pinnacle")
        return fair

    def _ticker_allowed(self, ticker: str) -> bool:
        if self.allowed_tickers:
            return ticker in self.allowed_tickers
        return ticker in self.fair_book.tickers()

    def _request_fair(
        self,
        request: RFQRequest,
        *,
        now: datetime | None = None,
    ) -> tuple[MoneylineFair, tuple[MoneylineFair, ...]]:
        if not request.is_combo:
            if self.config.combo_only:
                raise ValueError("single-market RFQs are disabled")
            if not self._ticker_allowed(request.ticker):
                raise ValueError("ticker is not on the moneyline allowlist")
            return self._fair(request.ticker, now=now), ()
        if self.allowed_collections and request.collection_ticker not in self.allowed_collections:
            raise ValueError("MVE collection is not on the allowlist")
        if not self.config.min_legs <= len(request.legs) <= self.config.max_legs:
            raise ValueError("parlay leg count is outside configured limits")
        market_tickers = [leg.market_ticker for leg in request.legs]
        event_tickers = [leg.event_ticker for leg in request.legs]
        if len(set(market_tickers)) != len(market_tickers):
            raise ValueError("parlay repeats a market")
        if len(set(event_tickers)) != len(event_tickers):
            raise ValueError("same-game parlay legs are not independent")

        probability = ONE
        leg_fairs: list[MoneylineFair] = []
        seen_participants: set[str] = set()
        for leg in request.legs:
            if not self._ticker_allowed(leg.market_ticker):
                raise ValueError("parlay leg ticker is not on the moneyline allowlist")
            fair = self._fair(leg.market_ticker, now=now)
            if (
                self.config.moneyline_yes_only
                and fair.market_type == "moneyline"
                and leg.side != "yes"
            ):
                raise ValueError("moneyline RFQ legs must be YES selections")
            if (
                fair.market_type == "moneyline"
                and leg.side == "yes"
                and self.config.max_moneyline_fair is not None
                and fair.probability > self.config.max_moneyline_fair
            ):
                raise ValueError("moneyline favorite is steeper than the configured limit")
            if not fair.event_ticker:
                raise ValueError("parlay leg fair is missing an event ticker")
            if fair.event_ticker != leg.event_ticker:
                raise ValueError("parlay leg event does not match its fair-value event")
            if len(fair.participants) < 2:
                raise ValueError("parlay leg fair is missing participant identities")
            overlap = seen_participants.intersection(fair.participants)
            if overlap:
                raise ValueError(
                    "parlay legs share a participant and are not independent: "
                    + ", ".join(sorted(overlap))
                )
            seen_participants.update(fair.participants)
            probability *= fair.probability if leg.side == "yes" else ONE - fair.probability
            leg_fairs.append(fair)

        aggregate = MoneylineFair(
            ticker=request.ticker,
            probability=probability,
            observed_at=min(item.observed_at for item in leg_fairs),
            event_start=min(item.event_start for item in leg_fairs),
            source="parlay-product:" + ",".join(sorted({item.source for item in leg_fairs})),
            event_ticker=",".join(event_tickers),
            participants=frozenset(seen_participants),
            market_type="parlay",
            sport="mixed",
        )
        aggregate.validate()
        return aggregate, tuple(leg_fairs)

    def _price_request(
        self,
        request: RFQRequest,
        fair: MoneylineFair,
        *,
        grid: PriceGrid,
        fee_multiplier: Decimal,
        leg_fairs: tuple[MoneylineFair, ...],
    ) -> RFQQuotePlan:
        if self.config.pricing_mode == "lead_fixed":
            return price_lead_style_rfq(
                request,
                fair,
                price_grid=grid,
                margin=self.config.fixed_margin,
                minimum_cushion=self.config.minimum_cushion,
                two_leg_premium=self.config.two_leg_premium,
                player_prop_premium=self.config.player_prop_premium,
                soccer_premium=self.config.soccer_premium,
                maker_fee_rate=self.config.maker_fee_rate,
                maker_fee_multiplier=fee_multiplier,
                leg_fairs=leg_fairs,
            )
        plan = price_moneyline_rfq(
            request,
            fair,
            price_grid=grid,
            edge_rate=self.config.edge_rate,
            maker_fee_rate=self.config.maker_fee_rate,
            maker_fee_multiplier=fee_multiplier,
            leg_fairs=leg_fairs,
        )
        if self.config.no_side_only:
            plan = replace(
                plan,
                yes_bid=ZERO,
                yes_edge_rate=None,
                yes_gross_edge_rate=None,
                yes_estimated_fee=ZERO,
            )
            if plan.no_bid <= ZERO:
                raise ValueError("NO-only strategy has no quoteable side")
        return plan

    async def _load_series_maker_fee_multiplier(self, series_ticker: str) -> Decimal:
        series = await asyncio.to_thread(self.client.get_series_details, series_ticker)
        if str(series.get("ticker", "")).strip() != series_ticker:
            raise ValueError("derived market series ticker failed canonical verification")
        fee_type = str(series.get("fee_type", "")).casefold().strip()
        if fee_type == "quadratic":
            multiplier = ZERO
        elif fee_type == "quadratic_with_maker_fees":
            multiplier = as_decimal(series.get("fee_multiplier", "0"))
            if multiplier <= ZERO:
                raise ValueError("maker-fee series has a non-positive fee multiplier")
        else:
            raise ValueError(f"unsupported or unknown RFQ maker fee type: {fee_type or 'missing'}")
        self.series_maker_fee_multipliers[series_ticker] = multiplier
        return multiplier

    async def _maker_fee_multiplier(
        self,
        market: dict[str, Any],
        *,
        market_ticker: str,
    ) -> Decimal:
        series_ticker = str(market.get("series_ticker", "")).strip()
        if not series_ticker and "-" in market_ticker:
            # Some current market payloads omit series_ticker. Kalshi market
            # tickers are series-prefixed; verify the derived value against the
            # canonical Series endpoint before using its fee metadata.
            series_ticker = market_ticker.split("-", 1)[0]
        if not series_ticker:
            raise ValueError("market is missing a series ticker required for fee checks")
        cached = self.series_maker_fee_multipliers.get(series_ticker)
        if cached is not None:
            return cached
        task = self._series_fee_tasks.get(series_ticker)
        if task is None:
            task = asyncio.create_task(
                self._load_series_maker_fee_multiplier(series_ticker)
            )
            self._series_fee_tasks[series_ticker] = task
        try:
            return await task
        finally:
            if task.done():
                self._series_fee_tasks.pop(series_ticker, None)

    async def _ensure_leg_market(self, ticker: str) -> PriceGrid:
        existing = self.price_grids.get(ticker)
        if existing is not None:
            return existing
        market = await asyncio.to_thread(self.client.get_market, ticker)
        if str(market.get("status", "")) not in {"active", "open"}:
            raise ValueError("market is not active")
        event_ticker = str(market.get("event_ticker", "")).strip()
        if not event_ticker:
            raise ValueError("market is missing an event ticker required for correlation checks")
        event, position, fee_multiplier = await asyncio.gather(
            asyncio.to_thread(self.client.get_event, event_ticker),
            asyncio.to_thread(
                self.client.get_position,
                ticker,
                subaccount=self.config.subaccount,
            ),
            self._maker_fee_multiplier(market, market_ticker=ticker),
        )
        event_market_tickers = tuple(
            sorted(
                {
                    str(item.get("ticker", "")).strip()
                    for item in event.get("markets", [])
                    if isinstance(item, dict) and str(item.get("ticker", "")).strip()
                }
                | {ticker}
            )
        )
        sibling_tickers = tuple(item for item in event_market_tickers if item != ticker)
        if sibling_tickers:
            sibling_positions = await asyncio.gather(
                *(
                    asyncio.to_thread(
                        self.client.get_position,
                        sibling,
                        subaccount=self.config.subaccount,
                    )
                    for sibling in sibling_tickers
                )
            )
            correlated_positions = {
                sibling: as_decimal(sibling_position)
                for sibling, sibling_position in zip(
                    sibling_tickers,
                    sibling_positions,
                    strict=True,
                )
                if as_decimal(sibling_position) != ZERO
            }
            if correlated_positions:
                detail = ", ".join(
                    f"{sibling}={sibling_position}"
                    for sibling, sibling_position in sorted(correlated_positions.items())
                )
                raise ValueError(
                    "existing position in a correlated same-event market blocks RFQ "
                    f"quoting: {detail}"
                )
        grid = PriceGrid.from_market(market)
        async with self._lock:
            self.price_grids[ticker] = grid
            self.maker_fee_multipliers[ticker] = fee_multiplier
            self.event_tickers[ticker] = event_ticker
            self.event_markets[event_ticker] = event_market_tickers
            self.ledger.positions[ticker] = as_decimal(position)
        return grid

    async def _ensure_quote_market(self, request: RFQRequest) -> PriceGrid:
        if not request.is_combo:
            return await self._ensure_leg_market(request.ticker)
        existing = self.price_grids.get(request.ticker)
        if existing is not None:
            return existing
        market, position = await asyncio.gather(
            asyncio.to_thread(self.client.get_market, request.ticker),
            asyncio.to_thread(
                self.client.get_position,
                request.ticker,
                subaccount=self.config.subaccount,
            ),
        )
        if str(market.get("status", "")) not in {"active", "open"}:
            raise ValueError("combo market is not active")
        actual_collection = str(market.get("mve_collection_ticker", "")).strip()
        if actual_collection and actual_collection != request.collection_ticker:
            raise ValueError("combo market collection does not match the RFQ")
        actual_legs_raw = market.get("mve_selected_legs")
        if not isinstance(actual_legs_raw, list):
            raise ValueError("combo market is missing selected-leg metadata")
        actual_legs = {
            (
                str(item.get("market_ticker", "")).strip(),
                str(item.get("event_ticker", "")).strip(),
                str(item.get("side", "")).casefold().strip(),
            )
            for item in actual_legs_raw
            if isinstance(item, dict)
        }
        requested_legs = {(leg.market_ticker, leg.event_ticker, leg.side) for leg in request.legs}
        if actual_legs != requested_legs:
            raise ValueError("combo market selected legs do not match the RFQ")
        fee_multiplier = await self._maker_fee_multiplier(
            market,
            market_ticker=request.ticker,
        )
        grid = PriceGrid.from_market(market)
        async with self._lock:
            self.price_grids[request.ticker] = grid
            self.maker_fee_multipliers[request.ticker] = fee_multiplier
            self.event_tickers[request.ticker] = str(market.get("event_ticker", ""))
            self.ledger.positions[request.ticker] = as_decimal(position)
        return grid

    def _plan_exposure(self, plan: RFQQuotePlan) -> tuple[set[str], set[str]]:
        request = plan.request
        requested_events = (
            {leg.event_ticker for leg in request.legs}
            if request.is_combo
            else {self.event_tickers.get(request.ticker, "")}
        )
        requested_events.discard("")
        requested_participants = set(plan.fair.participants)
        return requested_events, requested_participants

    def _ensure_no_overlapping_reservation(self, plan: RFQQuotePlan) -> None:
        if self.config.first_fill_wins:
            return
        requested_events, requested_participants = self._plan_exposure(plan)
        for reservation in self.ledger.reservations.values():
            other_events, other_participants = self._plan_exposure(reservation.plan)
            overlap = requested_events.intersection(other_events)
            if overlap:
                raise ValueError(
                    "active RFQ exposure overlaps an event: " + ", ".join(sorted(overlap))
                )
            participant_overlap = requested_participants.intersection(other_participants)
            if participant_overlap:
                raise ValueError(
                    "active RFQ exposure overlaps a participant: "
                    + ", ".join(sorted(participant_overlap))
                )

    def _ensure_no_existing_position_overlap(self, plan: RFQQuotePlan) -> None:
        requested_events, requested_participants = self._plan_exposure(plan)
        event_overlap = requested_events.intersection(self._existing_position_events)
        if event_overlap:
            raise ValueError(
                "RFQ exposure overlaps an existing-position event: "
                + ", ".join(sorted(event_overlap))
            )
        participant_overlap = requested_participants.intersection(
            self._existing_position_participants
        )
        if participant_overlap:
            raise ValueError(
                "RFQ exposure overlaps an existing-position participant: "
                + ", ".join(sorted(participant_overlap))
            )

    async def _load_existing_position_exposure(
        self,
        positions: list[dict[str, Any]],
    ) -> None:
        if positions and not self.config.allow_existing_positions:
            raise ValueError("RFQ account already has positions")
        for position in positions:
            ticker = str(position.get("ticker", "")).strip()
            count = as_decimal(position.get("position_fp", "0"))
            if not ticker or count == ZERO:
                continue
            market = await asyncio.to_thread(self.client.get_market, ticker)
            collection = str(market.get("mve_collection_ticker", "")).strip()
            raw_legs = market.get("mve_selected_legs")
            if collection not in self.allowed_collections or not isinstance(raw_legs, list):
                raise ValueError(
                    "existing position is not an allowed independent-moneyline combo"
                )
            legs = tuple(RFQLeg.from_payload(raw) for raw in raw_legs)
            if not self.config.min_legs <= len(legs) <= self.config.max_legs:
                raise ValueError("existing combo position has an unsupported leg count")
            events = {leg.event_ticker for leg in legs}
            if len(events) != len(legs):
                raise ValueError("existing combo position contains same-game legs")
            participants: set[str] = set()
            for leg in legs:
                fair = self.fair_book.get(leg.market_ticker)
                if fair is not None:
                    if fair.event_ticker != leg.event_ticker:
                        raise ValueError("existing combo leg lacks verified fair-value metadata")
                    leg_participants = set(fair.participants)
                else:
                    # A held position may be past the pregame freshness horizon and
                    # therefore absent from the current fair snapshot. Reconstruct
                    # its teams from canonical Kalshi event metadata so continuation
                    # can still exclude every shared participant without reusing
                    # stale prices.
                    leg_market = await asyncio.to_thread(
                        self.client.get_market, leg.market_ticker
                    )
                    series_ticker = str(leg_market.get("series_ticker", "")).strip()
                    if not series_ticker and "-" in leg.market_ticker:
                        series_ticker = leg.market_ticker.split("-", 1)[0]
                    allowed_series = {
                        ticker.split("-", 1)[0] for ticker in self.fair_book.tickers()
                    }
                    if (
                        series_ticker not in allowed_series
                        or str(leg_market.get("event_ticker", "")).strip()
                        != leg.event_ticker
                        or str(leg_market.get("market_type", "")).strip() != "binary"
                    ):
                        raise ValueError("existing combo leg is not an allowed moneyline")
                    leg_event = await asyncio.to_thread(
                        self.client.get_event, leg.event_ticker
                    )
                    leg_participants = {
                        normalize_name(str(item.get("yes_sub_title", "")))
                        for item in leg_event.get("markets", [])
                        if isinstance(item, dict)
                        and normalize_name(str(item.get("yes_sub_title", "")))
                        not in {"", "draw", "tie"}
                    }
                if len(leg_participants) < 2:
                    raise ValueError("existing combo leg lacks participant identities")
                overlap = participants.intersection(leg_participants)
                if overlap:
                    raise ValueError("existing combo position repeats a participant")
                participants.update(leg_participants)
            event_overlap = events.intersection(self._existing_position_events)
            participant_overlap = participants.intersection(
                self._existing_position_participants
            )
            if event_overlap or participant_overlap:
                raise ValueError("existing combo positions are correlated with each other")
            self._existing_position_events.update(events)
            self._existing_position_participants.update(participants)
            self.ledger.positions[ticker] = count
            self._audit(
                "rfq_existing_position_loaded",
                {
                    "ticker": ticker,
                    "position_fp": str(count),
                    "events": sorted(events),
                    "participants": sorted(participants),
                },
            )

    async def prepare(self) -> None:
        tickers = await asyncio.to_thread(self.fair_book.refresh)
        selected = tuple(ticker for ticker in tickers if self._ticker_allowed(ticker))
        if not selected:
            raise ValueError("no allowed moneyline tickers have a fresh fair value")
        now = datetime.now(UTC)
        active_sports: dict[str, int] = {}
        for ticker in selected:
            fair = self.fair_book.get(ticker)
            if fair is None or fair.event_start <= now:
                continue
            if self.config.max_hours_to_start is not None and (
                fair.event_start - now
            ).total_seconds() > float(self.config.max_hours_to_start) * 3600:
                continue
            active_sports[fair.sport] = active_sports.get(fair.sport, 0) + 1
        missing = set(self.config.required_active_sports) - set(active_sports)
        self._audit("rfq_lane_readiness", {"active_fairs_by_sport": active_sports})
        if missing:
            raise ValueError(
                "required RFQ sports have no eligible fixtures: "
                + ", ".join(sorted(missing))
            )
        if self.execute:
            existing = await asyncio.to_thread(
                self.client.get_rfq_quotes,
                user_filter="self",
                limit=500,
            )
            unresolved = [
                item
                for item in existing
                if str(item.get("status", "")).casefold()
                in {"open", "pending", "accepted", "confirmed"}
                and not item.get("executed_ts")
                and not item.get("cancelled_ts")
            ]
            if unresolved:
                ids = ", ".join(str(item.get("id", "?")) for item in unresolved[:5])
                raise ValueError(
                    "unresolved maker RFQ quotes already exist; inspect or cancel them "
                    f"before startup: {ids}"
                )
        balance = await asyncio.to_thread(
            self.client.get_balance,
            subaccount=self.config.subaccount,
        )
        available_cents = as_decimal(balance.get("balance", "0"))
        self.ledger.available_balance = available_cents / Decimal("100")
        positions = await asyncio.to_thread(
            self.client.get_positions,
            subaccount=self.config.subaccount,
            limit=1000,
        )
        await self._load_existing_position_exposure(positions)
        for offset in range(0, len(selected), PREPARE_MARKET_BATCH_SIZE):
            batch = selected[offset : offset + PREPARE_MARKET_BATCH_SIZE]
            await asyncio.gather(*(self._ensure_leg_market(ticker) for ticker in batch))
            if offset + PREPARE_MARKET_BATCH_SIZE < len(selected):
                # Basic accounts refill 200 read tokens/s. Four markets can
                # consume up to 160 default-cost tokens during initialization.
                await asyncio.sleep(PREPARE_MARKET_BATCH_SECONDS)
        self._audit(
            "rfq_maker_ready",
            {
                "execute": self.execute,
                "tickers": list(selected),
                "event_tickers": {ticker: self.event_tickers[ticker] for ticker in selected},
                "edge_rate": str(self.config.edge_rate),
                "pricing_mode": self.config.pricing_mode,
                "no_side_only": self.config.no_side_only,
                "pinnacle_only": self.config.pinnacle_only,
                "max_hours_to_start": (
                    str(self.config.max_hours_to_start)
                    if self.config.max_hours_to_start is not None
                    else None
                ),
                "moneyline_yes_only": self.config.moneyline_yes_only,
                "max_moneyline_fair": (
                    str(self.config.max_moneyline_fair)
                    if self.config.max_moneyline_fair is not None
                    else None
                ),
                "fixed_margin": str(self.config.fixed_margin),
                "minimum_cushion": str(self.config.minimum_cushion),
                "two_leg_premium": str(self.config.two_leg_premium),
                "player_prop_premium": str(self.config.player_prop_premium),
                "soccer_premium": str(self.config.soccer_premium),
                "per_leg_outcome_notional_cap": (
                    str(self.config.per_leg_outcome_notional_cap)
                    if self.config.per_leg_outcome_notional_cap is not None
                    else None
                ),
                "per_combo_notional_cap": (
                    str(self.config.per_combo_notional_cap)
                    if self.config.per_combo_notional_cap is not None
                    else None
                ),
                "maker_fee_rate": str(self.config.maker_fee_rate),
                "maker_fee_multipliers": {
                    ticker: str(self.maker_fee_multipliers[ticker]) for ticker in selected
                },
                "max_fair_age_seconds": self.config.max_fair_age_seconds,
                "max_session_contracts": (
                    str(self.config.max_session_contracts)
                    if self.config.max_session_contracts is not None
                    else None
                ),
                "max_session_notional": (
                    str(self.config.max_session_notional)
                    if self.config.max_session_notional is not None
                    else None
                ),
                "max_session_executions": self.config.max_session_executions,
                "max_target_cost": (
                    str(self.config.max_target_cost)
                    if self.config.max_target_cost is not None
                    else None
                ),
                "max_active_quotes": self.config.max_active_quotes,
                "first_fill_wins": self.config.first_fill_wins,
                "allow_existing_positions": self.config.allow_existing_positions,
                "existing_position_events": sorted(self._existing_position_events),
                "max_inflight_rfqs": self.config.max_inflight_rfqs,
                "contracts_only": self.config.contracts_only,
                "coverage_shadow": self.config.coverage_shadow,
                "creator_rate_limit": self.config.creator_rate_limit,
                "creator_burst_limit": self.config.creator_burst_limit,
                "creator_burst_window_seconds": self.config.creator_burst_window_seconds,
                "required_active_sports": list(self.config.required_active_sports),
                "max_unaccepted_quote_age_seconds": (
                    self.config.max_unaccepted_quote_age_seconds
                ),
                "subaccount": self.config.subaccount,
                "available_balance": str(self.ledger.available_balance),
            },
        )

    async def _created(self, message: dict[str, Any], received_at: float) -> None:
        request: RFQRequest | None = None
        reservation_created = False
        retain_reservation_on_error = False
        try:
            if time.monotonic() - received_at > self.config.max_quote_latency_seconds:
                raise ValueError("RFQ exceeded the maximum quote latency")
            request = RFQRequest.from_message(message)
            request = await self._enforce_creator_rate(request)
            if request.is_combo:
                await asyncio.gather(
                    *(self._ensure_leg_market(leg.market_ticker) for leg in request.legs)
                )
                mismatched = [
                    leg.market_ticker
                    for leg in request.legs
                    if self.event_tickers.get(leg.market_ticker) != leg.event_ticker
                ]
                if mismatched:
                    raise ValueError(
                        "parlay leg event metadata does not match Kalshi: "
                        + ", ".join(sorted(mismatched))
                    )
            fair, leg_fairs = self._request_fair(request)
            grid = await self._ensure_quote_market(request)
            plan = self._price_request(
                request,
                fair,
                grid=grid,
                fee_multiplier=self.maker_fee_multipliers[request.ticker],
                leg_fairs=leg_fairs,
            )
            async with self._lock:
                self._ensure_no_existing_position_overlap(plan)
                self._ensure_no_overlapping_reservation(plan)
                plan = self.ledger.constrain(plan)
                if request.rfq_id in self.closed_rfqs:
                    raise ValueError("RFQ closed before quote submission")
                quote_id = (
                    f"pending:{request.rfq_id}" if self.execute else f"dry-run:{request.rfq_id}"
                )
                self.ledger.reserve(plan, quote_id)
                if self.config.coverage_shadow:
                    self.ledger.reservations[
                        request.rfq_id
                    ].submitted_at_monotonic = time.monotonic()
                reservation_created = True
            if self.execute:
                if time.monotonic() - received_at > self.config.max_quote_latency_seconds:
                    async with self._lock:
                        self.ledger.release(request.rfq_id)
                    reservation_created = False
                    raise ValueError("RFQ exceeded the maximum quote latency")
                # A lost HTTP response is ambiguous: the quote may exist. Keep its
                # risk reserved until Kalshi emits rfq_deleted rather than overquote.
                retain_reservation_on_error = True
                quote_id = await asyncio.to_thread(
                    self.client.create_rfq_quote,
                    rfq_id=request.rfq_id,
                    yes_bid=_format_rfq_price(plan.yes_bid),
                    no_bid=_format_rfq_price(plan.no_bid),
                    rest_remainder=False,
                    post_only=True,
                    subaccount=self.config.subaccount,
                )
                delete_closed_quote = False
                async with self._lock:
                    if request.rfq_id in self.closed_rfqs:
                        self.ledger.release(request.rfq_id)
                        reservation_created = False
                        retain_reservation_on_error = False
                        delete_closed_quote = True
                    else:
                        reservation = self.ledger.reservations[request.rfq_id]
                        reservation.quote_id = quote_id
                        reservation.submitted_at_monotonic = time.monotonic()
                if delete_closed_quote:
                    await asyncio.to_thread(
                        self.client.delete_rfq_quote,
                        request.rfq_id,
                        quote_id,
                    )
                    raise ValueError("RFQ closed during quote submission")
            elapsed_ms = (time.monotonic() - received_at) * 1000
            self._audit(
                "rfq_quote_submitted" if self.execute else "rfq_quote_dry_run",
                {**plan.as_dict(), "quote_id": quote_id, "latency_ms": round(elapsed_ms, 3)},
            )
            coverage_outcome = "quote submitted" if self.execute else "quoteable dry run"
            if self.config.coverage_shadow:
                coverage_outcome = "quoteable shadow"
            self._record_coverage(request.sizing_mode, coverage_outcome)
        except Exception as exc:
            if isinstance(exc, KalshiAPIError) and 400 <= exc.status_code < 500:
                # A client-error response proves the exchange rejected the quote.
                # Only transport/server failures leave submission ambiguous.
                retain_reservation_on_error = False
            if reservation_created and not retain_reservation_on_error and request is not None:
                async with self._lock:
                    self.ledger.release(request.rfq_id)
            self._record_coverage(
                request.sizing_mode if request is not None else "invalid",
                "ambiguous quote submission" if retain_reservation_on_error else str(exc),
            )
            if not retain_reservation_on_error and str(exc) in AGGREGATED_RFQ_SKIP_REASONS:
                self._record_unsupported(str(exc))
                return
            payload = message.get("msg") if isinstance(message.get("msg"), dict) else {}
            self._audit(
                "rfq_quote_ambiguous" if retain_reservation_on_error else "rfq_quote_skipped",
                {
                    "rfq_id": str(payload.get("id", "")),
                    "ticker": str(payload.get("market_ticker", "")),
                    "reason": str(exc),
                    "risk_reserved": retain_reservation_on_error,
                },
            )

    async def _accepted(self, message: dict[str, Any], received_at: float) -> None:
        payload = message.get("msg")
        if not isinstance(payload, dict):
            return
        quote_id = str(payload.get("quote_id", ""))
        rfq_id = str(payload.get("rfq_id", ""))
        async with self._lock:
            reservation = self.ledger.by_quote(quote_id)
        if reservation is None or reservation.plan.request.rfq_id != rfq_id:
            return
        try:
            side = str(payload.get("accepted_side", ""))
            if side not in {"yes", "no"}:
                raise ValueError("accepted RFQ side is invalid")
            if self.config.no_side_only and side != "no":
                raise ValueError("YES-side acceptances are disabled")
            self._require_expected_subaccount(payload)
            count = self._accepted_contracts(payload, side)
            offered_raw = payload.get(f"{side}_contracts_offered_fp")
            if offered_raw not in {None, ""} and count != as_decimal(offered_raw):
                raise ValueError("accepted RFQ size does not match Kalshi's offered size")
            if count <= ZERO or count > reservation.plan.contracts_for(side):
                raise ValueError("accepted RFQ size exceeds the reserved quote")
            quoted_price = reservation.plan.yes_bid if side == "yes" else reservation.plan.no_bid
            if quoted_price <= ZERO:
                raise ValueError("customer accepted a disabled RFQ side")
            fair, leg_fairs = self._request_fair(reservation.plan.request)
            outcome_fair = fair.probability if side == "yes" else ONE - fair.probability
            gross_edge_rate, estimated_fee, current_edge_rate = modeled_rfq_edge(
                outcome_fair,
                quoted_price,
                count,
                fee_rate=reservation.plan.maker_fee_rate,
                fee_multiplier=reservation.plan.maker_fee_multiplier,
            )
            if self.config.pricing_mode == "lead_fixed":
                current_plan = self._price_request(
                    reservation.plan.request,
                    fair,
                    grid=self.price_grids[reservation.plan.request.ticker],
                    fee_multiplier=reservation.plan.maker_fee_multiplier,
                    leg_fairs=leg_fairs,
                )
                safe_price = (
                    current_plan.yes_bid if side == "yes" else current_plan.no_bid
                )
                if safe_price <= ZERO or quoted_price > safe_price:
                    raise ValueError(
                        "fair value moved through the lead-style margin before confirmation"
                    )
            elif current_edge_rate < self.config.edge_rate:
                raise ValueError(
                    "fair value moved through the minimum proportional edge net of fees "
                    "before confirmation"
                )
            if not self.execute:
                raise ValueError("dry-run quotes cannot be confirmed")
            # Mark the acceptance before the REST call. Kalshi can broadcast
            # rfq_deleted while the accepted trade is still in its execution timer;
            # the reservation must survive until quote_executed in that case.
            async with self._lock:
                self.ledger.mark_acceptance(
                    reservation,
                    side=side,
                    contracts=count,
                )
            quote_age_seconds = (
                time.monotonic() - reservation.submitted_at_monotonic
                if reservation.submitted_at_monotonic is not None
                else None
            )
            self._audit(
                "rfq_quote_accepted",
                {
                    "rfq_id": rfq_id,
                    "quote_id": quote_id,
                    "ticker": reservation.plan.request.ticker,
                    "accepted_side": side,
                    "contracts_fp": str(count),
                    "quote_age_seconds": (
                        round(quote_age_seconds, 3)
                        if quote_age_seconds is not None
                        else None
                    ),
                    "leg_count": len(reservation.plan.request.legs),
                    "sizing_mode": reservation.plan.request.sizing_mode,
                    "target_cost_dollars": (
                        str(reservation.plan.request.target_cost)
                        if reservation.plan.request.target_cost is not None
                        else None
                    ),
                    "quoted_price": str(quoted_price),
                    "quote_time_net_edge_rate": str(
                        reservation.plan.yes_edge_rate
                        if side == "yes"
                        else reservation.plan.no_edge_rate
                    ),
                    "current_net_edge_rate": str(current_edge_rate),
                },
            )
            if self.config.first_fill_wins:
                cancel_task = asyncio.create_task(
                    self._cancel_competing_quotes(winner_rfq_id=rfq_id)
                )
                self._tasks.add(cancel_task)
                cancel_task.add_done_callback(self._tasks.discard)
            await asyncio.to_thread(self.client.confirm_rfq_quote, rfq_id, quote_id)
            async with self._lock:
                reservation.confirmed_outcome_fair = outcome_fair
                reservation.confirmed_edge_rate = current_edge_rate
        except Exception as exc:
            if isinstance(exc, KalshiAPIError) and 400 <= exc.status_code < 500:
                async with self._lock:
                    reservation.accepted_side = None
                    reservation.accepted_contracts = ZERO
                    if rfq_id in self.closed_rfqs:
                        self.ledger.release(rfq_id)
            self._audit(
                "rfq_confirmation_withheld",
                {"rfq_id": rfq_id, "quote_id": quote_id, "reason": str(exc)},
            )
            return
        elapsed_ms = (time.monotonic() - received_at) * 1000
        self._audit(
            "rfq_quote_confirmed",
            {
                "rfq_id": rfq_id,
                "quote_id": quote_id,
                "ticker": reservation.plan.request.ticker,
                "accepted_side": side,
                "contracts_fp": str(count),
                "current_outcome_fair": str(outcome_fair),
                "current_gross_edge_rate": str(gross_edge_rate),
                "current_estimated_maker_fee": str(estimated_fee),
                "current_edge_rate": str(current_edge_rate),
                "current_net_edge_rate": str(current_edge_rate),
                "latency_ms": round(elapsed_ms, 3),
            },
        )

    async def _cancel_competing_quotes(self, *, winner_rfq_id: str) -> None:
        async with self._lock:
            candidates = tuple(
                (rfq_id, reservation)
                for rfq_id, reservation in self.ledger.reservations.items()
                if rfq_id != winner_rfq_id
                and reservation.accepted_side is None
                and not reservation.quote_id.startswith("pending:")
            )
        results = await asyncio.gather(
            *(
                asyncio.to_thread(
                    self.client.delete_rfq_quote,
                    rfq_id,
                    reservation.quote_id,
                )
                for rfq_id, reservation in candidates
            ),
            return_exceptions=True,
        )
        for (rfq_id, reservation), result in zip(candidates, results, strict=True):
            reconciled_absent = False
            if isinstance(result, KalshiAPIError) and result.status_code == 404:
                reconciled_absent = await self._ttl_quote_is_absent(rfq_id, reservation)
                if reconciled_absent:
                    result = None
            if isinstance(result, BaseException):
                self._audit(
                    "rfq_competing_quote_cancel_failed",
                    {
                        "rfq_id": rfq_id,
                        "quote_id": reservation.quote_id,
                        "winner_rfq_id": winner_rfq_id,
                        "reason": str(result),
                        "risk_reserved": True,
                    },
                )
                continue
            async with self._lock:
                current = self.ledger.reservations.get(rfq_id)
                if current is reservation and reservation.accepted_side is None:
                    self.ledger.release(rfq_id)
            self._audit(
                "rfq_competing_quote_cancelled",
                {
                    "rfq_id": rfq_id,
                    "quote_id": reservation.quote_id,
                    "winner_rfq_id": winner_rfq_id,
                    "reconciled_absent": reconciled_absent,
                },
            )

    async def _deleted(self, message: dict[str, Any]) -> None:
        payload = message.get("msg")
        if not isinstance(payload, dict):
            return
        rfq_id = str(payload.get("id", ""))
        async with self._lock:
            self.closed_rfqs.add(rfq_id)
            reservation = self.ledger.reservations.get(rfq_id)
            if reservation is not None and reservation.accepted_side is None:
                reservation = self.ledger.release(rfq_id)
        if reservation is not None:
            reason = (
                "accepted_awaiting_execution"
                if reservation.accepted_side is not None
                else "deleted"
            )
            self._audit(
                (
                    "rfq_reservation_retained"
                    if reservation.accepted_side is not None
                    else "rfq_reservation_released"
                ),
                {"rfq_id": rfq_id, "quote_id": reservation.quote_id, "reason": reason},
            )

    async def _executed(self, message: dict[str, Any]) -> None:
        payload = message.get("msg")
        if not isinstance(payload, dict):
            return
        rfq_id = str(payload.get("rfq_id", ""))
        async with self._lock:
            reservation = self.ledger.reservations.get(rfq_id)
        if reservation is None:
            return
        execution = payload
        if reservation.accepted_side is None:
            try:
                quote = await asyncio.to_thread(
                    self.client.get_rfq_quote,
                    reservation.quote_id,
                )
                self._require_expected_subaccount(quote)
                async with self._lock:
                    self._recover_acceptance(reservation, quote)
                execution = {**quote, **payload}
            except Exception as exc:
                self._audit(
                    "rfq_execution_ambiguous",
                    {
                        "rfq_id": rfq_id,
                        "quote_id": reservation.quote_id,
                        "reason": str(exc),
                        "risk_reserved": True,
                    },
                )
                return
        async with self._lock:
            reservation = self.ledger.release(rfq_id)
            if reservation is not None:
                self.ledger.record_execution(reservation)
        if reservation is not None:
            try:
                self._require_expected_subaccount(execution)
            except ValueError as exc:
                self._audit(
                    "rfq_execution_subaccount_mismatch",
                    {"rfq_id": rfq_id, "quote_id": reservation.quote_id, "reason": str(exc)},
                )
            execution = await self._execution_with_actual_fee(execution)
            self._record_fill(reservation, execution)

    async def handle(self, message: dict[str, Any], *, received_at: float | None = None) -> None:
        received_at = received_at or time.monotonic()
        message_type = message.get("type")
        if message_type == "rfq_created":
            await self._created(message, received_at)
        elif message_type == "rfq_deleted":
            await self._deleted(message)
        elif message_type == "quote_accepted":
            await self._accepted(message, received_at)
        elif message_type == "quote_executed":
            await self._executed(message)

    async def _refresh_fairs(self) -> None:
        last_error: str | None = None
        last_error_log = 0.0
        while True:
            await asyncio.sleep(self.fair_book.refresh_seconds)
            try:
                await asyncio.to_thread(self.fair_book.refresh)
                last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = str(exc)
                now = time.monotonic()
                if reason != last_error or now - last_error_log >= 30:
                    self._audit("rfq_fair_refresh_failed", {"reason": reason})
                    last_error = reason
                    last_error_log = now

    async def _reconcile_once(self) -> None:
        quotes = await asyncio.to_thread(
            self.client.get_rfq_quotes,
            user_filter="self",
            limit=500,
        )
        by_id = {str(item.get("id", "")): item for item in quotes}
        by_rfq = {
            str(item.get("rfq_id", "")): item
            for item in quotes
            if str(item.get("status", "")).casefold()
            in {"open", "pending", "accepted", "confirmed"}
            and not item.get("executed_ts")
            and not item.get("cancelled_ts")
        }
        reconciled_tickers: set[str] = set()
        released: list[tuple[str, QuoteReservation, dict[str, Any], str, bool]] = []
        async with self._lock:
            for rfq_id, reservation in tuple(self.ledger.reservations.items()):
                quote = by_id.get(reservation.quote_id)
                if quote is None and reservation.quote_id.startswith("pending:"):
                    quote = by_rfq.get(rfq_id)
                    if quote is not None:
                        reservation.quote_id = str(quote.get("id", reservation.quote_id))
                if quote is None:
                    continue
                status = str(quote.get("status", "")).casefold()
                accepted_side = str(quote.get("accepted_side", "")).casefold()
                if accepted_side in {"yes", "no"} and reservation.accepted_side is None:
                    reservation.accepted_side = accepted_side
                    reservation.accepted_contracts = self._accepted_contracts(
                        quote,
                        accepted_side,
                    )
                    if reservation.accepted_contracts <= ZERO:
                        reservation.accepted_contracts = reservation.plan.contracts_for(
                            accepted_side
                        )
                terminal = bool(quote.get("executed_ts") or quote.get("cancelled_ts")) or (
                    status in {"executed", "cancelled", "expired", "closed"}
                )
                if terminal:
                    self.ledger.release(rfq_id)
                    executed = bool(quote.get("executed_ts")) or status == "executed"
                    if executed:
                        self.ledger.record_execution(reservation)
                    ticker = reservation.plan.request.ticker
                    reconciled_tickers.add(ticker)
                    final_status = "executed" if executed else status or "terminal"
                    released.append((rfq_id, reservation, quote, final_status, executed))
        if reconciled_tickers:
            positions = await asyncio.gather(
                *(
                    asyncio.to_thread(
                        self.client.get_position,
                        ticker,
                        subaccount=self.config.subaccount,
                    )
                    for ticker in reconciled_tickers
                )
            )
            balance = await asyncio.to_thread(
                self.client.get_balance,
                subaccount=self.config.subaccount,
            )
            available = as_decimal(balance.get("balance", "0")) / Decimal("100")
            async with self._lock:
                self.ledger.positions.update(
                    {
                        ticker: as_decimal(position)
                        for ticker, position in zip(reconciled_tickers, positions, strict=True)
                    }
                )
                self.ledger.available_balance = available
        for rfq_id, reservation, quote, status, executed in released:
            if executed:
                execution = await self._execution_with_actual_fee(quote)
                self._record_fill(reservation, execution, reconciled=True)
            self._audit(
                "rfq_quote_reconciled",
                {
                    "rfq_id": rfq_id,
                    "quote_id": reservation.quote_id,
                    "status": status,
                },
            )

    async def _reconcile_quotes(self) -> None:
        while True:
            await asyncio.sleep(self.config.reconcile_seconds)
            try:
                await self._reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._audit("rfq_reconciliation_failed", {"reason": str(exc)})

    async def _expire_unaccepted_once(self, *, now: float | None = None) -> None:
        ttl = self.config.max_unaccepted_quote_age_seconds
        if (not self.execute and not self.config.coverage_shadow) or ttl is None:
            return
        now = time.monotonic() if now is None else now
        async with self._lock:
            candidates = tuple(
                (rfq_id, reservation)
                for rfq_id, reservation in self.ledger.reservations.items()
                if reservation.accepted_side is None
                and reservation.submitted_at_monotonic is not None
                and not reservation.quote_id.startswith("pending:")
                and now - reservation.submitted_at_monotonic >= ttl
                and now >= reservation.next_ttl_cancel_attempt_monotonic
            )
        for rfq_id, reservation in candidates:
            async with self._lock:
                current = self.ledger.reservations.get(rfq_id)
                if (
                    current is not reservation
                    or reservation.accepted_side is not None
                    or reservation.submitted_at_monotonic is None
                    or now - reservation.submitted_at_monotonic < ttl
                    or now < reservation.next_ttl_cancel_attempt_monotonic
                ):
                    continue
            age = now - reservation.submitted_at_monotonic
            if self.config.coverage_shadow:
                async with self._lock:
                    current = self.ledger.reservations.get(rfq_id)
                    if current is reservation:
                        self.ledger.release(rfq_id)
                self._audit(
                    "rfq_shadow_ttl_released",
                    {
                        "rfq_id": rfq_id,
                        "quote_id": reservation.quote_id,
                        "age_seconds": round(age, 3),
                        "reason": "simulated unaccepted quote reached the configured lifetime",
                    },
                )
                continue
            try:
                await asyncio.to_thread(
                    self.client.delete_rfq_quote,
                    rfq_id,
                    reservation.quote_id,
                )
            except Exception as exc:
                terminal_not_found = False
                if isinstance(exc, KalshiAPIError) and exc.status_code == 404:
                    terminal_not_found = await self._ttl_quote_is_absent(
                        rfq_id,
                        reservation,
                    )
                if terminal_not_found:
                    async with self._lock:
                        current = self.ledger.reservations.get(rfq_id)
                        if current is reservation:
                            self.ledger.release(rfq_id)
                    self._audit(
                        "rfq_quote_ttl_reconciled_absent",
                        {
                            "rfq_id": rfq_id,
                            "quote_id": reservation.quote_id,
                            "age_seconds": round(age, 3),
                            "reason": "delete returned 404 and authenticated reads proved "
                            "the quote absent with no matching fill",
                        },
                    )
                    continue
                async with self._lock:
                    current = self.ledger.reservations.get(rfq_id)
                    if current is reservation:
                        reservation.next_ttl_cancel_attempt_monotonic = now + max(
                            15.0,
                            self.config.reconcile_seconds,
                        )
                self._audit(
                    "rfq_quote_ttl_cancel_failed",
                    {
                        "rfq_id": rfq_id,
                        "quote_id": reservation.quote_id,
                        "age_seconds": round(age, 3),
                        "reason": str(exc),
                        "risk_reserved": True,
                    },
                )
                continue
            async with self._lock:
                current = self.ledger.reservations.get(rfq_id)
                if current is reservation:
                    self.ledger.release(rfq_id)
            self._audit(
                "rfq_quote_ttl_cancelled",
                {
                    "rfq_id": rfq_id,
                    "quote_id": reservation.quote_id,
                    "age_seconds": round(age, 3),
                    "reason": "unaccepted quote exceeded its configured lifetime",
                },
            )

    async def _ttl_quote_is_absent(
        self,
        rfq_id: str,
        reservation: QuoteReservation,
    ) -> bool:
        # A delete can race an acceptance. Wait beyond the exchange's complete
        # confirmation + execution window before proving that a missing quote
        # has no corresponding fill.
        await asyncio.sleep(
            HVM_RFQ_TERMINAL_GRACE_SECONDS
            if reservation.plan.request.is_combo
            else STANDARD_RFQ_TERMINAL_GRACE_SECONDS
        )
        try:
            quote = await asyncio.to_thread(
                self.client.get_rfq_quote,
                reservation.quote_id,
            )
        except KalshiAPIError as exc:
            if exc.status_code != 404:
                return False
        except Exception:
            return False
        else:
            status = str(quote.get("status", "")).casefold()
            terminal = bool(quote.get("cancelled_ts")) or status in {
                "cancelled",
                "expired",
                "closed",
            }
            return terminal and not quote.get("executed_ts")

        try:
            quotes = await asyncio.to_thread(
                self.client.get_rfq_quotes,
                rfq_id=rfq_id,
                user_filter="self",
                limit=500,
            )
            if any(str(quote.get("id", "")) == reservation.quote_id for quote in quotes):
                return False
            fills = await asyncio.to_thread(
                self.client.get_fills,
                ticker=reservation.plan.request.ticker,
                subaccount=self.config.subaccount,
                limit=1000,
            )
        except Exception:
            return False
        identifiers = (
            str(fill.get(key, ""))
            for fill in fills
            for key in ("creator_order_id", "order_id", "client_order_id")
        )
        return not any(reservation.quote_id in identifier for identifier in identifiers)

    async def _expire_unaccepted_quotes(self) -> None:
        ttl = self.config.max_unaccepted_quote_age_seconds
        if ttl is None:
            return
        interval = min(1.0, ttl)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._expire_unaccepted_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._audit("rfq_quote_ttl_loop_failed", {"reason": str(exc)})

    async def shutdown(self) -> None:
        if not self.execute:
            if self.config.coverage_shadow:
                async with self._lock:
                    reservations = tuple(self.ledger.reservations.items())
                    for rfq_id, _ in reservations:
                        self.ledger.release(rfq_id)
                for rfq_id, reservation in reservations:
                    self._audit(
                        "rfq_shadow_shutdown_released",
                        {
                            "rfq_id": rfq_id,
                            "quote_id": reservation.quote_id,
                            "reason": "coverage shadow stopped with a simulated quote active",
                        },
                    )
            return
        async with self._lock:
            reservations = tuple(self.ledger.reservations.items())
        cancellable: list[tuple[str, QuoteReservation]] = []
        for rfq_id, reservation in reservations:
            if reservation.accepted_side is not None:
                self._audit(
                    "rfq_shutdown_risk_retained",
                    {
                        "rfq_id": rfq_id,
                        "quote_id": reservation.quote_id,
                        "reason": "accepted_or_confirmation_ambiguous",
                    },
                )
                continue
            if reservation.quote_id.startswith("pending:"):
                self._audit(
                    "rfq_shutdown_risk_retained",
                    {
                        "rfq_id": rfq_id,
                        "quote_id": reservation.quote_id,
                        "reason": "quote_submission_ambiguous",
                    },
                )
                continue
            cancellable.append((rfq_id, reservation))
        results = await asyncio.gather(
            *(
                asyncio.to_thread(
                    self.client.delete_rfq_quote,
                    rfq_id,
                    reservation.quote_id,
                )
                for rfq_id, reservation in cancellable
            ),
            return_exceptions=True,
        )
        for (rfq_id, reservation), result in zip(cancellable, results, strict=True):
            if isinstance(result, KalshiAPIError) and result.status_code == 404:
                result = None
            if isinstance(result, BaseException):
                self._audit(
                    "rfq_shutdown_cancel_failed",
                    {
                        "rfq_id": rfq_id,
                        "quote_id": reservation.quote_id,
                        "reason": str(result),
                    },
                )
                continue
            async with self._lock:
                self.ledger.release(rfq_id)
            self._audit(
                "rfq_shutdown_cancelled",
                {"rfq_id": rfq_id, "quote_id": reservation.quote_id},
            )

    async def _consume(self, *, max_messages: int = 0) -> None:
        seen = 0
        async for message in self.stream.events():
            message_type = message.get("type")
            if message_type not in {
                "rfq_created",
                "rfq_deleted",
                "quote_accepted",
                "quote_executed",
            }:
                continue
            payload = message.get("msg")
            payload = payload if isinstance(payload, dict) else {}
            rfq_id = str(payload.get("rfq_id") or payload.get("id") or f"unkeyed:{seen}")
            seen += 1
            if message_type == "rfq_created":
                rejection, sizing_mode = self._prefilter_created(message)
                if rejection is not None:
                    self._record_unsupported(rejection)
                    self._record_coverage(sizing_mode, rejection)
                    if max_messages and seen >= max_messages:
                        return
                    continue
                if len(self._tasks) >= self.config.max_inflight_rfqs:
                    self._record_unsupported("RFQ handler capacity reached")
                    self._record_coverage(sizing_mode, "RFQ handler capacity reached")
                    if max_messages and seen >= max_messages:
                        return
                    continue
            elif rfq_id not in self._rfq_tails and rfq_id not in self.ledger.reservations:
                if max_messages and seen >= max_messages:
                    return
                continue
            prior = self._rfq_tails.get(rfq_id)
            received_at = time.monotonic()

            async def ordered_handle(
                *,
                previous: asyncio.Task[None] | None = prior,
                current_message: dict[str, Any] = message,
                current_received_at: float = received_at,
            ) -> None:
                if previous is not None:
                    await asyncio.gather(previous, return_exceptions=True)
                await self.handle(current_message, received_at=current_received_at)

            task = asyncio.create_task(ordered_handle())
            self._tasks.add(task)
            self._rfq_tails[rfq_id] = task

            def completed(done: asyncio.Task[None], *, key: str = rfq_id) -> None:
                self._tasks.discard(done)
                if self._rfq_tails.get(key) is done:
                    self._rfq_tails.pop(key, None)

            task.add_done_callback(completed)
            if max_messages and seen >= max_messages:
                return

    async def run(self, *, seconds: float = 0, max_messages: int = 0) -> None:
        await self.prepare()
        refresh_task = asyncio.create_task(self._refresh_fairs())
        reconcile_task = asyncio.create_task(self._reconcile_quotes()) if self.execute else None
        expiry_task = (
            asyncio.create_task(self._expire_unaccepted_quotes())
            if (self.execute or self.config.coverage_shadow)
            and self.config.max_unaccepted_quote_age_seconds is not None
            else None
        )
        try:
            if seconds > 0:
                # datetime.now(UTC) advances across host sleep on platforms where
                # the event-loop monotonic clock may pause. This keeps a real-money
                # canary inside its authorized wall-clock window.
                deadline_utc = datetime.now(UTC).timestamp() + seconds

                async def absolute_deadline() -> None:
                    while True:
                        remaining = deadline_utc - datetime.now(UTC).timestamp()
                        if remaining <= 0:
                            return
                        await asyncio.sleep(min(1.0, remaining))

                consume_task = asyncio.create_task(
                    self._consume(max_messages=max_messages)
                )
                deadline_task = asyncio.create_task(absolute_deadline())
                done, _ = await asyncio.wait(
                    {consume_task, deadline_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if deadline_task in done:
                    consume_task.cancel()
                else:
                    deadline_task.cancel()
                await asyncio.gather(consume_task, deadline_task, return_exceptions=True)
            else:
                await self._consume(max_messages=max_messages)
        finally:
            refresh_task.cancel()
            if reconcile_task is not None:
                reconcile_task.cancel()
            if expiry_task is not None:
                expiry_task.cancel()
            await asyncio.gather(
                *(
                    task
                    for task in (refresh_task, reconcile_task, expiry_task)
                    if task is not None
                ),
                return_exceptions=True,
            )
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            self._flush_unsupported_summary()
            self._flush_coverage_summary()
            await self.shutdown()
