import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_mm.models import PriceGrid
from kalshi_mm.rfq import (
    JsonMoneylineFairBook,
    MoneylineFair,
    RFQMaker,
    RFQMakerConfig,
    RFQRequest,
    RFQRiskLedger,
    price_moneyline_rfq,
)


def request(*, contracts: str = "1") -> RFQRequest:
    return RFQRequest(
        rfq_id="rfq-1",
        ticker="MARKET",
        contracts=Decimal(contracts),
        created_at=datetime.now(UTC),
    )


def fair(*, probability: str = "0.55", age_seconds: float = 0) -> MoneylineFair:
    now = datetime.now(UTC)
    return MoneylineFair(
        ticker="MARKET",
        probability=Decimal(probability),
        observed_at=now - timedelta(seconds=age_seconds),
        event_start=now + timedelta(hours=1),
        source="test",
    )


def test_moneyline_quote_applies_two_percent_to_each_outcome_fair() -> None:
    plan = price_moneyline_rfq(
        request(),
        fair(probability="0.30"),
        price_grid=PriceGrid.uniform("0.001"),
        edge_rate=Decimal("0.02"),
    )

    assert plan.yes_bid == Decimal("0.294")
    assert plan.no_bid == Decimal("0.686")
    assert plan.yes_edge_rate == Decimal("0.02")
    assert plan.no_edge_rate == Decimal("0.02")
    assert plan.fair.probability - plan.yes_bid == Decimal("0.006")
    assert Decimal("1") - plan.fair.probability - plan.no_bid == Decimal("0.014")
    assert plan.yes_bid + plan.no_bid == Decimal("0.980")


def test_grid_rounding_can_only_improve_maker_edge() -> None:
    plan = price_moneyline_rfq(
        request(),
        fair(),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )

    assert plan.yes_bid == Decimal("0.53")
    assert plan.no_bid == Decimal("0.44")
    assert plan.yes_edge_rate is not None
    assert plan.no_edge_rate is not None
    assert plan.yes_edge_rate > Decimal("0.02")
    assert plan.no_edge_rate > Decimal("0.02")


def test_rfq_edge_has_a_hard_two_percent_floor() -> None:
    with pytest.raises(ValueError, match=r"\[2%, 100%\)"):
        price_moneyline_rfq(
            request(),
            fair(),
            price_grid=PriceGrid.uniform(),
            edge_rate=Decimal("0.0199"),
        )

    with pytest.raises(ValueError, match=r"\[2%, 100%\)"):
        RFQMakerConfig(edge_rate=Decimal("0.01")).validate()


def test_risk_ledger_disables_only_the_position_increasing_side() -> None:
    plan = price_moneyline_rfq(
        request(),
        fair(),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )
    ledger = RFQRiskLedger(
        RFQMakerConfig(max_abs_position=Decimal("1")),
        positions={"MARKET": Decimal("1")},
        available_balance=Decimal("100"),
    )

    constrained = ledger.constrain(plan)

    assert constrained.yes_bid == Decimal("0")
    assert constrained.no_bid == Decimal("0.44")


def test_risk_ledger_rejects_fractional_dust_by_default() -> None:
    plan = price_moneyline_rfq(
        request(contracts="0.50"),
        fair(),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )
    ledger = RFQRiskLedger(
        RFQMakerConfig(),
        available_balance=Decimal("100"),
    )

    with pytest.raises(ValueError, match="below the minimum"):
        ledger.constrain(plan)


def test_json_fair_book_requires_freshness_metadata(tmp_path) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "rfq-fairs.json"
    path.write_text(
        json.dumps(
            {
                "markets": {
                    "MARKET": {
                        "probability": "0.55",
                        "observed_at": now.isoformat(),
                        "event_start": (now + timedelta(hours=1)).isoformat(),
                        "market_type": "moneyline",
                    }
                }
            }
        )
    )
    book = JsonMoneylineFairBook(path)

    assert book.refresh() == ("MARKET",)
    parsed = book.get("MARKET")
    assert parsed is not None
    assert parsed.probability == Decimal("0.55")
    assert parsed.observed_at == now
    assert parsed.event_start == now + timedelta(hours=1)


class FakeFairBook:
    refresh_seconds = 60.0

    def __init__(self, value: MoneylineFair) -> None:
        self.value = value

    def refresh(self) -> tuple[str, ...]:
        return ("MARKET",)

    def get(self, ticker: str) -> MoneylineFair | None:
        return self.value if ticker == "MARKET" else None

    def tickers(self) -> tuple[str, ...]:
        return ("MARKET",)


class EmptyStream:
    async def events(self):
        if False:
            yield {}


