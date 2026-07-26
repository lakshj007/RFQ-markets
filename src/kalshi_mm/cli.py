from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import stat
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .bot import run_bot, run_streaming_bot
from .client import DEMO_BASE_URL, PRODUCTION_BASE_URL, KalshiClient
from .config import load_env
from .display import level as display_level
from .display import money, number, table
from .fair_value import (
    FairValueSource,
    JsonFileFairValue,
    OddsConsensusFairValue,
    OddsFairSnapshot,
    StaticFairValue,
)
from .live_order import (
    BOUNDED_EXIT_ACKNOWLEDGEMENT,
    LIVE_ACKNOWLEDGEMENT,
    MONITORED_ENTRY_ACKNOWLEDGEMENT,
    BoundedExitRequest,
    FairAwareExitConfig,
    LiveAuditLog,
    LiveOrderRequest,
    LiveRiskLimits,
    MonitoredEntryConfig,
    execute_bounded_exit,
    execute_fair_aware_bounded_exit,
    execute_live_order,
    execute_monitored_live_order,
    preflight_live_order,
)
from .maker_paper import MakerPaperRecorder, MakerPaperUpdate
from .models import ZERO, Level, OrderBook, PriceGrid, as_decimal
from .odds import DEFAULT_SHARP_BOOKMAKERS, OddsClient
from .paper import PaperRecorder
from .rfq import (
    KALSHI_MAKER_FEE_RATE,
    RFQ_LIVE_ACKNOWLEDGEMENT,
    RFQ_LIVE_ENABLE_TOKEN,
    JsonMoneylineFairBook,
    MarkdownRFQFillLedger,
    OddsMoneylineFairBook,
    RFQMaker,
    RFQMakerConfig,
)
from .scanner import Discrepancy, scan_discrepancies
from .strategy import MarketMakerStrategy, StrategyConfig, devig_two_way
from .ws import KalshiRFQWebSocket, KalshiWebSocket, StreamUpdate


def _decimal(value: str) -> Decimal:
    try:
        return as_decimal(value)
    except Exception as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _moneyline(value: str) -> int:
    odds = int(value)
    if odds == 0:
        raise argparse.ArgumentTypeError("moneyline cannot be zero")
    return odds


def _client(*, demo: bool = False) -> KalshiClient:
    return KalshiClient(base_url=DEMO_BASE_URL if demo else PRODUCTION_BASE_URL)


def _cmd_series(args: argparse.Namespace) -> None:
    series = _client().get_series(category="Sports", tags=args.tag)
    query = args.query.casefold() if args.query else None
    if query:
        series = [
            item
            for item in series
            if query in f"{item.get('ticker', '')} {item.get('title', '')}".casefold()
        ]
    series.sort(key=lambda item: as_decimal(item.get("volume_fp", "0")), reverse=True)
    output = [
        {
            "ticker": item.get("ticker"),
            "title": item.get("title"),
            "tags": item.get("tags"),
            "volume_fp": item.get("volume_fp"),
            "fee_type": item.get("fee_type"),
            "fee_multiplier": item.get("fee_multiplier"),
        }
        for item in series[: args.limit]
    ]
    if args.json:
        for item in output:
            print(json.dumps(item))
        return
    print(f"Sports series ({len(output)})\n")
    print(
        table(
            ("TICKER", "TITLE", "TAGS", "VOLUME", "FEES"),
            (
                (
                    item["ticker"],
                    item["title"],
                    ", ".join(item["tags"] or []),
                    number(item["volume_fp"]),
                    str(item["fee_type"] or "-").replace("_with_maker_fees", " + maker"),
                )
                for item in output
            ),
            right_align={3},
        )
    )


def _cmd_markets(args: argparse.Namespace) -> None:
    events = _client().get_events(series_ticker=args.series, limit=args.limit)
    output: list[dict[str, Any]] = []
    for event in events:
        for market in event.get("markets", []):
            output.append(
                {
                    "ticker": market.get("ticker"),
                    "event_ticker": event.get("event_ticker"),
                    "event": event.get("title"),
                    "outcome": market.get("yes_sub_title"),
                    "yes_bid": market.get("yes_bid_dollars"),
                    "yes_ask": market.get("yes_ask_dollars"),
                    "bid_size": market.get("yes_bid_size_fp"),
                    "ask_size": market.get("yes_ask_size_fp"),
                    "volume": market.get("volume_fp"),
                    "close_time": market.get("close_time"),
                }
            )
            if len(output) >= args.limit:
                break
        if len(output) >= args.limit:
            break
    if args.json:
        for item in output:
            print(json.dumps(item))
        return
    print(f"Open markets in {args.series} ({len(output)})\n")
    print(
        table(
            ("TICKER", "OUTCOME", "YES BID", "YES ASK", "VOLUME", "CLOSE"),
            (
                (
                    item["ticker"],
                    item["outcome"],
                    f"{money(item['yes_bid'])} x {number(item['bid_size'])}",
                    f"{money(item['yes_ask'])} x {number(item['ask_size'])}",
                    number(item["volume"]),
                    item["close_time"],
                )
                for item in output
            ),
            right_align={2, 3, 4},
        )
    )


def _book_summary(book: OrderBook) -> dict[str, Any]:
    def level(level: Level | None) -> dict[str, str] | None:
        if not level:
            return None
        return {
            "price": format(level.price, "f"),
            "size": format(level.size, "f"),
        }

    return {
        "best_yes_bid": level(book.best_bid),
        "best_yes_ask": level(book.best_ask),
        "spread": format(book.spread, "f") if book.spread is not None else None,
        "midpoint": format(book.midpoint, "f") if book.midpoint is not None else None,
        "microprice": format(book.microprice, "f") if book.microprice is not None else None,
        "book_imbalance": format(book.book_imbalance, "f"),
    }


def _cmd_book(args: argparse.Namespace) -> None:
    book = OrderBook.from_api(_client().get_orderbook(args.ticker, depth=args.depth))
    if args.json:
        print(json.dumps(_book_summary(book), indent=2))
        return
    print(f"YES orderbook: {args.ticker}\n")
    print(
        table(
            ("METRIC", "VALUE"),
            (
                ("Best bid", display_level(book.best_bid)),
                ("Best ask", display_level(book.best_ask)),
                ("Spread", money(book.spread)),
                ("Midpoint", money(book.midpoint)),
                ("Microprice", money(book.microprice)),
                ("Book imbalance", f"{book.book_imbalance * 100:+.2f}%"),
            ),
        )
    )


def _fair_source(args: argparse.Namespace, client: KalshiClient) -> FairValueSource:
    choices = sum(
        (
            args.fair_probability is not None,
            args.fair_file is not None,
            args.yes_moneyline is not None or args.no_moneyline is not None,
            args.odds_sport is not None,
        )
    )
    if choices != 1:
        raise ValueError(
            "choose exactly one fair-value input: --fair-probability, --fair-file, "
            "both --yes-moneyline and --no-moneyline, or --odds-sport"
        )
    if args.fair_file:
        return JsonFileFairValue(args.fair_file)
    if args.yes_moneyline is not None or args.no_moneyline is not None:
        if args.yes_moneyline is None or args.no_moneyline is None:
            raise ValueError("both --yes-moneyline and --no-moneyline are required")
        return StaticFairValue(devig_two_way(args.yes_moneyline, args.no_moneyline))
    if args.odds_sport:
        if args.execute_demo or args.websocket_demo:
            raise ValueError("Odds API fair values cannot safely map to Kalshi demo markets")
        return OddsConsensusFairValue(
            kalshi=client,
            odds=OddsClient.from_env(),
            market_ticker=args.ticker,
            sport=args.odds_sport,
            regions=args.odds_regions,
            bookmakers=args.odds_bookmakers,
            min_bookmakers=args.odds_min_bookmakers,
            max_age_seconds=args.odds_max_age,
            refresh_seconds=args.odds_refresh,
            match_window_hours=args.odds_match_window_hours,
            include_live=args.odds_include_live,
        )
    return StaticFairValue(args.fair_probability)


