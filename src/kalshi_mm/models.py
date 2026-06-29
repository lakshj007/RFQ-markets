from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Literal


ZERO = Decimal("0")
ONE = Decimal("1")


def as_decimal(value: str | int | float | Decimal) -> Decimal:
    """Convert API fixed-point strings without introducing float rounding."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class OrderBook:
    """YES-price view of Kalshi's binary orderbook.

    REST returns YES bids and NO bids. A NO bid at n is a YES ask at 1-n.
    """

    yes_bids: tuple[Level, ...]
    yes_asks: tuple[Level, ...]

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> OrderBook:
        raw = payload.get("orderbook_fp") or payload.get("orderbook")
        if not isinstance(raw, dict):
            raise ValueError("response does not contain orderbook_fp")

        yes_levels = raw.get("yes_dollars") or []
        no_levels = raw.get("no_dollars") or []
        yes_bids = tuple(
            sorted(
                (Level(as_decimal(price), as_decimal(size)) for price, size in yes_levels),
                key=lambda level: level.price,
                reverse=True,
            )
        )
        yes_asks = tuple(
            sorted(
                (Level(ONE - as_decimal(price), as_decimal(size)) for price, size in no_levels),
                key=lambda level: level.price,
            )
        )
        return cls(yes_bids=yes_bids, yes_asks=yes_asks)

    @property
    def best_bid(self) -> Level | None:
        return self.yes_bids[0] if self.yes_bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.yes_asks[0] if self.yes_asks else None

    @property
    def midpoint(self) -> Decimal | None:
        if not self.best_bid or not self.best_ask:
            return None
        return (self.best_bid.price + self.best_ask.price) / 2

    @property
    def spread(self) -> Decimal | None:
        if not self.best_bid or not self.best_ask:
            return None
        return self.best_ask.price - self.best_bid.price

    @property
    def book_imbalance(self) -> Decimal:
        """Top-level imbalance in [-1, 1]; positive means stronger YES bids."""
        if not self.best_bid or not self.best_ask:
            return ZERO
        total = self.best_bid.size + self.best_ask.size
        if total == ZERO:
            return ZERO
        return (self.best_bid.size - self.best_ask.size) / total

    @property
    def microprice(self) -> Decimal | None:
        """Size-weighted top-of-book price, pulled toward the thinner side."""
        if not self.best_bid or not self.best_ask:
            return self.midpoint
        total = self.best_bid.size + self.best_ask.size
        if total == ZERO:
            return self.midpoint
        return (
            self.best_ask.price * self.best_bid.size
            + self.best_bid.price * self.best_ask.size
        ) / total


@dataclass(frozen=True, slots=True)
class PriceRange:
    start: Decimal
    end: Decimal
    step: Decimal

    def contains(self, price: Decimal) -> bool:
        return self.start <= price <= self.end


@dataclass(frozen=True, slots=True)
class PriceGrid:
    ranges: tuple[PriceRange, ...]

    @classmethod
    def uniform(cls, step: str | Decimal = "0.01") -> PriceGrid:
        return cls((PriceRange(ZERO, ONE, as_decimal(step)),))

    @classmethod
    def from_market(cls, market: dict[str, Any]) -> PriceGrid:
        raw_ranges = market.get("price_ranges") or []
        ranges = tuple(
            PriceRange(
                start=as_decimal(item["start"]),
                end=as_decimal(item["end"]),
                step=as_decimal(item["step"]),
            )
            for item in raw_ranges
        )
        if not ranges:
            return cls.uniform()
        if any(item.step <= ZERO for item in ranges):
            raise ValueError("market price range step must be positive")
        return cls(tuple(sorted(ranges, key=lambda item: item.start)))

    def _range_for(self, price: Decimal) -> PriceRange:
        bounded = min(max(price, self.ranges[0].start), self.ranges[-1].end)
        for item in self.ranges:
            if item.contains(bounded):
                return item
        return min(self.ranges, key=lambda item: abs(item.start - bounded))

    def floor(self, price: Decimal) -> Decimal:
        item = self._range_for(price)
        bounded = min(max(price, item.start), item.end)
        ticks = ((bounded - item.start) / item.step).to_integral_value(rounding=ROUND_FLOOR)
        return item.start + ticks * item.step

    def ceil(self, price: Decimal) -> Decimal:
        item = self._range_for(price)
        bounded = min(max(price, item.start), item.end)
        ticks = ((bounded - item.start) / item.step).to_integral_value(rounding=ROUND_CEILING)
        return item.start + ticks * item.step

    @property
    def minimum_order_price(self) -> Decimal:
        first = self.ranges[0]
        return first.start + first.step if first.start == ZERO else first.start

    @property
    def maximum_order_price(self) -> Decimal:
        last = self.ranges[-1]
        return last.end - last.step if last.end == ONE else last.end


@dataclass(frozen=True, slots=True)
class DesiredOrder:
    side: Literal["bid", "ask"]
    price: Decimal
    count: Decimal


@dataclass(frozen=True, slots=True)
class QuotePlan:
    fair_probability: Decimal
    reservation_price: Decimal
    bid: DesiredOrder | None
    ask: DesiredOrder | None
    book_imbalance: Decimal
    trade_imbalance: Decimal
    notes: tuple[str, ...] = ()

