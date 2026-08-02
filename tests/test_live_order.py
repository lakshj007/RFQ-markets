import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_mm.live_order import (
    LIVE_ENABLE_TOKEN,
    BoundedExitRequest,
    LiveAuditLog,
    LiveOrderRequest,
    LiveRiskLimits,
    execute_bounded_exit,
    execute_live_order,
    format_fixed_price,
    preflight_bounded_exit,
    preflight_live_order,
)


class FakeLiveClient:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.market_start_offset = timedelta(hours=1)
        self.position = "0"
        self.balance = 1000
        self.existing_orders: list[dict] = []
        self.created: list[dict] = []
        self.cancelled: list[str] = []

    def get_market(self, ticker: str) -> dict:
        return {
            "ticker": ticker,
            "event_ticker": "SERIES-EVENT",
            "title": "Seattle vs Portland Winner?",
            "status": "active",
            "occurrence_datetime": (self.now + self.market_start_offset).isoformat(),
            "price_ranges": [{"start": "0", "end": "1", "step": "0.01"}],
        }

    def get_series_details(self, series_ticker: str) -> dict:
        assert series_ticker == "SERIES"
        return {"ticker": series_ticker, "fee_type": "quadratic", "fee_multiplier": 1}

    def get_orderbook(self, ticker: str, *, depth: int = 20) -> dict:
        assert depth == 1
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.16", "93"]],
                "no_dollars": [["0.83", "484"]],
            }
        }

    def get_trades(self, ticker: str, *, limit: int = 100) -> list[dict]:
        return [
            {
                "trade_id": "recent",
                "created_time": (self.now - timedelta(seconds=60)).isoformat(),
                "count_fp": "14",
                "yes_price_dollars": "0.17",
                "taker_book_side": "bid",
                "is_block_trade": False,
            }
        ]

    def get_balance(self, *, subaccount: int = 0) -> dict:
        return {"balance": self.balance, "portfolio_value": 0, "updated_ts": 0}

    def get_position(self, ticker: str, *, subaccount: int = 0) -> str:
        return self.position

    def get_orders(
        self,
        *,
        ticker: str | None = None,
        status: str | None = None,
        subaccount: int = 0,
        limit: int = 100,
    ) -> list[dict]:
        if status == "resting" and not self.created:
            return []
        if self.created:
            return [
                {
                    "order_id": "order-1",
                    "client_order_id": self.created[0]["client_order_id"],
                    "status": "resting",
                    "remaining_count_fp": "1",
                }
            ]
        return list(self.existing_orders)

    def create_order(self, **kwargs) -> dict:
        self.created.append(kwargs)
        return {
            "order_id": "order-1",
            "client_order_id": kwargs["client_order_id"],
            "fill_count": "0",
            "remaining_count": "1",
            "ts_ms": 1,
        }

    def cancel_order(self, order_id: str, *, subaccount: int = 0) -> dict:
        self.cancelled.append(order_id)
        return {
            "order_id": order_id,
            "client_order_id": self.created[0]["client_order_id"],
            "reduced_by": "1",
            "ts_ms": 2,
        }


def request() -> LiveOrderRequest:
    return LiveOrderRequest(
        ticker="MARKET",
        side="bid",
        price=Decimal("0.16"),
        count=Decimal("1"),
        fair_probability=Decimal("0.19"),
        external_start_time=datetime(2026, 7, 11, 3, tzinfo=UTC),
        expiration_seconds=120,
    )


def test_fixed_price_preserves_required_trailing_precision() -> None:
    assert format_fixed_price(Decimal("0.60")) == "0.6000"
    assert format_fixed_price(Decimal("0.16")) == "0.1600"


def test_public_preflight_requires_recent_flow_and_edge() -> None:
    now = datetime(2026, 7, 11, 2, tzinfo=UTC)
    result = preflight_live_order(
        FakeLiveClient(now),
        request(),
        LiveRiskLimits(),
        authenticated=False,
        now=now,
    )

    assert result.modeled_edge == Decimal("0.03")
    assert result.recent_contracts == Decimal("14")
    assert result.queue_ahead == Decimal("93")
    assert result.market_occurrence_time == now + timedelta(hours=1)
    assert result.external_start_time == now + timedelta(hours=1)
    assert result.effective_start_time == now + timedelta(hours=1)
    assert result.order_expiration_time == now + timedelta(seconds=120)
    assert result.estimated_maker_fee == Decimal("0")
    assert result.maximum_loss == Decimal("0.16")
    assert result.position is None
    assert result.available_balance is None


