from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from .client import KalshiAPIError, KalshiClient
from .matching import match_events, normalize_name
from .models import ONE, ZERO, PriceGrid, as_decimal
from .odds import DEFAULT_SHARP_BOOKMAKERS, OddsClient, OddsEvent
from .scanner import consensus_probability

RFQ_LIVE_ENABLE_TOKEN = "I_UNDERSTAND_RFQ_REAL_MONEY"
RFQ_LIVE_ACKNOWLEDGEMENT = "REAL_MONEY_RFQ_AUTOCONFIRM"
HARD_MIN_RFQ_EDGE_RATE = Decimal("0.02")


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
        fair = MoneylineFair(
            ticker=ticker,
            probability=as_decimal(raw["probability"]),
            observed_at=_parse_time(raw.get("observed_at"), field=f"{ticker}.observed_at"),
            event_start=_parse_time(raw.get("event_start"), field=f"{ticker}.event_start"),
            source=str(raw.get("source", "json-file")),
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
    """Periodically refresh a league, then serve RFQs from an in-memory fair cache."""

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
    ) -> None:
        if min_bookmakers < 2:
            raise ValueError("RFQ odds consensus requires at least two bookmakers")
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

    @staticmethod
    def _oldest_selected_update(event: OddsEvent, selected: set[str]) -> datetime | None:
        updates = [
            market.last_update or bookmaker.last_update
            for bookmaker in event.bookmakers
            if bookmaker.key in selected
            for market in bookmaker.markets
            if market.key == "h2h" and (market.last_update or bookmaker.last_update) is not None
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
            markets="h2h",
            bookmakers=self.bookmakers,
        )
        now = datetime.now(UTC)
        odds_events = [
            event
            for event in odds_events
            if event.commence_time > now and self._is_two_way(event)
        ]
        matches = match_events(
            kalshi_events,
            odds_events,
            max_time_difference_seconds=self.match_window_hours * 60 * 60,
        )
        refreshed: dict[str, MoneylineFair] = {}
        for match in matches:
            for market in match.kalshi_event.get("markets", []):
                ticker = str(market.get("ticker", ""))
                outcome = str(market.get("yes_sub_title", ""))
                if not ticker or not outcome:
                    continue
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
                )
                fair.validate()
                refreshed[ticker] = fair
        if not refreshed:
            raise ValueError("no fresh, safely matched two-way moneyline fairs were found")
        self._values = refreshed
        return self.tickers()

    def get(self, ticker: str) -> MoneylineFair | None:
        return self._values.get(ticker)

    def tickers(self) -> tuple[str, ...]:
        return tuple(sorted(self._values))


@dataclass(frozen=True, slots=True)
class RFQRequest:
    rfq_id: str
    ticker: str
    contracts: Decimal
    created_at: datetime

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> RFQRequest:
        if message.get("type") != "rfq_created":
            raise ValueError("message is not an RFQ creation")
        payload = message.get("msg")
        if not isinstance(payload, dict):
            raise ValueError("RFQ message payload is missing")
        if payload.get("mve_collection_ticker") or payload.get("mve_selected_legs"):
            raise ValueError("combo RFQs are not supported by the moneyline maker")
        contracts_raw = payload.get("contracts_fp")
        target_cost = as_decimal(payload.get("target_cost_dollars") or "0")
        if contracts_raw in {None, ""} or target_cost > ZERO:
            raise ValueError("only contracts_fp RFQs are supported")
        request = cls(
            rfq_id=str(payload.get("id", "")),
            ticker=str(payload.get("market_ticker", "")),
            contracts=as_decimal(contracts_raw),
            created_at=_parse_time(payload.get("created_ts"), field="created_ts"),
        )
        if not request.rfq_id or not request.ticker or request.contracts <= ZERO:
            raise ValueError("RFQ ID, ticker, and positive contract size are required")
        return request


@dataclass(frozen=True, slots=True)
class RFQQuotePlan:
    request: RFQRequest
    fair: MoneylineFair
    yes_bid: Decimal
    no_bid: Decimal
    yes_edge_rate: Decimal | None
    no_edge_rate: Decimal | None

    @property
    def has_side(self) -> bool:
        return self.yes_bid > ZERO or self.no_bid > ZERO

    @property
    def maximum_cost(self) -> Decimal:
        return max(self.yes_bid, self.no_bid) * self.request.contracts

    def as_dict(self) -> dict[str, object]:
        return {
            "rfq_id": self.request.rfq_id,
            "ticker": self.request.ticker,
            "contracts_fp": str(self.request.contracts),
            "fair_probability": str(self.fair.probability),
            "fair_observed_at": self.fair.observed_at.isoformat(),
            "fair_source": self.fair.source,
            "yes_bid": str(self.yes_bid),
            "no_bid": str(self.no_bid),
            "yes_edge_rate": (
                str(self.yes_edge_rate) if self.yes_edge_rate is not None else None
            ),
            "no_edge_rate": (
                str(self.no_edge_rate) if self.no_edge_rate is not None else None
            ),
            "yes_edge_dollars": (
                str(self.fair.probability - self.yes_bid)
                if self.yes_bid > ZERO
                else None
            ),
            "no_edge_dollars": (
                str(ONE - self.fair.probability - self.no_bid)
                if self.no_bid > ZERO
                else None
            ),
            "maximum_cost": str(self.maximum_cost),
        }


