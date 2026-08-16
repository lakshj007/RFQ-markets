import asyncio
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from kalshi_mm.client import KalshiAPIError
from kalshi_mm.models import PriceGrid
from kalshi_mm.rfq import (
    CompositeMoneylineFairBook,
    JsonMoneylineFairBook,
    MarkdownRFQFillLedger,
    MoneylineFair,
    OddsMoneylineFairBook,
    RFQLeg,
    RFQMaker,
    RFQMakerConfig,
    RFQRequest,
    RFQRiskLedger,
    estimated_maker_fee,
    price_lead_style_rfq,
    price_moneyline_rfq,
)
from kalshi_mm.scanner import ConsensusPrice


def request(*, contracts: str = "1") -> RFQRequest:
    return RFQRequest(
        rfq_id="rfq-1",
        ticker="MARKET",
        contracts=Decimal(contracts),
        created_at=datetime.now(UTC),
    )


def fair(
    *,
    ticker: str = "MARKET",
    probability: str = "0.55",
    age_seconds: float = 0,
    event_ticker: str = "EVENT-MARKET",
    participants: tuple[str, str] = ("Team A", "Team B"),
) -> MoneylineFair:
    now = datetime.now(UTC)
    return MoneylineFair(
        ticker=ticker,
        probability=Decimal(probability),
        observed_at=now - timedelta(seconds=age_seconds),
        event_start=now + timedelta(hours=1),
        source="test",
        event_ticker=event_ticker,
        participants=frozenset(participants),
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


def test_lead_style_quote_is_no_only_and_applies_two_leg_premium() -> None:
    combo_request = RFQRequest(
        rfq_id="lead",
        ticker="COMBO",
        contracts=Decimal("1"),
        created_at=datetime.now(UTC),
        collection_ticker="COLLECTION",
        legs=(
            RFQLeg("LEG-1", "EVENT-1", "yes"),
            RFQLeg("LEG-2", "EVENT-2", "yes"),
        ),
    )
    plan = price_lead_style_rfq(
        combo_request,
        replace(fair(probability="0.40"), ticker="COMBO"),
        price_grid=PriceGrid.uniform("0.001"),
        leg_fairs=(fair(ticker="LEG-1"), fair(ticker="LEG-2")),
    )

    assert plan.yes_bid == Decimal("0")
    assert plan.no_bid == Decimal("0.585")
    assert Decimal("0.60") - plan.no_bid == Decimal("0.015")


def test_lead_style_quote_uses_tenth_cent_grid_on_finer_kalshi_market() -> None:
    plan = price_lead_style_rfq(
        request(),
        fair(probability="0.3714"),
        price_grid=PriceGrid.uniform("0.0001"),
    )

    assert plan.no_bid == Decimal("0.622")
    assert plan.no_bid % Decimal("0.001") == 0


def test_lead_style_margin_shrinks_to_half_boundary_cushion() -> None:
    plan = price_lead_style_rfq(
        request(),
        fair(probability="0.010"),
        price_grid=PriceGrid.uniform("0.001"),
    )

    assert plan.no_bid == Decimal("0.985")
    assert Decimal("0.990") - plan.no_bid == Decimal("0.005")


def test_lead_style_rejects_fair_below_premium_adjusted_cushion() -> None:
    combo_request = RFQRequest.from_message(combo_message())

    with pytest.raises(ValueError, match="cushion"):
        price_lead_style_rfq(
            combo_request,
            replace(fair(probability="0.015"), ticker="COMBO"),
            price_grid=PriceGrid.uniform("0.001"),
            leg_fairs=(fair(ticker="LEG-1"), fair(ticker="LEG-2")),
        )


def test_three_quarter_percent_edge_is_applied_independently_to_both_sides() -> None:
    plan = price_moneyline_rfq(
        request(),
        fair(probability="0.3718866002483975055749779624"),
        price_grid=PriceGrid.uniform("0.001"),
        edge_rate=Decimal("0.0075"),
    )

    assert plan.yes_bid == Decimal("0.369")
    assert plan.no_bid == Decimal("0.623")
    assert plan.yes_edge_rate is not None and plan.yes_edge_rate >= Decimal("0.0075")
    assert plan.no_edge_rate is not None and plan.no_edge_rate >= Decimal("0.0075")
    assert plan.yes_bid + plan.no_bid == Decimal("0.992")


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


def test_maker_fee_is_reserved_and_price_keeps_two_percent_net_edge() -> None:
    plan = price_moneyline_rfq(
        request(),
        fair(probability="0.30"),
        price_grid=PriceGrid.uniform("0.001"),
        edge_rate=Decimal("0.02"),
        maker_fee_multiplier=Decimal("1"),
    )

    assert estimated_maker_fee(Decimal("0.290"), Decimal("1")) == Decimal("0.0037")
    assert plan.yes_bid == Decimal("0.290")
    assert plan.yes_gross_edge_rate == Decimal("0.01") / Decimal("0.30")
    assert plan.yes_estimated_fee == Decimal("0.0037")
    assert plan.yes_edge_rate == Decimal("0.021")
    assert plan.maximum_cost >= plan.yes_bid + plan.yes_estimated_fee


def test_notional_cap_includes_modeled_maker_fee() -> None:
    plan = price_moneyline_rfq(
        request(),
        fair(probability="0.30"),
        price_grid=PriceGrid.uniform("0.001"),
        edge_rate=Decimal("0.02"),
        maker_fee_multiplier=Decimal("1"),
    )
    ledger = RFQRiskLedger(
        RFQMakerConfig(max_notional=Decimal("0.293")),
        available_balance=Decimal("100"),
    )

    with pytest.raises(ValueError, match="no side"):
        ledger.constrain(plan)


def test_per_leg_outcome_cap_aggregates_across_distinct_combos() -> None:
    first_request = RFQRequest(
        rfq_id="first",
        ticker="COMBO-1",
        contracts=Decimal("1"),
        created_at=datetime.now(UTC),
        collection_ticker="COLLECTION",
        legs=(
            RFQLeg("SHARED", "EVENT-1", "yes"),
            RFQLeg("OTHER-1", "EVENT-2", "yes"),
        ),
    )
    first = price_lead_style_rfq(
        first_request,
        replace(fair(probability="0.40"), ticker="COMBO-1"),
        price_grid=PriceGrid.uniform("0.001"),
        leg_fairs=(fair(ticker="SHARED"), fair(ticker="OTHER-1")),
    )
    second_request = replace(
        first_request,
        rfq_id="second",
        ticker="COMBO-2",
        legs=(
            RFQLeg("SHARED", "EVENT-1", "yes"),
            RFQLeg("OTHER-2", "EVENT-3", "yes"),
        ),
    )
    second = replace(first, request=second_request)
    ledger = RFQRiskLedger(
        RFQMakerConfig(
            pricing_mode="lead_fixed",
            per_leg_outcome_notional_cap=Decimal("1"),
        ),
        available_balance=Decimal("10"),
    )

    ledger.reserve(ledger.constrain(first), "quote-1")
    with pytest.raises(ValueError, match="per-leg-outcome"):
        ledger.constrain(second)


def test_per_combo_cap_is_rechecked_at_accepted_size() -> None:
    combo_request = RFQRequest(
        rfq_id="first",
        ticker="COMBO",
        contracts=Decimal("1"),
        created_at=datetime.now(UTC),
        collection_ticker="COLLECTION",
        legs=(
            RFQLeg("LEG-1", "EVENT-1", "yes"),
            RFQLeg("LEG-2", "EVENT-2", "yes"),
        ),
    )
    plan = price_lead_style_rfq(
        combo_request,
        replace(fair(probability="0.40"), ticker="COMBO"),
        price_grid=PriceGrid.uniform("0.001"),
        leg_fairs=(fair(ticker="LEG-1"), fair(ticker="LEG-2")),
    )
    ledger = RFQRiskLedger(
        RFQMakerConfig(
            pricing_mode="lead_fixed",
            per_combo_notional_cap=Decimal("0.59"),
        ),
        available_balance=Decimal("10"),
    )
    constrained = ledger.constrain(plan)
    ledger.reserve(constrained, "quote-1")
    reservation = ledger.reservations["first"]

    ledger.mark_acceptance(reservation, side="no", contracts=Decimal("1"))
    assert reservation.accepted_side == "no"


def test_fee_free_series_keeps_proportional_quote_at_fair_times_one_minus_edge() -> None:
    plan = price_moneyline_rfq(
        request(),
        fair(probability="0.30"),
        price_grid=PriceGrid.uniform("0.001"),
        edge_rate=Decimal("0.02"),
        maker_fee_multiplier=Decimal("0"),
    )

    assert plan.yes_bid == Decimal("0.294")
    assert plan.yes_estimated_fee == Decimal("0")
    assert plan.yes_edge_rate == Decimal("0.02")


def test_missing_market_series_is_derived_and_verified_from_market_ticker() -> None:
    maker = RFQMaker(
        client=FakeClient(),  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(),
        audit_log=RecordingAudit(),
        execute=False,
    )

    multiplier = asyncio.run(
        maker._maker_fee_multiplier({}, market_ticker="TEST-SERIES-MARKET")
    )

    assert multiplier == Decimal("0")


def test_parallel_market_setup_singleflights_series_fee_metadata() -> None:
    client = FakeClient()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(),
        audit_log=RecordingAudit(),
        execute=False,
    )

    async def scenario() -> tuple[Decimal, Decimal]:
        first, second = await asyncio.gather(
            maker._maker_fee_multiplier(
                {"series_ticker": "TEST-SERIES"},
                market_ticker="MARKET-1",
            ),
            maker._maker_fee_multiplier(
                {"series_ticker": "TEST-SERIES"},
                market_ticker="MARKET-2",
            ),
        )
        return first, second

    assert asyncio.run(scenario()) == (Decimal("0"), Decimal("0"))
    assert client.series_detail_requests == 1


