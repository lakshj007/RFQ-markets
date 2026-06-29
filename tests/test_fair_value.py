import json
from decimal import Decimal

import pytest

from kalshi_mm.fair_value import JsonFileFairValue, StaticFairValue


def test_json_fair_value_reloads_file(tmp_path) -> None:
    path = tmp_path / "fair.json"
    path.write_text(json.dumps({"MARKET": 0.54}))
    source = JsonFileFairValue(path)

    assert source.get("MARKET") == Decimal("0.54")
    path.write_text(json.dumps({"MARKET": "0.57"}))
    assert source.get("MARKET") == Decimal("0.57")


def test_fair_value_must_be_probability() -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        StaticFairValue(Decimal("1.1")).get("MARKET")

