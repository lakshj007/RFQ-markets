from decimal import Decimal

import pytest

from kalshi_mm.cli import _validate_rfq_live_canary, build_parser


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
            "--edge-percent",
            "2",
            "--max-contracts",
            "1",
            "--max-position",
            "1",
            "--max-notional",
            "1",
            "--max-session-contracts",
            "1",
            "--max-active-quotes",
            "1",
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

    assert args.edge_percent == Decimal("2")


def test_rfq_live_canary_rejects_primary_account() -> None:
    args = canary_args()
    args.subaccount = 0

    with pytest.raises(ValueError, match="dedicated numbered subaccount"):
        _validate_rfq_live_canary(args)


def test_rfq_live_canary_rejects_any_extra_collection() -> None:
    args = canary_args()
    args.allow_collection.append("KXMVECROSSCATEGORY-R")

    with pytest.raises(ValueError, match="must allow only collection"):
        _validate_rfq_live_canary(args)