def _cmd_run(args: argparse.Namespace) -> None:
    if args.websocket_demo and not args.websocket:
        raise ValueError("--websocket-demo requires --websocket")
    if args.execute_demo:
        if args.acknowledge_risk != "DEMO_ONLY":
            raise ValueError("--execute-demo requires --acknowledge-risk DEMO_ONLY")
        if not os.getenv("KALSHI_API_KEY_ID") or not os.getenv("KALSHI_PRIVATE_KEY_PATH"):
            raise ValueError(
                "set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH for demo execution"
            )
        client = KalshiClient.from_env(demo=True)
    elif args.websocket:
        client = KalshiClient.from_env(demo=args.websocket_demo)
        if not client.has_credentials:
            raise ValueError(
                "--websocket requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH"
            )
    else:
        client = _client()

    source = _fair_source(args, client)
    market = client.get_market(args.ticker)
    price_grid = PriceGrid.from_market(market)
    strategy = MarketMakerStrategy(
        StrategyConfig(
            edge=args.edge_cents / Decimal("100"),
            order_size=args.order_size,
            max_inventory=args.max_inventory,
            inventory_skew=args.inventory_skew_cents / Decimal("100"),
            book_imbalance_weight=args.book_weight_cents / Decimal("100"),
            trade_imbalance_weight=args.trade_weight_cents / Decimal("100"),
        ),
        price_grid=price_grid,
    )
    if args.websocket:
        asyncio.run(
            run_streaming_bot(
                client=client,
                ticker=args.ticker,
                strategy=strategy,
                fair_value_source=source,
                dry_run_inventory=args.inventory,
                execute_demo=args.execute_demo,
                demo_data=args.websocket_demo,
                minimum_quote_interval_seconds=args.interval,
                iterations=args.iterations,
                trade_sample_size=args.trade_sample_size,
                json_output=args.json,
            )
        )
    else:
        run_bot(
            client=client,
            ticker=args.ticker,
            strategy=strategy,
            fair_value_source=source,
            dry_run_inventory=args.inventory,
            execute_demo=args.execute_demo,
            interval_seconds=args.interval,
            iterations=args.iterations,
            trade_sample_size=args.trade_sample_size,
            json_output=args.json,
        )


def _discrepancy_dict(item: Discrepancy) -> dict[str, object]:
    return {
        "ticker": item.ticker,
        "event_ticker": item.event_ticker,
        "outcome": item.outcome,
        "odds_event_id": item.odds_event_id,
        "fair_probability": str(item.fair_probability),
        "yes_bid": str(item.yes_bid),
        "yes_ask": str(item.yes_ask),
        "yes_bid_size": str(item.yes_bid_size),
        "yes_ask_size": str(item.yes_ask_size),
        "midpoint": str(item.midpoint),
        "action": item.action,
        "edge": str(item.edge),
        "bookmaker_count": item.bookmaker_count,
        "match_score": item.match_score,
        "market_type": item.market_type,
        "line": str(item.line) if item.line is not None else None,
        "spread": str(item.yes_ask - item.yes_bid),
        "direct_yes_bid": (
            str(item.direct_yes_bid) if item.direct_yes_bid is not None else None
        ),
        "direct_yes_ask": (
            str(item.direct_yes_ask) if item.direct_yes_ask is not None else None
        ),
        "effective_bid_route": item.effective_bid_route,
        "effective_ask_route": item.effective_ask_route,
        "complement_ticker": item.complement_ticker,
        "action_route": item.action_route,
    }


def _scan(args: argparse.Namespace) -> tuple[list[Discrepancy], OddsClient]:
    odds = OddsClient.from_env()
    results = scan_discrepancies(
        kalshi=_client(),
        odds=odds,
        series_ticker=args.series,
        sport=args.sport,
        market_type=args.market_type,
        regions=args.regions,
        bookmakers=args.bookmakers,
        min_edge=args.min_edge_cents / Decimal("100"),
        min_bookmakers=args.min_bookmakers,
        max_odds_age_seconds=args.max_odds_age,
        match_window_hours=args.match_window_hours,
        event_limit=args.event_limit,
        include_live=args.include_live,
    )
    return results, odds


def _print_scan(
    results: list[Discrepancy],
    odds: OddsClient,
    *,
    market_type: str,
    show_all: bool,
    json_output: bool,
) -> None:
    visible = results if show_all else [item for item in results if item.action != "NONE"]
    if json_output:
        for item in visible:
            print(json.dumps(_discrepancy_dict(item)))
        return
    quota = odds.quota
    print(
        f"Odds/Kalshi {market_type} scan "
        f"(opportunities {sum(item.action != 'NONE' for item in results)}, "
        f"matched markets {len(results)})"
    )
    if quota.remaining is not None:
        print(
            f"Odds API quota: {quota.remaining} remaining, "
            f"last request cost {quota.last_cost}\n"
        )
    else:
        print()
    print(
        table(
            (
                "TICKER",
                "OUTCOME",
                "FAIR",
                "BID",
                "ASK",
                "SPREAD",
                "SYNTH",
                "ACTION",
                "EDGE",
                "BOOKS",
            ),
            (
                (
                    item.ticker,
                    item.outcome,
                    f"{item.fair_probability * 100:.2f}%",
                    money(item.yes_bid),
                    money(item.yes_ask),
                    f"{(item.yes_ask - item.yes_bid) * 100:.2f}c",
                    (
                        "BOTH"
                        if item.complement_ticker
                        and ":NO" in str(item.effective_bid_route)
                        and ":NO" in str(item.effective_ask_route)
                        else "BID"
                        if item.complement_ticker
                        and ":NO" in str(item.effective_bid_route)
                        else "ASK"
                        if item.complement_ticker
                        and ":NO" in str(item.effective_ask_route)
                        else "-"
                    ),
                    item.action,
                    f"{item.edge * 100:+.2f}c",
                    item.bookmaker_count,
                )
                for item in visible
            ),
            right_align={2, 3, 4, 5, 8, 9},
        )
    )


def _cmd_scan(args: argparse.Namespace) -> None:
    results, odds = _scan(args)
    _print_scan(
        results,
        odds,
        market_type=args.market_type,
        show_all=args.show_all,
        json_output=args.json,
    )


def _parse_horizons(value: str) -> tuple[int, ...]:
    try:
        horizons = tuple(int(item) for item in value.split(",") if item)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("horizons must be comma-separated seconds") from exc
    if not horizons or any(item <= 0 for item in horizons):
        raise argparse.ArgumentTypeError("horizons must be positive comma-separated seconds")
    return horizons


def _cmd_paper(args: argparse.Namespace) -> None:
    recorder = PaperRecorder(
        args.output,
        markout_horizons_seconds=args.markout_horizons,
        signal_cooldown_seconds=args.signal_cooldown,
    )
    completed = 0
    while args.iterations == 0 or completed < args.iterations:
        results, odds = _scan(args)
        update = recorder.update(results)
        _print_scan(
            results,
            odds,
            market_type=args.market_type,
            show_all=args.show_all,
            json_output=args.json,
        )
        if not args.json:
            print(
                f"\nPaper log: {args.output} | new signals {update.signals_recorded} | "
                f"new markouts {update.markouts_recorded}"
            )
        completed += 1
        if args.iterations == 0 or completed < args.iterations:
            time.sleep(args.interval)
            if not args.json:
                print("\n" + "=" * 72 + "\n")


