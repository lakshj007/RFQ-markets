from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from .models import as_decimal
from .scanner import Discrepancy

ZERO = Decimal("0")


class TradeReader(Protocol):
    def get_trades(self, ticker: str, *, limit: int = 100) -> list[dict[str, Any]]: ...


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(slots=True)
class SimulatedQuote:
    quote_id: str
    ticker: str
    event_ticker: str
    outcome: str
    created_at: datetime
    bid_price: Decimal
    ask_price: Decimal
    order_size: Decimal
    fair_probability: Decimal
    spread_at_open: Decimal
    bookmaker_count: int
    bid_top_size: Decimal
    ask_top_size: Decimal
    bid_filled: Decimal = ZERO
    ask_filled: Decimal = ZERO
    bid_fill_at: datetime | None = None
    ask_fill_at: datetime | None = None
    closed_at: datetime | None = None
    close_reason: str | None = None
    processed_trade_ids: set[str] = field(default_factory=set)
    bid_markout_horizons: set[int] = field(default_factory=set)
    ask_markout_horizons: set[int] = field(default_factory=set)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def paired_count(self) -> Decimal:
        return min(self.bid_filled, self.ask_filled)

    @property
    def inventory(self) -> Decimal:
        return self.bid_filled - self.ask_filled

    def to_json(self) -> dict[str, object]:
        return {
            "quote_id": self.quote_id,
            "ticker": self.ticker,
            "event_ticker": self.event_ticker,
            "outcome": self.outcome,
            "created_at": self.created_at.isoformat(),
            "bid_price": str(self.bid_price),
            "ask_price": str(self.ask_price),
            "order_size": str(self.order_size),
            "fair_probability": str(self.fair_probability),
            "spread_at_open": str(self.spread_at_open),
            "bookmaker_count": self.bookmaker_count,
            "bid_top_size": str(self.bid_top_size),
            "ask_top_size": str(self.ask_top_size),
            "bid_filled": str(self.bid_filled),
            "ask_filled": str(self.ask_filled),
            "bid_fill_at": self.bid_fill_at.isoformat() if self.bid_fill_at else None,
            "ask_fill_at": self.ask_fill_at.isoformat() if self.ask_fill_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "close_reason": self.close_reason,
            "processed_trade_ids": sorted(self.processed_trade_ids),
            "bid_markout_horizons": sorted(self.bid_markout_horizons),
            "ask_markout_horizons": sorted(self.ask_markout_horizons),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> SimulatedQuote:
        def optional_time(key: str) -> datetime | None:
            value = payload.get(key)
            return _parse_time(str(value)) if value else None

        return cls(
            quote_id=str(payload["quote_id"]),
            ticker=str(payload["ticker"]),
            event_ticker=str(payload["event_ticker"]),
            outcome=str(payload["outcome"]),
            created_at=_parse_time(str(payload["created_at"])),
            bid_price=as_decimal(payload["bid_price"]),
            ask_price=as_decimal(payload["ask_price"]),
            order_size=as_decimal(payload["order_size"]),
            fair_probability=as_decimal(payload["fair_probability"]),
            spread_at_open=as_decimal(payload["spread_at_open"]),
            bookmaker_count=int(payload["bookmaker_count"]),
            bid_top_size=as_decimal(payload["bid_top_size"]),
            ask_top_size=as_decimal(payload["ask_top_size"]),
            bid_filled=as_decimal(payload.get("bid_filled", "0")),
            ask_filled=as_decimal(payload.get("ask_filled", "0")),
            bid_fill_at=optional_time("bid_fill_at"),
            ask_fill_at=optional_time("ask_fill_at"),
            closed_at=optional_time("closed_at"),
            close_reason=str(payload["close_reason"]) if payload.get("close_reason") else None,
            processed_trade_ids=set(payload.get("processed_trade_ids", [])),
            bid_markout_horizons=set(payload.get("bid_markout_horizons", [])),
            ask_markout_horizons=set(payload.get("ask_markout_horizons", [])),
        )


@dataclass(frozen=True, slots=True)
class MakerPaperUpdate:
    quotes_opened: int
    fills_recorded: int
    quotes_completed: int
    quotes_cancelled: int
    markouts_recorded: int
    active_quotes: int
    open_inventory: Decimal


class MakerPaperRecorder:
    """Persistent passive-quote simulation driven by later public trades."""

    def __init__(
        self,
        output_path: str | Path,
        state_path: str | Path,
        *,
        min_spread: Decimal = Decimal("0.04"),
        max_spread: Decimal = Decimal("0.15"),
        min_edge: Decimal = Decimal("0.01"),
        tick: Decimal = Decimal("0.01"),
        order_size: Decimal = Decimal("1"),
        max_open_quotes: int = 5,
        max_top_size: Decimal = Decimal("500"),
        quote_lifetime_seconds: int = 600,
        markout_horizons_seconds: tuple[int, ...] = (60, 300),
    ) -> None:
        self.output_path = Path(output_path)
        self.state_path = Path(state_path)
        self.min_spread = as_decimal(min_spread)
        self.max_spread = as_decimal(max_spread)
        self.min_edge = as_decimal(min_edge)
        self.tick = as_decimal(tick)
        self.order_size = as_decimal(order_size)
        self.max_open_quotes = max_open_quotes
        self.max_top_size = as_decimal(max_top_size)
        self.quote_lifetime_seconds = quote_lifetime_seconds
        self.markout_horizons_seconds = tuple(sorted(set(markout_horizons_seconds)))
        if self.min_spread <= ZERO or self.min_edge <= ZERO or self.tick <= ZERO:
            raise ValueError("spread, edge, and tick must be positive")
        if self.max_spread < self.min_spread:
            raise ValueError("max spread must be at least min spread")
        if self.order_size <= ZERO or self.max_open_quotes <= 0:
            raise ValueError("order size and max open quotes must be positive")
        if self.quote_lifetime_seconds <= 0:
            raise ValueError("quote lifetime must be positive")
        if any(item <= 0 for item in self.markout_horizons_seconds):
            raise ValueError("markout horizons must be positive")
        self.quotes = self._load_state()

    def _load_state(self) -> list[SimulatedQuote]:
        if not self.state_path.exists():
            return []
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        if payload.get("version") != 1:
            raise ValueError("unsupported maker paper state version")
        return [SimulatedQuote.from_json(item) for item in payload.get("quotes", [])]

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "quotes": [item.to_json() for item in self.quotes]}
        self.state_path.write_text(
            json.dumps(payload, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def _append(self, payload: dict[str, object]) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(payload, separators=(",", ":")) + "\n")

    def _record_fill(
        self,
        quote: SimulatedQuote,
        *,
        side: str,
        count: Decimal,
        trade: dict[str, Any],
        trade_time: datetime,
    ) -> None:
        price = quote.bid_price if side == "bid" else quote.ask_price
        if side == "bid":
            quote.bid_filled += count
            quote.bid_fill_at = quote.bid_fill_at or trade_time
        else:
            quote.ask_filled += count
            quote.ask_fill_at = quote.ask_fill_at or trade_time
        self._append(
            {
                "record_type": "fill",
                "quote_id": quote.quote_id,
                "recorded_at": trade_time.isoformat(),
                "ticker": quote.ticker,
                "side": side,
                "price": str(price),
                "count": str(count),
                "trade_id": str(trade.get("trade_id", "")),
                "trade_price": str(trade.get("yes_price_dollars", "")),
                "inventory_after": str(quote.inventory),
            }
        )

    def _process_trades(self, quote: SimulatedQuote, trades: Iterable[dict[str, Any]]) -> int:
        recorded = 0
        ordered = sorted(trades, key=lambda item: str(item.get("created_time", "")))
        for trade in ordered:
            trade_id = str(trade.get("trade_id", ""))
            if not trade_id or trade_id in quote.processed_trade_ids:
                continue
            quote.processed_trade_ids.add(trade_id)
            if trade.get("is_block_trade"):
                continue
            created = trade.get("created_time")
            if not created:
                continue
            trade_time = _parse_time(str(created))
            if trade_time <= quote.created_at:
                continue
            price = as_decimal(trade.get("yes_price_dollars", "0"))
            count = as_decimal(trade.get("count_fp", "0"))
            if count <= ZERO:
                continue
            taker_side = trade.get("taker_book_side")
            if taker_side == "ask" and price <= quote.bid_price:
                remaining = quote.order_size - quote.bid_filled
                fill = min(count, max(remaining, ZERO))
                if fill > ZERO:
                    self._record_fill(
                        quote, side="bid", count=fill, trade=trade, trade_time=trade_time
                    )
                    recorded += 1
            elif taker_side == "bid" and price >= quote.ask_price:
                remaining = quote.order_size - quote.ask_filled
                fill = min(count, max(remaining, ZERO))
                if fill > ZERO:
                    self._record_fill(
                        quote, side="ask", count=fill, trade=trade, trade_time=trade_time
                    )
                    recorded += 1
            if quote.bid_filled >= quote.order_size and quote.ask_filled >= quote.order_size:
                quote.closed_at = trade_time
                quote.close_reason = "both_filled"
                gross_profit = quote.paired_count * (quote.ask_price - quote.bid_price)
                self._append(
                    {
                        "record_type": "quote_completed",
                        "quote_id": quote.quote_id,
                        "recorded_at": trade_time.isoformat(),
                        "ticker": quote.ticker,
                        "paired_count": str(quote.paired_count),
                        "gross_profit_before_fees": str(gross_profit),
                        "seconds_to_complete": (
                            trade_time - quote.created_at
                        ).total_seconds(),
                    }
                )
                break
        return recorded

    def _record_markouts(
        self,
        quote: SimulatedQuote,
        snapshot: Discrepancy,
        now: datetime,
    ) -> int:
        recorded = 0
        for side, fill_at, marked in (
            ("bid", quote.bid_fill_at, quote.bid_markout_horizons),
            ("ask", quote.ask_fill_at, quote.ask_markout_horizons),
        ):
            if fill_at is None:
                continue
            for horizon in self.markout_horizons_seconds:
                if horizon in marked or (now - fill_at).total_seconds() < horizon:
                    continue
                markout = (
                    snapshot.midpoint - quote.bid_price
                    if side == "bid"
                    else quote.ask_price - snapshot.midpoint
                )
                self._append(
                    {
                        "record_type": "maker_markout",
                        "quote_id": quote.quote_id,
                        "recorded_at": now.isoformat(),
                        "ticker": quote.ticker,
                        "side": side,
                        "horizon_seconds": horizon,
                        "midpoint": str(snapshot.midpoint),
                        "markout": str(markout),
                    }
                )
                marked.add(horizon)
                recorded += 1
        return recorded

    def _close_quote(
        self,
        quote: SimulatedQuote,
        *,
        now: datetime,
        reason: str,
        snapshot: Discrepancy | None,
    ) -> None:
        quote.closed_at = now
        quote.close_reason = reason
        paired_profit = quote.paired_count * (quote.ask_price - quote.bid_price)
        inventory_exit_price: Decimal | None = None
        inventory_exit_profit: Decimal | None = None
        if snapshot is not None and quote.inventory > ZERO:
            inventory_exit_price = snapshot.yes_bid
            inventory_exit_profit = quote.inventory * (
                inventory_exit_price - quote.bid_price
            )
        elif snapshot is not None and quote.inventory < ZERO:
            inventory_exit_price = snapshot.yes_ask
            inventory_exit_profit = -quote.inventory * (
                quote.ask_price - inventory_exit_price
            )
        gross_profit = (
            paired_profit + inventory_exit_profit
            if inventory_exit_profit is not None
            else paired_profit
        )
        self._append(
            {
                "record_type": "quote_cancelled",
                "quote_id": quote.quote_id,
                "recorded_at": now.isoformat(),
                "ticker": quote.ticker,
                "reason": reason,
                "bid_filled": str(quote.bid_filled),
                "ask_filled": str(quote.ask_filled),
                "paired_profit_before_fees": str(paired_profit),
                "inventory": str(quote.inventory),
                "inventory_exit_price": (
                    str(inventory_exit_price) if inventory_exit_price is not None else None
                ),
                "inventory_exit_profit": (
                    str(inventory_exit_profit) if inventory_exit_profit is not None else None
                ),
                "gross_profit_before_fees": str(gross_profit),
            }
        )

    def _markouts_complete(self, quote: SimulatedQuote) -> bool:
        required = set(self.markout_horizons_seconds)
        bid_complete = quote.bid_fill_at is None or required <= quote.bid_markout_horizons
        ask_complete = quote.ask_fill_at is None or required <= quote.ask_markout_horizons
        return bid_complete and ask_complete

    def _candidate(self, item: Discrepancy) -> tuple[Decimal, Decimal] | None:
        spread = item.yes_ask - item.yes_bid
        bid = item.yes_bid + self.tick
        ask = item.yes_ask - self.tick
        if spread < self.min_spread or spread > self.max_spread or bid >= ask:
            return None
        if item.fair_probability - bid < self.min_edge:
            return None
        if ask - item.fair_probability < self.min_edge:
            return None
        if self.max_top_size > ZERO and (
            item.yes_bid_size > self.max_top_size or item.yes_ask_size > self.max_top_size
        ):
            return None
        return bid, ask

    def _open_candidates(self, snapshots: list[Discrepancy], now: datetime) -> int:
        open_quotes = [item for item in self.quotes if item.is_open]
        available = self.max_open_quotes - len(open_quotes)
        if available <= 0:
            return 0
        active_tickers = {item.ticker for item in open_quotes}
        active_events = {item.event_ticker for item in open_quotes}
        candidates: list[tuple[Decimal, Discrepancy, Decimal, Decimal]] = []
        for item in snapshots:
            if item.ticker in active_tickers or item.event_ticker in active_events:
                continue
            prices = self._candidate(item)
            if prices is None:
                continue
            bid, ask = prices
            candidates.append((item.yes_ask - item.yes_bid, item, bid, ask))
        candidates.sort(key=lambda value: (value[0], value[1].bookmaker_count), reverse=True)
        opened = 0
        for spread, item, bid, ask in candidates:
            if opened >= available:
                break
            if item.event_ticker in active_events:
                continue
            quote = SimulatedQuote(
                quote_id=str(uuid.uuid4()),
                ticker=item.ticker,
                event_ticker=item.event_ticker,
                outcome=item.outcome,
                created_at=now,
                bid_price=bid,
                ask_price=ask,
                order_size=self.order_size,
                fair_probability=item.fair_probability,
                spread_at_open=spread,
                bookmaker_count=item.bookmaker_count,
                bid_top_size=item.yes_bid_size,
                ask_top_size=item.yes_ask_size,
            )
            self.quotes.append(quote)
            active_events.add(item.event_ticker)
            self._append(
                {
                    "record_type": "quote_opened",
                    "quote_id": quote.quote_id,
                    "recorded_at": now.isoformat(),
                    "ticker": quote.ticker,
                    "event_ticker": quote.event_ticker,
                    "outcome": quote.outcome,
                    "fair_probability": str(quote.fair_probability),
                    "market_bid": str(item.yes_bid),
                    "market_ask": str(item.yes_ask),
                    "quote_bid": str(quote.bid_price),
                    "quote_ask": str(quote.ask_price),
                    "order_size": str(quote.order_size),
                    "spread_at_open": str(spread),
                    "bookmaker_count": quote.bookmaker_count,
                    "bid_top_size": str(quote.bid_top_size),
                    "ask_top_size": str(quote.ask_top_size),
                }
            )
            opened += 1
        return opened

    def update(
        self,
        discrepancies: Iterable[Discrepancy],
        *,
        trades: TradeReader,
        now: datetime | None = None,
    ) -> MakerPaperUpdate:
        now = now or datetime.now(UTC)
        snapshots = list(discrepancies)
        by_ticker = {item.ticker: item for item in snapshots}
        fills = 0
        completed = 0
        cancelled = 0
        markouts = 0

        for quote in self.quotes:
            snapshot = by_ticker.get(quote.ticker)
            if quote.is_open:
                fills += self._process_trades(quote, trades.get_trades(quote.ticker, limit=100))
                if quote.close_reason == "both_filled":
                    completed += 1
                elif (now - quote.created_at).total_seconds() >= self.quote_lifetime_seconds:
                    self._close_quote(
                        quote,
                        now=now,
                        reason="expired",
                        snapshot=snapshot,
                    )
                    cancelled += 1
                elif snapshot and snapshot.yes_ask - snapshot.yes_bid > self.max_spread:
                    self._close_quote(
                        quote,
                        now=now,
                        reason="spread_out_of_range",
                        snapshot=snapshot,
                    )
                    cancelled += 1
                elif snapshot and (
                    snapshot.fair_probability - quote.bid_price < self.min_edge
                    or quote.ask_price - snapshot.fair_probability < self.min_edge
                ):
                    self._close_quote(
                        quote,
                        now=now,
                        reason="fair_value_moved",
                        snapshot=snapshot,
                    )
                    cancelled += 1
            if snapshot:
                markouts += self._record_markouts(quote, snapshot, now)

        self.quotes = [
            item for item in self.quotes if item.is_open or not self._markouts_complete(item)
        ]
        opened = self._open_candidates(snapshots, now)
        self._save_state()
        active = [item for item in self.quotes if item.is_open]
        return MakerPaperUpdate(
            quotes_opened=opened,
            fills_recorded=fills,
            quotes_completed=completed,
            quotes_cancelled=cancelled,
            markouts_recorded=markouts,
            active_quotes=len(active),
            open_inventory=sum((item.inventory for item in active), ZERO),
        )
