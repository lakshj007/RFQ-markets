import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from kalshi_mm.fair_value import OddsFairSnapshot, OddsFairValueUnavailable
from kalshi_mm.live_order import (
    LIVE_ENABLE_TOKEN,
    LiveAuditLog,
    LiveOrderRequest,
    LiveRiskLimits,
    MonitoredEntryConfig,
    execute_monitored_live_order,
)

NOW = datetime(2026, 7, 12, 12, tzinfo=UTC)


class AdvancingClock:
    def __init__(self, step: float = 1) -> None:
        self.value = -step
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class QuietStream:
    async def events(self, tickers):
        del tickers
        while True:
            await asyncio.sleep(3600)
            if False:
                yield None


class FillStream:
    async def events(self, tickers):
        del tickers
        yield SimpleNamespace(
            message={
                "type": "fill",
                "msg": {"order_id": "order-1", "market_ticker": "MARKET", "count_fp": "1"},
            }
        )


class ScriptedFair:
    def __init__(self, *updates) -> None:
        self.updates = list(updates)
        self.calls = 0

    def refresh_snapshot(self, ticker: str) -> OddsFairSnapshot:
        assert ticker == "MARKET"
        self.calls += 1
        update = self.updates.pop(0) if len(self.updates) > 1 else self.updates[0]
        if isinstance(update, BaseException):
            raise update
        return update


class MonitoredClient:
    def __init__(self, *, race_fill: bool = False, existing=None) -> None:
        self.race_fill = race_fill
        self.existing = list(existing or [])
        self.created: list[dict] = []
        self.cancelled: list[str] = []
        self.status = "resting"
        self.position = "0"
        self.fills: list[dict] = []

    def get_market(self, ticker: str) -> dict:
        return {
            "ticker": ticker,
            "event_ticker": "SERIES-EVENT",
            "title": "A vs B",
            "status": "active",
            "occurrence_datetime": (NOW + timedelta(hours=1)).isoformat(),
            "price_ranges": [{"start": "0", "end": "1", "step": "0.01"}],
        }

    def get_series_details(self, series_ticker: str) -> dict:
        return {"ticker": series_ticker, "fee_type": "quadratic", "fee_multiplier": 1}

    def get_orderbook(self, ticker: str, *, depth: int = 20) -> dict:
        assert depth == 1
        price = self.created[0]["price"] if self.created else "0.6000"
        return {
            "orderbook_fp": {
                "yes_dollars": [[price, "10"]],
                "no_dollars": [["0.3000", "10"]],
            }
        }

    def get_trades(self, ticker: str, *, limit: int = 100) -> list[dict]:
        return [
            {
                "created_time": (NOW - timedelta(seconds=10)).isoformat(),
                "count_fp": "2",
                "is_block_trade": False,
            }
        ]

    def get_balance(self, *, subaccount: int = 0) -> dict:
        return {"balance": 1000}

    def get_position(self, ticker: str, *, subaccount: int = 0) -> str:
        return self.position

    def get_orders(self, **kwargs) -> list[dict]:
        if not self.created:
            return list(self.existing)
        return [
            {
                "order_id": "order-1",
                "client_order_id": self.created[0]["client_order_id"],
                "status": self.status,
                "remaining_count_fp": "1" if self.status == "resting" else "0",
            }
        ]

    def get_fills(self, **kwargs) -> list[dict]:
        return list(self.fills)

    def create_order(self, **kwargs) -> dict:
        self.created.append(kwargs)
        return {"order_id": "order-1", "remaining_count": "1"}

    def cancel_order(self, order_id: str, *, subaccount: int = 0) -> dict:
        self.cancelled.append(order_id)
        if self.race_fill:
            self.status = "executed"
            self.position = "1"
            self.fills = [
                {
                    "fill_id": "fill-1",
                    "order_id": order_id,
                    "count_fp": "1",
                    "fee_cost": "0.01",
                }
            ]
            return {"order_id": order_id, "reduced_by": "0"}
        self.status = "canceled"
        return {"order_id": order_id, "reduced_by": "1"}


def snapshot(probability: str = "0.63") -> OddsFairSnapshot:
    return OddsFairSnapshot(
        probability=Decimal(probability),
        event_commence_time=NOW + timedelta(hours=1),
        observed_at=NOW,
        event_id="event-1",
        bookmaker_count=2,
        bookmaker_keys=("book-a", "book-b"),
        oldest_update=NOW - timedelta(seconds=5),
        quota_remaining=100,
    )


def request() -> LiveOrderRequest:
    return LiveOrderRequest(
        ticker="MARKET",
        side="bid",
        price=Decimal("0.60"),
        count=Decimal("1"),
        fair_probability=Decimal("0.63"),
        external_start_time=NOW + timedelta(hours=1),
        expiration_seconds=10,
    )


def config() -> MonitoredEntryConfig:
    return MonitoredEntryConfig(
        poll_interval_seconds=0.01,
        max_rest_seconds=10,
        max_odds_age_seconds=60,
        failure_grace_seconds=0.01,
        rest_reconcile_seconds=0.01,
    )


