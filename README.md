# Kalshi sports market-maker sample

This repository is a dry-run-first reference implementation for exploring Kalshi sports markets and generating two-sided quotes. It is intentionally not a production trading system. The only execution mode exposed by the CLI is Kalshi's demo environment; production writes are not wired into the command line.

The implementation targets the API shape documented in June 2026:

- fixed-point dollar and contract fields (`*_dollars` and `*_fp`)
- V2 order writes at `/portfolio/events/orders`
- `bid` / `ask` as the canonical YES-book direction
- per-market `price_ranges` rather than the removed `tick_size` field
- the dedicated `external-api` production and demo hosts

## What it demonstrates

- Discover sports series and open markets through public REST endpoints.
- Convert Kalshi's YES-bid/NO-bid orderbook into a normal YES bid/ask view.
- Turn two-way American sportsbook odds into a no-vig fair probability.
- Adjust a supplied fair value for top-of-book imbalance, recent signed taker flow, and inventory.
- Generate tick-valid, two-sided quotes with hard inventory cutoffs.
- Sign authenticated requests with RSA-PSS/SHA-256.
- Reconcile post-only orders in the demo environment and cancel them on shutdown.

## Setup

Python 3.11 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[dev]'
pytest
```

All market-data commands are public and require no credentials.

Commands print readable terminal tables by default. Add `--json` to any command when
machine-readable output is needed for another script.

## Explore sports markets

Find high-volume baseball series:

```bash
kalshi-mm series --tag Baseball --limit 10
```

List current markets in a series:

```bash
kalshi-mm markets --series KXMLBGAME --limit 10
```

Normalize one orderbook into YES bid/ask prices:

```bash
kalshi-mm book --ticker MARKET-TICKER
```

Example terminal output:

```text
YES orderbook: MARKET-TICKER

METRIC          VALUE
--------------  -------------
Best bid        $0.5200 x 100
Best ask        $0.5700 x 80
Spread          $0.0500
Midpoint        $0.5450
Microprice      $0.5478
Book imbalance  +11.11%
```

Kalshi's REST orderbook contains only YES bids and NO bids. This sample converts a NO bid at `n` into a YES ask at `1 - n`. For example, a best NO bid of `$0.35` is a best YES ask of `$0.65`.

## Generate market-maker quotes

Dry-run with an externally calculated fair probability:

```bash
kalshi-mm run \
  --ticker MARKET-TICKER \
  --fair-probability 0.55 \
  --edge-cents 2 \
  --order-size 1 \
  --iterations 1
```

Dry-run from two sportsbook moneylines. The sample removes the two-way overround by normalizing the implied probabilities:

```bash
kalshi-mm run \
  --ticker MARKET-TICKER \
  --yes-moneyline -125 \
  --no-moneyline +110
```

For a continuously updated external model, write a JSON file from a separate feed handler:

```json
{
  "MARKET-TICKER": 0.557
}
```

The bot reloads that file on every cycle:

```bash
kalshi-mm run \
  --ticker MARKET-TICKER \
  --fair-file fair-values.json \
  --iterations 0 \
  --interval 2
```

The reservation price is:

```text
external fair
+ book imbalance weight × top-level book imbalance
+ trade-flow weight × recent signed taker imbalance
- inventory skew × net YES inventory
```

The bot places a bid below and an ask above that reservation price. `--edge-cents` is the minimum modeled edge on each side before execution effects. It must be large enough to cover applicable fees, fee rounding, adverse selection, fair-value error, and operational latency. A book-derived midpoint or microprice is not an independent fair value and is therefore not accepted as the live input.

## Guarded demo execution

Create separate credentials in Kalshi's demo environment, copy `.env.example` values into your shell, and run:

```bash
export KALSHI_API_KEY_ID='demo-key-id'
export KALSHI_PRIVATE_KEY_PATH='/absolute/path/to/demo-private-key.key'

kalshi-mm run \
  --ticker DEMO-MARKET-TICKER \
  --fair-probability 0.55 \
  --execute-demo \
  --acknowledge-risk DEMO_ONLY \
  --iterations 10
```

The sample uses `post_only=true`, `cancel_order_on_pause=true`, `taker_at_cross` self-trade prevention, client-generated order IDs, and a maximum inventory cutoff. On normal exit or Ctrl-C it attempts to cancel every order it created during that process.

## API map

| Purpose | Endpoint | Authentication |
| --- | --- | --- |
| Browse sports products | `GET /series?category=Sports` | No |
| Find open series events | `GET /events?series_ticker=...&status=open` | No |
| Read market rules/tick ranges | `GET /markets/{ticker}` | No |
| Read a book | `GET /markets/{ticker}/orderbook` | No |
| Read recent order flow | `GET /markets/trades?ticker=...` | No |
| Read inventory | `GET /portfolio/positions?ticker=...` | Yes |
| Read resting orders | `GET /portfolio/orders?ticker=...&status=resting` | Yes |
| Submit V2 order | `POST /portfolio/events/orders` | Yes |
| Cancel V2 order | `DELETE /portfolio/events/orders/{order_id}` | Yes |

Authenticated requests sign `timestamp_ms + HTTP_METHOD + full_path` with RSA-PSS/SHA-256. The path includes `/trade-api/v2` and excludes the hostname and query string.

## Where a real sports edge could come from

The code supplies market-making mechanics, not alpha. Practical research directions are:

1. Build a timestamped consensus from multiple permitted sportsbook feeds, remove vig, and map the exact contract rules before comparing it with Kalshi.
2. React to lineup, injury, weather, and game-state updates while measuring source-to-order latency. Stale quotes during a score or injury are the main adverse-selection risk.
3. Model fill toxicity: compare post-fill price movement by sport, time-to-start, spread, queue position, and taker-flow regime. Widen or stop quoting when expected markout exceeds spread capture.
4. Enforce cross-market probability constraints across mutually exclusive outcomes, alternate lines, totals, and related player markets.
5. Include fees, incentives, queue priority, partial-fill imbalance, and cancellation rate in backtests. Gross spread is not net profit.

## Production gaps

Before any real-money deployment, add at least:

- WebSocket orderbook, fill, position, and order streams with sequence-gap recovery
- an order group or equivalent exchange-side kill switch
- persistent order/fill state and startup reconciliation
- stale-data clocks, feed disagreement checks, and event-status handling
- fee calculations from the current series/event fee schedule
- per-event and portfolio exposure limits, cash checks, and loss limits
- amend/batch logic that preserves queue position where appropriate
- 429 backoff, request metrics, alerts, and restart supervision
- replayable market-data capture and fill/markout backtests
- review of contract settlement rules and participant prohibitions

Relevant official references:

- [API environments](https://docs.kalshi.com/getting_started/api_environments)
- [API keys and request signing](https://docs.kalshi.com/getting_started/api_keys)
- [Orderbook responses](https://docs.kalshi.com/getting_started/orderbook_responses)
- [Order direction](https://docs.kalshi.com/getting_started/order_direction)
- [Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2)
- [WebSocket quick start](https://docs.kalshi.com/getting_started/quick_start_websockets)
- [Rate limits](https://docs.kalshi.com/getting_started/rate_limits)
- [Fee rounding](https://docs.kalshi.com/getting_started/fee_rounding)