def test_rfq_edge_has_a_hard_three_quarter_percent_floor() -> None:
    with pytest.raises(ValueError, match=r"\[0.75%, 100%\)"):
        price_moneyline_rfq(
            request(),
            fair(),
            price_grid=PriceGrid.uniform(),
            edge_rate=Decimal("0.00749"),
        )

    with pytest.raises(ValueError, match=r"\[0.75%, 100%\)"):
        RFQMakerConfig(edge_rate=Decimal("0.005")).validate()

    RFQMakerConfig(edge_rate=Decimal("0.0075")).validate()
    plan = price_moneyline_rfq(
        request(),
        fair(),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.0075"),
    )
    assert plan.yes_edge_rate is not None and plan.yes_edge_rate >= Decimal("0.0075")
    assert plan.no_edge_rate is not None and plan.no_edge_rate >= Decimal("0.0075")


def test_unaccepted_quote_lifetime_must_be_finite_and_positive() -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        RFQMakerConfig(max_unaccepted_quote_age_seconds=0).validate()
    with pytest.raises(ValueError, match="finite and positive"):
        RFQMakerConfig(max_unaccepted_quote_age_seconds=float("inf")).validate()


def test_first_fill_wins_requires_exactly_one_session_execution() -> None:
    with pytest.raises(ValueError, match="exactly one session execution"):
        RFQMakerConfig(first_fill_wins=True).validate()
    with pytest.raises(ValueError, match="exactly one session execution"):
        RFQMakerConfig(first_fill_wins=True, max_session_executions=2).validate()

    RFQMakerConfig(first_fill_wins=True, max_session_executions=1).validate()


def test_coverage_shadow_cannot_submit_quotes() -> None:
    with pytest.raises(ValueError, match="coverage shadow cannot submit"):
        RFQMaker(
            client=FakeClient(),  # type: ignore[arg-type]
            stream=EmptyStream(),
            fair_book=FakeFairBook(fair()),
            config=RFQMakerConfig(coverage_shadow=True),
            audit_log=RecordingAudit(),
            execute=True,
        )


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


def test_session_contract_cap_allows_only_one_total_fill_across_markets() -> None:
    first_plan = price_moneyline_rfq(
        request(),
        fair(),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )
    ledger = RFQRiskLedger(
        RFQMakerConfig(max_session_contracts=Decimal("1")),
        available_balance=Decimal("100"),
    )
    ledger.reserve(ledger.constrain(first_plan), "quote-1")
    reservation = ledger.release("rfq-1")
    assert reservation is not None
    reservation.accepted_side = "yes"
    reservation.accepted_contracts = Decimal("1")
    ledger.record_execution(reservation)

    second_request = replace(request(), rfq_id="rfq-2", ticker="MARKET-2")
    second_plan = price_moneyline_rfq(
        second_request,
        fair(ticker="MARKET-2"),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )

    with pytest.raises(ValueError, match="no side"):
        ledger.constrain(second_plan)


def test_session_notional_cap_aggregates_all_active_quote_reservations() -> None:
    ledger = RFQRiskLedger(
        RFQMakerConfig(
            max_active_quotes=2,
            max_session_notional=Decimal("1"),
        ),
        available_balance=Decimal("100"),
    )
    first = price_moneyline_rfq(
        request(),
        fair(),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )
    second = price_moneyline_rfq(
        replace(request(), rfq_id="rfq-2", ticker="MARKET-2"),
        fair(ticker="MARKET-2"),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )
    ledger.reserve(ledger.constrain(first), "quote-1")

    with pytest.raises(ValueError, match="session notional limit"):
        ledger.constrain(second)


def test_first_fill_wins_shares_one_risk_envelope_across_quotes() -> None:
    ledger = RFQRiskLedger(
        RFQMakerConfig(
            first_fill_wins=True,
            max_active_quotes=20,
            max_session_executions=1,
            max_session_contracts=Decimal("1"),
            max_session_notional=Decimal("0.60"),
        ),
        available_balance=Decimal("0.60"),
    )
    first = price_moneyline_rfq(
        request(), fair(), price_grid=PriceGrid.uniform(), edge_rate=Decimal("0.02")
    )
    second = price_moneyline_rfq(
        replace(request(), rfq_id="rfq-2", ticker="MARKET-2"),
        fair(ticker="MARKET-2"),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )

    ledger.reserve(ledger.constrain(first), "quote-1")
    ledger.reserve(ledger.constrain(second), "quote-2")

    assert len(ledger.reservations) == 2
    assert ledger._reserved_balance() == Decimal("0.53")


def test_session_notional_cap_includes_prior_execution_cost_and_fee() -> None:
    ledger = RFQRiskLedger(
        RFQMakerConfig(
            max_session_notional=Decimal("1"),
            max_session_executions=2,
        ),
        available_balance=Decimal("100"),
    )
    first = price_moneyline_rfq(
        request(),
        fair(),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )
    ledger.reserve(ledger.constrain(first), "quote-1")
    reservation = ledger.release("rfq-1")
    assert reservation is not None
    reservation.accepted_side = "yes"
    reservation.accepted_contracts = Decimal("1")
    ledger.record_execution(reservation)
    second = price_moneyline_rfq(
        replace(request(), rfq_id="rfq-2", ticker="MARKET-2"),
        fair(ticker="MARKET-2"),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )

    with pytest.raises(ValueError, match="session notional limit"):
        ledger.constrain(second)
    assert ledger.executed_notional == Decimal("0.53")


