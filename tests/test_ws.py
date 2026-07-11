import asyncio
import json
from decimal import Decimal

import pytest

from kalshi_mm.ws import KalshiStreamState, KalshiWebSocket, SequenceGapError


def snapshot(sequence: int = 10) -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": 2,
        "seq": sequence,
        "msg": {
            "market_ticker": "MARKET",
            "yes_dollars_fp": [["0.45", "10.00"]],
            # use_yes_price=true means this is already a YES ask.
            "no_dollars_fp": [["0.55", "12.00"]],
        },
    }


def test_stream_state_applies_snapshot_and_delta_in_yes_price_scale() -> None:
    state = KalshiStreamState()
    state.apply(snapshot())
    state.apply(
        {
            "type": "orderbook_delta",
            "sid": 2,
            "seq": 11,
            "msg": {
                "market_ticker": "MARKET",
                "side": "yes",
                "price_dollars": "0.45",
                "delta_fp": "5.00",
            },
        }
    )

    book = state.orderbook("MARKET")
    assert book is not None
    assert book.best_bid.price == Decimal("0.45")
    assert book.best_bid.size == Decimal("15.00")
    assert book.best_ask.price == Decimal("0.55")


def test_stream_state_rejects_sequence_gap() -> None:
    state = KalshiStreamState()
    state.apply(snapshot())

    with pytest.raises(SequenceGapError, match="jumped"):
        state.apply(
            {
                "type": "orderbook_delta",
                "sid": 2,
                "seq": 12,
                "msg": {
                    "market_ticker": "MARKET",
                    "side": "no",
                    "price_dollars": "0.55",
                    "delta_fp": "-1.00",
                },
            }
        )


def test_stream_state_removes_empty_levels() -> None:
    state = KalshiStreamState()
    state.apply(snapshot())
    state.apply(
        {
            "type": "orderbook_delta",
            "sid": 2,
            "seq": 11,
            "msg": {
                "market_ticker": "MARKET",
                "side": "no",
                "price_dollars": "0.55",
                "delta_fp": "-12.00",
            },
        }
    )

    book = state.orderbook("MARKET")
    assert book is not None
    assert book.best_ask is None


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, raw_message: str) -> None:
        self.messages.append(json.loads(raw_message))


def test_subscriptions_request_unified_prices_and_account_updates() -> None:
    websocket = FakeWebSocket()

    asyncio.run(KalshiWebSocket._subscribe(websocket, ["MARKET"]))

    assert len(websocket.messages) == 3
    assert websocket.messages[0]["params"]["use_yes_price"] is True
    assert websocket.messages[0]["params"]["channels"] == ["orderbook_delta"]
    assert "fill" in websocket.messages[2]["params"]["channels"]
