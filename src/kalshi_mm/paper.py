from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .scanner import Discrepancy


@dataclass(slots=True)
class OpenPaperSignal:
    signal_id: str
    ticker: str
    outcome: str
    action: str
    entry_price: Decimal
    fair_probability: Decimal
    created_at: datetime
    marked_horizons: set[int] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class PaperUpdate:
    signals_recorded: int
    markouts_recorded: int


class PaperRecorder:
    """Append-only signal and markout recorder; it never submits an order."""

    def __init__(
        self,
        path: str | Path,
        *,
        markout_horizons_seconds: tuple[int, ...] = (60, 300),
        signal_cooldown_seconds: int = 300,
    ) -> None:
        if any(horizon <= 0 for horizon in markout_horizons_seconds):
            raise ValueError("markout horizons must be positive")
        self.path = Path(path)
        self.markout_horizons_seconds = tuple(sorted(set(markout_horizons_seconds)))
        self.signal_cooldown_seconds = signal_cooldown_seconds
        self.open_signals: list[OpenPaperSignal] = []
        self.last_signal_at: dict[tuple[str, str], datetime] = {}

    def _append(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(payload, separators=(",", ":")) + "\n")

    def _record_markouts(
        self,
        snapshots: dict[str, Discrepancy],
        now: datetime,
    ) -> int:
        recorded = 0
        for signal in self.open_signals:
            snapshot = snapshots.get(signal.ticker)
            if snapshot is None:
                continue
            age_seconds = (now - signal.created_at).total_seconds()
            for horizon in self.markout_horizons_seconds:
                if horizon in signal.marked_horizons or age_seconds < horizon:
                    continue
                if signal.action in {"BUY YES", "MAKE BID"}:
                    markout = snapshot.midpoint - signal.entry_price
                else:
                    markout = signal.entry_price - snapshot.midpoint
                self._append(
                    {
                        "record_type": "markout",
                        "signal_id": signal.signal_id,
                        "recorded_at": now.isoformat(),
                        "horizon_seconds": horizon,
                        "midpoint": str(snapshot.midpoint),
                        "markout": str(markout),
                    }
                )
                signal.marked_horizons.add(horizon)
                recorded += 1
        return recorded

    def _record_signals(self, discrepancies: Iterable[Discrepancy], now: datetime) -> int:
        recorded = 0
        for item in discrepancies:
            if item.action == "NONE":
                continue
            key = (item.ticker, item.action)
            previous = self.last_signal_at.get(key)
            if previous and (now - previous).total_seconds() < self.signal_cooldown_seconds:
                continue
            entry_price = item.yes_ask if item.action == "BUY YES" else item.yes_bid
            signal = OpenPaperSignal(
                signal_id=str(uuid.uuid4()),
                ticker=item.ticker,
                outcome=item.outcome,
                action=item.action,
                entry_price=entry_price,
                fair_probability=item.fair_probability,
                created_at=now,
            )
            self.open_signals.append(signal)
            self.last_signal_at[key] = now
            self._append(
                {
                    "record_type": "signal",
                    "signal_id": signal.signal_id,
                    "recorded_at": now.isoformat(),
                    "ticker": item.ticker,
                    "outcome": item.outcome,
                    "action": item.action,
                    "entry_price": str(entry_price),
                    "fair_probability": str(item.fair_probability),
                    "edge": str(item.edge),
                    "bookmaker_count": item.bookmaker_count,
                    "match_score": item.match_score,
                }
            )
            recorded += 1
        return recorded

    def update(
        self,
        discrepancies: Iterable[Discrepancy],
        *,
        now: datetime | None = None,
    ) -> PaperUpdate:
        now = now or datetime.now(UTC)
        discrepancies = list(discrepancies)
        snapshots = {item.ticker: item for item in discrepancies}
        markouts = self._record_markouts(snapshots, now)
        signals = self._record_signals(discrepancies, now)
        return PaperUpdate(signals_recorded=signals, markouts_recorded=markouts)
