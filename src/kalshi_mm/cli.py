from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
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
    StaticFairValue,
)
from .live_order import (
    LIVE_ACKNOWLEDGEMENT,
    LiveAuditLog,
    LiveOrderRequest,
    LiveRiskLimits,
    execute_live_order,
    preflight_live_order,
)
from .maker_paper import MakerPaperRecorder, MakerPaperUpdate
from .models import Level, OrderBook, PriceGrid, as_decimal
from .odds import DEFAULT_SHARP_BOOKMAKERS, OddsClient
from .paper import PaperRecorder
from .scanner import Discrepancy, scan_discrepancies
from .strategy import MarketMakerStrategy, StrategyConfig, devig_two_way
from .ws import KalshiWebSocket, StreamUpdate


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
                    item.action,
                    f"{item.edge * 100:+.2f}c",
                    item.bookmaker_count,
                )
                for item in visible
            ),
            right_align={2, 3, 4, 5, 7, 8},
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
                ("Exchange expiration", "configured at submission"),
            ),
        )
    )
    print("\nExecution requires explicit production credentials and all live safety gates.")


def _live_fair_probability(
    args: argparse.Namespace,
    client: KalshiClient,
    *,
    now: datetime | None = None,
) -> Decimal:
    choices = sum((args.fair_probability is not None, args.odds_sport is not None))
    if choices != 1:
        raise ValueError("choose exactly one live fair source: --fair-probability or --odds-sport")
    if args.odds_sport:
        return OddsConsensusFairValue(
            kalshi=client,
            odds=OddsClient.from_env(),
            market_ticker=args.ticker,
            sport=args.odds_sport,
            regions=args.odds_regions,
            bookmakers=args.odds_bookmakers,
            min_bookmakers=args.odds_min_bookmakers,
            max_age_seconds=min(args.odds_max_age, 60),
            refresh_seconds=1,
            match_window_hours=args.odds_match_window_hours,
            include_live=False,
        ).get(args.ticker)

    if not args.fair_observed_at:
        raise ValueError("manual --fair-probability requires --fair-observed-at")
    try:
        observed_at = datetime.fromisoformat(args.fair_observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--fair-observed-at must be an ISO-8601 timestamp") from exc
    if observed_at.tzinfo is None:
        raise ValueError("--fair-observed-at must include a timezone")
    age = ((now or datetime.now(UTC)) - observed_at.astimezone(UTC)).total_seconds()
    if age < -5 or age > 60:
        raise ValueError("manual fair value must have been observed within the last 60 seconds")
    assert args.fair_probability is not None
    return args.fair_probability


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
    if not args.execute_live:
        client = _client()
        request = LiveOrderRequest(
            ticker=args.ticker,
            side=args.side,
            price=args.price_cents / Decimal("100"),
            count=args.count,
            fair_probability=_live_fair_probability(args, client),
            expiration_seconds=args.expiration_seconds,
        )
        preview = preflight_live_order(
            client,
            request,
            limits,
            authenticated=False,
        )
        _print_live_preflight(preview.as_dict(), json_output=args.json)
        return

    if args.acknowledge_risk != LIVE_ACKNOWLEDGEMENT:
        raise ValueError(
            f"--execute-live requires --acknowledge-risk {LIVE_ACKNOWLEDGEMENT}"
        )
    if args.confirm_ticker != args.ticker:
        raise ValueError("--confirm-ticker must exactly match --ticker")
    if not args.intent_id:
        raise ValueError("--execute-live requires a unique --intent-id")
    client = _production_client()

    request = LiveOrderRequest(
        ticker=args.ticker,
        side=args.side,
        price=args.price_cents / Decimal("100"),
        count=args.count,
        fair_probability=_live_fair_probability(args, client),
        expiration_seconds=args.expiration_seconds,
    )

    result = execute_live_order(
        client,
        request,
        limits,
        intent_id=args.intent_id,
        audit_log=LiveAuditLog(args.audit_log),
        wait_seconds=args.wait_seconds,
        poll_seconds=args.poll_seconds,
    )
    if args.json:
        print(json.dumps(result))
    else:
        print(f"Live order result: {result['result']}")
        if result.get("order_id"):
            print(f"Order ID: {result['order_id']}")
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
    live_order.add_argument("--odds-sport", help="Odds API sport key, e.g. soccer_usa_mls")
    live_order.add_argument("--odds-regions", default="us")
    live_order.add_argument("--odds-bookmakers", default=DEFAULT_SHARP_BOOKMAKERS)
    live_order.add_argument("--odds-min-bookmakers", type=int, default=2)
    live_order.add_argument("--odds-max-age", type=float, default=180)
    live_order.add_argument("--odds-match-window-hours", type=float, default=6)
    live_order.add_argument("--expiration-seconds", type=int, default=120)
    live_order.add_argument("--wait-seconds", type=int, default=60)
    live_order.add_argument("--poll-seconds", type=float, default=2)
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
