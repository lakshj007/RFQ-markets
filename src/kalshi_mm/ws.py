from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from .client import KalshiClient
from .models import Level, OrderBook, as_decimal

PRODUCTION_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
DEMO_WS_URL = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"


class SequenceGapError(RuntimeError):
    pass


@dataclass(slots=True)
class LiveBook:
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)

    @classmethod
    def from_snapshot(cls, message: dict[str, Any]) -> LiveBook:
        yes_levels = message.get("yes_dollars_fp") or message.get("yes_dollars") or []
        no_levels = message.get("no_dollars_fp") or message.get("no_dollars") or []
        return cls(
            bids={as_decimal(price): as_decimal(size) for price, size in yes_levels},
            # The subscription explicitly requests use_yes_price=true, so NO-side
            # levels arrive already expressed as YES asks.
            asks={as_decimal(price): as_decimal(size) for price, size in no_levels},
        )

    def apply_delta(self, message: dict[str, Any]) -> None:
        side = str(message["side"])
        levels = self.bids if side == "yes" else self.asks if side == "no" else None
        if levels is None:
            raise ValueError(f"unknown orderbook side: {side}")
        price = as_decimal(message["price_dollars"])
        updated_size = levels.get(price, Decimal("0")) + as_decimal(message["delta_fp"])
        if updated_size < 0:
            raise SequenceGapError(f"negative size at {price}; local book is stale")
        if updated_size == 0:
            levels.pop(price, None)
        else:
            levels[price] = updated_size

    def as_orderbook(self) -> OrderBook:
        bids = tuple(
            sorted(
                (Level(price, size) for price, size in self.bids.items()),
                key=lambda item: item.price,
                reverse=True,
            )
        )
        asks = tuple(
            sorted(
                (Level(price, size) for price, size in self.asks.items()),
                key=lambda item: item.price,
            )
        )
        return OrderBook(yes_bids=bids, yes_asks=asks)


@dataclass(slots=True)
class KalshiStreamState:
    books: dict[str, LiveBook] = field(default_factory=dict)
    last_sequence_by_subscription: dict[int, int] = field(default_factory=dict)

    def _check_sequence(self, message: dict[str, Any]) -> None:
        sid = message.get("sid")
        sequence = message.get("seq")
        if not isinstance(sid, int) or not isinstance(sequence, int):
            return
        previous = self.last_sequence_by_subscription.get(sid)
        if previous is not None and sequence != previous + 1:
            raise SequenceGapError(
                f"subscription {sid} jumped from sequence {previous} to {sequence}"
            )
        self.last_sequence_by_subscription[sid] = sequence

    def apply(self, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        if message_type not in {"orderbook_snapshot", "orderbook_delta"}:
            return
        self._check_sequence(message)
        payload = message.get("msg", {})
        ticker = str(payload.get("market_ticker", ""))
        if not ticker:
            raise ValueError("orderbook message is missing market_ticker")
        if message_type == "orderbook_snapshot":
            self.books[ticker] = LiveBook.from_snapshot(payload)
            return
        if ticker not in self.books:
            raise SequenceGapError(f"received a delta for {ticker} before its snapshot")
        self.books[ticker].apply_delta(payload)

    def orderbook(self, ticker: str) -> OrderBook | None:
        live_book = self.books.get(ticker)
        return live_book.as_orderbook() if live_book else None


@dataclass(frozen=True, slots=True)
class StreamUpdate:
    message: dict[str, Any]
    state: KalshiStreamState
    connection_number: int


class KalshiWebSocket:
    def __init__(
        self,
        client: KalshiClient,
        *,
        demo: bool = False,
        reconnect_max_seconds: float = 15,
    ) -> None:
        if not client.has_credentials:
            raise ValueError(
                "Kalshi WebSocket requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH"
            )
        self.client = client
        self.url = DEMO_WS_URL if demo else PRODUCTION_WS_URL
        self.reconnect_max_seconds = reconnect_max_seconds

    @staticmethod
    async def _subscribe(websocket: Any, tickers: list[str]) -> None:
        commands = [
            {
                "id": 1,
                "cmd": "subscribe",
                "params": {
                    "channels": ["orderbook_delta"],
                    "market_tickers": tickers,
                    "use_yes_price": True,
                },
            },
            {
                "id": 2,
                "cmd": "subscribe",
                "params": {"channels": ["ticker", "trade"], "market_tickers": tickers},
            },
            {
                "id": 3,
                "cmd": "subscribe",
                "params": {
                    "channels": ["user_orders", "fill", "market_positions"],
                    "market_tickers": tickers,
                },
            },
        ]
        for command in commands:
            await websocket.send(json.dumps(command))

    async def events(self, tickers: list[str]) -> AsyncIterator[StreamUpdate]:
        if not tickers:
            raise ValueError("at least one market ticker is required")
        connection_number = 0
        backoff = 1.0
        while True:
            connection_number += 1
            state = KalshiStreamState()
            try:
                async with websockets.connect(
                    self.url,
                    additional_headers=self.client.websocket_headers(),
                    ping_interval=20,
                    ping_timeout=20,
                    max_queue=4096,
                ) as websocket:
                    await self._subscribe(websocket, tickers)
                    backoff = 1.0
                    async for raw_message in websocket:
                        message = json.loads(raw_message)
                        if not isinstance(message, dict):
                            continue
                        if message.get("type") == "error":
                            raise RuntimeError(f"Kalshi WebSocket error: {message.get('msg')}")
                        state.apply(message)
                        yield StreamUpdate(message, state, connection_number)
            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, OSError, SequenceGapError):
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.reconnect_max_seconds)
