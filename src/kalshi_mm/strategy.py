from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .models import (
    ONE,
    ZERO,
    DesiredOrder,
    OrderBook,
    PriceGrid,
    QuotePlan,
    as_decimal,
)


def american_odds_to_probability(odds: int | Decimal) -> Decimal:
    odds = as_decimal(odds)
    if odds == ZERO:
        raise ValueError("American odds cannot be zero")
    if odds > ZERO:
        return Decimal("100") / (odds + Decimal("100"))
    return -odds / (-odds + Decimal("100"))


def devig_two_way(yes_odds: int, no_odds: int) -> Decimal:
    """Return the normalized YES probability from a two-way sportsbook market."""
    yes_raw = american_odds_to_probability(yes_odds)
    no_raw = american_odds_to_probability(no_odds)
    return (yes_raw / (yes_raw + no_raw)).quantize(Decimal("0.000000000001"))


def trade_imbalance(trades: Iterable[dict[str, Any]]) -> Decimal:
    """Volume-weighted taker imbalance; bid/YES takers are positive."""
    signed = ZERO
    total = ZERO
    seen: set[str] = set()
    for trade in trades:
        trade_id = str(trade.get("trade_id", ""))
        if trade_id and trade_id in seen:
            continue
        if trade_id:
            seen.add(trade_id)
        count = as_decimal(trade.get("count_fp", "0"))
        side = trade.get("taker_book_side")
        if side not in {"bid", "ask"} or count <= ZERO:
            continue
        signed += count if side == "bid" else -count
        total += count
    return signed / total if total else ZERO


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    edge: Decimal = Decimal("0.02")
    order_size: Decimal = Decimal("1")
    max_inventory: Decimal = Decimal("10")
    inventory_skew: Decimal = Decimal("0.002")
    book_imbalance_weight: Decimal = Decimal("0.005")
    trade_imbalance_weight: Decimal = Decimal("0.005")

    def __post_init__(self) -> None:
        if self.edge <= ZERO:
            raise ValueError("edge must be positive")
        if self.order_size <= ZERO:
            raise ValueError("order_size must be positive")
        if self.max_inventory <= ZERO:
            raise ValueError("max_inventory must be positive")
        if self.inventory_skew < ZERO:
            raise ValueError("inventory_skew cannot be negative")


class MarketMakerStrategy:
    def __init__(self, config: StrategyConfig, price_grid: PriceGrid | None = None) -> None:
        self.config = config
        self.price_grid = price_grid or PriceGrid.uniform()

    def quote(
        self,
        *,
        book: OrderBook,
        fair_probability: Decimal,
        inventory: Decimal = ZERO,
        recent_trade_imbalance: Decimal = ZERO,
    ) -> QuotePlan:
        fair_probability = as_decimal(fair_probability)
        inventory = as_decimal(inventory)
        recent_trade_imbalance = min(max(as_decimal(recent_trade_imbalance), -ONE), ONE)
        if not ZERO < fair_probability < ONE:
            raise ValueError("fair_probability must be strictly between 0 and 1")

        book_adjustment = self.config.book_imbalance_weight * book.book_imbalance
        trade_adjustment = self.config.trade_imbalance_weight * recent_trade_imbalance
        inventory_adjustment = self.config.inventory_skew * inventory
        reservation = fair_probability + book_adjustment + trade_adjustment - inventory_adjustment
        reservation = min(
            max(reservation, self.price_grid.minimum_order_price),
            self.price_grid.maximum_order_price,
        )

        bid_price = self.price_grid.floor(reservation - self.config.edge)
        ask_price = self.price_grid.ceil(reservation + self.config.edge)
        bid_price = min(
            max(bid_price, self.price_grid.minimum_order_price),
            self.price_grid.maximum_order_price,
        )
        ask_price = min(
            max(ask_price, self.price_grid.minimum_order_price),
            self.price_grid.maximum_order_price,
        )

        notes: list[str] = []
        bid: DesiredOrder | None = DesiredOrder("bid", bid_price, self.config.order_size)
        ask: DesiredOrder | None = DesiredOrder("ask", ask_price, self.config.order_size)

        if bid_price >= ask_price:
            bid = None
            ask = None
            notes.append("price grid and configured edge produced a crossed quote")
        if inventory >= self.config.max_inventory:
            bid = None
            notes.append("YES inventory limit reached; bid disabled")
        if inventory <= -self.config.max_inventory:
            ask = None
            notes.append("NO inventory limit reached; ask disabled")

        # post_only is the final exchange-side guard, but avoiding an already-crossing
        # local plan makes dry-run output reflect what can actually rest.
        if book.best_ask and bid and bid.price >= book.best_ask.price:
            bid = None
            notes.append("bid would cross the current ask; bid disabled")
        if book.best_bid and ask and ask.price <= book.best_bid.price:
            ask = None
            notes.append("ask would cross the current bid; ask disabled")

        return QuotePlan(
            fair_probability=fair_probability,
            reservation_price=reservation,
            bid=bid,
            ask=ask,
            book_imbalance=book.book_imbalance,
            trade_imbalance=recent_trade_imbalance,
            notes=tuple(notes),
        )
