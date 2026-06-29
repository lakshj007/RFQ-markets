from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from .models import ONE, ZERO, as_decimal


class FairValueSource(Protocol):
    def get(self, ticker: str) -> Decimal: ...


def _validate_probability(value: Decimal) -> Decimal:
    if not ZERO < value < ONE:
        raise ValueError("fair probability must be strictly between 0 and 1")
    return value


@dataclass(frozen=True, slots=True)
class StaticFairValue:
    probability: Decimal

    def get(self, ticker: str) -> Decimal:
        del ticker
        return _validate_probability(as_decimal(self.probability))


@dataclass(frozen=True, slots=True)
class JsonFileFairValue:
    """Reload a ticker-to-probability JSON map on every quote cycle."""

    path: Path

    def get(self, ticker: str) -> Decimal:
        payload = json.loads(self.path.read_text())
        if not isinstance(payload, dict) or ticker not in payload:
            raise ValueError(f"{self.path} must contain a {ticker!r} probability")
        return _validate_probability(as_decimal(payload[ticker]))