def test_session_execution_cap_stops_after_one_fill_below_contract_cap() -> None:
    first_plan = price_moneyline_rfq(
        request(),
        fair(),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )
    ledger = RFQRiskLedger(
        RFQMakerConfig(
            max_session_contracts=Decimal("10"),
            max_session_executions=1,
        ),
        available_balance=Decimal("100"),
    )
    ledger.reserve(ledger.constrain(first_plan), "quote-1")
    reservation = ledger.release("rfq-1")
    assert reservation is not None
    reservation.accepted_side = "yes"
    reservation.accepted_contracts = Decimal("1")
    ledger.record_execution(reservation)

    second_request = replace(request(), rfq_id="rfq-2", ticker="MARKET-2")
    second_plan = price_moneyline_rfq(
        second_request,
        fair(ticker="MARKET-2"),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )

    with pytest.raises(ValueError, match="session execution limit"):
        ledger.constrain(second_plan)


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


def test_odds_fair_book_keeps_both_moneyline_markets_for_combo_legs(monkeypatch) -> None:
    now = datetime.now(UTC)
    kalshi_event = {
        "event_ticker": "GAME",
        "markets": [
            {"ticker": "GAME-B", "yes_sub_title": "Team B"},
            {"ticker": "GAME-A", "yes_sub_title": "Team A"},
        ],
    }
    odds_event = SimpleNamespace(
        commence_time=now + timedelta(hours=1),
        home_team="Team A",
        away_team="Team B",
    )
    match = SimpleNamespace(kalshi_event=kalshi_event, odds_event=odds_event)

    class FakeKalshiEvents:
        def get_events(self, **_kwargs) -> list[dict]:
            return [kalshi_event]

    class FakeOddsEvents:
        def get_odds(self, *_args, **_kwargs) -> list[object]:
            return [odds_event]

    monkeypatch.setattr("kalshi_mm.rfq.match_events", lambda *_args, **_kwargs: [match])
    monkeypatch.setattr(OddsMoneylineFairBook, "_is_two_way", staticmethod(lambda _event: True))
    monkeypatch.setattr(
        OddsMoneylineFairBook,
        "_oldest_selected_update",
        staticmethod(lambda _event, _selected: now),
    )
    monkeypatch.setattr(
        "kalshi_mm.rfq.consensus_probability",
        lambda _event, outcome, **_kwargs: ConsensusPrice(
            fair_probability=Decimal("0.55") if outcome == "Team A" else Decimal("0.45"),
            bookmaker_count=2,
            minimum=Decimal("0.54"),
            maximum=Decimal("0.56"),
            bookmaker_keys=("book-a", "book-b"),
        ),
    )
    book = OddsMoneylineFairBook(
        kalshi=FakeKalshiEvents(),  # type: ignore[arg-type]
        odds=FakeOddsEvents(),  # type: ignore[arg-type]
        series_ticker="SERIES",
        sport="sport",
    )

    assert book.refresh() == ("GAME-A", "GAME-B")
    assert book.get("GAME-A") is not None
    assert book.get("GAME-B") is not None
    assert book.get("GAME-A").participants == frozenset({"team a", "team b"})


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


class MultiFairBook:
    refresh_seconds = 60.0

    def __init__(self, values: dict[str, MoneylineFair]) -> None:
        self.values = values

    def refresh(self) -> tuple[str, ...]:
        return self.tickers()

    def get(self, ticker: str) -> MoneylineFair | None:
        return self.values.get(ticker)

    def tickers(self) -> tuple[str, ...]:
        return tuple(sorted(self.values))


def test_composite_fair_book_merges_independent_lanes() -> None:
    now = datetime.now(UTC)
    first = MultiFairBook(
        {
            "MLB": MoneylineFair(
                ticker="MLB",
                probability=Decimal("0.55"),
                observed_at=now,
                event_start=now + timedelta(hours=1),
                source="odds-consensus:pinnacle",
            )
        }
    )
    second = MultiFairBook(
        {
            "WNBA": MoneylineFair(
                ticker="WNBA",
                probability=Decimal("0.52"),
                observed_at=now,
                event_start=now + timedelta(hours=2),
                source="odds-consensus:pinnacle",
            )
        }
    )

    book = CompositeMoneylineFairBook((first, second))

    assert book.refresh() == ("MLB", "WNBA")
    assert book.get("MLB") is first.get("MLB")
    assert book.get("WNBA") is second.get("WNBA")


class EmptyStream:
    async def events(self):
        if False:
            yield {}


class OneMessageStream:
    async def events(self):
        yield created_message()


class UnsupportedBurstStream:
    async def events(self):
        for index in range(2_500):
            yield {
                "type": "rfq_created",
                "msg": {
                    "id": f"combo-{index}",
                    "market_ticker": f"COMBO-{index}",
                    "contracts_fp": "1.00",
                    "created_ts": datetime.now(UTC).isoformat(),
                    "mve_collection_ticker": "COMBO-COLLECTION",
                    "mve_selected_legs": [{"market_ticker": "LEG-1"}],
                },
            }
            yield {"type": "rfq_deleted", "msg": {"id": f"combo-{index}"}}


class EligibleBurstStream:
    async def events(self):
        for index in range(100):
            message = created_message()
            message["msg"]["id"] = f"rfq-{index}"
            yield message


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
        self.market_events: dict[str, str] = {"MARKET": "EVENT-MARKET"}
        self.event_markets: dict[str, tuple[str, ...]] = {"EVENT-MARKET": ("MARKET",)}
        self.positions: dict[str, str] = {}
        self.combo_legs: dict[str, list[dict[str, str]]] = {}
        self.collections: dict[str, str] = {}
        self.fills: list[dict] = []
        self.portfolio_positions: list[dict] = []
        self.retrieved_quotes: list[str] = []
        self.series_detail_requests = 0
        self.rfq_creators: dict[str, str] = {}

    def get_market(self, ticker: str) -> dict:
        market = {
            "ticker": ticker,
            "series_ticker": "TEST-SERIES",
            "event_ticker": self.market_events[ticker],
            "status": "active",
            "price_ranges": [{"start": "0", "end": "1", "step": "0.01"}],
        }
        if ticker in self.combo_legs:
            market["mve_selected_legs"] = self.combo_legs[ticker]
            market["mve_collection_ticker"] = self.collections[ticker]
        return market

    def get_series_details(self, ticker: str) -> dict:
        self.series_detail_requests += 1
        return {"ticker": ticker, "fee_type": "quadratic", "fee_multiplier": 1}

    def get_event(self, event_ticker: str) -> dict:
        return {
            "event_ticker": event_ticker,
            "markets": [{"ticker": ticker} for ticker in self.event_markets[event_ticker]],
        }

    def get_position(self, ticker: str, *, subaccount: int = 0) -> str:
        return self.positions.get(ticker, "0")

    def get_balance(self, *, subaccount: int = 0) -> dict:
        return {"balance": 10_000}

    def get_fills(self, **_kwargs) -> list[dict]:
        return self.fills

    def get_positions(self, *, subaccount: int, limit: int) -> list[dict]:
        assert limit == 1000
        return self.portfolio_positions

    def create_rfq_quote(self, **kwargs) -> str:
        self.created.append(kwargs)
        return "quote-1"

    def get_rfq_quotes(self, **kwargs) -> list[dict]:
        return self.existing_quotes

    def get_rfq(self, rfq_id: str) -> dict:
        return {"id": rfq_id, "creator_id": self.rfq_creators.get(rfq_id, "creator")}

    def get_rfq_quote(self, quote_id: str) -> dict:
        self.retrieved_quotes.append(quote_id)
        return {
            "id": quote_id,
            "rfq_id": "rfq-1",
            "accepted_side": "yes",
            "contracts_fp": "1.00",
            "status": "executed",
        }

    def delete_rfq_quote(self, rfq_id: str, quote_id: str) -> None:
        self.deleted.append((rfq_id, quote_id))

    def confirm_rfq_quote(self, rfq_id: str, quote_id: str) -> None:
        self.confirmed.append((rfq_id, quote_id))