def _print_maker_paper(
    recorder: MakerPaperRecorder,
    update: MakerPaperUpdate,
    odds: OddsClient,
    *,
    json_output: bool,
) -> None:
    active = [item for item in recorder.quotes if item.is_open]
    if json_output:
        print(
            json.dumps(
                {
                    "quotes_opened": update.quotes_opened,
                    "fills_recorded": update.fills_recorded,
                    "quotes_completed": update.quotes_completed,
                    "quotes_cancelled": update.quotes_cancelled,
                    "markouts_recorded": update.markouts_recorded,
                    "active_quotes": update.active_quotes,
                    "open_inventory": str(update.open_inventory),
                }
            )
        )
        return
    print(
        "Maker paper update: "
        f"opened {update.quotes_opened}, fills {update.fills_recorded}, "
        f"completed {update.quotes_completed}, cancelled {update.quotes_cancelled}, "
        f"markouts {update.markouts_recorded}"
    )
    if odds.quota.remaining is not None:
        print(f"Odds API quota: {odds.quota.remaining} remaining")
    print(f"Active quotes: {update.active_quotes} | inventory {update.open_inventory}\n")
    print(
        table(
            ("TICKER", "OUTCOME", "FAIR", "BID", "ASK", "BID FILL", "ASK FILL"),
            (
                (
                    item.ticker,
                    item.outcome,
                    f"{item.fair_probability * 100:.2f}%",
                    money(item.bid_price),
                    money(item.ask_price),
                    number(item.bid_filled),
                    number(item.ask_filled),
                )
                for item in active
            ),
            right_align={2, 3, 4, 5, 6},
        )
    )


def _cmd_maker_paper(args: argparse.Namespace) -> None:
    recorder = MakerPaperRecorder(
        args.output,
        args.state,
        min_spread=args.min_spread_cents / Decimal("100"),
        max_spread=args.max_spread_cents / Decimal("100"),
        min_edge=args.min_edge_cents / Decimal("100"),
        tick=args.tick_cents / Decimal("100"),
        order_size=args.order_size,
        max_open_quotes=args.max_open_quotes,
        max_top_size=args.max_top_size,
        quote_lifetime_seconds=args.quote_lifetime,
        markout_horizons_seconds=args.markout_horizons,
    )
    trade_client = _client()
    completed = 0
    while args.iterations == 0 or completed < args.iterations:
        results, odds = _scan(args)
        update = recorder.update(results, trades=trade_client)
        _print_maker_paper(recorder, update, odds, json_output=args.json)
        completed += 1
        if args.iterations == 0 or completed < args.iterations:
            time.sleep(args.interval)
            if not args.json:
                print("\n" + "=" * 72 + "\n")


def _print_live_preflight(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"mode": "preview", **payload}))
        return
    print("Production live-order preview (no order submitted)\n")
    print(
        table(
            ("FIELD", "VALUE"),
            (
                ("Ticker", payload["ticker"]),
                ("Title", payload["title"]),
                ("Side", payload["side"]),
                ("Limit price", money(payload["price"])),
                ("Count", number(payload["count"])),
                ("Fair", f"{as_decimal(payload['fair_probability']) * 100:.2f}%"),
                ("Modeled edge", f"{as_decimal(payload['modeled_edge']) * 100:.2f}c"),
                ("Book", f"{money(payload['best_bid'])} / {money(payload['best_ask'])}"),
                ("Recent contracts", number(payload["recent_contracts"])),
                ("Queue ahead", number(payload["queue_ahead"])),
                ("Latest public trade", payload["latest_trade_time"]),
                ("Independent start", payload["external_start_time"]),
                ("Kalshi start", payload["market_occurrence_time"]),
                ("Pregame cutoff uses", payload["effective_start_time"]),
                ("Start offset", f"{payload['start_time_delta_seconds']} seconds"),
                ("Exchange expiration", payload["order_expiration_time"]),
                ("Estimated maker fee", money(payload["estimated_maker_fee"])),
                ("Maximum loss", money(payload["maximum_loss"])),
            ),
        )
    )
    auto_exit = payload.get("auto_exit")
    monitor = payload.get("monitored_entry")
    if isinstance(monitor, dict):
        print("\nBounded fair-value monitor")
        print(
            table(
                ("FIELD", "VALUE"),
                (
                    ("Cancel below fair", f"{as_decimal(monitor['cancel_below']) * 100:.2f}%"),
                    ("Odds poll interval", f"{monitor['poll_interval_seconds']} seconds"),
                    ("Maximum resting", f"{monitor['max_rest_seconds']} seconds"),
                    ("Maximum odds age", f"{monitor['max_odds_age_seconds']} seconds"),
                    ("API failure grace", f"{monitor['failure_grace_seconds']} seconds"),
                    ("Adverse Kalshi move", money(monitor["adverse_move"])),
                    ("Order handling", "cancel only; never amend or replace"),
                ),
            )
        )
    if isinstance(auto_exit, dict):
        print("\nPreauthorized bounded exit")
        print(
            table(
                ("FIELD", "VALUE"),
                (
                    ("Target ask", money(auto_exit["target_price"])),
                    ("Target wait", "until fill, adverse move, or pregame cutoff"),
                    ("Hard floor", money(auto_exit["floor_price"])),
                    ("Adverse trigger", money(auto_exit.get("adverse_move"))),
                    ("Fallback", "one reduce-only IOC only after adverse move"),
                ),
            )
        )
    print("\nExecution requires explicit production credentials and all live safety gates.")


def _bounded_exit_from_args(
    args: argparse.Namespace,
    *,
    external_start_time: datetime,
) -> BoundedExitRequest | None:
    values = (args.auto_exit_target_cents, args.auto_exit_floor_cents)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError(
            "auto-exit requires both --auto-exit-target-cents "
            "and --auto-exit-floor-cents"
        )
    if args.side != "bid":
        raise ValueError("bounded auto-exit is available only for a YES entry bid")
    assert args.auto_exit_target_cents is not None
    assert args.auto_exit_floor_cents is not None
    request = BoundedExitRequest(
        ticker=args.ticker,
        target_price=args.auto_exit_target_cents / Decimal("100"),
        floor_price=args.auto_exit_floor_cents / Decimal("100"),
        count=args.count,
        external_start_time=external_start_time,
        target_wait_seconds=args.auto_exit_wait_seconds,
    )
    request.validate()
    if request.target_price <= args.price_cents / Decimal("100"):
        raise ValueError("auto-exit target must be above the entry price")
    return request


def _monitor_config(
    args: argparse.Namespace,
    *,
    expiration_seconds: int | None = None,
) -> MonitoredEntryConfig:
    config = MonitoredEntryConfig(
        poll_interval_seconds=args.monitor_poll_seconds,
        max_rest_seconds=expiration_seconds or args.expiration_seconds,
        max_odds_age_seconds=min(args.odds_max_age, 60),
        failure_grace_seconds=args.monitor_failure_grace_seconds,
        rest_reconcile_seconds=args.monitor_reconcile_seconds,
        adverse_move=args.adverse_move_cents / Decimal("100"),
    )
    config.validate()
    if config.poll_interval_seconds < 25:
        raise ValueError("live monitored-entry polling cannot be faster than 25 seconds")
    return config


def _entry_expiration_seconds(
    args: argparse.Namespace,
    fair: OddsFairSnapshot,
    *,
    now: datetime | None = None,
) -> int:
    if not args.monitor_until_pregame:
        return args.expiration_seconds
    cutoff = fair.event_commence_time.astimezone(UTC) - timedelta(minutes=5)
    seconds = int((cutoff - (now or datetime.now(UTC))).total_seconds())
    if seconds < 10:
        raise ValueError("pregame monitoring cutoff is less than 10 seconds away")
    if seconds > 12 * 60 * 60:
        raise ValueError("pregame monitoring cannot exceed 12 hours")
    return seconds


def _parse_live_time(value: str | None, *, argument: str) -> datetime:
    if not value:
        raise ValueError(f"{argument} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{argument} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{argument} must include a timezone")
    return parsed.astimezone(UTC)


