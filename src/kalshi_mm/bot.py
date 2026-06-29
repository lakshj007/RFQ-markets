from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

from .client import KalshiAPIError, KalshiClient
from .display import quote_cycle
from .fair_value import FairValueSource
from .models import DesiredOrder, Level, OrderBook, QuotePlan, as_decimal
from .strategy import MarketMakerStrategy, trade_imbalance


LOGGER = logging.getLogger(__name__)


def fixed_point(value: Decimal) -> str:
    """Avoid exponent notation while preserving the market's chosen precision."""
    return format(value, "f")


@dataclass(frozen=True, slots=True)
class ActiveOrder:
    order_id: str
    client_order_id: str
    side: str
    price: Decimal
    count: Decimal


class DemoOrderManager:
    """Minimal cancel/replace manager deliberately restricted by the CLI to demo."""

    def __init__(self, client: KalshiClient, ticker: str) -> None:
        self.client = client
        self.ticker = ticker
        self.active: dict[str, ActiveOrder] = {}

    def _server_order_ids(self) -> set[str]:
        return {str(order["order_id"]) for order in self.client.get_resting_orders(self.ticker)}

    def sync(self, plan: QuotePlan) -> None:
        server_ids = self._server_order_ids()
        self.active = {
            side: order for side, order in self.active.items() if order.order_id in server_ids
        }
        desired = {"bid": plan.bid, "ask": plan.ask}
        for side, target in desired.items():
            current = self.active.get(side)
            if (
                current
                and target
                and current.price == target.price
                and current.count == target.count
            ):
                continue
            if current:
                self._cancel(current)
            if target:
                self._create(target)

    def _create(self, order: DesiredOrder) -> None:
        client_order_id = f"sample-mm-{order.side}-{uuid.uuid4()}"
        response = self.client.create_order(
            ticker=self.ticker,
            client_order_id=client_order_id,
            side=order.side,
            count=fixed_point(order.count),
            price=fixed_point(order.price),
        )
        if response.get("error"):
            raise RuntimeError(f"order rejected: {response['error']}")
        self.active[order.side] = ActiveOrder(
            order_id=str(response["order_id"]),
            client_order_id=client_order_id,
            side=order.side,
            price=order.price,
            count=order.count,
        )

    def _cancel(self, order: ActiveOrder) -> None:
        try:
            self.client.cancel_order(order.order_id)
        except KalshiAPIError as exc:
            # A fill or external cancel can race our reconciliation read.
            if exc.status_code != 404:
                raise
        self.active.pop(order.side, None)

    def cancel_all(self) -> None:
        for order in list(self.active.values()):
            self._cancel(order)


def plan_as_dict(
    *,
    ticker: str,
    book: OrderBook,
    plan: QuotePlan,
    inventory: Decimal,
) -> dict[str, object]:
    def level_price(level: Level | None) -> str | None:
        return fixed_point(level.price) if level else None

    def order_data(order: DesiredOrder | None) -> dict[str, str] | None:
        if not order:
            return None
        return {
            "side": order.side,
            "price": fixed_point(order.price),
            "count": fixed_point(order.count),
        }

    return {
        "ticker": ticker,
        "best_bid": level_price(book.best_bid),
        "best_ask": level_price(book.best_ask),
        "midpoint": fixed_point(book.midpoint) if book.midpoint is not None else None,
        "microprice": fixed_point(book.microprice) if book.microprice is not None else None,
        "fair_probability": fixed_point(plan.fair_probability),
        "reservation_price": fixed_point(plan.reservation_price),
        "book_imbalance": fixed_point(plan.book_imbalance),
        "trade_imbalance": fixed_point(plan.trade_imbalance),
        "inventory": fixed_point(inventory),
        "bid": order_data(plan.bid),
        "ask": order_data(plan.ask),
        "notes": list(plan.notes),
    }


def run_bot(
    *,
    client: KalshiClient,
    ticker: str,
    strategy: MarketMakerStrategy,
    fair_value_source: FairValueSource,
    dry_run_inventory: Decimal,
    execute_demo: bool,
    interval_seconds: float,
    iterations: int,
    trade_sample_size: int = 100,
    json_output: bool = False,
) -> None:
    manager = DemoOrderManager(client, ticker) if execute_demo else None
    completed = 0
    try:
        while iterations == 0 or completed < iterations:
            book = OrderBook.from_api(client.get_orderbook(ticker))
            trades = client.get_trades(ticker, limit=trade_sample_size)
            recent_flow = trade_imbalance(trades)
            inventory = (
                as_decimal(client.get_position(ticker)) if execute_demo else dry_run_inventory
            )
            plan = strategy.quote(
                book=book,
                fair_probability=fair_value_source.get(ticker),
                inventory=inventory,
                recent_trade_imbalance=recent_flow,
            )
            output = plan_as_dict(ticker=ticker, book=book, plan=plan, inventory=inventory)
            if json_output:
                print(json.dumps(output))
            else:
                if completed:
                    print("\n" + "=" * 72 + "\n")
                print(
                    quote_cycle(
                        cycle=completed + 1,
                        ticker=ticker,
                        book=book,
                        plan=plan,
                        inventory=inventory,
                        execute_demo=execute_demo,
                    )
                )
            if manager:
                manager.sync(plan)
            completed += 1
            if iterations == 0 or completed < iterations:
                time.sleep(interval_seconds)
    except KeyboardInterrupt:
        LOGGER.info("interrupted")
    finally:
        if manager:
            manager.cancel_all()