def run_monitor(
    monkeypatch,
    tmp_path,
    client,
    source,
    *,
    interrupt=False,
    stream=None,
    monotonic=None,
):
    monkeypatch.setenv("KALSHI_LIVE_TRADING_ENABLED", LIVE_ENABLE_TOKEN)
    audit = tmp_path / "audit.jsonl"

    async def run():
        if interrupt:
            task = asyncio.current_task()
            assert task is not None
            asyncio.get_running_loop().call_soon(task.cancel)
        return await execute_monitored_live_order(
            client,
            request(),
            LiveRiskLimits(),
            fair_source=source,
            initial_snapshot=snapshot(),
            stream=stream or QuietStream(),  # type: ignore[arg-type]
            config=config(),
            intent_id="test-intent-001",
            audit_log=LiveAuditLog(audit),
            now=NOW,
            monotonic=monotonic or AdvancingClock(),
        )

    return asyncio.run(run()), audit


def test_valid_fair_rests_unchanged_until_bounded_timeout(monkeypatch, tmp_path) -> None:
    client = MonitoredClient()
    result, _ = run_monitor(monkeypatch, tmp_path, client, ScriptedFair(snapshot()))

    assert result["cancellation_reason"] == "maximum_rest_timeout"
    assert client.cancelled == ["order-1"]
    assert len(client.created) == 1
    assert client.created[0]["price"] == "0.6000"
    assert client.created[0]["expiration_time"] == int(NOW.timestamp()) + 10


def test_fair_below_two_cent_threshold_cancels(monkeypatch, tmp_path) -> None:
    client = MonitoredClient()
    result, _ = run_monitor(monkeypatch, tmp_path, client, ScriptedFair(snapshot("0.6199")))

    assert result["cancellation_reason"] == "fair_below_threshold"
    assert client.cancelled == ["order-1"]


def test_insufficient_or_stale_books_cancel(monkeypatch, tmp_path) -> None:
    for reason in ("insufficient_bookmakers", "stale_odds"):
        client = MonitoredClient()
        source = ScriptedFair(OddsFairValueUnavailable(reason, reason))
        result, _ = run_monitor(monkeypatch, tmp_path, client, source)
        assert result["cancellation_reason"] == reason
        assert client.cancelled == ["order-1"]


def test_odds_api_failure_grace_expires_and_cancels(monkeypatch, tmp_path) -> None:
    client = MonitoredClient()
    result, audit = run_monitor(
        monkeypatch,
        tmp_path,
        client,
        ScriptedFair(RuntimeError("temporary outage")),
    )

    assert result["cancellation_reason"] == "odds_api_failure_grace_expired"
    records = [json.loads(line) for line in audit.read_text().splitlines()]
    assert sum(item["event"] == "fair_refresh_failed" for item in records) >= 2


def test_fill_race_reconciles_position_fill_and_fee(monkeypatch, tmp_path) -> None:
    client = MonitoredClient(race_fill=True)
    result, audit = run_monitor(
        monkeypatch,
        tmp_path,
        client,
        ScriptedFair(snapshot("0.61")),
    )

    assert result["result"] == "filled_after_cancel_race"
    assert result["final_position"] == "1"
    assert result["fee"] == "0.01"
    records = [json.loads(line) for line in audit.read_text().splitlines()]
    assert "fill_race" in [item["event"] for item in records]
    assert records[-1]["event"] == "monitored_final_reconciliation"


def test_websocket_fill_triggers_immediate_cancel_and_rest_reconciliation(
    monkeypatch, tmp_path
) -> None:
    client = MonitoredClient(race_fill=True)
    result, audit = run_monitor(
        monkeypatch,
        tmp_path,
        client,
        ScriptedFair(snapshot()),
        stream=FillStream(),
        monotonic=lambda: 0,
    )

    assert result["cancellation_reason"] == "fill_detected_cancel_remainder"
    records = [json.loads(line) for line in audit.read_text().splitlines()]
    assert "websocket_fill" in [item["event"] for item in records]
    assert "monitored_final_reconciliation" in [item["event"] for item in records]


def test_ctrl_c_path_cancels_and_reconciles(monkeypatch, tmp_path) -> None:
    client = MonitoredClient()
    result, _ = run_monitor(
        monkeypatch,
        tmp_path,
        client,
        ScriptedFair(snapshot()),
        interrupt=True,
    )

    assert result["cancellation_reason"] == "interrupted"
    assert client.cancelled == ["order-1"]


def test_duplicate_intent_never_submits_or_reprices(monkeypatch, tmp_path) -> None:
    existing = [
        {
            "order_id": "existing",
            "client_order_id": "manual-live-test-intent-001",
            "status": "resting",
        }
    ]
    client = MonitoredClient(existing=existing)
    result, _ = run_monitor(monkeypatch, tmp_path, client, ScriptedFair(snapshot()))

    assert result["result"] == "reconciled_existing"
    assert client.created == []
    assert client.cancelled == []