def test_preflight_allows_ten_minute_guarded_expiration() -> None:
    now = datetime(2026, 7, 11, 2, tzinfo=UTC)
    ten_minute_request = LiveOrderRequest(
        ticker="MARKET",
        side="bid",
        price=Decimal("0.16"),
        count=Decimal("1"),
        fair_probability=Decimal("0.19"),
        external_start_time=now + timedelta(hours=1),
        expiration_seconds=600,
    )

    result = preflight_live_order(
        FakeLiveClient(now),
        ten_minute_request,
        LiveRiskLimits(max_expiration_seconds=600),
        authenticated=False,
        now=now,
    )

    assert result.order_expiration_time == now + timedelta(minutes=10)


def test_pregame_monitor_mode_allows_bounded_multi_hour_expiration() -> None:
    now = datetime(2026, 7, 11, 2, tzinfo=UTC)
    client = FakeLiveClient(now)
    client.market_start_offset = timedelta(hours=4)
    pregame_request = LiveOrderRequest(
        ticker="MARKET",
        side="bid",
        price=Decimal("0.16"),
        count=Decimal("1"),
        fair_probability=Decimal("0.19"),
        external_start_time=now + timedelta(hours=4),
        expiration_seconds=60 * 60,
        monitor_until_pregame=True,
    )

    result = preflight_live_order(
        client,
        pregame_request,
        LiveRiskLimits(max_expiration_seconds=600),
        authenticated=False,
        now=now,
    )

    assert result.order_expiration_time == now + timedelta(hours=1)


def test_preflight_rejects_crossing_and_insufficient_edge() -> None:
    now = datetime(2026, 7, 11, 2, tzinfo=UTC)
    client = FakeLiveClient(now)
    crossing = LiveOrderRequest(
        ticker="MARKET",
        side="bid",
        price=Decimal("0.17"),
        count=Decimal("1"),
        fair_probability=Decimal("0.20"),
        external_start_time=now + timedelta(hours=1),
        expiration_seconds=120,
    )
    low_edge = LiveOrderRequest(
        ticker="MARKET",
        side="bid",
        price=Decimal("0.16"),
        count=Decimal("1"),
        fair_probability=Decimal("0.169"),
        external_start_time=now + timedelta(hours=1),
        expiration_seconds=120,
    )

    with pytest.raises(ValueError, match="would cross"):
        preflight_live_order(client, crossing, LiveRiskLimits(), authenticated=False, now=now)
    with pytest.raises(ValueError, match="below the live minimum"):
        preflight_live_order(client, low_edge, LiveRiskLimits(), authenticated=False, now=now)


def test_preflight_allows_one_cent_passive_bid_edge() -> None:
    now = datetime(2026, 7, 11, 2, tzinfo=UTC)
    one_cent_edge = LiveOrderRequest(
        ticker="MARKET",
        side="bid",
        price=Decimal("0.16"),
        count=Decimal("1"),
        fair_probability=Decimal("0.17"),
        external_start_time=now + timedelta(hours=1),
        expiration_seconds=120,
    )

    result = preflight_live_order(
        FakeLiveClient(now),
        one_cent_edge,
        LiveRiskLimits(),
        authenticated=False,
        now=now,
    )

    assert result.modeled_edge == Decimal("0.01")


def test_reduce_only_ask_can_exit_without_positive_edge() -> None:
    now = datetime(2026, 7, 11, 2, tzinfo=UTC)
    client = FakeLiveClient(now)
    client.position = "1"
    exit_request = LiveOrderRequest(
        ticker="MARKET",
        side="ask",
        price=Decimal("0.17"),
        count=Decimal("1"),
        fair_probability=Decimal("0.19"),
        external_start_time=now + timedelta(hours=1),
        expiration_seconds=120,
    )

    result = preflight_live_order(
        client,
        exit_request,
        LiveRiskLimits(),
        authenticated=True,
        now=now,
    )

    assert result.modeled_edge == Decimal("-0.02")
    assert result.position == Decimal("1")


def test_preflight_rejects_noncompetitive_price_and_large_queue() -> None:
    now = datetime(2026, 7, 11, 2, tzinfo=UTC)
    client = FakeLiveClient(now)
    noncompetitive = LiveOrderRequest(
        ticker="MARKET",
        side="bid",
        price=Decimal("0.15"),
        count=Decimal("1"),
        fair_probability=Decimal("0.19"),
        external_start_time=now + timedelta(hours=1),
        expiration_seconds=120,
    )

    with pytest.raises(ValueError, match="join or improve"):
        preflight_live_order(
            client,
            noncompetitive,
            LiveRiskLimits(),
            authenticated=False,
            now=now,
        )
    with pytest.raises(ValueError, match="queue exceeds"):
        preflight_live_order(
            client,
            request(),
            LiveRiskLimits(max_queue_ahead=Decimal("50")),
            authenticated=False,
            now=now,
        )