def _live_fair_snapshot(
    args: argparse.Namespace,
    client: KalshiClient,
    *,
    now: datetime | None = None,
) -> OddsFairSnapshot:
    choices = sum((args.fair_probability is not None, args.odds_sport is not None))
    if choices != 1:
        raise ValueError("choose exactly one live fair source: --fair-probability or --odds-sport")
    if args.odds_sport:
        return _live_odds_source(args, client).snapshot(args.ticker)

    observed_at = _parse_live_time(
        args.fair_observed_at,
        argument="--fair-observed-at",
    )
    external_start = _parse_live_time(
        args.external_start_time,
        argument="--external-start-time",
    )
    age = ((now or datetime.now(UTC)) - observed_at).total_seconds()
    if age < -5 or age > 60:
        raise ValueError("manual fair value must have been observed within the last 60 seconds")
    assert args.fair_probability is not None
    return OddsFairSnapshot(
        probability=args.fair_probability,
        event_commence_time=external_start,
        observed_at=observed_at,
    )


def _live_odds_source(
    args: argparse.Namespace,
    client: KalshiClient,
) -> OddsConsensusFairValue:
    if not args.odds_sport:
        raise ValueError("monitored entry requires --odds-sport")
    return OddsConsensusFairValue(
        kalshi=client,
        odds=OddsClient.from_env(),
        market_ticker=args.ticker,
        sport=args.odds_sport,
        regions=args.odds_regions,
        bookmakers=args.odds_bookmakers,
        min_bookmakers=max(2, args.odds_min_bookmakers),
        max_age_seconds=min(args.odds_max_age, 60),
        refresh_seconds=1,
        match_window_hours=args.odds_match_window_hours,
        include_live=False,
    )


def _production_client() -> KalshiClient:
    client = KalshiClient.from_production_env()
    if not client.has_credentials:
        raise ValueError(
            "set KALSHI_PROD_API_KEY_ID and KALSHI_PROD_PRIVATE_KEY_PATH for live access"
        )
    assert client.private_key_path is not None
    if not client.private_key_path.is_file():
        raise ValueError("KALSHI_PROD_PRIVATE_KEY_PATH does not point to a file")
    if client.private_key_path.stat().st_mode & 0o077:
        raise ValueError("production private key permissions are too broad; run chmod 600")
    return client


def _cmd_live_order(args: argparse.Namespace) -> None:
    limits = LiveRiskLimits.from_env()
    if args.monitor_entry and (not args.odds_sport or args.fair_probability is not None):
        raise ValueError("--monitor-entry is available only with --odds-sport")
    if args.monitor_until_pregame and not args.monitor_entry:
        raise ValueError("--monitor-until-pregame requires --monitor-entry")
    if args.monitor_entry and args.side != "bid":
        raise ValueError("--monitor-entry is available only for a YES entry bid")
    if not args.execute_live:
        client = _client()
        odds_source = _live_odds_source(args, client) if args.monitor_entry else None
        fair = (
            odds_source.snapshot(args.ticker)
            if odds_source is not None
            else _live_fair_snapshot(args, client)
        )
        bounded_exit = _bounded_exit_from_args(
            args,
            external_start_time=fair.event_commence_time,
        )
        expiration_seconds = _entry_expiration_seconds(args, fair)
        request = LiveOrderRequest(
            ticker=args.ticker,
            side=args.side,
            price=args.price_cents / Decimal("100"),
            count=args.count,
            fair_probability=fair.probability,
            external_start_time=fair.event_commence_time,
            expiration_seconds=expiration_seconds,
            monitor_until_pregame=args.monitor_until_pregame,
        )
        preview = preflight_live_order(
            client,
            request,
            limits,
            authenticated=False,
        )
        payload = preview.as_dict()
        if bounded_exit is not None:
            payload["auto_exit"] = {
                "target_price": str(bounded_exit.target_price),
                "floor_price": str(bounded_exit.floor_price),
                "target_wait_seconds": bounded_exit.target_wait_seconds,
                "mode": "fair_and_kalshi_adverse_move",
                "adverse_move": str(args.adverse_move_cents / Decimal("100")),
            }
        if args.monitor_entry:
            config = _monitor_config(args, expiration_seconds=expiration_seconds)
            payload["monitored_entry"] = {
                "cancel_below": str(request.price + limits.min_edge),
                "poll_interval_seconds": config.poll_interval_seconds,
                "max_rest_seconds": config.max_rest_seconds,
                "max_odds_age_seconds": config.max_odds_age_seconds,
                "failure_grace_seconds": config.failure_grace_seconds,
                "adverse_move": str(config.adverse_move),
                "until_pregame": args.monitor_until_pregame,
            }
        _print_live_preflight(payload, json_output=args.json)
        return

    if args.acknowledge_risk != LIVE_ACKNOWLEDGEMENT:
        raise ValueError(
            f"--execute-live requires --acknowledge-risk {LIVE_ACKNOWLEDGEMENT}"
        )
    if args.confirm_ticker != args.ticker:
        raise ValueError("--confirm-ticker must exactly match --ticker")
    if not args.intent_id:
        raise ValueError("--execute-live requires a unique --intent-id")
    if (
        args.monitor_entry
        and args.acknowledge_monitored_entry != MONITORED_ENTRY_ACKNOWLEDGEMENT
    ):
        raise ValueError(
            "monitored live entry requires --acknowledge-monitored-entry "
            f"{MONITORED_ENTRY_ACKNOWLEDGEMENT}"
        )
    client = _production_client()

    odds_source = _live_odds_source(args, client) if args.monitor_entry else None
    fair = (
        odds_source.snapshot(args.ticker)
        if odds_source is not None
        else _live_fair_snapshot(args, client)
    )
    bounded_exit = _bounded_exit_from_args(
        args,
        external_start_time=fair.event_commence_time,
    )
    if (
        bounded_exit is not None
        and args.acknowledge_auto_exit != BOUNDED_EXIT_ACKNOWLEDGEMENT
    ):
        raise ValueError(
            "bounded auto-exit requires --acknowledge-auto-exit "
            f"{BOUNDED_EXIT_ACKNOWLEDGEMENT}"
        )
    expiration_seconds = _entry_expiration_seconds(args, fair)
    request = LiveOrderRequest(
        ticker=args.ticker,
        side=args.side,
        price=args.price_cents / Decimal("100"),
        count=args.count,
        fair_probability=fair.probability,
        external_start_time=fair.event_commence_time,
        expiration_seconds=expiration_seconds,
        monitor_until_pregame=args.monitor_until_pregame,
    )

    if odds_source is not None:
        result = asyncio.run(
            execute_monitored_live_order(
                client,
                request,
                limits,
                fair_source=odds_source,
                initial_snapshot=fair,
                stream=KalshiWebSocket(client),
                config=_monitor_config(args, expiration_seconds=expiration_seconds),
                intent_id=args.intent_id,
                audit_log=LiveAuditLog(args.audit_log),
            )
        )
    else:
        result = execute_live_order(
            client,
            request,
            limits,
            intent_id=args.intent_id,
            audit_log=LiveAuditLog(args.audit_log),
            wait_seconds=args.wait_seconds,
            poll_seconds=args.poll_seconds,
        )
    if bounded_exit is not None:
        position = as_decimal(client.get_position(args.ticker, subaccount=0))
        if position <= Decimal("0"):
            result["auto_exit"] = {"result": "not_needed_no_position"}
        elif odds_source is not None:
            exit_fair = odds_source.refresh_snapshot(args.ticker)
            result["auto_exit"] = asyncio.run(
                execute_fair_aware_bounded_exit(
                    client,
                    bounded_exit,
                    fair_source=odds_source,
                    initial_snapshot=exit_fair,
                    stream=KalshiWebSocket(client),
                    config=FairAwareExitConfig(
                        fair_poll_seconds=args.monitor_poll_seconds,
                        rest_reconcile_seconds=args.monitor_reconcile_seconds,
                        failure_grace_seconds=args.monitor_failure_grace_seconds,
                        adverse_move=args.adverse_move_cents / Decimal("100"),
                    ),
                    intent_id=args.intent_id,
                    audit_log=LiveAuditLog(args.audit_log),
                )
            )
        else:
            result["auto_exit"] = execute_bounded_exit(
                client,
                bounded_exit,
                intent_id=args.intent_id,
                audit_log=LiveAuditLog(args.audit_log),
                poll_seconds=args.poll_seconds,
            )
    if args.json:
        print(json.dumps(result))
    else:
        print(f"Live order result: {result['result']}")
        if result.get("order_id"):
            print(f"Order ID: {result['order_id']}")
        if result.get("auto_exit"):
            auto_exit = result["auto_exit"]
            assert isinstance(auto_exit, dict)
            print(f"Bounded exit result: {auto_exit['result']}")
            if auto_exit.get("fallback_price"):
                print(f"Fallback price: {money(auto_exit['fallback_price'])}")
            if auto_exit.get("remaining_position") is not None:
                print(f"Remaining position: {number(auto_exit['remaining_position'])}")
        print(f"Audit log: {args.audit_log}")


