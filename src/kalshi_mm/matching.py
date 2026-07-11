from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

from .odds import OddsEvent, parse_datetime

DRAW_OUTCOMES = {"draw", "tie"}
GENERIC_OUTCOMES = {"yes", "no", "over", "under", "other", "field"} | DRAW_OUTCOMES


def normalize_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_value = decomposed.encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", ascii_value))


def name_similarity(left: str, right: str) -> float:
    left_normalized = normalize_name(left)
    right_normalized = normalize_name(right)
    if not left_normalized or not right_normalized:
        return 0.0
    if left_normalized == right_normalized:
        return 1.0
    if left_normalized in DRAW_OUTCOMES or right_normalized in DRAW_OUTCOMES:
        both_are_draws = (
            left_normalized in DRAW_OUTCOMES and right_normalized in DRAW_OUTCOMES
        )
        return 1.0 if both_are_draws else 0.0
    shorter, longer = sorted((left_normalized, right_normalized), key=len)
    if len(shorter) >= 4 and longer.startswith(shorter):
        return 0.9
    left_tokens = set(left_normalized.split())
    right_tokens = set(right_normalized.split())
    union = left_tokens | right_tokens
    token_score = len(left_tokens & right_tokens) / len(union) if union else 0.0
    sequence_score = SequenceMatcher(None, left_normalized, right_normalized).ratio()
    return max(token_score, sequence_score)


def _split_title(title: str) -> list[str]:
    for separator in (" vs ", " at ", " @ ", " versus "):
        if separator in title.casefold():
            return re.split(separator, title, maxsplit=1, flags=re.IGNORECASE)
    return []


def kalshi_participants(event: dict[str, Any]) -> tuple[str, ...]:
    candidates: list[str] = []
    for market in event.get("markets", []):
        for key in ("yes_sub_title", "no_sub_title"):
            value = str(market.get(key, "")).strip()
            if value and normalize_name(value) not in GENERIC_OUTCOMES:
                candidates.append(value)
    candidates.extend(_split_title(str(event.get("title", ""))))
    unique: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        normalized = normalize_name(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(value)
    return tuple(unique)


def kalshi_event_time(event: dict[str, Any]) -> datetime | None:
    times: list[datetime] = []
    for market in event.get("markets", []):
        value = parse_datetime(
            market.get("occurrence_datetime")
            or market.get("expected_expiration_time")
            or market.get("close_time")
        )
        if value:
            times.append(value)
    return min(times) if times else parse_datetime(event.get("strike_date"))


@dataclass(frozen=True, slots=True)
class EventMatch:
    kalshi_event: dict[str, Any]
    odds_event: OddsEvent
    score: float
    time_difference_seconds: float


def _participant_pair_score(participants: tuple[str, ...], odds_event: OddsEvent) -> float:
    if len(participants) < 2:
        return 0.0
    odds_participants = (odds_event.home_team, odds_event.away_team)
    best = 0.0
    for first in participants:
        for second in participants:
            if first == second:
                continue
            direct = (
                name_similarity(first, odds_participants[0])
                + name_similarity(second, odds_participants[1])
            ) / 2
            swapped = (
                name_similarity(first, odds_participants[1])
                + name_similarity(second, odds_participants[0])
            ) / 2
            best = max(best, direct, swapped)
    return best


def match_events(
    kalshi_events: Iterable[dict[str, Any]],
    odds_events: Iterable[OddsEvent],
    *,
    max_time_difference_seconds: float = 6 * 60 * 60,
    minimum_score: float = 0.72,
    ambiguity_margin: float = 0.03,
) -> list[EventMatch]:
    odds_events = list(odds_events)
    matches: list[EventMatch] = []
    for event in kalshi_events:
        event_time = kalshi_event_time(event)
        if event_time is None:
            continue
        participants = kalshi_participants(event)
        candidates: list[EventMatch] = []
        for odds_event in odds_events:
            time_difference = abs((event_time - odds_event.commence_time).total_seconds())
            if time_difference > max_time_difference_seconds:
                continue
            participant_score = _participant_pair_score(participants, odds_event)
            time_score = 1 - 0.15 * time_difference / max_time_difference_seconds
            score = participant_score * time_score
            if score >= minimum_score:
                candidates.append(
                    EventMatch(
                        kalshi_event=event,
                        odds_event=odds_event,
                        score=score,
                        time_difference_seconds=time_difference,
                    )
                )
        candidates.sort(key=lambda item: item.score, reverse=True)
        if not candidates:
            continue
        if len(candidates) > 1 and candidates[0].score - candidates[1].score < ambiguity_margin:
            continue
        matches.append(candidates[0])
    return matches


def best_outcome_match(target: str, choices: Iterable[str]) -> tuple[str, float] | None:
    scored = sorted(
        ((choice, name_similarity(target, choice)) for choice in choices),
        key=lambda item: item[1],
        reverse=True,
    )
    if not scored or scored[0][1] < 0.65:
        return None
    if len(scored) > 1 and scored[0][1] - scored[1][1] < 0.05:
        return None
    return scored[0]
