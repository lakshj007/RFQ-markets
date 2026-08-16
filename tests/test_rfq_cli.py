from decimal import Decimal

import pytest

from kalshi_mm.cli import (
    _odds_lane,
    _preflight_rfq_live_canary,
    _rfq_client,
    _validate_rfq_live_canary,
    build_parser,
)


class CanaryClient:
    api_key_id = "rfq-key-id"

    def __init__(self, private_key_path, *, subaccount: int, balance: str) -> None:
        self.private_key_path = private_key_path
        self.subaccount = subaccount
        self.balance = balance

    def get_api_keys(self):
        return [
            {
                "api_key_id": self.api_key_id,
                "name": "rfq-key",
                "scopes": ["read", "write"],
            }
        ]

    def get_subaccount_balances(self):
        return [{"subaccount_number": self.subaccount}]

    def get_balance(self, *, subaccount: int):
        assert subaccount == self.subaccount
        return {"balance": self.balance}

    def get_orders(self, *, status: str, subaccount: int, limit: int):
        assert (status, subaccount, limit) == ("resting", self.subaccount, 1000)
        return []

    def get_positions(self, *, subaccount: int, limit: int):
        assert (subaccount, limit) == (self.subaccount, 1000)
        return []


def test_odds_lane_parses_explicit_series_sport_and_market() -> None:
    assert _odds_lane("KXMLBTOTAL:baseball_mlb:totals") == (
        "KXMLBTOTAL",
        "baseball_mlb",
        "totals",
    )


def test_odds_lane_rejects_unknown_market_type() -> None:
    with pytest.raises(Exception, match="h2h, totals, or spreads"):
        _odds_lane("KXMLBHR:baseball_mlb:player_props")


def test_rfq_defaults_to_one_point_one_percent_proportional_edge() -> None:
    args = build_parser().parse_args(["rfq-maker", "--fair-file", "fairs.json"])

    assert args.edge_percent == Decimal("1.1")
    assert args.proportional_pricing is False


def canary_args():
    return build_parser().parse_args(
        [
            "rfq-maker",
            "--series",
            "KXMLBGAME",
            "--odds-sport",
            "baseball_mlb",
            "--allow-collection",
            "KXMVESPORTSMULTIGAMEEXTENDED-R",
            "--combo-only",
            "--contracts-only",
            "--edge-percent",
            "0.75",
            "--max-contracts",
            "10",
            "--max-position",
            "10",
            "--max-notional",
            "5",
            "--max-session-notional",
            "20",
            "--max-session-contracts",
            "40",
            "--max-session-executions",
            "4",
            "--max-active-quotes",
            "1",
            "--max-unaccepted-quote-age",
            "60",
            "--max-inflight-rfqs",
            "1",
            "--max-legs",
            "6",
            "--subaccount",
            "1",
            "--seconds",
            "900",
            "--canary-live",
        ]
    )


def test_locked_rfq_live_canary_profile_is_accepted() -> None:
    args = canary_args()

    _validate_rfq_live_canary(args)

    assert args.edge_percent == Decimal("0.75")


def test_live_canary_accepts_guarded_wnba_and_soccer_lanes() -> None:
    args = canary_args()
    args.series = None
    args.odds_sport = None
    args.lead_style_profile = True
    args.odds_lane = [
        ("KXMLBGAME", "baseball_mlb", "h2h"),
        ("KXWNBAGAME", "basketball_wnba", "h2h"),
        ("KXWNBATOTAL", "basketball_wnba", "totals"),
        ("KXWNBASPREAD", "basketball_wnba", "spreads"),
        ("KXMLSGAME", "soccer_usa_mls", "h2h"),
        ("KXMLSTOTAL", "soccer_usa_mls", "totals"),
        ("KXLIGAMXGAME", "soccer_mexico_ligamx", "h2h"),
        ("KXLIGAMXTOTAL", "soccer_mexico_ligamx", "totals"),
    ]

    _validate_rfq_live_canary(args)


def test_live_canary_rejects_unapproved_multi_lane() -> None:
    args = canary_args()
    args.series = None
    args.odds_sport = None
    args.lead_style_profile = True
    args.odds_lane = [("KXUFCFIGHT", "mma_mixed_martial_arts", "h2h")]

    with pytest.raises(ValueError, match="unsupported odds lane"):
        _validate_rfq_live_canary(args)


def test_rfq_live_canary_rejects_edge_below_three_quarter_percent() -> None:
    args = canary_args()
    args.edge_percent = Decimal("0.749")

    with pytest.raises(ValueError, match="at least 0.75%"):
        _validate_rfq_live_canary(args)


def test_rfq_live_canary_requires_sixty_second_unaccepted_quote_lifetime() -> None:
    args = canary_args()
    args.max_unaccepted_quote_age = None

    with pytest.raises(ValueError, match="60-second"):
        _validate_rfq_live_canary(args)


def test_rfq_live_canary_requires_aggregate_ten_dollar_notional_cap() -> None:
    args = canary_args()
    args.max_session_notional = None

    with pytest.raises(ValueError, match="session-wide notional cap"):
        _validate_rfq_live_canary(args)


def test_rfq_live_canary_rejects_primary_account() -> None:
    args = canary_args()
    args.subaccount = 0

    with pytest.raises(ValueError, match="dedicated numbered subaccount"):
        _validate_rfq_live_canary(args)