def test_creator_burst_limit_blocks_third_rfq_and_session() -> None:
    client = FakeClient()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(creator_rate_limit=True),
        audit_log=RecordingAudit(),
        execute=False,
    )

    async def scenario() -> None:
        for index in range(2):
            request = replace(RFQRequest.from_message(created_message()), rfq_id=f"rfq-{index}")
            enriched = await maker._enforce_creator_rate(request)
            assert enriched.creator_id == "creator"
        third = replace(RFQRequest.from_message(created_message()), rfq_id="rfq-2")
        with pytest.raises(ValueError, match="burst-posting"):
            await maker._enforce_creator_rate(third)
        fourth = replace(RFQRequest.from_message(created_message()), rfq_id="rfq-3")
        with pytest.raises(ValueError, match="blocked"):
            await maker._enforce_creator_rate(fourth)

    asyncio.run(scenario())


class RejectedQuoteClient(FakeClient):
    def create_rfq_quote(self, **kwargs) -> str:
        self.created.append(kwargs)
        raise KalshiAPIError(400, "POST", "/communications/quotes", "invalid quote")


class RejectedDeleteClient(FakeClient):
    def delete_rfq_quote(self, rfq_id: str, quote_id: str) -> None:
        self.deleted.append((rfq_id, quote_id))
        raise KalshiAPIError(409, "DELETE", "/communications/quotes", "quote accepted")


class MissingDeleteClient(FakeClient):
    def delete_rfq_quote(self, rfq_id: str, quote_id: str) -> None:
        self.deleted.append((rfq_id, quote_id))
        raise KalshiAPIError(404, "DELETE", "/communications/quotes", "not found")

    def get_rfq_quote(self, quote_id: str) -> dict:
        raise KalshiAPIError(404, "GET", f"/communications/quotes/{quote_id}", "not found")


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


def combo_message(*, target_cost: str | None = None) -> dict:
    message = created_message()
    message["msg"].update(
        {
            "market_ticker": "COMBO",
            "mve_collection_ticker": "COMBO-COLLECTION",
            "mve_selected_legs": [
                {"market_ticker": "LEG-1", "event_ticker": "EVENT-1", "side": "yes"},
                {"market_ticker": "LEG-2", "event_ticker": "EVENT-2", "side": "no"},
            ],
        }
    )
    if target_cost is not None:
        message["msg"]["contracts_fp"] = None
        message["msg"]["target_cost_dollars"] = target_cost
    return message


def combo_fairs() -> dict[str, MoneylineFair]:
    return {
        "LEG-1": fair(
            ticker="LEG-1",
            probability="0.60",
            event_ticker="EVENT-1",
            participants=("A", "B"),
        ),
        "LEG-2": fair(
            ticker="LEG-2",
            probability="0.25",
            event_ticker="EVENT-2",
            participants=("C", "D"),
        ),
    }


def configure_combo(client: FakeClient) -> None:
    legs = combo_message()["msg"]["mve_selected_legs"]
    client.market_events.update({"LEG-1": "EVENT-1", "LEG-2": "EVENT-2", "COMBO": "MVE-EVENT"})
    client.event_markets.update({"EVENT-1": ("LEG-1",), "EVENT-2": ("LEG-2",)})
    client.combo_legs["COMBO"] = legs
    client.collections["COMBO"] = "COMBO-COLLECTION"


def test_combo_rfq_parses_variable_contract_size_and_legs() -> None:
    request = RFQRequest.from_message(combo_message())

    assert request.contracts == Decimal("1.00")
    assert request.is_combo
    assert request.collection_ticker == "COMBO-COLLECTION"
    assert request.legs[1] == RFQLeg("LEG-2", "EVENT-2", "no")


def test_target_cost_rfq_reserves_conservative_complement_price_upper_bounds() -> None:
    request = RFQRequest.from_message(combo_message(target_cost="1.00"))
    aggregate = replace(fair(probability="0.45"), ticker="COMBO")
    plan = price_moneyline_rfq(
        request,
        aggregate,
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )

    assert request.contracts is None
    assert request.target_cost == Decimal("1.00")
    assert plan.yes_contracts == Decimal("1.79")
    assert plan.no_contracts == Decimal("2.13")
    assert plan.maximum_cost >= Decimal("1.00")


def test_live_target_cost_attempt_would_reserve_above_exchange_no_count() -> None:
    target_request = RFQRequest(
        rfq_id="live-regression",
        ticker="COMBO",
        contracts=None,
        target_cost=Decimal("2.34"),
        created_at=datetime.now(UTC),
    )
    plan = price_moneyline_rfq(
        target_request,
        replace(
            fair(probability="0.3718866002483975055749779624"),
            ticker="COMBO",
        ),
        price_grid=PriceGrid.uniform("0.001"),
        edge_rate=Decimal("0.0075"),
    )
    ledger = RFQRiskLedger(
        RFQMakerConfig(
            edge_rate=Decimal("0.0075"),
            max_target_cost=Decimal("10"),
            max_contracts=Decimal("10"),
            max_notional=Decimal("10"),
            max_session_notional=Decimal("10"),
            max_session_contracts=Decimal("10"),
        ),
        available_balance=Decimal("10"),
    )

    constrained = ledger.constrain(plan)

    assert constrained.yes_bid == Decimal("0.369")
    assert constrained.no_bid == Decimal("0.623")
    assert constrained.yes_contracts == Decimal("3.71")
    assert constrained.no_contracts == Decimal("6.21")
    assert Decimal("5.94") <= constrained.no_contracts
    assert constrained.maximum_cost == Decimal("3.86883")


def test_prepare_allows_sibling_moneyline_fairs_but_never_combines_them() -> None:
    client = FakeClient()
    client.market_events["MARKET-2"] = "EVENT-MARKET"
    client.event_markets["EVENT-MARKET"] = ("MARKET", "MARKET-2")
    fair_two = fair(ticker="MARKET-2", probability="0.45")
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=MultiFairBook({"MARKET": fair(), "MARKET-2": fair_two}),
        config=RFQMakerConfig(),
        audit_log=RecordingAudit(),
        execute=False,
    )

    asyncio.run(maker.prepare())

    bad_request = RFQRequest(
        "rfq",
        "COMBO",
        Decimal("1"),
        datetime.now(UTC),
        collection_ticker="COLLECTION",
        legs=(
            RFQLeg("MARKET", "EVENT-MARKET", "yes"),
            RFQLeg("MARKET-2", "EVENT-MARKET", "yes"),
        ),
    )
    with pytest.raises(ValueError, match="same-game"):
        maker._request_fair(bad_request)


def test_prepare_rejects_existing_position_in_same_event_sibling() -> None:
    client = FakeClient()
    client.event_markets["EVENT-MARKET"] = ("MARKET", "SAME-GAME-PROP")
    client.positions["SAME-GAME-PROP"] = "1.00"
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(),
        audit_log=RecordingAudit(),
        execute=False,
    )

    with pytest.raises(ValueError, match="correlated same-event market"):
        asyncio.run(maker.prepare())


def test_parlay_fair_is_product_of_selected_leg_probabilities() -> None:
    client = FakeClient()
    configure_combo(client)
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=MultiFairBook(combo_fairs()),
        config=RFQMakerConfig(),
        audit_log=RecordingAudit(),
        execute=False,
        allowed_collections={"COMBO-COLLECTION"},
    )
    request = RFQRequest.from_message(combo_message())

    aggregate, legs = maker._request_fair(request)

    assert aggregate.probability == Decimal("0.45")
    assert tuple(item.ticker for item in legs) == ("LEG-1", "LEG-2")