def test_preflight_uses_earlier_independent_start_time() -> None:
    now = datetime(2026, 7, 11, 2, tzinfo=UTC)
    client = FakeLiveClient(now)
    client.market_start_offset = timedelta(hours=3, minutes=4)
    live_request = LiveOrderRequest(
        ticker="MARKET",
        side="bid",
        price=Decimal("0.16"),
        count=Decimal("1"),
        fair_probability=Decimal("0.19"),
        external_start_time=now + timedelta(minutes=4),
        expiration_seconds=120,
    )

    with pytest.raises(ValueError, match="stops five minutes before start"):
        preflight_live_order(
            client,
            live_request,
            LiveRiskLimits(),
            authenticated=False,
            now=now,
        )


def test_preflight_reports_three_hour_market_start_offset() -> None:
    now = datetime(2026, 7, 11, 2, tzinfo=UTC)
    client = FakeLiveClient(now)
    client.market_start_offset = timedelta(hours=4)
    live_request = LiveOrderRequest(
        ticker="MARKET",
        side="bid",
        price=Decimal("0.16"),
        count=Decimal("1"),
        fair_probability=Decimal("0.19"),
        external_start_time=now + timedelta(hours=1),
        expiration_seconds=120,
    )

    result = preflight_live_order(
        client,
        live_request,
        LiveRiskLimits(),
        authenticated=False,
        now=now,
    )

    assert result.effective_start_time == now + timedelta(hours=1)
    assert result.start_time_delta_seconds == 3 * 60 * 60


def test_preflight_includes_maker_fee_in_maximum_loss() -> None:
    now = datetime(2026, 7, 11, 2, tzinfo=UTC)

    class MakerFeeClient(FakeLiveClient):
        def get_series_details(self, series_ticker: str) -> dict:
            return {
                "ticker": series_ticker,
                "fee_type": "quadratic_with_maker_fees",
                "fee_multiplier": 1,
            }

    result = preflight_live_order(
        MakerFeeClient(now),
        request(),
        LiveRiskLimits(),
        authenticated=False,
        now=now,
    )

    assert result.estimated_order_cost == Decimal("0.16")
    assert result.estimated_maker_fee == Decimal("0.01")
    assert result.maximum_loss == Decimal("0.17")


def exit_request() -> BoundedExitRequest:
    return BoundedExitRequest(
        ticker="MARKET",
        target_price=Decimal("0.17"),
        floor_price=Decimal("0.15"),
        count=Decimal("1"),
        external_start_time=datetime(2026, 7, 11, 3, tzinfo=UTC),
        target_wait_seconds=5,
    )


def test_bounded_exit_preflight_estimates_ioc_fallback() -> None:
    now = datetime(2026, 7, 11, 2, tzinfo=UTC)
    client = FakeLiveClient(now)
    client.position = "1"

    result = preflight_bounded_exit(client, exit_request(), now=now)

    assert result.fallback_price == Decimal("0.16")
    assert result.estimated_fallback_fee == Decimal("0.01")
    assert result.position == Decimal("1")


class FakeBoundedExitClient(FakeLiveClient):
    def __init__(self, now: datetime, *, fallback_bid: str = "0.16") -> None:
        super().__init__(now)
        self.position = "1"
        self.fallback_bid = fallback_bid
        self.target_cancelled = False

    def get_orderbook(self, ticker: str, *, depth: int = 20) -> dict:
        return {
            "orderbook_fp": {
                "yes_dollars": [[self.fallback_bid, "10"]],
                "no_dollars": [["0.83", "10"]],
            }
        }

    def get_orders(self, **kwargs) -> list[dict]:
        if not self.created or self.target_cancelled:
            return []
        return [{"order_id": "order-1", "status": "resting"}]

    def create_order(self, **kwargs) -> dict:
        self.created.append(kwargs)
        if kwargs["time_in_force"] == "immediate_or_cancel":
            self.position = "0"
            return {"order_id": "order-2", "fill_count": "1", "remaining_count": "0"}
        return {"order_id": "order-1", "fill_count": "0", "remaining_count": "1"}

    def cancel_order(self, order_id: str, *, subaccount: int = 0) -> dict:
        self.target_cancelled = True
        self.cancelled.append(order_id)
        return {"order_id": order_id, "reduced_by": "1"}