def test_rfq_live_canary_accepts_explicit_target_cost_support() -> None:
    args = canary_args()
    args.contracts_only = False
    args.allow_target_cost = True
    args.max_target_cost = Decimal("5")

    _validate_rfq_live_canary(args)


def test_rfq_live_canary_target_cost_requires_explicit_cap() -> None:
    args = canary_args()
    args.contracts_only = False
    args.allow_target_cost = True

    with pytest.raises(ValueError, match="max-target-cost in"):
        _validate_rfq_live_canary(args)


def test_explicit_target_cost_mode_always_requires_a_dollar_cap() -> None:
    args = canary_args()
    args.contracts_only = False
    args.allow_target_cost = True

    with pytest.raises(ValueError, match="allow-target-cost requires"):
        _rfq_client(args)


def test_coverage_shadow_requires_a_simulated_quote_lifetime() -> None:
    args = canary_args()
    args.contracts_only = False
    args.allow_target_cost = True
    args.max_target_cost = Decimal("10")
    args.coverage_shadow = True
    args.max_unaccepted_quote_age = None

    with pytest.raises(ValueError, match="max-unaccepted-quote-age"):
        _rfq_client(args)


def test_rfq_live_canary_contract_mode_rejects_target_cost_cap() -> None:
    args = canary_args()
    args.max_target_cost = Decimal("10")

    with pytest.raises(ValueError, match="contracts-only live canary"):
        _validate_rfq_live_canary(args)


def test_rfq_live_canary_requires_explicit_sizing_mode() -> None:
    args = canary_args()
    args.contracts_only = False

    with pytest.raises(ValueError, match="explicit sizing mode"):
        _validate_rfq_live_canary(args)


def test_rfq_live_canary_allows_four_disjoint_quote_slots() -> None:
    args = canary_args()
    args.max_active_quotes = 4
    args.max_inflight_rfqs = 4

    _validate_rfq_live_canary(args)


def test_rfq_live_canary_rejects_more_than_four_quote_slots() -> None:
    args = canary_args()
    args.max_active_quotes = 5

    with pytest.raises(ValueError, match="first-fill-wins"):
        _validate_rfq_live_canary(args)


def test_rfq_live_canary_rejects_first_fill_wins_for_four_fill_run() -> None:
    args = canary_args()
    args.first_fill_wins = True
    args.max_active_quotes = 20
    args.max_inflight_rfqs = 20

    with pytest.raises(ValueError, match="four-fill"):
        _validate_rfq_live_canary(args)


def test_rfq_live_canary_allows_up_to_ten_independent_legs() -> None:
    args = canary_args()
    args.max_legs = 10

    _validate_rfq_live_canary(args)

    args.max_legs = 11
    with pytest.raises(ValueError, match="2-10 independent"):
        _validate_rfq_live_canary(args)


def test_rfq_live_canary_accepts_primary_account_with_explicit_flag() -> None:
    args = canary_args()
    args.subaccount = 0
    args.allow_primary_account_canary = True

    _validate_rfq_live_canary(args)


def test_rfq_live_canary_rejects_primary_flag_for_numbered_subaccount() -> None:
    args = canary_args()
    args.allow_primary_account_canary = True

    with pytest.raises(ValueError, match="requires --subaccount 0"):
        _validate_rfq_live_canary(args)


def test_rfq_live_canary_primary_preflight_allows_balance_above_one_dollar(tmp_path) -> None:
    key = tmp_path / "rfq.key"
    key.write_text("test", encoding="utf-8")
    key.chmod(0o600)
    args = canary_args()
    args.subaccount = 0
    args.allow_primary_account_canary = True
    client = CanaryClient(key, subaccount=0, balance="1000")

    _preflight_rfq_live_canary(client, args)


def test_rfq_live_canary_requires_explicit_open_position_continuation(tmp_path) -> None:
    class PositionedCanaryClient(CanaryClient):
        def get_positions(self, *, subaccount: int, limit: int):
            return [{"ticker": "COMBO", "position_fp": "-1.00"}]

    key = tmp_path / "rfq.key"
    key.write_text("test", encoding="utf-8")
    key.chmod(0o600)
    args = canary_args()
    client = PositionedCanaryClient(key, subaccount=1, balance="649")

    with pytest.raises(ValueError, match="already has positions"):
        _preflight_rfq_live_canary(client, args)

    args.continue_open_independent_positions = True
    _preflight_rfq_live_canary(client, args)


def test_rfq_live_canary_allows_smaller_cash_and_target_cost_caps() -> None:
    args = canary_args()
    args.contracts_only = False
    args.allow_target_cost = True
    args.max_target_cost = Decimal("5")
    args.max_session_notional = Decimal("5")

    _validate_rfq_live_canary(args)


def test_rfq_live_canary_numbered_preflight_keeps_ten_dollar_balance_cap(tmp_path) -> None:
    key = tmp_path / "rfq.key"
    key.write_text("test", encoding="utf-8")
    key.chmod(0o600)
    args = canary_args()
    client = CanaryClient(key, subaccount=1, balance="1001")

    with pytest.raises(ValueError, match=r"no more than \$10.00"):
        _preflight_rfq_live_canary(client, args)


def test_rfq_live_canary_rejects_any_extra_collection() -> None:
    args = canary_args()
    args.allow_collection.append("KXMVECROSSCATEGORY-R")

    with pytest.raises(ValueError, match="must allow only collection"):
        _validate_rfq_live_canary(args)