def test_parlay_rejects_shared_participant_across_distinct_events() -> None:
    values = combo_fairs()
    values["LEG-2"] = replace(values["LEG-2"], participants=frozenset({"B", "C"}))
    maker = RFQMaker(
        client=FakeClient(),  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=MultiFairBook(values),
        config=RFQMakerConfig(),
        audit_log=RecordingAudit(),
        execute=False,
    )

    with pytest.raises(ValueError, match="share a participant"):
        maker._request_fair(RFQRequest.from_message(combo_message()))


def lead_style_maker(values: dict[str, MoneylineFair]) -> RFQMaker:
    return RFQMaker(
        client=FakeClient(),  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=MultiFairBook(values),
        config=RFQMakerConfig(
            pricing_mode="lead_fixed",
            no_side_only=True,
            pinnacle_only=True,
            max_hours_to_start=12,
            moneyline_yes_only=True,
            max_moneyline_fair=Decimal("5") / Decimal("7"),
        ),
        audit_log=RecordingAudit(),
        execute=False,
        allowed_collections={"COMBO-COLLECTION"},
    )


def test_lead_style_requires_yes_moneyline_legs() -> None:
    values = {
        key: replace(value, source="odds-consensus:pinnacle")
        for key, value in combo_fairs().items()
    }

    with pytest.raises(ValueError, match="YES selections"):
        lead_style_maker(values)._request_fair(
            RFQRequest.from_message(combo_message())
        )


def test_lead_style_rejects_moneyline_favorite_steeper_than_minus_250() -> None:
    message = combo_message()
    message["msg"]["mve_selected_legs"][1]["side"] = "yes"
    values = combo_fairs()
    values["LEG-1"] = replace(
        values["LEG-1"],
        probability=Decimal("0.72"),
        source="odds-consensus:pinnacle",
    )
    values["LEG-2"] = replace(values["LEG-2"], source="odds-consensus:pinnacle")

    with pytest.raises(ValueError, match="steeper"):
        lead_style_maker(values)._request_fair(RFQRequest.from_message(message))


def test_lead_style_rejects_event_more_than_twelve_hours_away() -> None:
    message = combo_message()
    message["msg"]["mve_selected_legs"][1]["side"] = "yes"
    values = {
        key: replace(value, source="odds-consensus:pinnacle")
        for key, value in combo_fairs().items()
    }
    values["LEG-2"] = replace(
        values["LEG-2"],
        event_start=datetime.now(UTC) + timedelta(hours=13),
    )

    with pytest.raises(ValueError, match="pregame horizon"):
        lead_style_maker(values)._request_fair(RFQRequest.from_message(message))


def test_lead_style_rejects_non_pinnacle_fair() -> None:
    values = combo_fairs()

    with pytest.raises(ValueError, match="Pinnacle"):
        lead_style_maker(values)._request_fair(
            RFQRequest.from_message(combo_message())
        )


def test_target_cost_risk_can_disable_only_oversized_side() -> None:
    request = RFQRequest.from_message(combo_message(target_cost="1.00"))
    plan = price_moneyline_rfq(
        request,
        replace(fair(probability="0.45"), ticker="COMBO"),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )
    ledger = RFQRiskLedger(
        RFQMakerConfig(max_contracts=Decimal("2")),
        available_balance=Decimal("100"),
    )

    constrained = ledger.constrain(plan)

    assert constrained.yes_bid == Decimal("0.44")
    assert constrained.no_bid == Decimal("0")


def test_target_cost_risk_rejects_request_above_explicit_dollar_cap() -> None:
    request = RFQRequest.from_message(combo_message(target_cost="10.01"))
    plan = price_moneyline_rfq(
        request,
        replace(fair(probability="0.45"), ticker="COMBO"),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )
    ledger = RFQRiskLedger(
        RFQMakerConfig(max_target_cost=Decimal("10")),
        available_balance=Decimal("100"),
    )

    with pytest.raises(ValueError, match="target cost exceeds"):
        ledger.constrain(plan)


def test_contracts_only_risk_profile_rejects_target_cost_rfq() -> None:
    request = RFQRequest.from_message(combo_message(target_cost="1.00"))
    plan = price_moneyline_rfq(
        request,
        replace(fair(probability="0.45"), ticker="COMBO"),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )
    ledger = RFQRiskLedger(
        RFQMakerConfig(contracts_only=True),
        available_balance=Decimal("100"),
    )

    with pytest.raises(ValueError, match="target-cost RFQs are disabled"):
        ledger.constrain(plan)


def test_session_execution_cap_blocks_second_active_quote_before_confirmation() -> None:
    config = RFQMakerConfig(max_active_quotes=2, max_session_executions=1)
    ledger = RFQRiskLedger(config, available_balance=Decimal("100"))
    first = price_moneyline_rfq(
        request(),
        fair(),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )
    second = price_moneyline_rfq(
        replace(request(), rfq_id="rfq-2", ticker="MARKET-2"),
        fair(
            ticker="MARKET-2",
            event_ticker="EVENT-2",
            participants=("Team C", "Team D"),
        ),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )
    ledger.reserve(ledger.constrain(first), "quote-1")
    ledger.reserve(ledger.constrain(second), "quote-2")
    first_reservation = ledger.reservations["rfq-1"]
    second_reservation = ledger.reservations["rfq-2"]

    ledger.mark_acceptance(first_reservation, side="yes", contracts=Decimal("1"))

    with pytest.raises(ValueError, match="execution limit reached before confirmation"):
        ledger.mark_acceptance(second_reservation, side="no", contracts=Decimal("1"))
    assert second_reservation.accepted_side is None


def test_active_quotes_must_have_disjoint_participants_across_events() -> None:
    maker = RFQMaker(
        client=FakeClient(),  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(max_active_quotes=2),
        audit_log=RecordingAudit(),
        execute=False,
    )
    first = price_moneyline_rfq(
        request(),
        fair(event_ticker="EVENT-1", participants=("Shared Team", "Team B")),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )
    second = price_moneyline_rfq(
        replace(request(), rfq_id="rfq-2", ticker="MARKET-2"),
        fair(
            ticker="MARKET-2",
            event_ticker="EVENT-2",
            participants=("Shared Team", "Team C"),
        ),
        price_grid=PriceGrid.uniform(),
        edge_rate=Decimal("0.02"),
    )
    maker.event_tickers.update({"MARKET": "EVENT-1", "MARKET-2": "EVENT-2"})
    maker.ledger.available_balance = Decimal("100")
    maker.ledger.reserve(maker.ledger.constrain(first), "quote-1")

    with pytest.raises(ValueError, match="overlaps a participant"):
        maker._ensure_no_overlapping_reservation(second)


def test_first_fill_wins_allows_overlapping_displayed_quotes() -> None:
    maker = RFQMaker(
        client=FakeClient(),  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(first_fill_wins=True, max_session_executions=1),
        audit_log=RecordingAudit(),
        execute=False,
    )
    plan = price_moneyline_rfq(
        request(), fair(), price_grid=PriceGrid.uniform(), edge_rate=Decimal("0.02")
    )
    maker.ledger.available_balance = Decimal("100")
    maker.ledger.reserve(maker.ledger.constrain(plan), "quote-1")

    second = replace(plan, request=replace(request(), rfq_id="rfq-2"))
    maker._ensure_no_overlapping_reservation(second)