def _cmd_live_status(args: argparse.Namespace) -> None:
    client = _production_client()
    balance = client.get_balance(subaccount=0)
    resting = client.get_orders(status="resting", subaccount=0, limit=1000)
    payload: dict[str, object] = {
        "available_balance_dollars": str(
            as_decimal(balance.get("balance", 0)) / Decimal("100")
        ),
        "portfolio_value_dollars": str(
            as_decimal(balance.get("portfolio_value", 0)) / Decimal("100")
        ),
        "resting_orders": resting,
    }
    if args.ticker:
        payload["ticker"] = args.ticker
        payload["position"] = client.get_position(args.ticker, subaccount=0)
    if args.json:
        print(json.dumps(payload))
        return
    print("Production account status (read-only)\n")
    print(
        table(
            ("FIELD", "VALUE"),
            (
                ("Available balance", money(payload["available_balance_dollars"])),
                ("Portfolio value", money(payload["portfolio_value_dollars"])),
                ("Resting orders", len(resting)),
                ("Ticker", payload.get("ticker")),
                ("Ticker position", payload.get("position")),
            ),
        )
    )
    if resting:
        print("\nResting production orders")
        print(
            table(
                ("ORDER ID", "TICKER", "BOOK SIDE", "PRICE", "REMAINING"),
                (
                    (
                        item.get("order_id"),
                        item.get("ticker"),
                        item.get("book_side"),
                        money(item.get("yes_price_dollars")),
                        number(item.get("remaining_count_fp")),
                    )
                    for item in resting
                ),
            )
        )


def _cmd_live_cancel(args: argparse.Namespace) -> None:
    if args.acknowledge_risk != "CANCEL_REAL_ORDER":
        raise ValueError("live-cancel requires --acknowledge-risk CANCEL_REAL_ORDER")
    if args.confirm_order_id != args.order_id:
        raise ValueError("--confirm-order-id must exactly match --order-id")
    client = _production_client()
    response = client.cancel_order(args.order_id, subaccount=0)
    LiveAuditLog(args.audit_log).append(
        "operator_cancelled",
        {"order_id": args.order_id, "response": response},
    )
    if args.json:
        print(json.dumps(response))
    else:
        print(f"Cancelled production order: {args.order_id}")
        print(f"Reduced remaining count: {response.get('reduced_by', '-')}")
        print(f"Audit log: {args.audit_log}")


class _RFQAudit:
    def __init__(self, path: Path, *, json_output: bool) -> None:
        self.log = LiveAuditLog(path)
        self.json_output = json_output

    def append(self, event: str, payload: dict[str, object]) -> None:
        self.log.append(event, payload)
        if self.json_output:
            print(json.dumps({"event": event, **payload}), flush=True)
            return
        if event in {
            "rfq_quote_dry_run",
            "rfq_quote_submitted",
            "rfq_quote_confirmed",
            "rfq_quote_executed",
            "rfq_quote_ambiguous",
            "rfq_quote_skipped",
            "rfq_unsupported_summary",
            "rfq_confirmation_withheld",
        }:
            ticker = payload.get("ticker", "-")
            rfq_id = payload.get("rfq_id", "-")
            detail = (
                f"{payload.get('messages', 0)} unsupported: {payload.get('reasons', {})}"
                if event == "rfq_unsupported_summary"
                else payload.get("reason")
                or f"YES {payload.get('yes_bid', '-')} / NO {payload.get('no_bid', '-')}"
            )
            print(f"{event}: {ticker} {rfq_id} — {detail}", flush=True)


RFQ_CANARY_COLLECTION = "KXMVESPORTSMULTIGAMEEXTENDED-R"
RFQ_CANARY_SERIES = "KXMLBGAME"
RFQ_CANARY_SPORT = "baseball_mlb"


def _validate_rfq_live_canary(args: argparse.Namespace) -> None:
    if not args.canary_live:
        raise ValueError("production RFQ execution requires --canary-live")
    if args.fair_file is not None or args.series != RFQ_CANARY_SERIES:
        raise ValueError(f"live canary requires Odds API fair values from {RFQ_CANARY_SERIES}")
    if args.odds_sport != RFQ_CANARY_SPORT:
        raise ValueError(f"live canary requires --odds-sport {RFQ_CANARY_SPORT}")
    if set(args.allow_collection) != {RFQ_CANARY_COLLECTION} or args.allow_ticker:
        raise ValueError(
            f"live canary must allow only collection {RFQ_CANARY_COLLECTION}"
        )
    if not args.combo_only or args.allow_live:
        raise ValueError("live canary requires --combo-only and forbids --allow-live")
    if not args.contracts_only:
        raise ValueError("live canary requires --contracts-only")
    if args.edge_percent < Decimal("1.5"):
        raise ValueError("live canary requires at least 1.5% net modeled edge")
    minimum_fee_percent = KALSHI_MAKER_FEE_RATE * Decimal("100")
    if args.maker_fee_rate_percent < minimum_fee_percent:
        raise ValueError(f"live canary maker-fee rate must be at least {minimum_fee_percent}%")
    if args.subaccount == 0:
        if not args.allow_primary_account_canary:
            raise ValueError(
                "live canary requires a dedicated numbered subaccount unless "
                "--allow-primary-account-canary is set"
            )
    elif args.allow_primary_account_canary:
        raise ValueError("--allow-primary-account-canary requires --subaccount 0")
    elif not 1 <= args.subaccount <= 32:
        raise ValueError("live canary requires subaccount 0 or a numbered subaccount 1-32")
    if not ZERO < args.min_contracts <= args.max_contracts <= Decimal("10"):
        raise ValueError("live canary contract limits must be positive and capped at 10")
    if args.max_position > Decimal("10") or args.max_notional > Decimal("10"):
        raise ValueError("live canary position and notional limits must be capped at 10")
    if args.max_session_contracts != Decimal("10"):
        raise ValueError("live canary requires a ten-contract session-wide execution cap")
    if args.max_session_executions != 1:
        raise ValueError("live canary requires a one-execution session-wide cap")
    if args.max_active_quotes != 1 or args.max_inflight_rfqs != 1:
        raise ValueError("live canary requires one active quote and one in-flight handler")
    if args.min_legs < 2 or args.max_legs > 6:
        raise ValueError("live canary allows only 2-6 independent moneyline legs")
    if (
        not math.isfinite(args.max_fair_age)
        or not math.isfinite(args.max_quote_latency)
        or args.max_fair_age > 60
        or args.max_quote_latency > 1
    ):
        raise ValueError("live canary requires fair age <=60s and quote latency <=1s")
    if args.reconcile_seconds > 15:
        raise ValueError("live canary reconciliation interval cannot exceed 15 seconds")
    if not math.isfinite(args.seconds) or args.seconds <= 0 or args.seconds > 30 * 60:
        raise ValueError("live canary runtime must be between 1 second and 30 minutes")


