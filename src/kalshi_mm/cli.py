from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from .bot import run_bot
from .client import DEMO_BASE_URL, PRODUCTION_BASE_URL, KalshiClient
from .display import level as display_level
from .display import money, number, table
from .fair_value import JsonFileFairValue, StaticFairValue
from .models import Level, OrderBook, PriceGrid, as_decimal
from .strategy import MarketMakerStrategy, StrategyConfig, devig_two_way


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


def _fair_source(args: argparse.Namespace) -> StaticFairValue | JsonFileFairValue:
    choices = sum(
        (
            args.fair_probability is not None,
            args.fair_file is not None,
            args.yes_moneyline is not None or args.no_moneyline is not None,
        )
    )
    if choices != 1:
        raise ValueError(
            "choose exactly one fair-value input: --fair-probability, --fair-file, "
            "or both --yes-moneyline and --no-moneyline"
        )
    if args.fair_file:
        return JsonFileFairValue(args.fair_file)
    if args.yes_moneyline is not None or args.no_moneyline is not None:
        if args.yes_moneyline is None or args.no_moneyline is None:
            raise ValueError("both --yes-moneyline and --no-moneyline are required")
        return StaticFairValue(devig_two_way(args.yes_moneyline, args.no_moneyline))
    return StaticFairValue(args.fair_probability)


def _cmd_run(args: argparse.Namespace) -> None:
    source = _fair_source(args)
    if args.execute_demo:
        if args.acknowledge_risk != "DEMO_ONLY":
            raise ValueError("--execute-demo requires --acknowledge-risk DEMO_ONLY")
        if not os.getenv("KALSHI_API_KEY_ID") or not os.getenv("KALSHI_PRIVATE_KEY_PATH"):
            raise ValueError(
                "set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH for demo execution"
            )
        client = KalshiClient.from_env(demo=True)
    else:
        client = _client()

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
    execution = run.add_argument_group("guarded demo execution")
    execution.add_argument("--execute-demo", action="store_true")
    execution.add_argument("--acknowledge-risk")
    run.set_defaults(func=_cmd_run)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
