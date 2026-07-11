from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal

from .models import DesiredOrder, Level, OrderBook, QuotePlan, as_decimal


def _cell(value: object) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def table(
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
    *,
    right_align: set[int] | None = None,
) -> str:
    """Render a dependency-free ASCII table suitable for normal terminals."""
    rendered_rows = [[_cell(value) for value in row] for row in rows]
    if not rendered_rows:
        return "No results."
    widths = [len(header) for header in headers]
    for row in rendered_rows:
        if len(row) != len(headers):
            raise ValueError("table row has a different number of columns than headers")
        widths = [max(width, len(value)) for width, value in zip(widths, row, strict=True)]

    right_align = right_align or set()

    def render_row(row: Sequence[str]) -> str:
        cells = []
        for index, (value, width) in enumerate(zip(row, widths, strict=True)):
            cells.append(value.rjust(width) if index in right_align else value.ljust(width))
        return "  ".join(cells).rstrip()

    header = render_row(list(headers))
    separator = render_row(["-" * width for width in widths])
    body = "\n".join(render_row(row) for row in rendered_rows)
    return f"{header}\n{separator}\n{body}"


def number(
    value: str | int | float | Decimal | None,
    *,
    decimals: int = 2,
) -> str:
    if value is None or value == "":
        return "-"
    parsed = as_decimal(value)
    if parsed == parsed.to_integral_value():
        return f"{parsed:,.0f}"
    return f"{parsed:,.{decimals}f}".rstrip("0").rstrip(".")


def money(value: Decimal | str | None) -> str:
    if value is None:
        return "-"
    return f"${as_decimal(value):.4f}"


def percent(value: Decimal | str | None, *, signed: bool = False) -> str:
    if value is None:
        return "-"
    parsed = as_decimal(value) * Decimal("100")
    sign = "+" if signed else ""
    return f"{parsed:{sign}.2f}%"


def level(value: Level | None) -> str:
    if value is None:
        return "-"
    return f"{money(value.price)} x {number(value.size)}"


def order(value: DesiredOrder | None) -> tuple[str, str]:
    if value is None:
        return "-", "-"
    return money(value.price), number(value.count)


def quote_cycle(
    *,
    cycle: int,
    ticker: str,
    book: OrderBook,
    plan: QuotePlan,
    inventory: Decimal,
    execute_demo: bool,
) -> str:
    mode = "DEMO EXECUTION" if execute_demo else "DRY RUN"
    bid_price, bid_count = order(plan.bid)
    ask_price, ask_count = order(plan.ask)
    lines = [
        f"Quote cycle {cycle} [{mode}]",
        f"Market: {ticker}",
        "",
        table(
            ("BOOK", "PRICE x SIZE"),
            (
                ("Best YES bid", level(book.best_bid)),
                ("Best YES ask", level(book.best_ask)),
                ("Midpoint", money(book.midpoint)),
                ("Microprice", money(book.microprice)),
            ),
        ),
        "",
        table(
            ("MODEL", "VALUE"),
            (
                ("External fair", percent(plan.fair_probability)),
                ("Reservation", percent(plan.reservation_price)),
                ("Book imbalance", percent(plan.book_imbalance, signed=True)),
                ("Trade imbalance", percent(plan.trade_imbalance, signed=True)),
                ("YES inventory", number(inventory)),
            ),
        ),
        "",
        "Proposed orders",
        table(
            ("SIDE", "PRICE", "COUNT"),
            (("BID", bid_price, bid_count), ("ASK", ask_price, ask_count)),
            right_align={1, 2},
        ),
    ]
    if plan.notes:
        lines.extend(("", "Notes:", *(f"- {note}" for note in plan.notes)))
    return "\n".join(lines)
