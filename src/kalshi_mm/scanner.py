from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from statistics import median

from .client import KalshiClient
from .matching import DRAW_OUTCOMES, EventMatch, best_outcome_match, match_events, normalize_name
from .models import OrderBook, as_decimal
from .odds import DEFAULT_SHARP_BOOKMAKERS, BookmakerOdds, OddsClient, OddsEvent, OddsMarket


@dataclass(frozen=True, slots=True)
class ConsensusPrice:
    fair_probability: Decimal
    bookmaker_count: int
    minimum: Decimal
    maximum: Decimal
    bookmaker_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Discrepancy:
    ticker: str
    event_ticker: str
    outcome: str
    odds_event_id: str
    fair_probability: Decimal
    yes_bid: Decimal
    yes_ask: Decimal
    midpoint: Decimal
    action: str
    edge: Decimal
    bookmaker_count: int
    match_score: float
    yes_bid_size: Decimal = Decimal("0")
    yes_ask_size: Decimal = Decimal("0")
    market_type: str = "h2h"
    line: Decimal | None = None
    direct_yes_bid: Decimal | None = None
    direct_yes_ask: Decimal | None = None
    effective_bid_route: str | None = None
    effective_ask_route: str | None = None
    complement_ticker: str | None = None
    action_route: str | None = None


@dataclass(frozen=True, slots=True)
class EffectiveTopOfBook:
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    bid_route: str
    ask_route: str
    complement_ticker: str | None = None

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True, slots=True)
class _PricedMoneyline:
    ticker: str
    outcome: str
    consensus: ConsensusPrice
    book: OrderBook


