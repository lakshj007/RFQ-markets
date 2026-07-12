from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, Protocol

from .client import KalshiAPIError
from .fair_value import OddsFairSnapshot, OddsFairValueUnavailable
from .models import ONE, ZERO, OrderBook, PriceGrid, as_decimal
from .ws import KalshiWebSocket, StreamUpdate

LIVE_ENABLE_TOKEN = "I_UNDERSTAND_REAL_MONEY"
LIVE_ACKNOWLEDGEMENT = "REAL_MONEY_ONE_CONTRACT"
BOUNDED_EXIT_ACKNOWLEDGEMENT = "BOUNDED_REDUCE_ONLY_EXIT"
MONITORED_ENTRY_ACKNOWLEDGEMENT = "MONITOR_ODDS_AND_CANCEL_ENTRY"
HARD_MAX_CONTRACTS = Decimal("1")
HARD_MAX_ORDER_COST = Decimal("1")
HARD_MIN_EDGE = Decimal("0.02")
HARD_MAX_SPREAD = Decimal("0.15")
HARD_MAX_QUEUE_AHEAD = Decimal("500")
HARD_MAX_TRADE_AGE_SECONDS = 900
HARD_MAX_EXPIRATION_SECONDS = 300
MAKER_FEE_RATE = Decimal("0.0175")
TAKER_FEE_RATE = Decimal("0.07")
INTENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,39}$")