def test_execute_combo_quotes_full_parlay_and_reprices_before_confirmation() -> None:
    client = FakeClient()
    configure_combo(client)
    audit = RecordingAudit()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=MultiFairBook(combo_fairs()),
        config=RFQMakerConfig(),
        audit_log=audit,
        execute=True,
        allowed_collections={"COMBO-COLLECTION"},
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(combo_message())
        accepted = accepted_message()
        accepted["msg"]["contracts_accepted_fp"] = "1.00"
        await maker.handle(accepted)

    asyncio.run(scenario())

    assert client.created[0]["yes_bid"] == "0.4400"
    assert client.created[0]["no_bid"] == "0.5400"
    assert client.confirmed == [("rfq-1", "quote-1")]
    submitted = [payload for event, payload in audit.records if event == "rfq_quote_submitted"]
    assert submitted[0]["fair_probability"] == "0.4500"
    assert submitted[0]["collection_ticker"] == "COMBO-COLLECTION"


def test_continuation_rejects_new_combo_overlapping_existing_position() -> None:
    client = FakeClient()
    configure_combo(client)
    client.portfolio_positions = [{"ticker": "COMBO", "position_fp": "-1.00"}]
    client.market_events.update({"LEG-3": "EVENT-3", "COMBO-2": "MVE-EVENT-2"})
    client.event_markets["EVENT-3"] = ("LEG-3",)
    client.combo_legs["COMBO-2"] = [
        {"market_ticker": "LEG-1", "event_ticker": "EVENT-1", "side": "yes"},
        {"market_ticker": "LEG-3", "event_ticker": "EVENT-3", "side": "yes"},
    ]
    client.collections["COMBO-2"] = "COMBO-COLLECTION"
    fairs = combo_fairs()
    fairs["LEG-3"] = fair(
        ticker="LEG-3",
        event_ticker="EVENT-3",
        participants=("E", "F"),
    )
    audit = RecordingAudit()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=MultiFairBook(fairs),
        config=RFQMakerConfig(
            allow_existing_positions=True,
            combo_only=True,
        ),
        audit_log=audit,
        execute=True,
        allowed_collections={"COMBO-COLLECTION"},
    )
    message = combo_message()
    message["msg"].update(
        {
            "id": "rfq-2",
            "market_ticker": "COMBO-2",
            "mve_selected_legs": client.combo_legs["COMBO-2"],
        }
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(message)

    asyncio.run(scenario())

    assert client.created == []
    skipped = [payload for event, payload in audit.records if event == "rfq_quote_skipped"]
    assert "existing-position event" in str(skipped[0]["reason"])
    loaded = [
        payload
        for event, payload in audit.records
        if event == "rfq_existing_position_loaded"
    ]
    assert loaded[0]["events"] == ["EVENT-1", "EVENT-2"]


def test_execute_one_sided_quote_uses_fixed_precision_for_disabled_side() -> None:
    client = FakeClient()
    configure_combo(client)
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=MultiFairBook(combo_fairs()),
        config=RFQMakerConfig(max_contracts=Decimal("2")),
        audit_log=RecordingAudit(),
        execute=True,
        allowed_collections={"COMBO-COLLECTION"},
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(combo_message(target_cost="1.00"))

    asyncio.run(scenario())

    assert client.created[0]["yes_bid"] == "0.4400"
    assert client.created[0]["no_bid"] == "0.0000"


def test_target_cost_coverage_shadow_reports_quoteable_sizing_mode() -> None:
    client = FakeClient()
    configure_combo(client)
    audit = RecordingAudit()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=MultiFairBook(combo_fairs()),
        config=RFQMakerConfig(
            max_target_cost=Decimal("10"),
            coverage_shadow=True,
        ),
        audit_log=audit,
        execute=False,
        allowed_collections={"COMBO-COLLECTION"},
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(combo_message(target_cost="1.00"))
        maker._flush_coverage_summary()

    asyncio.run(scenario())

    summaries = [payload for event, payload in audit.records if event == "rfq_coverage_summary"]
    assert summaries == [
        {
            "messages": 1,
            "sizing": {"target_cost": {"quoteable shadow": 1}},
            "execute": False,
            "coverage_shadow": True,
        }
    ]


def test_coverage_shadow_releases_simulated_quote_at_configured_ttl() -> None:
    client = FakeClient()
    configure_combo(client)
    audit = RecordingAudit()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=MultiFairBook(combo_fairs()),
        config=RFQMakerConfig(
            max_target_cost=Decimal("10"),
            max_unaccepted_quote_age_seconds=60,
            coverage_shadow=True,
        ),
        audit_log=audit,
        execute=False,
        allowed_collections={"COMBO-COLLECTION"},
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(combo_message(target_cost="1.00"))
        reservation = maker.ledger.reservations["rfq-1"]
        assert reservation.submitted_at_monotonic is not None
        await maker._expire_unaccepted_once(
            now=reservation.submitted_at_monotonic + 60,
        )

    asyncio.run(scenario())

    assert maker.ledger.reservations == {}
    assert client.deleted == []
    assert any(event == "rfq_shadow_ttl_released" for event, _ in audit.records)


def test_coverage_shadow_logs_and_releases_active_simulation_on_shutdown() -> None:
    client = FakeClient()
    configure_combo(client)
    audit = RecordingAudit()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=MultiFairBook(combo_fairs()),
        config=RFQMakerConfig(
            max_target_cost=Decimal("10"),
            coverage_shadow=True,
        ),
        audit_log=audit,
        execute=False,
        allowed_collections={"COMBO-COLLECTION"},
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(combo_message(target_cost="1.00"))
        await maker.shutdown()

    asyncio.run(scenario())

    assert maker.ledger.reservations == {}
    assert any(event == "rfq_shadow_shutdown_released" for event, _ in audit.records)


def test_quote_http_rejection_releases_reservation_as_definitive_failure() -> None:
    client = RejectedQuoteClient()
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

    asyncio.run(scenario())

    assert maker.ledger.reservations == {}
    assert any(event == "rfq_quote_skipped" for event, _ in audit.records)
    assert not any(event == "rfq_quote_ambiguous" for event, _ in audit.records)


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

    assert client.created[0]["yes_bid"] == "0.5400"
    assert client.created[0]["no_bid"] == "0.4400"
    assert client.created[0]["rest_remainder"] is False
    assert client.created[0]["post_only"] is True
    assert client.confirmed == [("rfq-1", "quote-1")]
    assert any(event == "rfq_quote_confirmed" for event, _ in audit.records)


def test_primary_canary_accepts_missing_subaccount_as_primary() -> None:
    client = FakeClient()
    audit = RecordingAudit()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(
            subaccount=0,
            require_subaccount_metadata=True,
        ),
        audit_log=audit,
        execute=True,
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(created_message())
        await maker.handle(accepted_message())

    asyncio.run(scenario())

    assert client.confirmed == [("rfq-1", "quote-1")]
    assert not any(event == "rfq_confirmation_withheld" for event, _ in audit.records)


def test_numbered_subaccount_canary_still_rejects_missing_subaccount() -> None:
    client = FakeClient()
    audit = RecordingAudit()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(
            subaccount=1,
            require_subaccount_metadata=True,
        ),
        audit_log=audit,
        execute=True,
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(created_message())
        await maker.handle(accepted_message())

    asyncio.run(scenario())

    assert client.confirmed == []
    withheld = [payload for event, payload in audit.records if event == "rfq_confirmation_withheld"]
    assert len(withheld) == 1
    assert "subaccount is missing" in str(withheld[0]["reason"])


def test_target_cost_acceptance_uses_exchange_count_within_conservative_bound() -> None:
    client = FakeClient()
    configure_combo(client)
    audit = RecordingAudit()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=MultiFairBook(combo_fairs()),
        config=RFQMakerConfig(max_target_cost=Decimal("10")),
        audit_log=audit,
        execute=True,
        allowed_collections={"COMBO-COLLECTION"},
    )
    accepted = accepted_message()
    accepted["msg"].update(
        {
            "accepted_side": "no",
            "contracts_accepted_fp": "2.05",
            "no_contracts_offered_fp": "2.05",
        }
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(combo_message(target_cost="1.00"))
        assert maker.ledger.reservations["rfq-1"].plan.no_contracts == Decimal("2.18")
        await maker.handle(accepted)

    asyncio.run(scenario())

    assert client.confirmed == [("rfq-1", "quote-1")]
    assert maker.ledger.reservations["rfq-1"].accepted_contracts == Decimal("2.05")


def test_acceptance_rejects_exchange_count_above_conservative_bound() -> None:
    client = FakeClient()
    configure_combo(client)
    audit = RecordingAudit()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=MultiFairBook(combo_fairs()),
        config=RFQMakerConfig(max_target_cost=Decimal("10")),
        audit_log=audit,
        execute=True,
        allowed_collections={"COMBO-COLLECTION"},
    )
    accepted = accepted_message()
    accepted["msg"].update(
        {
            "accepted_side": "no",
            "contracts_accepted_fp": "2.19",
            "no_contracts_offered_fp": "2.19",
        }
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(combo_message(target_cost="1.00"))
        await maker.handle(accepted)

    asyncio.run(scenario())

    assert client.confirmed == []
    withheld = [payload for event, payload in audit.records if event == "rfq_confirmation_withheld"]
    assert len(withheld) == 1
    assert "exceeds the reserved quote" in str(withheld[0]["reason"])


def test_two_disjoint_active_quotes_can_never_exceed_one_confirmation() -> None:
    class UniqueQuoteClient(FakeClient):
        def create_rfq_quote(self, **kwargs) -> str:
            self.created.append(kwargs)
            return f"quote-{len(self.created)}"

    client = UniqueQuoteClient()
    client.market_events["MARKET-2"] = "EVENT-2"
    client.event_markets["EVENT-2"] = ("MARKET-2",)
    audit = RecordingAudit()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=MultiFairBook(
            {
                "MARKET": fair(),
                "MARKET-2": fair(
                    ticker="MARKET-2",
                    event_ticker="EVENT-2",
                    participants=("Team C", "Team D"),
                ),
            }
        ),
        config=RFQMakerConfig(
            max_active_quotes=2,
            max_session_contracts=Decimal("10"),
            max_session_executions=1,
        ),
        audit_log=audit,
        execute=True,
    )
    second_created = created_message()
    second_created["msg"].update({"id": "rfq-2", "market_ticker": "MARKET-2"})
    second_accepted = accepted_message()
    second_accepted["msg"].update({"rfq_id": "rfq-2", "quote_id": "quote-2"})

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(created_message())
        await maker.handle(second_created)
        await maker.handle(accepted_message())
        await maker.handle(second_accepted)

    asyncio.run(scenario())

    assert client.confirmed == [("rfq-1", "quote-1")]
    assert maker.ledger.reservations["rfq-2"].accepted_side is None
    withheld = [payload for event, payload in audit.records if event == "rfq_confirmation_withheld"]
    assert len(withheld) == 1
    assert "execution limit reached before confirmation" in str(withheld[0]["reason"])