def _preflight_rfq_live_canary(client: KalshiClient, args: argparse.Namespace) -> None:
    keys = client.get_api_keys()
    current = next((item for item in keys if item.get("api_key_id") == client.api_key_id), None)
    if current is None:
        raise ValueError("current production API key was not returned by Kalshi")
    if "rfq" not in str(current.get("name", "")).casefold():
        raise ValueError("live canary requires a dedicated API key whose name contains 'rfq'")
    if set(current.get("scopes") or ()) != {"read", "write"}:
        raise ValueError("live canary API key must have exactly read and write scopes")
    if client.private_key_path is None:
        raise ValueError("live canary private-key path is missing")
    key_mode = stat.S_IMODE(client.private_key_path.stat().st_mode)
    if key_mode & 0o077:
        raise ValueError("live canary private-key file must not be accessible by group or others")

    subaccounts = client.get_subaccount_balances()
    if not any(int(item.get("subaccount_number", -1)) == args.subaccount for item in subaccounts):
        raise ValueError(f"canary subaccount {args.subaccount} does not exist")
    balance = as_decimal(client.get_balance(subaccount=args.subaccount).get("balance", "0"))
    if balance <= ZERO:
        raise ValueError("live canary account must contain at least $0.01")
    if args.subaccount != 0 and balance > Decimal("1000"):
        raise ValueError("live canary numbered subaccount must contain no more than $10.00")
    if client.get_orders(status="resting", subaccount=args.subaccount, limit=1000):
        raise ValueError("live canary subaccount already has resting orders")
    if client.get_positions(subaccount=args.subaccount, limit=1000):
        raise ValueError("live canary subaccount already has positions")


def _rfq_client(args: argparse.Namespace) -> tuple[KalshiClient, bool, bool]:
    if args.execute_demo and args.execute_live:
        raise ValueError("choose at most one of --execute-demo and --execute-live")
    if args.execute_demo:
        if args.acknowledge_risk != "DEMO_ONLY":
            raise ValueError("--execute-demo requires --acknowledge-risk DEMO_ONLY")
        client = KalshiClient.from_env(demo=True)
        return client, True, True
    if args.execute_live:
        if args.acknowledge_risk != RFQ_LIVE_ACKNOWLEDGEMENT:
            raise ValueError(
                "--execute-live requires --acknowledge-risk "
                f"{RFQ_LIVE_ACKNOWLEDGEMENT}"
            )
        if os.getenv("KALSHI_RFQ_LIVE_ENABLED") != RFQ_LIVE_ENABLE_TOKEN:
            raise ValueError(
                "set KALSHI_RFQ_LIVE_ENABLED=I_UNDERSTAND_RFQ_REAL_MONEY "
                "for production RFQ execution"
            )
        if not args.allow_ticker and not args.allow_collection:
            raise ValueError(
                "production RFQ execution requires --allow-ticker or --allow-collection"
            )
        _validate_rfq_live_canary(args)
        client = _production_client()
        _preflight_rfq_live_canary(client, args)
        return client, False, True
    if args.demo:
        return KalshiClient.from_env(demo=True), True, False
    return _production_client(), False, False


def _cmd_rfq_maker(args: argparse.Namespace) -> None:
    client, demo, execute = _rfq_client(args)
    if not client.has_credentials:
        raise ValueError("RFQ WebSocket access requires Kalshi API credentials")
    fair_choices = sum((args.fair_file is not None, args.odds_sport is not None))
    if fair_choices != 1:
        raise ValueError("choose exactly one RFQ fair source: --fair-file or --odds-sport")
    if args.fair_file is not None:
        fair_book = JsonMoneylineFairBook(
            args.fair_file,
            refresh_seconds=args.fair_file_refresh,
        )
    else:
        if demo:
            raise ValueError("Odds API moneylines cannot safely map to Kalshi demo markets")
        if not args.series:
            raise ValueError("--odds-sport requires --series")
        fair_book = OddsMoneylineFairBook(
            kalshi=client,
            odds=OddsClient.from_env(),
            series_ticker=args.series,
            sport=args.odds_sport,
            regions=args.odds_regions,
            bookmakers=args.odds_bookmakers,
            min_bookmakers=max(2, args.odds_min_bookmakers),
            max_source_age_seconds=args.odds_max_age,
            match_window_hours=args.odds_match_window_hours,
            refresh_seconds=args.odds_refresh,
        )
    maker = RFQMaker(
        client=client,
        stream=KalshiRFQWebSocket(
            client,
            demo=demo,
            shard_factor=args.shard_factor,
            shard_key=args.shard_key,
        ),
        fair_book=fair_book,
        config=RFQMakerConfig(
            edge_rate=args.edge_percent / Decimal("100"),
            maker_fee_rate=args.maker_fee_rate_percent / Decimal("100"),
            min_contracts=args.min_contracts,
            max_contracts=args.max_contracts,
            max_abs_position=args.max_position,
            max_notional=args.max_notional,
            max_session_contracts=args.max_session_contracts,
            max_session_executions=args.max_session_executions,
            max_active_quotes=args.max_active_quotes,
            max_fair_age_seconds=args.max_fair_age,
            reconcile_seconds=args.reconcile_seconds,
            subaccount=args.subaccount,
            allow_live_games=args.allow_live,
            min_legs=args.min_legs,
            max_legs=args.max_legs,
            max_inflight_rfqs=args.max_inflight_rfqs,
            max_quote_latency_seconds=args.max_quote_latency,
            combo_only=args.combo_only,
            contracts_only=args.contracts_only,
            require_subaccount_metadata=args.execute_live,
        ),
        audit_log=_RFQAudit(args.audit_log, json_output=args.json),
        execute=execute,
        fill_ledger=MarkdownRFQFillLedger(args.fill_ledger),
        allowed_tickers=set(args.allow_ticker or ()),
        allowed_collections=set(args.allow_collection or ()),
    )
    mode = (
        "production execution"
        if args.execute_live
        else "demo execution"
        if execute
        else "dry run"
    )
    if not args.json:
        print(
            f"Starting moneyline RFQ maker ({mode}); minimum edge "
            f"{args.edge_percent}% of fair value net of modeled maker fees. "
            f"Audit: {args.audit_log}",
            flush=True,
        )
    asyncio.run(maker.run(seconds=args.seconds, max_messages=args.messages))


def _format_stream_update(update: StreamUpdate, ticker: str) -> str | None:
    message = update.message
    message_type = str(message.get("type", ""))
    payload = message.get("msg", {})
    timestamp_ms = payload.get("ts_ms")
    timestamp = (
        datetime.fromtimestamp(timestamp_ms / 1000, UTC).strftime("%H:%M:%S.%f")[:-3]
        if isinstance(timestamp_ms, int)
        else datetime.now(UTC).strftime("%H:%M:%S.%f")[:-3]
    )
    if message_type in {"orderbook_snapshot", "orderbook_delta"}:
        book = update.state.orderbook(ticker)
        if book:
            return (
                f"[{timestamp}] BOOK  bid {display_level(book.best_bid)}  "
                f"ask {display_level(book.best_ask)}"
            )
    if message_type == "trade":
        return (
            f"[{timestamp}] TRADE {payload.get('taker_book_side', '-')} "
            f"{money(payload.get('yes_price_dollars'))} x "
            f"{number(payload.get('count_fp'))}"
        )
    if message_type in {"user_order", "fill", "market_position"}:
        return f"[{timestamp}] {message_type.upper()} {payload}"
    return None


async def _watch_stream(args: argparse.Namespace) -> None:
    client = KalshiClient.from_env(demo=args.demo)
    stream = KalshiWebSocket(client, demo=args.demo)
    emitted = 0

    async def consume() -> None:
        nonlocal emitted
        async for update in stream.events([args.ticker]):
            if args.json:
                print(json.dumps(update.message))
                emitted += 1
            else:
                line = _format_stream_update(update, args.ticker)
                if line:
                    print(line)
                    emitted += 1
            if args.messages and emitted >= args.messages:
                return

    if args.seconds:
        try:
            async with asyncio.timeout(args.seconds):
                await consume()
        except TimeoutError:
            return
    else:
        await consume()


def _cmd_stream(args: argparse.Namespace) -> None:
    asyncio.run(_watch_stream(args))