class LiveOrderClient(Protocol):
    def get_series_details(self, series_ticker: str) -> dict[str, Any]: ...

    def get_market(self, ticker: str) -> dict[str, Any]: ...

    def get_orderbook(self, ticker: str, *, depth: int = 20) -> dict[str, Any]: ...

    def get_trades(self, ticker: str, *, limit: int = 100) -> list[dict[str, Any]]: ...

    def get_balance(self, *, subaccount: int = 0) -> dict[str, Any]: ...

    def get_position(self, ticker: str, *, subaccount: int = 0) -> str: ...

    def get_orders(
        self,
        *,
        ticker: str | None = None,
        status: str | None = None,
        subaccount: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def get_fills(
        self,
        *,
        ticker: str | None = None,
        order_id: str | None = None,
        subaccount: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def create_order(
        self,
        *,
        ticker: str,
        client_order_id: str,
        side: str,
        count: str,
        price: str,
        expiration_time: int | None = None,
        reduce_only: bool = False,
        subaccount: int = 0,
        post_only: bool = True,
        cancel_order_on_pause: bool = True,
        time_in_force: str = "good_till_canceled",
    ) -> dict[str, Any]: ...

    def cancel_order(self, order_id: str, *, subaccount: int = 0) -> dict[str, Any]: ...


class MonitoredFairSource(Protocol):
    def refresh_snapshot(self, ticker: str) -> OddsFairSnapshot: ...


def _env_decimal(name: str, default: str) -> Decimal:
    try:
        return as_decimal(os.getenv(name, default))
    except Exception as exc:
        raise ValueError(f"{name} must be a decimal") from exc


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class LiveRiskLimits:
    max_contracts: Decimal = HARD_MAX_CONTRACTS
    max_order_cost: Decimal = HARD_MAX_ORDER_COST
    max_abs_position: Decimal = HARD_MAX_CONTRACTS
    min_edge: Decimal = HARD_MIN_EDGE
    max_spread: Decimal = HARD_MAX_SPREAD
    max_queue_ahead: Decimal = HARD_MAX_QUEUE_AHEAD
    max_trade_age_seconds: int = HARD_MAX_TRADE_AGE_SECONDS
    min_recent_contracts: Decimal = Decimal("1")
    max_expiration_seconds: int = HARD_MAX_EXPIRATION_SECONDS

    @classmethod
    def from_env(cls) -> LiveRiskLimits:
        limits = cls(
            max_contracts=_env_decimal("KALSHI_LIVE_MAX_CONTRACTS", "1"),
            max_order_cost=_env_decimal("KALSHI_LIVE_MAX_ORDER_COST", "1"),
            max_abs_position=_env_decimal("KALSHI_LIVE_MAX_ABS_POSITION", "1"),
            min_edge=_env_decimal("KALSHI_LIVE_MIN_EDGE", "0.02"),
            max_spread=_env_decimal("KALSHI_LIVE_MAX_SPREAD", "0.15"),
            max_queue_ahead=_env_decimal("KALSHI_LIVE_MAX_QUEUE_AHEAD", "500"),
            max_trade_age_seconds=_env_int(
                "KALSHI_LIVE_MAX_TRADE_AGE_SECONDS", HARD_MAX_TRADE_AGE_SECONDS
            ),
            min_recent_contracts=_env_decimal("KALSHI_LIVE_MIN_RECENT_CONTRACTS", "1"),
            max_expiration_seconds=_env_int(
                "KALSHI_LIVE_MAX_EXPIRATION_SECONDS", HARD_MAX_EXPIRATION_SECONDS
            ),
        )
        limits.validate()
        return limits

    def validate(self) -> None:
        if not ZERO < self.max_contracts <= HARD_MAX_CONTRACTS:
            raise ValueError("live max contracts must be in (0, 1]")
        if not ZERO < self.max_order_cost <= HARD_MAX_ORDER_COST:
            raise ValueError("live max order cost must be in (0, $1]")
        if not ZERO < self.max_abs_position <= HARD_MAX_CONTRACTS:
            raise ValueError("live max absolute position must be in (0, 1]")
        if self.min_edge < HARD_MIN_EDGE:
            raise ValueError("live minimum edge cannot be below $0.02")
        if not ZERO < self.max_spread <= HARD_MAX_SPREAD:
            raise ValueError("live maximum spread must be in (0, $0.15]")
        if not ZERO < self.max_queue_ahead <= HARD_MAX_QUEUE_AHEAD:
            raise ValueError("live maximum queue ahead must be in (0, 500]")
        if not 1 <= self.max_trade_age_seconds <= HARD_MAX_TRADE_AGE_SECONDS:
            raise ValueError("live max trade age must be between 1 and 900 seconds")
        if self.min_recent_contracts <= ZERO:
            raise ValueError("live minimum recent contracts must be positive")
        if not 10 <= self.max_expiration_seconds <= HARD_MAX_EXPIRATION_SECONDS:
            raise ValueError("live max expiration must be between 10 and 300 seconds")


@dataclass(frozen=True, slots=True)
class LiveOrderRequest:
    ticker: str
    side: str
    price: Decimal
    count: Decimal
    fair_probability: Decimal
    external_start_time: datetime
    expiration_seconds: int
    subaccount: int = 0

    def validate(self) -> None:
        if self.side not in {"bid", "ask"}:
            raise ValueError("side must be bid or ask")
        if not ZERO < self.price < ONE:
            raise ValueError("price must be strictly between 0 and 1")
        if self.count <= ZERO:
            raise ValueError("count must be positive")
        if not ZERO < self.fair_probability < ONE:
            raise ValueError("fair probability must be strictly between 0 and 1")
        if self.external_start_time.tzinfo is None:
            raise ValueError("external start time must include a timezone")
        if self.expiration_seconds < 10:
            raise ValueError("expiration must be at least 10 seconds")
        if self.subaccount != 0:
            raise ValueError("only the primary subaccount is enabled for live testing")


@dataclass(frozen=True, slots=True)
class BoundedExitRequest:
    ticker: str
    target_price: Decimal
    floor_price: Decimal
    count: Decimal
    external_start_time: datetime
    target_wait_seconds: int = 60
    subaccount: int = 0

    def validate(self) -> None:
        if not ZERO < self.floor_price <= self.target_price < ONE:
            raise ValueError("exit prices must satisfy 0 < floor <= target < 1")
        if not ZERO < self.count <= HARD_MAX_CONTRACTS:
            raise ValueError("bounded exit count must be in (0, 1]")
        if not 5 <= self.target_wait_seconds <= 240:
            raise ValueError("target wait must be between 5 and 240 seconds")
        if self.external_start_time.tzinfo is None:
            raise ValueError("external start time must include a timezone")
        if self.subaccount != 0:
            raise ValueError("only the primary subaccount is enabled for live testing")


@dataclass(frozen=True, slots=True)
class MonitoredEntryConfig:
    poll_interval_seconds: float = 30
    max_rest_seconds: int = HARD_MAX_EXPIRATION_SECONDS
    max_odds_age_seconds: float = 60
    failure_grace_seconds: float = 15
    rest_reconcile_seconds: float = 10

    def validate(self) -> None:
        if not 0.01 <= self.poll_interval_seconds <= 60:
            raise ValueError("monitored fair polling must be between 0.01 and 60 seconds")
        if not 10 <= self.max_rest_seconds <= HARD_MAX_EXPIRATION_SECONDS:
            raise ValueError("monitored maximum rest must be between 10 and 300 seconds")
        if not 1 <= self.max_odds_age_seconds <= 180:
            raise ValueError("monitored odds age must be between 1 and 180 seconds")
        if not 0.01 <= self.failure_grace_seconds <= 60:
            raise ValueError("monitored failure grace must be between 0.01 and 60 seconds")
        if not 0.01 <= self.rest_reconcile_seconds <= 30:
            raise ValueError("REST reconciliation must be between 0.01 and 30 seconds")


@dataclass(frozen=True, slots=True)
class BoundedExitPreflight:
    ticker: str
    target_price: Decimal
    floor_price: Decimal
    count: Decimal
    best_bid: Decimal
    best_ask: Decimal
    fallback_price: Decimal | None
    estimated_fallback_fee: Decimal | None
    effective_start_time: datetime
    position: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "target_price": str(self.target_price),
            "floor_price": str(self.floor_price),
            "count": str(self.count),
            "best_bid": str(self.best_bid),
            "best_ask": str(self.best_ask),
            "fallback_price": (
                str(self.fallback_price) if self.fallback_price is not None else None
            ),
            "estimated_fallback_fee": (
                str(self.estimated_fallback_fee)
                if self.estimated_fallback_fee is not None
                else None
            ),
            "effective_start_time": self.effective_start_time.isoformat(),
            "position": str(self.position),
        }


@dataclass(frozen=True, slots=True)
class LivePreflight:
    ticker: str
    title: str
    side: str
    price: Decimal
    count: Decimal
    fair_probability: Decimal
    modeled_edge: Decimal
    best_bid: Decimal
    best_ask: Decimal
    spread: Decimal
    market_occurrence_time: datetime
    external_start_time: datetime
    effective_start_time: datetime
    start_time_delta_seconds: int
    order_expiration_time: datetime
    latest_trade_time: datetime
    recent_contracts: Decimal
    queue_ahead: Decimal
    estimated_order_cost: Decimal
    estimated_maker_fee: Decimal
    maximum_loss: Decimal
    position: Decimal | None
    available_balance: Decimal | None

    def as_dict(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "title": self.title,
            "side": self.side,
            "price": str(self.price),
            "count": str(self.count),
            "fair_probability": str(self.fair_probability),
            "modeled_edge": str(self.modeled_edge),
            "best_bid": str(self.best_bid),
            "best_ask": str(self.best_ask),
            "spread": str(self.spread),
            "market_occurrence_time": self.market_occurrence_time.isoformat(),
            "external_start_time": self.external_start_time.isoformat(),
            "effective_start_time": self.effective_start_time.isoformat(),
            "start_time_delta_seconds": self.start_time_delta_seconds,
            "order_expiration_time": self.order_expiration_time.isoformat(),
            "latest_trade_time": self.latest_trade_time.isoformat(),
            "recent_contracts": str(self.recent_contracts),
            "queue_ahead": str(self.queue_ahead),
            "estimated_order_cost": str(self.estimated_order_cost),
            "estimated_maker_fee": str(self.estimated_maker_fee),
            "maximum_loss": str(self.maximum_loss),
            "position": str(self.position) if self.position is not None else None,
            "available_balance": (
                str(self.available_balance) if self.available_balance is not None else None
            ),
        }


class LiveAuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, event: str, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "event": event,
            "recorded_at": datetime.now(UTC).isoformat(),
            **payload,
        }
        with self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, separators=(",", ":")) + "\n")