def test_first_fill_wins_atomically_confirms_one_and_cancels_the_rest() -> None:
    class UniqueQuoteClient(FakeClient):
        def create_rfq_quote(self, **kwargs) -> str:
            self.created.append(kwargs)
            return f"quote-{len(self.created)}"

    client = UniqueQuoteClient()
    audit = RecordingAudit()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(
            first_fill_wins=True,
            max_active_quotes=20,
            max_session_contracts=Decimal("10"),
            max_session_executions=1,
        ),
        audit_log=audit,
        execute=True,
    )
    second_created = created_message()
    second_created["msg"]["id"] = "rfq-2"
    second_accepted = accepted_message()
    second_accepted["msg"].update({"rfq_id": "rfq-2", "quote_id": "quote-2"})

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(created_message())
        await maker.handle(second_created)
        await asyncio.gather(
            maker.handle(accepted_message()),
            maker.handle(second_accepted),
        )
        await asyncio.sleep(0)
        if maker._tasks:
            await asyncio.gather(*tuple(maker._tasks))

    asyncio.run(scenario())

    assert len(client.confirmed) == 1
    assert len(client.deleted) == 1
    assert client.deleted[0][0] != client.confirmed[0][0]
    assert len([event for event, _ in audit.records if event == "rfq_quote_accepted"]) == 1
    assert len([event for event, _ in audit.records if event == "rfq_quote_confirmed"]) == 1
    withheld = [payload for event, payload in audit.records if event == "rfq_confirmation_withheld"]
    assert len(withheld) == 1
    assert "execution limit reached before confirmation" in str(withheld[0]["reason"])


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


def test_execution_recovers_missed_acceptance_before_releasing_risk() -> None:
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

    assert client.retrieved_quotes == ["quote-1"]
    assert maker.ledger.positions["MARKET"] == Decimal("1.00")
    assert maker.ledger.reservations == {}
    assert any(event == "rfq_quote_executed" for event, _ in audit.records)


def test_execution_recovery_uses_target_cost_side_count_when_generic_count_is_zero() -> None:
    class TargetCostExecutionClient(FakeClient):
        def get_rfq_quote(self, quote_id: str) -> dict:
            self.retrieved_quotes.append(quote_id)
            return {
                "id": quote_id,
                "rfq_id": "rfq-1",
                "accepted_side": "no",
                "contracts_fp": "0.00",
                "no_contracts_fp": "2.05",
                "status": "executed",
            }

    client = TargetCostExecutionClient()
    configure_combo(client)
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=MultiFairBook(combo_fairs()),
        config=RFQMakerConfig(max_target_cost=Decimal("10")),
        audit_log=RecordingAudit(),
        execute=True,
        allowed_collections={"COMBO-COLLECTION"},
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(combo_message(target_cost="1.00"))
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

    assert client.retrieved_quotes == ["quote-1"]
    assert maker.ledger.positions["COMBO"] == Decimal("-2.05")
    assert maker.ledger.executed_contracts == Decimal("2.05")


def test_executed_fill_is_written_to_markdown_ledger(tmp_path) -> None:
    client = FakeClient()
    client.fills = [{"fee_cost": "0.0010"}]
    audit = RecordingAudit()
    ledger_path = tmp_path / "RFQ_FILLS.md"
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(),
        audit_log=audit,
        execute=True,
        fill_ledger=MarkdownRFQFillLedger(ledger_path),
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(created_message())
        await maker.handle(accepted_message())
        await maker.handle(
            {
                "type": "quote_executed",
                "msg": {
                    "rfq_id": "rfq-1",
                    "quote_id": "quote-1",
                    "order_id": "order-1",
                    "executed_ts": "2026-07-24T20:00:00Z",
                },
            }
        )

    asyncio.run(scenario())

    contents = ledger_path.read_text()
    assert "2026-07-24T20:00:00Z" in contents
    assert "`MARKET` YES" in contents
    assert "1.8182%" in contents
    assert "1.6364%" in contents
    assert "$0.0010" in contents
    assert "fills_api" in contents
    assert "$0.0100" in contents
    assert "<!-- kalshi-rfq-fill:quote-1 -->" in contents
    executed = [payload for event, payload in audit.records if event == "rfq_quote_executed"]
    assert executed[0]["actual_fee"] == "0.0010"
    assert executed[0]["net_edge_rate"] == str(Decimal("0.0090") / Decimal("0.55"))