def test_bounded_exit_cancels_target_then_uses_reduce_only_ioc(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KALSHI_LIVE_TRADING_ENABLED", LIVE_ENABLE_TOKEN)
    now = datetime(2026, 7, 11, 2, tzinfo=UTC)
    client = FakeBoundedExitClient(now)
    clock = iter((0.0, 6.0))

    result = execute_bounded_exit(
        client,
        exit_request(),
        intent_id="test-intent-001",
        audit_log=LiveAuditLog(tmp_path / "audit.jsonl"),
        now=now,
        monotonic=lambda: next(clock),
        sleep=lambda _: None,
    )

    assert result["result"] == "fallback_submitted"
    assert client.cancelled == ["order-1"]
    assert len(client.created) == 2
    fallback = client.created[1]
    assert fallback["side"] == "ask"
    assert fallback["price"] == "0.1600"
    assert fallback["reduce_only"] is True
    assert fallback["post_only"] is False
    assert fallback["time_in_force"] == "immediate_or_cancel"
    assert result["remaining_position"] == "0"


def test_bounded_exit_holds_when_bid_is_below_floor(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KALSHI_LIVE_TRADING_ENABLED", LIVE_ENABLE_TOKEN)
    now = datetime(2026, 7, 11, 2, tzinfo=UTC)
    client = FakeBoundedExitClient(now, fallback_bid="0.14")
    clock = iter((0.0, 6.0))

    result = execute_bounded_exit(
        client,
        exit_request(),
        intent_id="test-intent-001",
        audit_log=LiveAuditLog(tmp_path / "audit.jsonl"),
        now=now,
        monotonic=lambda: next(clock),
        sleep=lambda _: None,
    )

    assert result["result"] == "held_below_floor"
    assert len(client.created) == 1
    assert client.position == "1"


def test_execute_requires_environment_kill_switch(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("KALSHI_LIVE_TRADING_ENABLED", raising=False)
    now = datetime(2026, 7, 11, 2, tzinfo=UTC)

    with pytest.raises(ValueError, match="I_UNDERSTAND_REAL_MONEY"):
        execute_live_order(
            FakeLiveClient(now),
            request(),
            LiveRiskLimits(),
            intent_id="test-intent-001",
            audit_log=LiveAuditLog(tmp_path / "audit.jsonl"),
            now=now,
        )


def test_execute_times_out_and_cancels_remainder(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KALSHI_LIVE_TRADING_ENABLED", LIVE_ENABLE_TOKEN)
    now = datetime(2026, 7, 11, 2, tzinfo=UTC)
    client = FakeLiveClient(now)
    clock = iter((0.0, 6.0))

    result = execute_live_order(
        client,
        request(),
        LiveRiskLimits(),
        intent_id="test-intent-001",
        audit_log=LiveAuditLog(tmp_path / "audit.jsonl"),
        wait_seconds=5,
        now=now,
        monotonic=lambda: next(clock),
        sleep=lambda _: None,
    )

    assert result["result"] == "cancelled_after_timeout"
    assert client.cancelled == ["order-1"]
    assert client.created[0]["post_only"] is True
    assert client.created[0]["cancel_order_on_pause"] is True
    assert client.created[0]["reduce_only"] is False
    assert client.created[0]["expiration_time"] == int(now.timestamp()) + 120
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [item["event"] for item in records] == [
        "intent_validated",
        "submitted",
        "cancelled",
    ]


def test_duplicate_intent_reconciles_without_resubmitting(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KALSHI_LIVE_TRADING_ENABLED", LIVE_ENABLE_TOKEN)
    now = datetime(2026, 7, 11, 2, tzinfo=UTC)
    client = FakeLiveClient(now)
    client.existing_orders = [
        {
            "order_id": "existing-order",
            "client_order_id": "manual-live-test-intent-001",
            "status": "executed",
        }
    ]

    result = execute_live_order(
        client,
        request(),
        LiveRiskLimits(),
        intent_id="test-intent-001",
        audit_log=LiveAuditLog(tmp_path / "audit.jsonl"),
        now=now,
    )

    assert result["result"] == "reconciled_existing"
    assert client.created == []


def test_ambiguous_submit_is_recovered_and_cancelled(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KALSHI_LIVE_TRADING_ENABLED", LIVE_ENABLE_TOKEN)
    now = datetime(2026, 7, 11, 2, tzinfo=UTC)

    class AmbiguousClient(FakeLiveClient):
        def create_order(self, **kwargs) -> dict:
            self.created.append(kwargs)
            raise TimeoutError("response lost after submission")

    client = AmbiguousClient(now)
    audit_path = tmp_path / "audit.jsonl"

    with pytest.raises(TimeoutError, match="response lost"):
        execute_live_order(
            client,
            request(),
            LiveRiskLimits(),
            intent_id="test-intent-001",
            audit_log=LiveAuditLog(audit_path),
            now=now,
        )

    assert client.cancelled == ["order-1"]
    records = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert [item["event"] for item in records][-3:] == [
        "recovered_after_submit_error",
        "cancelled_recovered_order",
        "error",
    ]