def _recent_activity(
    trades: list[dict[str, Any]],
    *,
    now: datetime,
    max_age_seconds: int,
) -> tuple[datetime, Decimal]:
    cutoff = now - timedelta(seconds=max_age_seconds)
    recent: list[tuple[datetime, Decimal]] = []
    for trade in trades:
        if trade.get("is_block_trade"):
            continue
        created_at = _parse_time(trade.get("created_time"))
        if created_at is None or created_at < cutoff or created_at > now:
            continue
        count = as_decimal(trade.get("count_fp", "0"))
        if count > ZERO:
            recent.append((created_at, count))
    if not recent:
        raise ValueError("no qualifying public trade occurred within the live activity window")
    return max(item[0] for item in recent), sum((item[1] for item in recent), ZERO)


def _maximum_maker_fee(
    market: dict[str, Any],
    series: dict[str, Any],
    request: LiveOrderRequest,
    *,
    order_expiration: datetime,
) -> Decimal:
    waiver_expiration = _parse_time(market.get("fee_waiver_expiration_time"))
    if waiver_expiration is not None and waiver_expiration >= order_expiration:
        return ZERO
    fee_type = str(series.get("fee_type", ""))
    if fee_type == "quadratic":
        return ZERO
    if fee_type != "quadratic_with_maker_fees":
        raise ValueError(f"unsupported or unknown live maker fee type: {fee_type or 'missing'}")
    multiplier = as_decimal(series.get("fee_multiplier", "0"))
    if not ZERO < multiplier <= ONE:
        raise ValueError("live maker fee multiplier must be in (0, 1]")
    raw_fee = (
        MAKER_FEE_RATE
        * multiplier
        * request.count
        * request.price
        * (ONE - request.price)
    )
    return raw_fee.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def _maximum_taker_fee(
    market: dict[str, Any],
    series: dict[str, Any],
    *,
    price: Decimal,
    count: Decimal,
) -> Decimal:
    fee_type = str(series.get("fee_type", ""))
    if fee_type not in {"quadratic", "quadratic_with_maker_fees"}:
        raise ValueError(f"unsupported or unknown live taker fee type: {fee_type or 'missing'}")
    multiplier = as_decimal(series.get("fee_multiplier", "0"))
    if not ZERO < multiplier <= ONE:
        raise ValueError("live taker fee multiplier must be in (0, 1]")
    waiver_expiration = _parse_time(market.get("fee_waiver_expiration_time"))
    if waiver_expiration is not None and waiver_expiration >= datetime.now(UTC):
        return ZERO
    raw_fee = TAKER_FEE_RATE * multiplier * count * price * (ONE - price)
    return raw_fee.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def preflight_bounded_exit(
    client: LiveOrderClient,
    request: BoundedExitRequest,
    *,
    now: datetime | None = None,
    require_no_resting_orders: bool = True,
    require_target_post_only: bool = True,
) -> BoundedExitPreflight:
    request.validate()
    now = now or datetime.now(UTC)
    market = client.get_market(request.ticker)
    if market.get("status") != "active":
        raise ValueError("market is not active")
    market_start = _parse_time(market.get("occurrence_datetime"))
    if market_start is None:
        raise ValueError("market occurrence time is missing or invalid")
    effective_start = min(market_start, request.external_start_time.astimezone(UTC))
    if effective_start <= now + timedelta(minutes=5):
        raise ValueError("bounded exits stop five minutes before start")
    grid = PriceGrid.from_market(market)
    for price in (request.target_price, request.floor_price):
        if grid.floor(price) != price or grid.ceil(price) != price:
            raise ValueError("exit price is not valid on the market price grid")
    book = OrderBook.from_api(client.get_orderbook(request.ticker, depth=1))
    if book.best_bid is None or book.best_ask is None:
        raise ValueError("bounded exit requires a two-sided order book")
    if require_target_post_only and request.target_price <= book.best_bid.price:
        raise ValueError("target ask must be post-only above the current best bid")
    if require_no_resting_orders:
        resting = client.get_orders(status="resting", subaccount=request.subaccount, limit=1000)
        if resting:
            raise ValueError("cancel or reconcile all resting production orders first")
    position = as_decimal(client.get_position(request.ticker, subaccount=request.subaccount))
    if position < request.count:
        raise ValueError("bounded exit requires the confirmed YES position")
    event_ticker = str(market.get("event_ticker", request.ticker))
    series = client.get_series_details(event_ticker.split("-", 1)[0])
    fallback_price = (
        book.best_bid.price if book.best_bid.price >= request.floor_price else None
    )
    fallback_fee = (
        _maximum_taker_fee(
            market,
            series,
            price=fallback_price,
            count=request.count,
        )
        if fallback_price is not None
        else None
    )
    return BoundedExitPreflight(
        ticker=request.ticker,
        target_price=request.target_price,
        floor_price=request.floor_price,
        count=request.count,
        best_bid=book.best_bid.price,
        best_ask=book.best_ask.price,
        fallback_price=fallback_price,
        estimated_fallback_fee=fallback_fee,
        effective_start_time=effective_start,
        position=position,
    )


