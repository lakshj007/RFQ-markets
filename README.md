# Kalshi sports market-maker sample

This repository is a dry-run-first implementation for exploring Kalshi sports markets
and generating two-sided quotes. Continuous execution remains restricted to Kalshi's
demo environment. Production execution is limited to a separate, guarded, one-shot
`live-order` command for a single pregame contract; automated production market making
and in-play order entry are not enabled.

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

REST discovery commands are public and require no Kalshi credentials.

Commands print readable terminal tables by default. Add `--json` to any command when
machine-readable output is needed for another script.

Create a local `.env` for credentials. The file is ignored by Git:

```text
ODDS_API_KEY=your-odds-api-key
KALSHI_API_KEY_ID=your-kalshi-key-id
KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/kalshi-private-key.key
```

The Odds API key is sufficient for discrepancy scans. Kalshi credentials are only
required for WebSocket sessions and demo order execution.

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

Use a live, multi-book no-vig consensus directly as fair value:

```bash
kalshi-mm run \
  --ticker KALSHI-MARKET-TICKER \
  --odds-sport baseball_mlb \
  --odds-regions us \
  --odds-min-bookmakers 3 \
  --odds-refresh 60 \
  --edge-cents 2
```

The fair-value source caches its result between refreshes, so high-frequency Kalshi
book updates do not consume an Odds API request each time. In-play external odds are
rejected by default because the documented feed latency is materially slower during a
live game. `--odds-include-live` is an explicit, higher-risk override. By default,
Odds API fair values are restricted to the sharper-book set
`pinnacle,circasports,bookmaker,fanduel`; the feed may return only the subset of
those books available for a given sport.

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

## Guarded production order

`live-order` means a real-money production order. It does not permit in-play trading,
taker orders, continuous repricing, or automated two-sided production market making.
The hard-coded ceiling is one contract, at most $1 of entry cost, and at most one
contract of absolute position.

Create a separate production API key with read/write scope, store the downloaded key
outside the repository, and restrict its permissions:

```bash
chmod 600 /absolute/path/to/kalshi-production.key
export KALSHI_PROD_API_KEY_ID='production-key-id'
export KALSHI_PROD_PRIVATE_KEY_PATH='/absolute/path/to/kalshi-production.key'
```

First inspect the production account. This is authenticated but read-only:

```bash
kalshi-mm live-status --ticker MARKET-TICKER
```

Preview a one-contract post-only YES bid using a fresh sharp-book consensus. Preview
does not require Kalshi credentials and never submits an order:

```bash
kalshi-mm live-order \
  --ticker MARKET-TICKER \
  --side bid \
  --price-cents 16 \
  --odds-sport soccer_usa_mls \
  --expiration-seconds 120
```

The preview and execution both reject the order unless the market is active and at
least five minutes pregame, the book is two-sided and no wider than 15 cents, the
price is tick-valid and post-only, a public trade occurred within 15 minutes, recent
volume is sufficient, the price joins or improves the current best level, no more than
500 contracts are ahead at the same price, and a bid has at least two cents of modeled
edge. Sharp-book prices must be no more than 60 seconds old. Execution also requires
sufficient balance, no existing resting production orders, and compliance with the
one-contract position limit.

After reviewing the preview, submit the exact same order with all real-money gates.
Use a unique intent ID and reuse that ID if a command must be retried; the client will
reconcile an existing matching order instead of submitting a duplicate:

```bash
export KALSHI_LIVE_TRADING_ENABLED='I_UNDERSTAND_REAL_MONEY'

kalshi-mm live-order \
  --ticker MARKET-TICKER \
  --side bid \
  --price-cents 16 \
  --odds-sport soccer_usa_mls \
  --expiration-seconds 120 \
  --wait-seconds 60 \
  --execute-live \
  --acknowledge-risk REAL_MONEY_ONE_CONTRACT \
  --confirm-ticker MARKET-TICKER \
  --intent-id 20260711-market-001

unset KALSHI_LIVE_TRADING_ENABLED
```

Every submitted order is `post_only=true`, `cancel_order_on_pause=true`, uses
`taker_at_cross` self-trade prevention, and carries an exchange-side expiration. The
command polls the order and cancels any remainder after its local timeout. It also
attempts cancellation on interruption or error and writes an append-only audit trail
to `logs/live-orders.jsonl`. If submission returns an ambiguous network error, it
reconciles by client order ID and cancels any recovered resting order.

Manual fair values are supported only when accompanied by an ISO-8601 observation
timestamp no more than 60 seconds old:

```bash
kalshi-mm live-order \
  --ticker MARKET-TICKER \
  --side bid \
  --price-cents 16 \
  --fair-probability 0.19 \
  --fair-observed-at 2026-07-11T02:00:00Z
```

Asks are reduce-only and require an existing YES position. A risk-reducing exit may
execute without the two-cent entry edge requirement. If a process crashes or an order
needs manual recovery, inspect and cancel it explicitly:

```bash
kalshi-mm live-status

kalshi-mm live-cancel \
  --order-id ORDER-ID \
  --confirm-order-id ORDER-ID \
  --acknowledge-risk CANCEL_REAL_ORDER
```

## Scan sportsbook/Kalshi discrepancies

Compare an Odds API moneyline consensus with executable Kalshi bids and asks:

```bash
kalshi-mm scan \
  --series KXMLBGAME \
  --sport baseball_mlb \
  --regions us \
  --min-bookmakers 3 \
  --min-edge-cents 3
```

Compare exact full-game total lines, such as an MLB over/under:

```bash
kalshi-mm scan \
  --series KXMLBTOTAL \
  --sport baseball_mlb \
  --market-type totals \
  --min-bookmakers 3 \
  --min-edge-cents 3 \
  --show-all
```

Add `--show-all` to inspect every safely matched market, including markets without an
actionable edge. The scan displays remaining Odds API quota and the cost of the last
request. It is pregame-only unless `--include-live` is provided. Scans use the same
default sharp-book filter, overridable with `--bookmakers`.

The scanner supports same-game head-to-head/moneyline markets and exact full-game
totals. A totals comparison requires the same event and exact numerical line at each
bookmaker; books quoting a different line are ignored. Team totals, first-half/period
totals, player props, spreads, and advancement markets remain excluded rather than
assuming their settlement rules are equivalent.

## Paper signals and markouts

Record qualifying signals once per minute and measure their later midpoint movement:

```bash
kalshi-mm paper \
  --series KXMLBGAME \
  --sport baseball_mlb \
  --min-edge-cents 3 \
  --interval 60 \
  --iterations 0 \
  --markout-horizons 60,300 \
  --output logs/mlb-paper.jsonl
```

The JSONL file contains append-only `signal` and `markout` records. This mode never
submits orders. A signal is priced at the executable ask for BUY YES or executable bid
for SELL YES, rather than at the midpoint. Run it continuously in one process to collect
the configured future markouts.

## Passive maker simulation

Simulate one-contract quotes on both sides of high-spread markets and infer later fills
from public Kalshi trades:

```bash
kalshi-mm maker-paper \
  --series KXLIGAMXGAME \
  --sport soccer_mexico_ligamx \
  --min-spread-cents 4 \
  --max-spread-cents 15 \
  --min-edge-cents 1 \
  --max-top-size 500 \
  --quote-lifetime 600 \
  --interval 60 \
  --iterations 6 \
  --state logs/ligamx-maker-state.json \
  --output logs/ligamx-maker.jsonl
```

The simulator improves the current bid and ask by one tick only when the sharp-book
fair value leaves the configured minimum edge on both sides. State survives process
restarts. A simulated bid fill requires a later ask-side public trade at or below the
quote; an ask fill requires a later bid-side trade at or above it. Quotes expire after
the configured lifetime, and one-sided inventory is flattened at the then-executable
bid or ask. Logged profit is before fees. This remains a model: public trades cannot
prove queue position perfectly, so results must not be treated as actual fills.
The maximum-spread guard excludes placeholder or broken books whose extreme width is
not a credible market-making opportunity.

## WebSocket market data

Kalshi requires an authenticated handshake even for its market-data WebSocket. Watch
a market without submitting orders:

```bash
kalshi-mm stream --ticker MARKET-TICKER --seconds 30
```

Use the sequence-checked WebSocket book in the dry-run market maker:

```bash
kalshi-mm run \
  --ticker MARKET-TICKER \
  --odds-sport baseball_mlb \
  --websocket \
  --interval 0.25 \
  --iterations 20
```

The stream subscribes to orderbook snapshots/deltas, ticker updates, public trades,
user orders, fills, and positions. It requests unified YES-price levels, checks every
subscription sequence number, and reconnects with exponential backoff after a gap or
disconnect. `--interval` becomes the minimum quote recalculation interval in WebSocket
mode.

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

- soak-tested WebSocket recovery, subscription acknowledgements, and burst handling
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