def test_executed_parlay_fill_lists_every_leg_and_full_parlay_edge(tmp_path) -> None:
    client = FakeClient()
    configure_combo(client)
    ledger_path = tmp_path / "RFQ_FILLS.md"
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=MultiFairBook(combo_fairs()),
        config=RFQMakerConfig(),
        audit_log=RecordingAudit(),
        execute=True,
        fill_ledger=MarkdownRFQFillLedger(ledger_path),
        allowed_collections={"COMBO-COLLECTION"},
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(combo_message())
        await maker.handle(accepted_message())
        await maker.handle(
            {
                "type": "quote_executed",
                "msg": {
                    "rfq_id": "rfq-1",
                    "quote_id": "quote-1",
                    "order_id": "parlay-order",
                    "executed_ts": "2026-07-24T20:00:00Z",
                },
            }
        )

    asyncio.run(scenario())

    contents = ledger_path.read_text()
    assert "Independent moneyline parlay" in contents
    assert "`LEG-1` YES; `LEG-2` NO" in contents
    assert "EVENT-1, EVENT-2" in contents
    assert "$0.4500" in contents
    assert "$0.4400" in contents
    assert "2.2222%" in contents


def test_reconciliation_records_missed_execution_once(tmp_path) -> None:
    client = FakeClient()
    ledger_path = tmp_path / "RFQ_FILLS.md"
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(),
        audit_log=RecordingAudit(),
        execute=True,
        fill_ledger=MarkdownRFQFillLedger(ledger_path),
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(created_message())
        await maker.handle(accepted_message())
        client.existing_quotes = [
            {
                "id": "quote-1",
                "rfq_id": "rfq-1",
                "status": "executed",
                "accepted_side": "yes",
                "contracts_fp": "1.00",
                "executed_ts": "2026-07-24T20:00:00Z",
                "creator_order_id": "order-1",
            }
        ]
        await maker._reconcile_once()
        await maker._reconcile_once()

    asyncio.run(scenario())

    contents = ledger_path.read_text()
    assert contents.count("<!-- kalshi-rfq-fill:quote-1 -->") == 1
    assert "order-1" in contents


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


def test_unaccepted_quote_ttl_cancels_only_after_the_deadline() -> None:
    client = FakeClient()
    audit = RecordingAudit()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(max_unaccepted_quote_age_seconds=60),
        audit_log=audit,
        execute=True,
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(created_message())
        reservation = maker.ledger.reservations["rfq-1"]
        assert reservation.submitted_at_monotonic is not None
        await maker._expire_unaccepted_once(
            now=reservation.submitted_at_monotonic + 59.999
        )
        assert "rfq-1" in maker.ledger.reservations
        await maker._expire_unaccepted_once(now=reservation.submitted_at_monotonic + 60)

    asyncio.run(scenario())

    assert client.deleted == [("rfq-1", "quote-1")]
    assert maker.ledger.reservations == {}
    cancelled = [payload for event, payload in audit.records if event == "rfq_quote_ttl_cancelled"]
    assert cancelled[0]["age_seconds"] == 60


def test_unaccepted_quote_ttl_never_cancels_an_accepted_quote() -> None:
    client = FakeClient()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(max_unaccepted_quote_age_seconds=60),
        audit_log=RecordingAudit(),
        execute=True,
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(created_message())
        reservation = maker.ledger.reservations["rfq-1"]
        assert reservation.submitted_at_monotonic is not None
        reservation.accepted_side = "yes"
        reservation.accepted_contracts = Decimal("1")
        await maker._expire_unaccepted_once(now=reservation.submitted_at_monotonic + 60)

    asyncio.run(scenario())

    assert client.deleted == []
    assert "rfq-1" in maker.ledger.reservations


def test_unaccepted_quote_ttl_retains_risk_when_delete_fails() -> None:
    client = RejectedDeleteClient()
    audit = RecordingAudit()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(max_unaccepted_quote_age_seconds=60),
        audit_log=audit,
        execute=True,
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(created_message())
        reservation = maker.ledger.reservations["rfq-1"]
        assert reservation.submitted_at_monotonic is not None
        await maker._expire_unaccepted_once(now=reservation.submitted_at_monotonic + 60)
        await maker._expire_unaccepted_once(now=reservation.submitted_at_monotonic + 61)

    asyncio.run(scenario())

    assert client.deleted == [("rfq-1", "quote-1")]
    assert "rfq-1" in maker.ledger.reservations
    failed = [
        payload for event, payload in audit.records if event == "rfq_quote_ttl_cancel_failed"
    ]
    assert failed[0]["risk_reserved"] is True


def test_unaccepted_quote_ttl_reconciles_a_missing_quote_after_404(monkeypatch) -> None:
    client = MissingDeleteClient()
    audit = RecordingAudit()
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("kalshi_mm.rfq.asyncio.sleep", record_sleep)
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(max_unaccepted_quote_age_seconds=60),
        audit_log=audit,
        execute=True,
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(created_message())
        reservation = maker.ledger.reservations["rfq-1"]
        assert reservation.submitted_at_monotonic is not None
        await maker._expire_unaccepted_once(now=reservation.submitted_at_monotonic + 60)

    asyncio.run(scenario())

    assert client.deleted == [("rfq-1", "quote-1")]
    assert maker.ledger.reservations == {}
    assert sleeps == [46.0]
    assert any(event == "rfq_quote_ttl_reconciled_absent" for event, _ in audit.records)


def test_missing_combo_quote_waits_through_hvm_execution_window(monkeypatch) -> None:
    client = MissingDeleteClient()
    configure_combo(client)
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("kalshi_mm.rfq.asyncio.sleep", record_sleep)
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=MultiFairBook(combo_fairs()),
        config=RFQMakerConfig(
            max_unaccepted_quote_age_seconds=60,
            combo_only=True,
        ),
        audit_log=RecordingAudit(),
        execute=True,
        allowed_collections={"COMBO-COLLECTION"},
    )

    async def scenario() -> None:
        await maker.prepare()
        await maker.handle(combo_message())
        reservation = maker.ledger.reservations["rfq-1"]
        assert reservation.submitted_at_monotonic is not None
        await maker._expire_unaccepted_once(now=reservation.submitted_at_monotonic + 60)

    asyncio.run(scenario())

    assert sleeps == [5.0]
    assert maker.ledger.reservations == {}


def test_run_aggregates_unsupported_rfqs_without_creating_tasks() -> None:
    client = FakeClient()
    audit = RecordingAudit()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=UnsupportedBurstStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(),
        audit_log=audit,
        execute=False,
    )

    asyncio.run(maker.run())

    summaries = [payload for event, payload in audit.records if event == "rfq_unsupported_summary"]
    assert sum(int(item["messages"]) for item in summaries) == 2_500
    assert len(summaries) == 3
    assert not any(event == "rfq_quote_skipped" for event, _ in audit.records)
    assert maker.closed_rfqs == set()
    assert maker.ledger.reservations == {}
    assert client.created == []


def test_run_bounds_inflight_rfq_handlers_under_burst_load() -> None:
    client = FakeClient()
    audit = RecordingAudit()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EligibleBurstStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(max_inflight_rfqs=1),
        audit_log=audit,
        execute=False,
    )

    asyncio.run(maker.run())

    capacity = sum(
        int(payload["reasons"].get("RFQ handler capacity reached", 0))
        for event, payload in audit.records
        if event == "rfq_unsupported_summary"
    )
    assert capacity == 99
    assert len(maker.ledger.reservations) == 1


def test_stale_queued_rfq_is_never_quoted() -> None:
    client = FakeClient()
    audit = RecordingAudit()
    maker = RFQMaker(
        client=client,  # type: ignore[arg-type]
        stream=EmptyStream(),
        fair_book=FakeFairBook(fair()),
        config=RFQMakerConfig(max_quote_latency_seconds=0.1),
        audit_log=audit,
        execute=False,
    )

    asyncio.run(maker.handle(created_message(), received_at=time.monotonic() - 1))

    skipped = [payload for event, payload in audit.records if event == "rfq_quote_skipped"]
    assert skipped[0]["reason"] == "RFQ exceeded the maximum quote latency"
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