def preflight_live_order(
    client: LiveOrderClient,
    request: LiveOrderRequest,
    limits: LiveRiskLimits,
    *,
    authenticated: bool,
    now: datetime | None = None,
) -> LivePreflight:
    request.validate()
    limits.validate()
    now = now or datetime.now(UTC)
    if request.count > limits.max_contracts:
        raise ValueError("order count exceeds the live contract limit")
    if request.expiration_seconds > limits.max_expiration_seconds:
        raise ValueError("order expiration exceeds the live expiration limit")

    market = client.get_market(request.ticker)
    if market.get("status") != "active":
        raise ValueError("market is not active")
    market_occurrence = _parse_time(market.get("occurrence_datetime"))
    if market_occurrence is None:
        raise ValueError("market occurrence time is missing or invalid")
    external_start = request.external_start_time.astimezone(UTC)
    effective_start = min(market_occurrence, external_start)
    if effective_start <= now + timedelta(minutes=5):
        raise ValueError("live testing is pregame-only and stops five minutes before start")
    order_expiration = now + timedelta(seconds=request.expiration_seconds)
    event_ticker = str(market.get("event_ticker", request.ticker))
    series_ticker = event_ticker.split("-", 1)[0]
    series = client.get_series_details(series_ticker)
    estimated_maker_fee = _maximum_maker_fee(
        market,
        series,
        request,
        order_expiration=order_expiration,
    )

    grid = PriceGrid.from_market(market)
    if grid.floor(request.price) != request.price or grid.ceil(request.price) != request.price:
        raise ValueError("price is not valid on the market price grid")
    book = OrderBook.from_api(client.get_orderbook(request.ticker, depth=1))
    if book.best_bid is None or book.best_ask is None or book.spread is None:
        raise ValueError("live testing requires a two-sided order book")
    if book.spread > limits.max_spread:
        raise ValueError("displayed spread exceeds the live maximum")
    if request.side == "bid":
        if request.price >= book.best_ask.price:
            raise ValueError("post-only bid would cross the current ask")
        if request.price < book.best_bid.price:
            raise ValueError("live bid must join or improve the current best bid")
        queue_ahead = book.best_bid.size if request.price == book.best_bid.price else ZERO
    else:
        if request.price <= book.best_bid.price:
            raise ValueError("post-only ask would cross the current bid")
        if request.price > book.best_ask.price:
            raise ValueError("live ask must join or improve the current best ask")
        queue_ahead = book.best_ask.size if request.price == book.best_ask.price else ZERO
    if queue_ahead > limits.max_queue_ahead:
        raise ValueError("existing same-price queue exceeds the live maximum")

    modeled_edge = (
        request.fair_probability - request.price
        if request.side == "bid"
        else request.price - request.fair_probability
    )
    if request.side == "bid" and modeled_edge < limits.min_edge:
        raise ValueError("modeled edge is below the live minimum")
    latest_trade, recent_contracts = _recent_activity(
        client.get_trades(request.ticker, limit=100),
        now=now,
        max_age_seconds=limits.max_trade_age_seconds,
    )
    if recent_contracts < limits.min_recent_contracts:
        raise ValueError("recent public trade volume is below the live minimum")

    estimated_cost = request.price * request.count if request.side == "bid" else ZERO
    maximum_loss = estimated_cost + estimated_maker_fee
    if maximum_loss > limits.max_order_cost:
        raise ValueError("maximum order loss including maker fees exceeds the live cost limit")

    position: Decimal | None = None
    balance: Decimal | None = None
    if authenticated:
        resting = client.get_orders(status="resting", subaccount=request.subaccount, limit=1000)
        if resting:
            raise ValueError("cancel or reconcile all resting production orders first")
        position = as_decimal(client.get_position(request.ticker, subaccount=request.subaccount))
        balance_payload = client.get_balance(subaccount=request.subaccount)
        balance = as_decimal(balance_payload.get("balance", 0)) / Decimal("100")
        if request.side == "bid":
            if abs(position + request.count) > limits.max_abs_position:
                raise ValueError("projected position exceeds the live position limit")
            if balance < maximum_loss:
                raise ValueError("available balance is below the maximum order loss")
        else:
            if position < request.count:
                raise ValueError("live asks are reduce-only and require an existing YES position")

    return LivePreflight(
        ticker=request.ticker,
        title=str(market.get("title", "")),
        side=request.side,
        price=request.price,
        count=request.count,
        fair_probability=request.fair_probability,
        modeled_edge=modeled_edge,
        best_bid=book.best_bid.price,
        best_ask=book.best_ask.price,
        spread=book.spread,
        market_occurrence_time=market_occurrence,
        external_start_time=external_start,
        effective_start_time=effective_start,
        start_time_delta_seconds=int((market_occurrence - external_start).total_seconds()),
        order_expiration_time=order_expiration,
        latest_trade_time=latest_trade,
        recent_contracts=recent_contracts,
        queue_ahead=queue_ahead,
        estimated_order_cost=estimated_cost,
        estimated_maker_fee=estimated_maker_fee,
        maximum_loss=maximum_loss,
        position=position,
        available_balance=balance,
    )