class OneMessageStream:
    async def events(self):
        yield created_message()


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def append(self, event: str, payload: dict[str, object]) -> None:
        self.records.append((event, payload))


class FakeClient:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.confirmed: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.existing_quotes: list[dict] = []

    def get_market(self, ticker: str) -> dict:
        return {
            "ticker": ticker,
            "status": "active",
            "price_ranges": [{"start": "0", "end": "1", "step": "0.01"}],
        }

    def get_position(self, ticker: str, *, subaccount: int = 0) -> str:
        return "0"

    def get_balance(self, *, subaccount: int = 0) -> dict:
        return {"balance": 10_000}

    def create_rfq_quote(self, **kwargs) -> str:
        self.created.append(kwargs)
        return "quote-1"

    def get_rfq_quotes(self, **kwargs) -> list[dict]:
        return self.existing_quotes

    def delete_rfq_quote(self, rfq_id: str, quote_id: str) -> None:
        self.deleted.append((rfq_id, quote_id))

    def confirm_rfq_quote(self, rfq_id: str, quote_id: str) -> None:
        self.confirmed.append((rfq_id, quote_id))


def created_message() -> dict:
    return {
        "type": "rfq_created",
        "msg": {
            "id": "rfq-1",
            "market_ticker": "MARKET",
            "contracts_fp": "1.00",
            "target_cost_dollars": "0",
            "created_ts": datetime.now(UTC).isoformat(),
        },
    }


def accepted_message() -> dict:
    return {
        "type": "quote_accepted",
        "msg": {
            "rfq_id": "rfq-1",
            "quote_id": "quote-1",
            "accepted_side": "yes",
            "contracts_accepted_fp": "1.00",
        },
    }


def test_execute_quotes_and_confirms_from_cached_fair() -> None:
    client = FakeClient()
    audit = RecordingAudit()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(),
        audit_log=audit,
        execute=True,
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(created_message())
        await maker.handle(accepted_message())

    asyncio.run(scenario())

    assert client.created[0]["yes_bid"] == "0.53"
    assert client.created[0]["no_bid"] == "0.44"
    assert client.created[0]["rest_remainder"] is False
    assert client.created[0]["post_only"] is True
    assert client.confirmed == [("rfq-1", "quote-1")]
    assert any(event == "rfq_quote_confirmed" for event, _ in audit.records)


def test_confirmation_is_withheld_after_adverse_fair_move() -> None:
    client = FakeClient()
    audit = RecordingAudit()
    fair_book = FakeFairBook(fair())
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=fair_book,
        config=RFQMakerConfig(),
        audit_log=audit,
        execute=True,
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(created_message())
        fair_book.value = replace(fair_book.value, probability=Decimal("0.54"))
        await maker.handle(accepted_message())

    asyncio.run(scenario())

    assert client.confirmed == []
    withheld = [payload for event, payload in audit.records if event == "rfq_confirmation_withheld"]
    assert len(withheld) == 1
    assert "minimum proportional edge" in str(withheld[0]["reason"])


def test_accepted_risk_is_retained_until_execution() -> None:
    client = FakeClient()
    audit = RecordingAudit()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(),
        audit_log=audit,
        execute=True,
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(created_message())
        await maker.handle(accepted_message())
        await maker.handle({"type": "rfq_deleted", "msg": {"id": "rfq-1"}})
        assert "rfq-1" in maker.ledger.reservations
        await maker.handle(
            {
                "type": "quote_executed",
                "msg": {
                    "rfq_id": "rfq-1",
                    "quote_id": "quote-1",
                    "order_id": "order-1",
                },
            }
        )

    asyncio.run(scenario())

    assert "rfq-1" not in maker.ledger.reservations
    assert maker.ledger.positions["MARKET"] == Decimal("1")
    assert any(event == "rfq_reservation_retained" for event, _ in audit.records)


def test_run_cancels_unaccepted_quotes_on_clean_shutdown() -> None:
    client = FakeClient()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=OneMessageStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(),
        audit_log=RecordingAudit(),
        execute=True,
    )

    asyncio.run(maker.run(max_messages=1))

    assert client.deleted == [("rfq-1", "quote-1")]
    assert maker.ledger.reservations == {}


def test_startup_refuses_unresolved_existing_quotes() -> None:
    client = FakeClient()
    client.existing_quotes = [
        {
            "id": "old-quote",
            "rfq_id": "old-rfq",
            "status": "open",
            "executed_ts": None,
            "cancelled_ts": None,
        }
    ]
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(),
        audit_log=RecordingAudit(),
        execute=True,
    )

    with pytest.raises(ValueError, match="unresolved maker RFQ quotes"):
        asyncio.run(maker.prepare())