def _add_scan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--series", required=True, help="Kalshi series, e.g. KXMLBGAME")
    parser.add_argument("--sport", required=True, help="Odds API sport, e.g. baseball_mlb")
    parser.add_argument(
        "--market-type",
        choices=("h2h", "totals"),
        default="h2h",
        help="compare game winners or exact full-game over/under lines",
    )
    parser.add_argument("--regions", default="us")
    parser.add_argument(
        "--bookmakers",
        default=DEFAULT_SHARP_BOOKMAKERS,
        help=(
            "comma-separated bookmaker keys "
            f"(default: {DEFAULT_SHARP_BOOKMAKERS})"
        ),
    )
    parser.add_argument("--min-edge-cents", type=_decimal, default=Decimal("3"))
    parser.add_argument("--min-bookmakers", type=int, default=2)
    parser.add_argument("--max-odds-age", type=float, default=180)
    parser.add_argument("--match-window-hours", type=float, default=6)
    parser.add_argument("--event-limit", type=int, default=50)
    parser.add_argument(
        "--include-live",
        action="store_true",
        help="include in-play games despite slower external-odds updates",
    )
    parser.add_argument("--show-all", action="store_true")
    parser.add_argument("--json", action="store_true", help="emit JSON lines")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kalshi-mm",
        description=(
            "Explore Kalshi sports markets and generate guarded sample market-maker quotes."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    series = subparsers.add_parser("series", help="list sports series")
    series.add_argument("--tag", help="Kalshi tag, for example Baseball or Basketball")
    series.add_argument("--query", help="local case-insensitive title/ticker filter")
    series.add_argument("--limit", type=int, default=20)
    series.add_argument("--json", action="store_true", help="emit JSON lines")
    series.set_defaults(func=_cmd_series)

    markets = subparsers.add_parser("markets", help="list open markets in a sports series")
    markets.add_argument("--series", required=True, help="series ticker, for example KXMLBGAME")
    markets.add_argument("--limit", type=int, default=20)
    markets.add_argument("--json", action="store_true", help="emit JSON lines")
    markets.set_defaults(func=_cmd_markets)

    book = subparsers.add_parser("book", help="show a normalized YES-price orderbook")
    book.add_argument("--ticker", required=True)
    book.add_argument("--depth", type=int, default=20)
    book.add_argument("--json", action="store_true", help="emit JSON")
    book.set_defaults(func=_cmd_book)

    run = subparsers.add_parser("run", help="generate quotes; dry-run is the default")
    run.add_argument("--ticker", required=True)
    fair = run.add_argument_group("fair-value source")
    fair.add_argument("--fair-probability", type=_decimal)
    fair.add_argument("--fair-file", type=Path, help='JSON map such as {"TICKER": 0.54}')
    fair.add_argument("--yes-moneyline", type=_moneyline)
    fair.add_argument("--no-moneyline", type=_moneyline)
    fair.add_argument("--odds-sport", help="live Odds API sport key, e.g. baseball_mlb")
    fair.add_argument("--odds-regions", default="us")
    fair.add_argument(
        "--odds-bookmakers",
        default=DEFAULT_SHARP_BOOKMAKERS,
        help=(
            "comma-separated bookmaker keys "
            f"(default: {DEFAULT_SHARP_BOOKMAKERS})"
        ),
    )
    fair.add_argument("--odds-min-bookmakers", type=int, default=2)
    fair.add_argument("--odds-max-age", type=float, default=180)
    fair.add_argument("--odds-refresh", type=float, default=60)
    fair.add_argument("--odds-match-window-hours", type=float, default=6)
    fair.add_argument(
        "--odds-include-live",
        action="store_true",
        help="allow in-play external odds despite slower updates",
    )
    strategy = run.add_argument_group("strategy")
    strategy.add_argument("--edge-cents", type=_decimal, default=Decimal("2"))
    strategy.add_argument("--order-size", type=_decimal, default=Decimal("1"))
    strategy.add_argument("--max-inventory", type=_decimal, default=Decimal("10"))
    strategy.add_argument("--inventory", type=_decimal, default=Decimal("0"))
    strategy.add_argument("--inventory-skew-cents", type=_decimal, default=Decimal("0.2"))
    strategy.add_argument("--book-weight-cents", type=_decimal, default=Decimal("0.5"))
    strategy.add_argument("--trade-weight-cents", type=_decimal, default=Decimal("0.5"))
    run.add_argument("--trade-sample-size", type=int, default=100)
    run.add_argument("--interval", type=float, default=2.0)
    run.add_argument("--iterations", type=int, default=1, help="0 runs until interrupted")
    run.add_argument("--json", action="store_true", help="emit JSON lines")
    streaming = run.add_argument_group("WebSocket market data")
    streaming.add_argument(
        "--websocket",
        action="store_true",
        help="use sequence-checked Kalshi WebSocket orderbooks",
    )
    streaming.add_argument(
        "--websocket-demo",
        action="store_true",
        help="connect to demo WebSocket data (requires demo credentials)",
    )
    execution = run.add_argument_group("guarded demo execution")
    execution.add_argument("--execute-demo", action="store_true")
    execution.add_argument("--acknowledge-risk")
    run.set_defaults(func=_cmd_run)

    scan = subparsers.add_parser(
        "scan",
        help="compare no-vig sportsbook prices with exact Kalshi contracts",
    )
    _add_scan_arguments(scan)
    scan.set_defaults(func=_cmd_scan)

    paper = subparsers.add_parser(
        "paper",
        help="record discrepancy signals and future midpoint markouts",
    )
    _add_scan_arguments(paper)
    paper.add_argument("--output", type=Path, default=Path("logs/paper.jsonl"))
    paper.add_argument("--markout-horizons", type=_parse_horizons, default=(60, 300))
    paper.add_argument("--signal-cooldown", type=int, default=300)
    paper.add_argument("--interval", type=float, default=60)
    paper.add_argument("--iterations", type=int, default=1, help="0 runs until interrupted")
    paper.set_defaults(func=_cmd_paper)

    maker_paper = subparsers.add_parser(
        "maker-paper",
        help="simulate passive two-sided quotes and infer fills from later public trades",
    )
    _add_scan_arguments(maker_paper)
    maker_paper.add_argument("--min-spread-cents", type=_decimal, default=Decimal("4"))
    maker_paper.add_argument("--max-spread-cents", type=_decimal, default=Decimal("15"))
    maker_paper.add_argument("--tick-cents", type=_decimal, default=Decimal("1"))
    maker_paper.add_argument("--order-size", type=_decimal, default=Decimal("1"))
    maker_paper.add_argument("--max-open-quotes", type=int, default=5)
    maker_paper.add_argument(
        "--max-top-size",
        type=_decimal,
        default=Decimal("500"),
        help="exclude markets with more contracts at either current top level; 0 disables",
    )
    maker_paper.add_argument("--quote-lifetime", type=int, default=600)
    maker_paper.add_argument(
        "--markout-horizons", type=_parse_horizons, default=(60, 300)
    )
    maker_paper.add_argument("--state", type=Path, default=Path("logs/maker-paper-state.json"))
    maker_paper.add_argument("--output", type=Path, default=Path("logs/maker-paper.jsonl"))
    maker_paper.add_argument("--interval", type=float, default=60)
    maker_paper.add_argument(
        "--iterations", type=int, default=1, help="0 runs until interrupted"
    )
    maker_paper.set_defaults(min_edge_cents=Decimal("1"), func=_cmd_maker_paper)

    live_order = subparsers.add_parser(
        "live-order",
        help="preview or submit one guarded, pregame, production post-only order",
    )
    live_order.add_argument("--ticker", required=True)
    live_order.add_argument("--side", choices=("bid", "ask"), required=True)
    live_order.add_argument("--price-cents", type=_decimal, required=True)
    live_order.add_argument("--count", type=_decimal, default=Decimal("1"))
    live_order.add_argument("--fair-probability", type=_decimal)
    live_order.add_argument("--fair-observed-at")
    live_order.add_argument(
        "--external-start-time",
        help="independent event start (required with a manual fair value)",
    )
    live_order.add_argument("--odds-sport", help="Odds API sport key, e.g. soccer_usa_mls")
    live_order.add_argument("--odds-regions", default="us")
    live_order.add_argument("--odds-bookmakers", default=DEFAULT_SHARP_BOOKMAKERS)
    live_order.add_argument("--odds-min-bookmakers", type=int, default=2)
    live_order.add_argument("--odds-max-age", type=float, default=180)
    live_order.add_argument("--odds-match-window-hours", type=float, default=6)
    live_order.add_argument("--expiration-seconds", type=int, default=600)
    live_order.add_argument("--wait-seconds", type=int, default=60)
    live_order.add_argument("--poll-seconds", type=float, default=2)
    live_order.add_argument(
        "--monitor-entry",
        action="store_true",
        help="monitor sportsbook fair value and cancel the unchanged resting entry",
    )
    live_order.add_argument(
        "--monitor-until-pregame",
        action="store_true",
        help="rest the monitored entry until five minutes before the independent start",
    )
    live_order.add_argument("--monitor-poll-seconds", type=float, default=30)
    live_order.add_argument("--monitor-failure-grace-seconds", type=float, default=15)
    live_order.add_argument("--monitor-reconcile-seconds", type=float, default=10)
    live_order.add_argument(
        "--adverse-move-cents",
        type=_decimal,
        default=Decimal("2"),
        help="fair or Kalshi move that triggers a defensive cancel/exit",
    )
    live_order.add_argument("--auto-exit-target-cents", type=_decimal)
    live_order.add_argument("--auto-exit-floor-cents", type=_decimal)
    live_order.add_argument(
        "--auto-exit-wait-seconds",
        type=int,
        default=60,
        help="legacy manual-fair exit timeout; sportsbook-monitored exits are fair-aware",
    )
    live_order.add_argument("--acknowledge-auto-exit")
    live_order.add_argument("--acknowledge-monitored-entry")
    live_order.add_argument("--audit-log", type=Path, default=Path("logs/live-orders.jsonl"))
    live_order.add_argument("--execute-live", action="store_true")
    live_order.add_argument("--acknowledge-risk")
    live_order.add_argument("--confirm-ticker")
    live_order.add_argument("--intent-id")
    live_order.add_argument("--json", action="store_true")
    live_order.set_defaults(func=_cmd_live_order)

    live_status = subparsers.add_parser(
        "live-status",
        help="read production balance, resting orders, and an optional ticker position",
    )
    live_status.add_argument("--ticker")
    live_status.add_argument("--json", action="store_true")
    live_status.set_defaults(func=_cmd_live_status)

    live_cancel = subparsers.add_parser(
        "live-cancel",
        help="cancel one explicitly confirmed resting production order",
    )
    live_cancel.add_argument("--order-id", required=True)
    live_cancel.add_argument("--confirm-order-id", required=True)
    live_cancel.add_argument("--acknowledge-risk", required=True)
    live_cancel.add_argument("--audit-log", type=Path, default=Path("logs/live-orders.jsonl"))
    live_cancel.add_argument("--json", action="store_true")
    live_cancel.set_defaults(func=_cmd_live_cancel)

    rfq = subparsers.add_parser(
        "rfq-maker",
        help="quote two-way moneyline RFQs from a fresh external fair value",
    )
    fair = rfq.add_argument_group("fair-value cache")
    fair.add_argument("--fair-file", type=Path)
    fair.add_argument("--fair-file-refresh", type=float, default=0.25)
    fair.add_argument("--series", help="Kalshi moneyline series, used with --odds-sport")
    fair.add_argument("--odds-sport", help="Odds API sport key, e.g. baseball_mlb")
    fair.add_argument("--odds-regions", default="us")
    fair.add_argument("--odds-bookmakers", default=DEFAULT_SHARP_BOOKMAKERS)
    fair.add_argument("--odds-min-bookmakers", type=int, default=2)
    fair.add_argument("--odds-max-age", type=float, default=60)
    fair.add_argument("--odds-refresh", type=float, default=30)
    fair.add_argument("--odds-match-window-hours", type=float, default=6)
    pricing = rfq.add_argument_group("pricing and risk")
    pricing.add_argument("--allow-ticker", action="append", default=[])
    pricing.add_argument("--allow-collection", action="append", default=[])
    pricing.add_argument("--edge-percent", type=_decimal, default=Decimal("2"))
    pricing.add_argument(
        "--maker-fee-rate-percent",
        type=_decimal,
        default=KALSHI_MAKER_FEE_RATE * Decimal("100"),
        help="quadratic maker-fee coefficient; applied only to maker-fee series",
    )
    pricing.add_argument("--min-contracts", type=_decimal, default=Decimal("1"))
    pricing.add_argument("--max-contracts", type=_decimal, default=Decimal("10"))
    pricing.add_argument("--min-legs", type=int, default=2)
    pricing.add_argument("--max-legs", type=int, default=10)
    pricing.add_argument("--max-inflight-rfqs", type=int, default=32)
    pricing.add_argument("--max-quote-latency", type=float, default=1.0)
    pricing.add_argument("--max-position", type=_decimal, default=Decimal("10"))
    pricing.add_argument("--max-notional", type=_decimal, default=Decimal("10"))
    pricing.add_argument("--max-session-contracts", type=_decimal)
    pricing.add_argument("--max-session-executions", type=int)
    pricing.add_argument("--max-active-quotes", type=int, default=20)
    pricing.add_argument("--max-fair-age", type=float, default=60)
    pricing.add_argument("--reconcile-seconds", type=float, default=15)
    pricing.add_argument("--subaccount", type=int, default=0)
    pricing.add_argument(
        "--combo-only",
        action="store_true",
        help="reject single-market RFQs and quote only allowed MVE collections",
    )
    pricing.add_argument(
        "--contracts-only",
        action="store_true",
        help="reject target-cost RFQs and quote only explicit contract-count requests",
    )
    pricing.add_argument(
        "--allow-live",
        action="store_true",
        help="explicitly allow in-play fair values; disabled by default",
    )
    transport = rfq.add_argument_group("WebSocket and process")
    transport.add_argument("--demo", action="store_true", help="use demo RFQ data")
    transport.add_argument("--shard-factor", type=int)
    transport.add_argument("--shard-key", type=int)
    transport.add_argument("--seconds", type=float, default=0, help="0 runs until interrupted")
    transport.add_argument("--messages", type=int, default=0, help="0 means no message limit")
    execution = rfq.add_argument_group("execution")
    execution.add_argument("--execute-demo", action="store_true")
    execution.add_argument("--execute-live", action="store_true")
    execution.add_argument(
        "--canary-live",
        action="store_true",
        help="enforce the locked ten-contract/$10 production MLB canary profile",
    )
    execution.add_argument(
        "--allow-primary-account-canary",
        action="store_true",
        help="explicitly allow the locked production canary to use subaccount 0",
    )
    execution.add_argument("--acknowledge-risk")
    rfq.add_argument("--audit-log", type=Path, default=Path("logs/rfq-maker.jsonl"))
    rfq.add_argument("--fill-ledger", type=Path, default=Path("RFQ_FILLS.md"))
    rfq.add_argument("--json", action="store_true")
    rfq.set_defaults(func=_cmd_rfq_maker)

    stream = subparsers.add_parser("stream", help="watch Kalshi WebSocket updates")
    stream.add_argument("--ticker", required=True)
    stream.add_argument("--demo", action="store_true")
    stream.add_argument("--seconds", type=float, default=30, help="0 runs until interrupted")
    stream.add_argument("--messages", type=int, default=0, help="0 means no message limit")
    stream.add_argument("--json", action="store_true", help="emit raw JSON messages")
    stream.set_defaults(func=_cmd_stream)
    return parser


def main(argv: list[str] | None = None) -> None:
    load_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