_FULL_GAME_TOTAL_PATTERN = re.compile(
    r"^(?:reg(?:ulation)?\s+time:\s*)?over\s+(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
_FULL_GAME_SPREAD_PATTERN = re.compile(
    r"^(?P<team>.+?)\s+wins(?:\s+the\s+game)?\s+by\s+over\s+"
    r"(?P<line>\d+(?:\.\d+)?)\s+(?:runs?|points?)\b",
    re.IGNORECASE,
)
_PARTIAL_GAME_PATTERN = re.compile(
    r"\b(?:1h|2h|first\s+half|1st\s+half|second\s+half|2nd\s+half|"
    r"first\s+(?:five|5)|1st\s+5|first\s+inning|1st\s+inning|"
    r"[1-4](?:st|nd|rd|th)?\s+quarter|q[1-4])\b",
    re.IGNORECASE,
)


def parse_full_game_total_line(
    outcome: str,
    *,
    event_title: str = "",
    market_title: str = "",
) -> Decimal | None:
    """Return an exact full-game OVER line, rejecting team and period totals."""
    context = " ".join((outcome, event_title, market_title))
    if _PARTIAL_GAME_PATTERN.search(context):
        return None
    matched = _FULL_GAME_TOTAL_PATTERN.match(outcome.strip())
    return as_decimal(matched.group(1)) if matched else None


def parse_full_game_spread(outcome: str) -> tuple[str, Decimal] | None:
    matched = _FULL_GAME_SPREAD_PATTERN.match(outcome.strip())
    if matched is None:
        return None
    return matched.group("team").strip(), as_decimal(matched.group("line"))


def _market_for(bookmaker: BookmakerOdds, market_key: str) -> OddsMarket | None:
    return next((market for market in bookmaker.markets if market.key == market_key), None)


def _merge_level(
    direct_price: Decimal,
    direct_size: Decimal,
    direct_route: str,
    synthetic_price: Decimal,
    synthetic_size: Decimal,
    synthetic_route: str,
    *,
    prefer_higher: bool,
) -> tuple[Decimal, Decimal, str]:
    if synthetic_price == direct_price:
        return direct_price, direct_size + synthetic_size, f"{direct_route}|{synthetic_route}"
    use_synthetic = (
        synthetic_price > direct_price if prefer_higher else synthetic_price < direct_price
    )
    if use_synthetic:
        return synthetic_price, synthetic_size, synthetic_route
    return direct_price, direct_size, direct_route


def combine_complementary_books(
    ticker: str,
    book: OrderBook,
    complement_ticker: str,
    complement_book: OrderBook,
) -> EffectiveTopOfBook | None:
    """Merge YES with the other outcome's economically equivalent NO book."""
    if (
        book.best_bid is None
        or book.best_ask is None
        or complement_book.best_bid is None
        or complement_book.best_ask is None
    ):
        return None
    direct_route = f"{ticker}:YES"
    complement_route = f"{complement_ticker}:NO"
    bid, bid_size, bid_route = _merge_level(
        book.best_bid.price,
        book.best_bid.size,
        direct_route,
        Decimal("1") - complement_book.best_ask.price,
        complement_book.best_ask.size,
        complement_route,
        prefer_higher=True,
    )
    ask, ask_size, ask_route = _merge_level(
        book.best_ask.price,
        book.best_ask.size,
        direct_route,
        Decimal("1") - complement_book.best_bid.price,
        complement_book.best_bid.size,
        complement_route,
        prefer_higher=False,
    )
    return EffectiveTopOfBook(
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        bid_route=bid_route,
        ask_route=ask_route,
        complement_ticker=complement_ticker,
    )


def _is_two_outcome_moneyline(event: OddsEvent) -> bool:
    found = False
    for bookmaker in event.bookmakers:
        market = _market_for(bookmaker, "h2h")
        if market is None:
            continue
        outcomes = [item for item in market.outcomes if item.price > 1]
        if len(outcomes) != 2:
            return False
        if any(normalize_name(item.name) in DRAW_OUTCOMES for item in outcomes):
            return False
        found = True
    return found


def consensus_probability(
    event: OddsEvent,
    outcome_name: str,
    *,
    market_key: str = "h2h",
    min_bookmakers: int = 2,
    max_age_seconds: float = 180,
    now: datetime | None = None,
) -> ConsensusPrice | None:
    now = now or datetime.now(UTC)
    probabilities: list[tuple[str, Decimal]] = []
    for bookmaker in event.bookmakers:
        market = _market_for(bookmaker, market_key)
        if market is None or len(market.outcomes) < 2:
            continue
        last_update = market.last_update or bookmaker.last_update
        if last_update and (now - last_update).total_seconds() > max_age_seconds:
            continue
        valid_outcomes = [item for item in market.outcomes if item.price > 1]
        if len(valid_outcomes) < 2:
            continue
        matched = best_outcome_match(outcome_name, (item.name for item in valid_outcomes))
        if matched is None:
            continue
        matched_name, _ = matched
        inverse_sum = sum((Decimal("1") / item.price for item in valid_outcomes), Decimal("0"))
        target = next(item for item in valid_outcomes if item.name == matched_name)
        fair = (Decimal("1") / target.price) / inverse_sum
        probabilities.append((bookmaker.key, fair))
    if len(probabilities) < min_bookmakers:
        return None
    values = [value for _, value in probabilities]
    return ConsensusPrice(
        fair_probability=median(values),
        bookmaker_count=len(values),
        minimum=min(values),
        maximum=max(values),
        bookmaker_keys=tuple(key for key, _ in probabilities),
    )


def consensus_total_probability(
    event: OddsEvent,
    line: Decimal,
    *,
    min_bookmakers: int = 2,
    max_age_seconds: float = 180,
    now: datetime | None = None,
) -> ConsensusPrice | None:
    """Build a no-vig OVER consensus using only books offering the exact line."""
    now = now or datetime.now(UTC)
    line = as_decimal(line)
    probabilities: list[tuple[str, Decimal]] = []
    for bookmaker in event.bookmakers:
        market = _market_for(bookmaker, "totals")
        if market is None:
            continue
        last_update = market.last_update or bookmaker.last_update
        if last_update and (now - last_update).total_seconds() > max_age_seconds:
            continue
        exact = {
            item.name.casefold(): item
            for item in market.outcomes
            if item.point == line and item.price > 1 and item.name.casefold() in {"over", "under"}
        }
        if set(exact) != {"over", "under"}:
            continue
        over_inverse = Decimal("1") / exact["over"].price
        under_inverse = Decimal("1") / exact["under"].price
        fair_over = over_inverse / (over_inverse + under_inverse)
        probabilities.append((bookmaker.key, fair_over))
    if len(probabilities) < min_bookmakers:
        return None
    values = [value for _, value in probabilities]
    return ConsensusPrice(
        fair_probability=median(values),
        bookmaker_count=len(values),
        minimum=min(values),
        maximum=max(values),
        bookmaker_keys=tuple(key for key, _ in probabilities),
    )


def consensus_spread_probability(
    event: OddsEvent,
    team: str,
    margin: Decimal,
    *,
    min_bookmakers: int = 2,
    max_age_seconds: float = 180,
    now: datetime | None = None,
) -> ConsensusPrice | None:
    """Build a no-vig team-cover probability for one exact full-game spread."""
    now = now or datetime.now(UTC)
    margin = as_decimal(margin)
    probabilities: list[tuple[str, Decimal]] = []
    for bookmaker in event.bookmakers:
        market = _market_for(bookmaker, "spreads")
        if market is None:
            continue
        last_update = market.last_update or bookmaker.last_update
        if last_update and (now - last_update).total_seconds() > max_age_seconds:
            continue
        valid = [item for item in market.outcomes if item.price > 1 and item.point is not None]
        matched = best_outcome_match(team, (item.name for item in valid))
        if matched is None:
            continue
        target_name, _ = matched
        target = next(
            (
                item
                for item in valid
                if item.name == target_name and item.point == -margin
            ),
            None,
        )
        if target is None:
            continue
        exact_line = [item for item in valid if abs(item.point or Decimal("0")) == margin]
        if len(exact_line) != 2:
            continue
        inverse_sum = sum((Decimal("1") / item.price for item in exact_line), Decimal("0"))
        fair = (Decimal("1") / target.price) / inverse_sum
        probabilities.append((bookmaker.key, fair))
    if len(probabilities) < min_bookmakers:
        return None
    values = [value for _, value in probabilities]
    return ConsensusPrice(
        fair_probability=median(values),
        bookmaker_count=len(values),
        minimum=min(values),
        maximum=max(values),
        bookmaker_keys=tuple(key for key, _ in probabilities),
    )


def _priced_discrepancy(
    kalshi: KalshiClient,
    match: EventMatch,
    *,
    ticker: str,
    outcome: str,
    consensus: ConsensusPrice,
    min_edge: Decimal,
    market_type: str,
    line: Decimal | None = None,
    book: OrderBook | None = None,
    effective_book: EffectiveTopOfBook | None = None,
) -> Discrepancy | None:
    book = book or OrderBook.from_api(kalshi.get_orderbook(ticker, depth=1))
    if book.best_bid is None or book.best_ask is None or book.midpoint is None:
        return None
    effective_book = effective_book or EffectiveTopOfBook(
        bid=book.best_bid.price,
        ask=book.best_ask.price,
        bid_size=book.best_bid.size,
        ask_size=book.best_ask.size,
        bid_route=f"{ticker}:YES",
        ask_route=f"{ticker}:YES",
    )
    buy_edge = consensus.fair_probability - effective_book.ask
    passive_bid_edge = consensus.fair_probability - effective_book.bid
    sell_edge = effective_book.bid - consensus.fair_probability
    if buy_edge >= min_edge:
        action = "BUY YES"
        edge = buy_edge
        action_route = effective_book.ask_route
    elif passive_bid_edge >= min_edge:
        action = "MAKE BID"
        edge = passive_bid_edge
        action_route = effective_book.bid_route
    elif sell_edge >= min_edge:
        action = "SELL YES"
        edge = sell_edge
        action_route = effective_book.bid_route
    else:
        action = "NONE"
        edge = max(buy_edge, passive_bid_edge, sell_edge)
        action_route = None
    return Discrepancy(
        ticker=ticker,
        event_ticker=str(match.kalshi_event.get("event_ticker", "")),
        outcome=outcome,
        odds_event_id=match.odds_event.event_id,
        fair_probability=consensus.fair_probability,
        yes_bid=effective_book.bid,
        yes_ask=effective_book.ask,
        midpoint=effective_book.midpoint,
        action=action,
        edge=edge,
        bookmaker_count=consensus.bookmaker_count,
        match_score=match.score,
        yes_bid_size=effective_book.bid_size,
        yes_ask_size=effective_book.ask_size,
        market_type=market_type,
        line=line,
        direct_yes_bid=book.best_bid.price,
        direct_yes_ask=book.best_ask.price,
        effective_bid_route=effective_book.bid_route,
        effective_ask_route=effective_book.ask_route,
        complement_ticker=effective_book.complement_ticker,
        action_route=action_route,
    )


def _scan_moneyline_match(
    kalshi: KalshiClient,
    match: EventMatch,
    *,
    min_edge: Decimal,
    min_bookmakers: int,
    max_odds_age_seconds: float,
    now: datetime,
) -> list[Discrepancy]:
    event = match.kalshi_event
    priced: list[_PricedMoneyline] = []
    for market in event.get("markets", []):
        ticker = str(market.get("ticker", ""))
        outcome = str(market.get("yes_sub_title", ""))
        if not ticker or not outcome:
            continue
        consensus = consensus_probability(
            match.odds_event,
            outcome,
            min_bookmakers=min_bookmakers,
            max_age_seconds=max_odds_age_seconds,
            now=now,
        )
        if consensus is None:
            continue
        book = OrderBook.from_api(kalshi.get_orderbook(ticker, depth=1))
        if book.best_bid is None or book.best_ask is None:
            continue
        priced.append(_PricedMoneyline(ticker, outcome, consensus, book))

    results: list[Discrepancy] = []
    use_complements = len(priced) == 2 and _is_two_outcome_moneyline(match.odds_event)
    for index, item in enumerate(priced):
        effective_book = None
        if use_complements:
            complement = priced[1 - index]
            effective_book = combine_complementary_books(
                item.ticker,
                item.book,
                complement.ticker,
                complement.book,
            )
        discrepancy = _priced_discrepancy(
            kalshi,
            match,
            ticker=item.ticker,
            outcome=item.outcome,
            consensus=item.consensus,
            min_edge=min_edge,
            market_type="h2h",
            book=item.book,
            effective_book=effective_book,
        )
        if discrepancy is not None:
            results.append(discrepancy)
    return results


def _scan_total_match(
    kalshi: KalshiClient,
    match: EventMatch,
    *,
    min_edge: Decimal,
    min_bookmakers: int,
    max_odds_age_seconds: float,
    now: datetime,
) -> list[Discrepancy]:
    results: list[Discrepancy] = []
    event = match.kalshi_event
    event_title = str(event.get("title", ""))
    for market in event.get("markets", []):
        ticker = str(market.get("ticker", ""))
        outcome = str(market.get("yes_sub_title", ""))
        line = parse_full_game_total_line(
            outcome,
            event_title=event_title,
            market_title=str(market.get("title", "")),
        )
        if not ticker or line is None:
            continue
        consensus = consensus_total_probability(
            match.odds_event,
            line,
            min_bookmakers=min_bookmakers,
            max_age_seconds=max_odds_age_seconds,
            now=now,
        )
        if consensus is None:
            continue
        discrepancy = _priced_discrepancy(
            kalshi,
            match,
            ticker=ticker,
            outcome=outcome,
            consensus=consensus,
            min_edge=min_edge,
            market_type="totals",
            line=line,
        )
        if discrepancy is not None:
            results.append(discrepancy)
    return results


def scan_discrepancies(
    *,
    kalshi: KalshiClient,
    odds: OddsClient,
    series_ticker: str,
    sport: str,
    market_type: str = "h2h",
    regions: str = "us",
    bookmakers: str | None = DEFAULT_SHARP_BOOKMAKERS,
    min_edge: Decimal = Decimal("0.03"),
    min_bookmakers: int = 2,
    max_odds_age_seconds: float = 180,
    match_window_hours: float = 6,
    event_limit: int = 50,
    include_live: bool = False,
    now: datetime | None = None,
) -> list[Discrepancy]:
    if market_type not in {"h2h", "totals"}:
        raise ValueError("market type must be h2h or totals")
    now = now or datetime.now(UTC)
    kalshi_events = kalshi.get_events(series_ticker=series_ticker, limit=event_limit)
    odds_events = odds.get_odds(
        sport,
        regions=regions,
        markets=market_type,
        bookmakers=bookmakers,
    )
    if not include_live:
        odds_events = [event for event in odds_events if event.commence_time > now]
    matches = match_events(
        kalshi_events,
        odds_events,
        max_time_difference_seconds=match_window_hours * 60 * 60,
    )
    results: list[Discrepancy] = []
    scan_match = _scan_moneyline_match if market_type == "h2h" else _scan_total_match
    for match in matches:
        results.extend(
            scan_match(
                kalshi,
                match,
                min_edge=as_decimal(min_edge),
                min_bookmakers=min_bookmakers,
                max_odds_age_seconds=max_odds_age_seconds,
                now=now,
            )
        )
    return sorted(results, key=lambda item: item.edge, reverse=True)


def scan_moneyline_discrepancies(
    **kwargs: object,
) -> list[Discrepancy]:
    """Backward-compatible moneyline scanner."""
    return scan_discrepancies(market_type="h2h", **kwargs)  # type: ignore[arg-type]


def scan_total_discrepancies(
    **kwargs: object,
) -> list[Discrepancy]:
    return scan_discrepancies(market_type="totals", **kwargs)  # type: ignore[arg-type]
