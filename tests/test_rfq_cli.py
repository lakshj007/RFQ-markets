from decimal import Decimal

import pytest

from kalshi_mm.cli import _preflight_rfq_live_canary, _validate_rfq_live_canary, build_parser


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
            "1.5",
            "--max-contracts",
            "10",
            "--max-position",
            "10",
            "--max-notional",
            "10",
            "--max-session-contracts",
            "10",
            "--max-session-executions",
            "1",
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

    assert args.edge_percent == Decimal("1.5")


def test_rfq_live_canary_rejects_edge_below_one_and_a_half_percent() -> None:
    args = canary_args()
    args.edge_percent = Decimal("1.499")

    with pytest.raises(ValueError, match="at least 1.5%"):
        _validate_rfq_live_canary(args)


def test_rfq_live_canary_requires_sixty_second_unaccepted_quote_lifetime() -> None:
    args = canary_args()
    args.max_unaccepted_quote_age = None

    with pytest.raises(ValueError, match="60-second"):
        _validate_rfq_live_canary(args)


def test_rfq_live_canary_rejects_primary_account() -> None:
    args = canary_args()
    args.subaccount = 0

    with pytest.raises(ValueError, match="dedicated numbered subaccount"):
        _validate_rfq_live_canary(args)


def test_rfq_live_canary_rejects_target_cost_support() -> None:
    args = canary_args()
    args.contracts_only = False

    with pytest.raises(ValueError, match="requires --contracts-only"):
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