def price_moneyline_rfq(
    request: RFQRequest,
    fair: MoneylineFair,
    *,
    price_grid: PriceGrid,
    edge_rate: Decimal,
) -> RFQQuotePlan:
    edge_rate = as_decimal(edge_rate)
    if not HARD_MIN_RFQ_EDGE_RATE <= edge_rate < ONE:
        raise ValueError("RFQ proportional edge must be in [2%, 100%)")

    def bid_for(outcome_fair: Decimal) -> tuple[Decimal, Decimal | None]:
        raw = outcome_fair * (ONE - edge_rate)
        if raw < price_grid.minimum_order_price:
            return ZERO, None
        bid = price_grid.floor(raw)
        if bid <= ZERO:
            return ZERO, None
        modeled_edge_rate = (outcome_fair - bid) / outcome_fair
        if modeled_edge_rate < HARD_MIN_RFQ_EDGE_RATE:
            raise ValueError("price-grid rounding violated the proportional RFQ edge floor")
        return bid, modeled_edge_rate

    yes_bid, yes_edge_rate = bid_for(fair.probability)
    no_bid, no_edge_rate = bid_for(ONE - fair.probability)
    if yes_bid + no_bid > ONE:
        raise ValueError("RFQ quote prices cannot sum above $1")
    plan = RFQQuotePlan(
        request,
        fair,
        yes_bid,
        no_bid,
        yes_edge_rate,
        no_edge_rate,
    )
    if not plan.has_side:
        raise ValueError("fair value is too close to a boundary to quote either side")
    return plan


@dataclass(frozen=True, slots=True)
class RFQMakerConfig:
    edge_rate: Decimal = HARD_MIN_RFQ_EDGE_RATE
    min_contracts: Decimal = Decimal("1")
    max_contracts: Decimal = Decimal("1")
    max_abs_position: Decimal = Decimal("10")
    max_notional: Decimal = Decimal("10")
    max_active_quotes: int = 20
    max_fair_age_seconds: float = 60
    reconcile_seconds: float = 15
    subaccount: int = 0
    allow_live_games: bool = False

    def validate(self) -> None:
        if not HARD_MIN_RFQ_EDGE_RATE <= self.edge_rate < ONE:
            raise ValueError("RFQ proportional edge must be in [2%, 100%)")
        if self.min_contracts <= ZERO or self.max_contracts < self.min_contracts:
            raise ValueError("RFQ contract limits must be positive and ordered")
        if self.max_abs_position <= ZERO:
            raise ValueError("RFQ contract and position limits must be positive")
        if self.max_notional <= ZERO or self.max_active_quotes <= 0:
            raise ValueError("RFQ notional and active-quote limits must be positive")
        if self.max_fair_age_seconds <= 0:
            raise ValueError("RFQ maximum fair age must be positive")
        if not 1 <= self.reconcile_seconds <= 60:
            raise ValueError("RFQ reconciliation interval must be between 1 and 60 seconds")
        if not 0 <= self.subaccount <= 63:
            raise ValueError("RFQ subaccount must be between 0 and 63")