def live_client_order_id(intent_id: str) -> str:
    if not INTENT_PATTERN.fullmatch(intent_id):
        raise ValueError("intent ID must be 8-40 letters, digits, underscores, or hyphens")
    return f"manual-live-{intent_id}"


def format_fixed_price(price: Decimal) -> str:
    return format(price, ".4f")


def execute_live_order(
    client: LiveOrderClient,
    request: LiveOrderRequest,
    limits: LiveRiskLimits,
    *,
    intent_id: str,
    audit_log: LiveAuditLog,
    wait_seconds: int = 60,
    poll_seconds: float = 2,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    if os.getenv("KALSHI_LIVE_TRADING_ENABLED") != LIVE_ENABLE_TOKEN:
        raise ValueError(
            f"set KALSHI_LIVE_TRADING_ENABLED={LIVE_ENABLE_TOKEN} to enable real orders"
        )
    if not 5 <= wait_seconds < request.expiration_seconds:
        raise ValueError("wait seconds must be at least 5 and below the exchange expiration")
    if not 0.25 <= poll_seconds <= 10:
        raise ValueError("poll seconds must be between 0.25 and 10")

    client_order_id = live_client_order_id(intent_id)
    existing = client.get_orders(ticker=request.ticker, subaccount=request.subaccount, limit=1000)
    duplicate = next(
        (item for item in existing if item.get("client_order_id") == client_order_id), None
    )
    if duplicate:
        result = {
            "result": "reconciled_existing",
            "client_order_id": client_order_id,
            "order": duplicate,
        }
        audit_log.append("reconciled_existing", result)
        return result

    preflight = preflight_live_order(client, request, limits, authenticated=True, now=now)
    expiration_time = int(preflight.order_expiration_time.timestamp())
    audit_log.append(
        "intent_validated",
        {"client_order_id": client_order_id, "preflight": preflight.as_dict()},
    )

    order_id: str | None = None
    terminal = False
    try:
        response = client.create_order(
            ticker=request.ticker,
            client_order_id=client_order_id,
            side=request.side,
            count=format(request.count, "f"),
            price=format_fixed_price(request.price),
            expiration_time=expiration_time,
            reduce_only=request.side == "ask",
            subaccount=request.subaccount,
            post_only=True,
            cancel_order_on_pause=True,
        )
        if response.get("error"):
            raise RuntimeError(f"order rejected: {response['error']}")
        order_id = str(response["order_id"])
        audit_log.append("submitted", {"order_id": order_id, "response": response})
        remaining_count = response.get("remaining_count")
        if remaining_count is not None and as_decimal(remaining_count) <= ZERO:
            terminal = True
            result = {"result": "filled", "order_id": order_id, "response": response}
            audit_log.append("terminal", result)
            return result

        deadline = monotonic() + wait_seconds
        while monotonic() < deadline:
            orders = client.get_orders(
                ticker=request.ticker,
                subaccount=request.subaccount,
                limit=1000,
            )
            current = next((item for item in orders if item.get("order_id") == order_id), None)
            if current and current.get("status") != "resting":
                terminal = True
                result = {
                    "result": str(current.get("status", "terminal")),
                    "order_id": order_id,
                    "order": current,
                }
                audit_log.append("terminal", result)
                return result
            sleep(poll_seconds)

        cancellation = client.cancel_order(order_id, subaccount=request.subaccount)
        terminal = True
        result = {
            "result": "cancelled_after_timeout",
            "order_id": order_id,
            "cancellation": cancellation,
        }
        audit_log.append("cancelled", result)
        return result
    except BaseException as exc:
        if order_id is None:
            try:
                recovered_orders = client.get_orders(
                    ticker=request.ticker,
                    subaccount=request.subaccount,
                    limit=1000,
                )
                recovered = next(
                    (
                        item
                        for item in recovered_orders
                        if item.get("client_order_id") == client_order_id
                    ),
                    None,
                )
                if recovered:
                    order_id = str(recovered["order_id"])
                    audit_log.append(
                        "recovered_after_submit_error",
                        {"order_id": order_id, "order": recovered},
                    )
                    if recovered.get("status") == "resting":
                        cancellation = client.cancel_order(
                            order_id, subaccount=request.subaccount
                        )
                        terminal = True
                        audit_log.append(
                            "cancelled_recovered_order",
                            {"order_id": order_id, "cancellation": cancellation},
                        )
                    else:
                        terminal = True
            except Exception as recovery_error:
                audit_log.append(
                    "submit_error_recovery_failed",
                    {
                        "client_order_id": client_order_id,
                        "error_type": type(recovery_error).__name__,
                        "error": str(recovery_error),
                    },
                )
        audit_log.append(
            "error",
            {"order_id": order_id, "error_type": type(exc).__name__, "error": str(exc)},
        )
        raise
    finally:
        if order_id and not terminal:
            try:
                cancellation = client.cancel_order(order_id, subaccount=request.subaccount)
                audit_log.append(
                    "cancelled_on_exit",
                    {"order_id": order_id, "cancellation": cancellation},
                )
            except KalshiAPIError as exc:
                if exc.status_code != 404:
                    audit_log.append(
                        "cancel_on_exit_failed",
                        {"order_id": order_id, "error": str(exc)},
                    )


def _fair_snapshot_payload(snapshot: OddsFairSnapshot) -> dict[str, object]:
    return {
        "probability": str(snapshot.probability),
        "event_commence_time": snapshot.event_commence_time.isoformat(),
        "observed_at": snapshot.observed_at.isoformat(),
        "event_id": snapshot.event_id,
        "bookmaker_count": snapshot.bookmaker_count,
        "bookmaker_keys": list(snapshot.bookmaker_keys),
        "oldest_update": (
            snapshot.oldest_update.isoformat() if snapshot.oldest_update is not None else None
        ),
        "quota_remaining": snapshot.quota_remaining,
    }


def _fill_fee(fill: dict[str, Any]) -> Decimal:
    for key in ("fee_cost", "fee_cost_dollars", "fees_paid_dollars"):
        if fill.get(key) is not None:
            return as_decimal(fill[key])
    return ZERO


def _next_fair_delay(snapshot: OddsFairSnapshot, config: MonitoredEntryConfig) -> float:
    if snapshot.oldest_update is None:
        return 0.01
    age = max(0.0, (snapshot.observed_at - snapshot.oldest_update).total_seconds())
    freshness_remaining = max(0.01, config.max_odds_age_seconds - age)
    return min(config.poll_interval_seconds, freshness_remaining)


async def execute_monitored_live_order(
    client: LiveOrderClient,
    request: LiveOrderRequest,
    limits: LiveRiskLimits,
    *,
    fair_source: MonitoredFairSource,
    initial_snapshot: OddsFairSnapshot,
    stream: KalshiWebSocket,
    config: MonitoredEntryConfig,
    intent_id: str,
    audit_log: LiveAuditLog,
    now: datetime | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Rest one unchanged entry while fair value and exchange state are monitored."""
    if os.getenv("KALSHI_LIVE_TRADING_ENABLED") != LIVE_ENABLE_TOKEN:
        raise ValueError(
            f"set KALSHI_LIVE_TRADING_ENABLED={LIVE_ENABLE_TOKEN} to enable real orders"
        )
    config.validate()
    if request.side != "bid":
        raise ValueError("monitored entry is available only for sportsbook-backed YES bids")
    if request.expiration_seconds > HARD_MAX_EXPIRATION_SECONDS:
        raise ValueError("monitored entry expiration cannot exceed 300 seconds")
    if config.max_rest_seconds > request.expiration_seconds:
        raise ValueError("monitored maximum rest cannot exceed exchange expiration")
    if initial_snapshot.bookmaker_count < 2:
        raise ValueError("monitored entry requires at least two fresh bookmakers")
    effective_now = now or datetime.now(UTC)
    if initial_snapshot.oldest_update is None:
        raise ValueError("monitored entry requires timestamped sportsbook prices")
    initial_age = (effective_now - initial_snapshot.oldest_update).total_seconds()
    if initial_age < -5 or initial_age > config.max_odds_age_seconds:
        raise ValueError("monitored entry requires fresh sportsbook prices")
    if initial_snapshot.quota_remaining is not None and initial_snapshot.quota_remaining <= 0:
        raise ValueError("Odds API quota is exhausted; refusing monitored entry")

    client_order_id = live_client_order_id(intent_id)
    existing = client.get_orders(ticker=request.ticker, subaccount=request.subaccount, limit=1000)
    duplicate = next(
        (item for item in existing if item.get("client_order_id") == client_order_id), None
    )
    if duplicate:
        result = {
            "result": "reconciled_existing",
            "client_order_id": client_order_id,
            "order": duplicate,
        }
        audit_log.append("reconciled_existing", result)
        return result

    preflight = preflight_live_order(
        client,
        request,
        limits,
        authenticated=True,
        now=effective_now,
    )
    initial_position = preflight.position or ZERO
    cancellation_threshold = request.price + limits.min_edge
    audit_log.append(
        "monitored_intent_validated",
        {
            "client_order_id": client_order_id,
            "preflight": preflight.as_dict(),
            "cancellation_threshold": str(cancellation_threshold),
            "poll_interval_seconds": config.poll_interval_seconds,
            "max_rest_seconds": config.max_rest_seconds,
            "max_odds_age_seconds": config.max_odds_age_seconds,
            "failure_grace_seconds": config.failure_grace_seconds,
        },
    )
    audit_log.append("fair_snapshot", _fair_snapshot_payload(initial_snapshot))

    queue: asyncio.Queue[StreamUpdate] = asyncio.Queue(maxsize=4096)

    async def pump_stream() -> None:
        try:
            async for update in stream.events([request.ticker]):
                await queue.put(update)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            audit_log.append(
                "websocket_monitor_failed",
                {"error_type": type(exc).__name__, "error": str(exc)},
            )

    stream_task = asyncio.create_task(pump_stream())
    order_id: str | None = None
    cancellation_reason: str | None = None
    terminal_reason: str | None = None
    failure_started: float | None = None
    unexpected: BaseException | None = None
    cancellation: dict[str, Any] | None = None
    try:
        response = client.create_order(
            ticker=request.ticker,
            client_order_id=client_order_id,
            side=request.side,
            count=format(request.count, "f"),
            price=format_fixed_price(request.price),
            expiration_time=int(preflight.order_expiration_time.timestamp()),
            reduce_only=False,
            subaccount=request.subaccount,
            post_only=True,
            cancel_order_on_pause=True,
        )
        if response.get("error"):
            raise RuntimeError(f"order rejected: {response['error']}")
        order_id = str(response["order_id"])
        audit_log.append("monitored_submitted", {"order_id": order_id, "response": response})

        started = monotonic()
        deadline = started + config.max_rest_seconds
        next_fair = started + _next_fair_delay(initial_snapshot, config)
        next_reconcile = started + config.rest_reconcile_seconds
        while True:
            current_time = monotonic()
            wall_now = (
                effective_now + timedelta(seconds=current_time - started)
                if now is not None
                else datetime.now(UTC)
            )
            if wall_now >= request.external_start_time.astimezone(UTC) - timedelta(minutes=5):
                cancellation_reason = "event_start_within_five_minutes"
                break
            if current_time >= deadline:
                cancellation_reason = "maximum_rest_timeout"
                break

            timeout = max(0.0, min(deadline, next_fair, next_reconcile) - current_time)
            update: StreamUpdate | None = None
            with contextlib.suppress(TimeoutError):
                update = await asyncio.wait_for(queue.get(), timeout=timeout)

            if update is not None:
                message_type = update.message.get("type")
                payload = update.message.get("msg", {})
                update_order_id = str(payload.get("order_id", ""))
                if message_type == "fill" and update_order_id == order_id:
                    audit_log.append("websocket_fill", {"order_id": order_id, "fill": payload})
                    cancellation_reason = "fill_detected_cancel_remainder"
                    break
                if message_type == "user_order" and update_order_id == order_id:
                    audit_log.append(
                        "websocket_order", {"order_id": order_id, "order": payload}
                    )
                    if payload.get("status") != "resting":
                        terminal_reason = str(payload.get("status", "terminal"))
                        break
                if message_type == "market_position":
                    ticker = payload.get("market_ticker") or payload.get("ticker")
                    if ticker == request.ticker:
                        audit_log.append("websocket_position", {"position": payload})

            current_time = monotonic()
            if current_time >= next_reconcile:
                orders = client.get_orders(
                    ticker=request.ticker,
                    subaccount=request.subaccount,
                    limit=1000,
                )
                current = next(
                    (item for item in orders if item.get("order_id") == order_id), None
                )
                audit_log.append(
                    "rest_reconciliation",
                    {"order_id": order_id, "order": current},
                )
                if current is not None and current.get("status") != "resting":
                    terminal_reason = str(current.get("status", "terminal"))
                    break
                next_reconcile = current_time + config.rest_reconcile_seconds

            if current_time >= next_fair:
                try:
                    snapshot = fair_source.refresh_snapshot(request.ticker)
                except OddsFairValueUnavailable as exc:
                    audit_log.append(
                        "fair_unavailable",
                        {"reason": exc.reason, "error": str(exc)},
                    )
                    cancellation_reason = exc.reason
                    break
                except Exception as exc:
                    if failure_started is None:
                        failure_started = current_time
                    elapsed = current_time - failure_started
                    audit_log.append(
                        "fair_refresh_failed",
                        {
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "failure_elapsed_seconds": elapsed,
                            "failure_grace_seconds": config.failure_grace_seconds,
                        },
                    )
                    if elapsed >= config.failure_grace_seconds:
                        cancellation_reason = "odds_api_failure_grace_expired"
                        break
                    next_fair = current_time + min(5.0, config.failure_grace_seconds - elapsed)
                else:
                    failure_started = None
                    audit_log.append("fair_snapshot", _fair_snapshot_payload(snapshot))
                    if snapshot.bookmaker_count < 2:
                        cancellation_reason = "insufficient_bookmakers"
                        break
                    if snapshot.probability < cancellation_threshold:
                        cancellation_reason = "fair_below_threshold"
                        break
                    if snapshot.quota_remaining is not None and snapshot.quota_remaining <= 0:
                        cancellation_reason = "odds_api_quota_exhausted"
                        break
                    next_fair = current_time + _next_fair_delay(snapshot, config)
    except (asyncio.CancelledError, KeyboardInterrupt):
        cancellation_reason = "interrupted"
    except BaseException as exc:
        cancellation_reason = "internal_error"
        unexpected = exc
    finally:
        if order_id is None:
            recovered = next(
                (
                    item
                    for item in client.get_orders(
                        ticker=request.ticker,
                        subaccount=request.subaccount,
                        limit=1000,
                    )
                    if item.get("client_order_id") == client_order_id
                ),
                None,
            )
            if recovered is not None:
                order_id = str(recovered["order_id"])
                audit_log.append(
                    "recovered_after_submit_error", {"order_id": order_id, "order": recovered}
                )
        if order_id is not None and cancellation_reason is not None:
            audit_log.append(
                "cancellation_requested",
                {"order_id": order_id, "reason": cancellation_reason},
            )
            try:
                cancellation = client.cancel_order(order_id, subaccount=request.subaccount)
                audit_log.append(
                    "cancellation_response",
                    {
                        "order_id": order_id,
                        "reason": cancellation_reason,
                        "response": cancellation,
                    },
                )
            except Exception as exc:
                if isinstance(exc, KalshiAPIError) and exc.status_code == 404:
                    audit_log.append(
                        "cancellation_response",
                        {
                            "order_id": order_id,
                            "reason": cancellation_reason,
                            "response": {"status": "not_found_requires_reconciliation"},
                        },
                    )
                else:
                    audit_log.append(
                        "cancellation_failed",
                        {"order_id": order_id, "reason": cancellation_reason, "error": str(exc)},
                    )
        stream_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stream_task

    if order_id is None:
        if unexpected is not None:
            raise unexpected
        raise RuntimeError("monitored order submission could not be reconciled")

    orders = client.get_orders(ticker=request.ticker, subaccount=request.subaccount, limit=1000)
    final_order = next((item for item in orders if item.get("order_id") == order_id), None)
    fills = client.get_fills(
        ticker=request.ticker,
        order_id=order_id,
        subaccount=request.subaccount,
        limit=1000,
    )
    final_position = as_decimal(
        client.get_position(request.ticker, subaccount=request.subaccount)
    )
    fee = sum((_fill_fee(fill) for fill in fills), ZERO)
    filled = bool(fills) or final_position > initial_position
    if cancellation_reason is not None and filled:
        audit_log.append(
            "fill_race",
            {
                "order_id": order_id,
                "cancellation_reason": cancellation_reason,
                "fills": fills,
                "final_order": final_order,
            },
        )
    audit_log.append(
        "monitored_final_reconciliation",
        {
            "order_id": order_id,
            "final_order": final_order,
            "fills": fills,
            "final_position": str(final_position),
            "fee": str(fee),
        },
    )
    if unexpected is not None:
        raise unexpected
    final_status = str(final_order.get("status")) if final_order is not None else "unknown"
    if cancellation_reason is not None and filled:
        result_name = "filled_after_cancel_race"
    elif cancellation_reason is not None and final_status == "resting":
        result_name = "cancel_unconfirmed_order_still_resting"
    elif cancellation_reason is not None:
        result_name = f"cancelled_{cancellation_reason}"
    else:
        result_name = terminal_reason or final_status
    return {
        "result": result_name,
        "order_id": order_id,
        "cancellation_reason": cancellation_reason,
        "cancellation": cancellation,
        "order": final_order,
        "fills": fills,
        "final_position": str(final_position),
        "fee": str(fee),
    }


def execute_bounded_exit(
    client: LiveOrderClient,
    request: BoundedExitRequest,
    *,
    intent_id: str,
    audit_log: LiveAuditLog,
    poll_seconds: float = 2,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    if os.getenv("KALSHI_LIVE_TRADING_ENABLED") != LIVE_ENABLE_TOKEN:
        raise ValueError(
            f"set KALSHI_LIVE_TRADING_ENABLED={LIVE_ENABLE_TOKEN} to enable real orders"
        )
    live_client_order_id(intent_id)
    if not 0.25 <= poll_seconds <= 10:
        raise ValueError("poll seconds must be between 0.25 and 10")
    effective_now = now or datetime.now(UTC)
    preflight = preflight_bounded_exit(client, request, now=effective_now)
    audit_log.append("bounded_exit_validated", {"preflight": preflight.as_dict()})

    target_client_id = f"manual-exit-target-{intent_id}"
    target_response = client.create_order(
        ticker=request.ticker,
        client_order_id=target_client_id,
        side="ask",
        count=format(request.count, "f"),
        price=format_fixed_price(request.target_price),
        expiration_time=int(effective_now.timestamp()) + request.target_wait_seconds + 30,
        reduce_only=True,
        subaccount=request.subaccount,
        post_only=True,
        cancel_order_on_pause=True,
        time_in_force="good_till_canceled",
    )
    target_order_id = str(target_response["order_id"])
    audit_log.append(
        "bounded_exit_target_submitted",
        {"order_id": target_order_id, "response": target_response},
    )
    target_remaining = as_decimal(target_response.get("remaining_count", request.count))
    if target_remaining <= ZERO:
        result = {
            "result": "target_filled",
            "target_order_id": target_order_id,
            "response": target_response,
        }
        audit_log.append("bounded_exit_terminal", result)
        return result

    deadline = monotonic() + request.target_wait_seconds
    target_terminal = False
    try:
        while monotonic() < deadline:
            orders = client.get_orders(
                ticker=request.ticker,
                subaccount=request.subaccount,
                limit=1000,
            )
            current = next(
                (item for item in orders if item.get("order_id") == target_order_id),
                None,
            )
            if current and current.get("status") != "resting":
                target_terminal = True
                break
            sleep(poll_seconds)
        if not target_terminal:
            cancellation = client.cancel_order(
                target_order_id,
                subaccount=request.subaccount,
            )
            target_terminal = True
            audit_log.append(
                "bounded_exit_target_cancelled",
                {"order_id": target_order_id, "cancellation": cancellation},
            )
    finally:
        if not target_terminal:
            try:
                client.cancel_order(target_order_id, subaccount=request.subaccount)
            except KalshiAPIError as exc:
                if exc.status_code != 404:
                    raise

    remaining_position = as_decimal(
        client.get_position(request.ticker, subaccount=request.subaccount)
    )
    if remaining_position <= ZERO:
        result = {"result": "target_filled", "target_order_id": target_order_id}
        audit_log.append("bounded_exit_terminal", result)
        return result

    fallback_request = BoundedExitRequest(
        ticker=request.ticker,
        target_price=request.target_price,
        floor_price=request.floor_price,
        count=min(remaining_position, request.count),
        external_start_time=request.external_start_time,
        target_wait_seconds=request.target_wait_seconds,
        subaccount=request.subaccount,
    )
    fallback = preflight_bounded_exit(
        client,
        fallback_request,
        now=effective_now if now is not None else None,
        require_target_post_only=False,
    )
    if fallback.fallback_price is None:
        result = {
            "result": "held_below_floor",
            "best_bid": str(fallback.best_bid),
            "floor_price": str(request.floor_price),
            "remaining_position": str(remaining_position),
        }
        audit_log.append("bounded_exit_terminal", result)
        return result

    fallback_client_id = f"manual-exit-fallback-{intent_id}"
    fallback_response = client.create_order(
        ticker=request.ticker,
        client_order_id=fallback_client_id,
        side="ask",
        count=format(fallback_request.count, "f"),
        price=format_fixed_price(fallback.fallback_price),
        expiration_time=None,
        reduce_only=True,
        subaccount=request.subaccount,
        post_only=False,
        cancel_order_on_pause=True,
        time_in_force="immediate_or_cancel",
    )
    final_position = as_decimal(
        client.get_position(request.ticker, subaccount=request.subaccount)
    )
    result = {
        "result": "fallback_submitted",
        "fallback_price": str(fallback.fallback_price),
        "estimated_fee": (
            str(fallback.estimated_fallback_fee)
            if fallback.estimated_fallback_fee is not None
            else None
        ),
        "response": fallback_response,
        "remaining_position": str(final_position),
    }
    audit_log.append("bounded_exit_terminal", result)
    return result