@dataclass(slots=True)
class QuoteReservation:
    plan: RFQQuotePlan
    quote_id: str
    accepted_side: str | None = None
    accepted_contracts: Decimal = ZERO


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

    def _reserved_balance(self) -> Decimal:
        return sum(
            (reservation.plan.maximum_cost for reservation in self.reservations.values()),
            ZERO,
        )

    def constrain(self, plan: RFQQuotePlan) -> RFQQuotePlan:
        if plan.request.contracts < self.config.min_contracts:
            raise ValueError("RFQ size is below the minimum contract limit")
        if plan.request.contracts > self.config.max_contracts:
            raise ValueError("RFQ size exceeds the per-request contract limit")
        if len(self.reservations) >= self.config.max_active_quotes:
            raise ValueError("active RFQ quote limit reached")
        position = self.positions.get(plan.request.ticker, ZERO)
        long_reserve = sum(
            (
                item.plan.request.contracts
                for item in self.reservations.values()
                if item.plan.request.ticker == plan.request.ticker and item.plan.yes_bid > ZERO
            ),
            ZERO,
        )
        short_reserve = sum(
            (
                item.plan.request.contracts
                for item in self.reservations.values()
                if item.plan.request.ticker == plan.request.ticker and item.plan.no_bid > ZERO
            ),
            ZERO,
        )
        yes_bid = plan.yes_bid
        no_bid = plan.no_bid
        if (
            yes_bid * plan.request.contracts > self.config.max_notional
            or position + long_reserve + plan.request.contracts > self.config.max_abs_position
        ):
            yes_bid = ZERO
        if (
            no_bid * plan.request.contracts > self.config.max_notional
            or position - short_reserve - plan.request.contracts < -self.config.max_abs_position
        ):
            no_bid = ZERO
        constrained = replace(
            plan,
            yes_bid=yes_bid,
            no_bid=no_bid,
            yes_edge_rate=plan.yes_edge_rate if yes_bid > ZERO else None,
            no_edge_rate=plan.no_edge_rate if no_bid > ZERO else None,
        )
        if not constrained.has_side:
            raise ValueError("RFQ has no side within position and notional limits")
        if self._reserved_balance() + constrained.maximum_cost > self.available_balance:
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

    def record_execution(self, reservation: QuoteReservation) -> None:
        side = reservation.accepted_side
        count = reservation.accepted_contracts
        if side not in {"yes", "no"} or count <= ZERO:
            return
        ticker = reservation.plan.request.ticker
        self.positions[ticker] = self.positions.get(ticker, ZERO) + (
            count if side == "yes" else -count
        )
        price = reservation.plan.yes_bid if side == "yes" else reservation.plan.no_bid
        self.available_balance = max(self.available_balance - price * count, ZERO)


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
        allowed_tickers: set[str] | None = None,
    ) -> None:
        config.validate()
        self.client = client
        self.stream = stream
        self.fair_book = fair_book
        self.config = config
        self.audit_log = audit_log
        self.execute = execute
        self.allowed_tickers = set(allowed_tickers or ())
        self.price_grids: dict[str, PriceGrid] = {}
        self.ledger = RFQRiskLedger(config)
        self.closed_rfqs: set[str] = set()
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._rfq_tails: dict[str, asyncio.Task[None]] = {}

    def _audit(self, event: str, payload: dict[str, object]) -> None:
        self.audit_log.append(event, payload)

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
        return fair

    def _ticker_allowed(self, ticker: str) -> bool:
        if self.allowed_tickers:
            return ticker in self.allowed_tickers
        return ticker in self.fair_book.tickers()

    async def _ensure_market(self, ticker: str) -> PriceGrid:
        existing = self.price_grids.get(ticker)
        if existing is not None:
            return existing
        market, position = await asyncio.gather(
            asyncio.to_thread(self.client.get_market, ticker),
            asyncio.to_thread(
                self.client.get_position,
                ticker,
                subaccount=self.config.subaccount,
            ),
        )
        if str(market.get("status", "")) not in {"active", "open"}:
            raise ValueError("market is not active")
        grid = PriceGrid.from_market(market)
        async with self._lock:
            self.price_grids[ticker] = grid
            self.ledger.positions[ticker] = as_decimal(position)
        return grid

    async def prepare(self) -> None:
        tickers = await asyncio.to_thread(self.fair_book.refresh)
        selected = tuple(ticker for ticker in tickers if self._ticker_allowed(ticker))
        if not selected:
            raise ValueError("no allowed moneyline tickers have a fresh fair value")
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
        await asyncio.gather(*(self._ensure_market(ticker) for ticker in selected))
        self._audit(
            "rfq_maker_ready",
            {
                "execute": self.execute,
                "tickers": list(selected),
                "edge_rate": str(self.config.edge_rate),
                "max_fair_age_seconds": self.config.max_fair_age_seconds,
                "available_balance": str(self.ledger.available_balance),
            },
        )

    async def _created(self, message: dict[str, Any], received_at: float) -> None:
        request: RFQRequest | None = None
        reservation_created = False
        retain_reservation_on_error = False
        try:
            request = RFQRequest.from_message(message)
            if not self._ticker_allowed(request.ticker):
                raise ValueError("ticker is not on the moneyline allowlist")
            fair = self._fair(request.ticker)
            grid = await self._ensure_market(request.ticker)
            plan = price_moneyline_rfq(
                request,
                fair,
                price_grid=grid,
                edge_rate=self.config.edge_rate,
            )
            async with self._lock:
                plan = self.ledger.constrain(plan)
                if request.rfq_id in self.closed_rfqs:
                    raise ValueError("RFQ closed before quote submission")
                quote_id = (
                    f"pending:{request.rfq_id}"
                    if self.execute
                    else f"dry-run:{request.rfq_id}"
                )
                self.ledger.reserve(plan, quote_id)
                reservation_created = True
            if self.execute:
                # A lost HTTP response is ambiguous: the quote may exist. Keep its
                # risk reserved until Kalshi emits rfq_deleted rather than overquote.
                retain_reservation_on_error = True
                quote_id = await asyncio.to_thread(
                    self.client.create_rfq_quote,
                    rfq_id=request.rfq_id,
                    yes_bid=format(plan.yes_bid, "f"),
                    no_bid=format(plan.no_bid, "f"),
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
        except Exception as exc:
            if (
                reservation_created
                and not retain_reservation_on_error
                and request is not None
            ):
                async with self._lock:
                    self.ledger.release(request.rfq_id)
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
            count = as_decimal(payload.get("contracts_accepted_fp", "0"))
            if count <= ZERO or count > reservation.plan.request.contracts:
                raise ValueError("accepted RFQ size exceeds the reserved quote")
            quoted_price = reservation.plan.yes_bid if side == "yes" else reservation.plan.no_bid
            if quoted_price <= ZERO:
                raise ValueError("customer accepted a disabled RFQ side")
            fair = self._fair(reservation.plan.request.ticker)
            outcome_fair = fair.probability if side == "yes" else ONE - fair.probability
            current_edge_rate = (outcome_fair - quoted_price) / outcome_fair
            if current_edge_rate < self.config.edge_rate:
                raise ValueError(
                    "fair value moved through the minimum proportional edge "
                    "before confirmation"
                )
            if not self.execute:
                raise ValueError("dry-run quotes cannot be confirmed")
            # Mark the acceptance before the REST call. Kalshi can broadcast
            # rfq_deleted while the accepted trade is still in its execution timer;
            # the reservation must survive until quote_executed in that case.
            async with self._lock:
                reservation.accepted_side = side
                reservation.accepted_contracts = count
            await asyncio.to_thread(self.client.confirm_rfq_quote, rfq_id, quote_id)
            elapsed_ms = (time.monotonic() - received_at) * 1000
            self._audit(
                "rfq_quote_confirmed",
                {
                    "rfq_id": rfq_id,
                    "quote_id": quote_id,
                    "ticker": reservation.plan.request.ticker,
                    "accepted_side": side,
                    "contracts_fp": str(count),
                    "current_edge_rate": str(current_edge_rate),
                    "latency_ms": round(elapsed_ms, 3),
                },
            )
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
            reservation = self.ledger.release(rfq_id)
            if reservation is not None:
                self.ledger.record_execution(reservation)
        if reservation is not None:
            self._audit(
                "rfq_quote_executed",
                {
                    "rfq_id": rfq_id,
                    "quote_id": reservation.quote_id,
                    "ticker": reservation.plan.request.ticker,
                    "accepted_side": reservation.accepted_side,
                    "contracts_fp": str(reservation.accepted_contracts),
                    "order_id": str(payload.get("order_id", "")),
                },
            )

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
        released: list[tuple[str, str, str]] = []
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
                    reservation.accepted_contracts = as_decimal(
                        quote.get("contracts_fp") or reservation.plan.request.contracts
                    )
                terminal = bool(quote.get("executed_ts") or quote.get("cancelled_ts")) or (
                    status in {"executed", "cancelled", "expired", "closed"}
                )
                if terminal:
                    self.ledger.release(rfq_id)
                    ticker = reservation.plan.request.ticker
                    reconciled_tickers.add(ticker)
                    released.append((rfq_id, reservation.quote_id, status or "terminal"))
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
        for rfq_id, quote_id, status in released:
            self._audit(
                "rfq_quote_reconciled",
                {"rfq_id": rfq_id, "quote_id": quote_id, "status": status},
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

    async def shutdown(self) -> None:
        if not self.execute:
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
            if message.get("type") not in {
                "rfq_created",
                "rfq_deleted",
                "quote_accepted",
                "quote_executed",
            }:
                continue
            payload = message.get("msg")
            payload = payload if isinstance(payload, dict) else {}
            rfq_id = str(
                payload.get("rfq_id")
                or payload.get("id")
                or f"unkeyed:{seen}"
            )
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
            seen += 1
            if max_messages and seen >= max_messages:
                return

    async def run(self, *, seconds: float = 0, max_messages: int = 0) -> None:
        await self.prepare()
        refresh_task = asyncio.create_task(self._refresh_fairs())
        reconcile_task = (
            asyncio.create_task(self._reconcile_quotes()) if self.execute else None
        )
        try:
            if seconds > 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._consume(max_messages=max_messages), timeout=seconds
                    )
            else:
                await self._consume(max_messages=max_messages)
        finally:
            refresh_task.cancel()
            if reconcile_task is not None:
                reconcile_task.cancel()
            await asyncio.gather(
                *(task for task in (refresh_task, reconcile_task) if task is not None),
                return_exceptions=True,
            )
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            await self.shutdown()
