# Kalshi sports market-maker sample

This repository is a dry-run-first implementation for exploring Kalshi sports markets
and generating two-sided quotes. Public-order-book market making remains restricted to
Kalshi's demo environment. A separate RFQ maker can quote and auto-confirm production
moneyline and independent-moneyline-parlay RFQs behind explicit credentials,
collection/ticker allowlists, acknowledgements, fresh external fair values, and bounded
exposure. In-play RFQs remain disabled by default.

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

## Moneyline RFQ maker

The RFQ maker listens to Kalshi's authenticated `communications` WebSocket channel.
It receives `rfq_created`, `rfq_deleted`, `quote_accepted`, and `quote_executed` without
polling. Kalshi currently exposes quote creation and confirmation as authenticated REST
writes, so those are the only network calls in the hot quote/confirm path.
This follows Kalshi's current [RFQ flow](https://docs.kalshi.com/getting_started/rfqs).

Pricing is deliberately simple. Given an external no-vig YES probability `p` and a
proportional edge rate `e`, the maker sends:

```text
yes_bid <= p × (1 - e)
no_bid  <= (1 - p) × (1 - e)
```

Both prices are rounded down to the market's current `price_ranges` grid. With the hard
minimum `e = 1.5%`, each executable side is at least 1.5% below that outcome's fair value
and the two raw bids sum to `$0.985` before grid rounding. At a 30% YES fair, the raw
quotes are 29.55 cents for YES and 68.95 cents for NO, giving absolute edges of 0.45 cents
and 1.05 cents respectively. Those formulas are the pre-fee starting point; the pricing
loop lowers an executable bid further when needed to preserve the configured edge after
modeled maker fees and fee rounding.

For a combo/MVE, each selected leg contributes `p_i` for a YES leg or `1 - p_i` for a
NO leg. Only after calculating the complete parlay fair `P = product(selected p_i)` does
the maker apply the edge:

```text
parlay_yes_bid <= P × (1 - e)
parlay_no_bid  <= (1 - P) × (1 - e)
```

The edge is therefore on the customer's finished parlay, not separately compounded on
each leg. Both bids remain prices per contract. The quote implicitly covers the RFQ's
full `contracts_fp` size or the side-specific contract count Kalshi derives from
`target_cost_dollars`.

The fast external-feed interface is an atomically replaced JSON file. Each entry must
explicitly identify a fresh, pregame moneyline:

```json
{
  "markets": {
    "KALSHI-MARKET-TICKER": {
      "probability": "0.55",
      "observed_at": "2026-07-24T18:30:00Z",
      "event_start": "2026-07-24T23:10:00Z",
      "event_ticker": "KXMLBGAME-26JUL24TEAMATEAMB",
      "participants": ["Team A", "Team B"],
      "market_type": "moneyline",
      "source": "your-low-latency-feed"
    }
  }
}
```

The file is cached and checked for updates every 250ms by default; an RFQ reads the
in-memory snapshot. `event_ticker` and `participants` are mandatory for parlay legs so
same-game and repeated-team combinations can fail closed. First run against demo RFQs
without writing quotes:

```bash
kalshi-mm rfq-maker \
  --demo \
  --fair-file rfq-fairs.json \
  --allow-ticker DEMO-MARKET-TICKER \
  --edge-percent 1.5 \
  --max-fair-age 5 \
  --min-contracts 1 \
  --max-contracts 10 \
  --seconds 60
```

To submit and auto-confirm in demo, add both guarded flags:

```bash
kalshi-mm rfq-maker \
  --fair-file rfq-fairs.json \
  --allow-ticker DEMO-MARKET-TICKER \
  --execute-demo \
  --acknowledge-risk DEMO_ONLY
```

For a slower turnkey pregame source, the maker can build and refresh an in-memory
two-way no-vig consensus from The Odds API. Matching happens before RFQs arrive, not in
the quote path:

```bash
kalshi-mm rfq-maker \
  --series KXMLBGAME \
  --odds-sport baseball_mlb \
  --odds-refresh 30 \
  --max-fair-age 60
```

Before production, run the exact canary profile as a read-only shadow. This sees live
RFQs and produces quote decisions, but does not call any quote or confirmation write
endpoint:

```bash
kalshi-mm rfq-maker \
  --series KXMLBGAME \
  --odds-sport baseball_mlb \
  --allow-collection KXMVESPORTSMULTIGAMEEXTENDED-R \
  --combo-only \
  --contracts-only \
  --edge-percent 1.5 \
  --min-contracts 1 \
  --max-contracts 10 \
  --min-legs 2 \
  --max-legs 6 \
  --max-inflight-rfqs 1 \
  --max-position 10 \
  --max-notional 10 \
  --max-session-contracts 10 \
  --max-session-executions 1 \
  --max-active-quotes 1 \
  --seconds 1800
```

Production RFQs are locked to a bounded MLB canary. It requires a dedicated numbered
subaccount funded with no more than $10, a private key file with mode `600`, a dedicated
API key whose name contains `rfq` and whose scopes are exactly `read`/`write`, the exact
collection and fair source below, and the ephemeral enable variable and acknowledgement:

```bash
export KALSHI_RFQ_LIVE_ENABLED='I_UNDERSTAND_RFQ_REAL_MONEY'

kalshi-mm rfq-maker \
  --series KXMLBGAME \
  --odds-sport baseball_mlb \
  --allow-collection KXMVESPORTSMULTIGAMEEXTENDED-R \
  --combo-only \
  --contracts-only \
  --edge-percent 1.5 \
  --min-contracts 1 \
  --max-contracts 10 \
  --min-legs 2 \
  --max-legs 6 \
  --max-inflight-rfqs 1 \
  --max-quote-latency 1 \
  --max-position 10 \
  --max-notional 10 \
  --max-session-contracts 10 \
  --max-session-executions 1 \
  --max-active-quotes 1 \
  --max-unaccepted-quote-age 60 \
  --subaccount 1 \
  --seconds 900 \
  --canary-live \
  --execute-live \
  --acknowledge-risk REAL_MONEY_RFQ_AUTOCONFIRM

unset KALSHI_RFQ_LIVE_ENABLED
```

If numbered subaccounts are unavailable, the same locked canary can use the primary
account only with the additional explicit flag below. The ten-contract, $10 maximum
notional, one-active-quote, and one-session-execution caps are unchanged, and preflight still
requires the primary account to have no positions or resting orders:

```bash
kalshi-mm rfq-maker \
  --series KXMLBGAME \
  --odds-sport baseball_mlb \
  --allow-collection KXMVESPORTSMULTIGAMEEXTENDED-R \
  --combo-only \
  --contracts-only \
  --edge-percent 1.5 \
  --min-contracts 1 \
  --max-contracts 10 \
  --min-legs 2 \
  --max-legs 6 \
  --max-inflight-rfqs 1 \
  --max-quote-latency 1 \
  --max-position 10 \
  --max-notional 10 \
  --max-session-contracts 10 \
  --max-session-executions 1 \
  --max-active-quotes 1 \
  --max-unaccepted-quote-age 60 \
  --subaccount 0 \
  --seconds 900 \
  --canary-live \
  --allow-primary-account-canary \
  --execute-live \
  --acknowledge-risk REAL_MONEY_RFQ_AUTOCONFIRM
```

The maker reads each quote market's series fee type and multiplier. For
`quadratic_with_maker_fees`, it models Kalshi's maker fee by rounding the position cost
plus `0.0175 × multiplier × contracts × price × (1-price)` up to a centicent, then
subtracting the position cost. `quadratic` series have zero maker fee. Prices move down
the valid grid
until the expected edge after that fee is at least the configured threshold. Unknown fee types
fail closed.

The maker rechecks fair-value age, event start, accepted side, accepted size, and the
full net configured edge immediately before confirmation. It reserves balance, modeled
fees, and both directional position outcomes across outstanding quotes, retains accepted
exposure
through Kalshi's execution timer, uses `post_only=true`, and never rests a remainder.
The locked production canary deletes a successfully submitted quote if it remains
unaccepted for 60 seconds, freeing its one active-quote slot without releasing accepted
or ambiguous exposure.
It reconciles quote state and portfolio risk every 15 seconds, refuses to start over
unresolved quotes from an earlier process, and cancels its unaccepted quotes on clean
shutdown. Every decision and measured quote/confirmation latency is appended to
`logs/rfq-maker.jsonl`. Every observed execution is also appended idempotently to
`RFQ_FILLS.md` by default; use `--fill-ledger PATH` to choose another Markdown file.
Each row records the game/event, legs, accepted side, contracts, confirmation fair,
quote price, gross edge, actual or modeled fee, net edge, IDs, and fair source. The Fills
API is queried by the maker order ID so actual `fee_cost` replaces the modeled fee when
available. REST
reconciliation writes a missed execution too, so a dropped WebSocket execution message
does not silently omit the fill.

### RFQ event workflow

1. Refresh and safely match pregame moneyline fair values before handling RFQs. JSON
   feeds are checked every 250ms; the Odds API source refreshes on its configured
   interval.
2. Receive `rfq_created` on the authenticated `communications` WebSocket. For a combo,
   require an allowed MVE collection, configured leg count, fresh supported moneyline
   fair for every selected leg, and exact selected-leg agreement with Kalshi's combo
   market metadata.
3. Reject repeated markets, repeated Kalshi `event_ticker` values, shared normalized
   participant identities, in-play legs, sibling-event positions, and overlap with any
   active quote reservation. Missing correlation metadata is a rejection, not an
   assumption of independence.
4. Convert every leg to its selected-side probability and multiply them once. Start each
   combo outcome bid at `outcome_fair × (1 - edge_rate)`, round down to the valid grid,
   then step down as needed until modeled maker fees still leave the full proportional
   edge net of fees.
5. Resolve fixed-size requests directly. For target-cost requests, conservatively round
   each side's derived size up to the next 0.01 contract for local risk checks. Disable
   a side if its size, position, notional, or balance limit would be exceeded.
6. Reserve worst-case balance and directional inventory locally, then submit the quote
   through `POST /communications/quotes`. At most 32 RFQs are processed concurrently
   by default; excess burst traffic and any request delayed over one second are dropped
   fail-closed instead of building a stale work queue.
7. On `quote_accepted`, validate the exchange-reported accepted count, reload every
   latest leg fair, rebuild the full parlay fair, recompute the accepted-size fee, and
   confirm only if the configured net proportional edge still remains.
8. Retain the exposure reservation through Kalshi's execution timer, reconcile it from
   quote and portfolio state, and release it after execution or cancellation. Record
   every observed fill, its legs, quote, fair, actual or modeled fee, and gross/net edge
   in `RFQ_FILLS.md`.
9. Cancel all unaccepted quotes on clean shutdown; refuse a new executable session if
   unresolved quotes from an earlier process still exist.

Correlation checks are fail-closed and structural. Every parlay leg must belong to a
different Kalshi event and have a participant set disjoint from every other leg. The
Odds API source retains both team markets for a two-way game so either can be selected,
while the combo validator prevents those siblings from appearing together. These
checks block same-game parlays and repeated teams; they cannot mathematically prove
independence from shared weather, venue, tournament, or other latent risks, so supported
series and collection allowlists should remain narrow.

## Guarded production order

`live-order` means a real-money production order. It does not permit in-play trading,
continuous repricing, or automated two-sided production market making. Entries and
normal exits are post-only. An optional fair-aware bounded exit may make one reduce-only
IOC sale after a material adverse fair or Kalshi-book move, but only at or above an
explicitly confirmed floor.
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
  --expiration-seconds 600
```

The preview and execution both reject the order unless the market is active and at
least five minutes pregame, the book is two-sided and no wider than 15 cents, the
price is tick-valid and post-only, a public trade occurred within 15 minutes, recent
volume is sufficient, the price joins or improves the current best level, no more than
500 contracts are ahead at the same price, and a bid has at least one cent of modeled
edge. Sharp-book prices must be no more than 60 seconds old. Execution also requires
sufficient balance, no existing resting production orders, and compliance with the
one-contract position limit.

For sportsbook-backed fair values, the matched sportsbook event start is carried into
the live request as an independent start time. The pregame gate uses whichever is
earlier: that independent start or Kalshi's occurrence timestamp. The preview prints
both timestamps and their offset, so a late Kalshi timestamp cannot accidentally keep
the order path enabled after the actual game begins. The preview also reads the series
fee model and includes any applicable maker fee in its maximum-loss figure; unknown fee
models fail closed.

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
  --expiration-seconds 600 \
  --wait-seconds 60 \
  --execute-live \
  --acknowledge-risk REAL_MONEY_ONE_CONTRACT \
  --confirm-ticker MARKET-TICKER \
  --intent-id 20260711-market-001

unset KALSHI_LIVE_TRADING_ENABLED
```

Every entry and ordinary exit is `post_only=true`, `cancel_order_on_pause=true`, uses
`taker_at_cross` self-trade prevention, and carries an exchange-side expiration. The
command polls the order and cancels any remainder after its local timeout. It also
attempts cancellation on interruption or error and writes an append-only audit trail
to `logs/live-orders.jsonl`. If submission returns an ambiguous network error, it
reconciles by client order ID and cancels any recovered resting order.

### Monitored resting entry

Sportsbook-backed YES entries can opt into a bounded fair-value monitor. The original
post-only order remains at its submitted price; the monitor can only leave it alone or
cancel it, never amend, replace, or submit a second entry. The preview shows the initial
fair and edge, the exact cancellation threshold (entry price plus the configured live
minimum edge; one cent by default), poll interval, maximum resting duration, odds
staleness limit, API failure grace, and any attached bounded exit:

```bash
kalshi-mm live-order \
  --ticker MARKET-TICKER \
  --side bid \
  --price-cents 60 \
  --odds-sport basketball_nba \
  --expiration-seconds 600 \
  --monitor-entry
```

Live execution additionally requires the exact acknowledgement
`--acknowledge-monitored-entry MONITOR_ODDS_AND_CANCEL_ENTRY` along with every normal
live-order gate. Monitored entries default to the hard 600-second maximum. The default
sportsbook poll interval is 30 seconds and production
polling cannot be configured below 25 seconds. The initial league response is used to
safely match one event; later polls use The Odds API's single-event endpoint. Each
refresh requires at least two timestamped, fresh bookmakers and recomputes the same
no-vig consensus. The order is canceled when fair falls below the threshold, too few
books remain, odds become stale, the quota is exhausted, the API failure grace expires,
the Kalshi bid or ask falls by the configured adverse-move threshold (two cents by
default), the independent start is within five minutes, the maximum rest expires, or
the process is interrupted.

For a specifically confirmed longer-lived test, `--monitor-until-pregame` replaces the
600-second timeout with exchange expiration five minutes before the independently
matched event start, capped at 12 hours. The price remains unchanged throughout; the
same fair, freshness, Kalshi adverse-move, interruption, and fill-race safeguards stay
active.

Kalshi's authenticated WebSocket supplies real-time order, fill, position, and book
updates. Authenticated REST reconciliation runs alongside it and always runs after a
cancellation attempt. A fill already in flight can win the race with cancellation, so
the result is not treated as canceled until orders, fills, fees, and final position are
reconciled. If a position exists and a bounded exit was preauthorized, that existing
bounded reduce-only exit starts after reconciliation.

The Odds API does not document a WebSocket for featured pregame odds. Those prices
generally update around once per minute, so 30-second internal polling cannot remove
upstream latency. Kalshi book moves arrive faster, but are not an independent fair-value
source by themselves. Cancellation also cannot prevent a match that was already in
flight when the cancel reached the exchange.

An entry may include a preauthorized fair-aware bounded exit. After a confirmed fill,
it records the sportsbook fair and Kalshi best bid, then posts one reduce-only target
ask. The target is not marked down merely because time passes or volume is low. While
it rests, sportsbook fair is refreshed every 30 seconds and the Kalshi book is watched
over WebSocket. A drop of at least `--adverse-move-cents` from either baseline cancels
the target and permits at most one reduce-only immediate-or-cancel sale at the refreshed
best bid, provided that bid is at or above the hard floor. Below the floor it reports
`held_below_floor` and holds the position. Without an adverse signal it holds through
the pregame cutoff rather than forcing a sale. It never continuously reprices and
cannot create a short position:

```bash
kalshi-mm live-order \
  --ticker MARKET-TICKER \
  --side bid \
  --price-cents 58 \
  --odds-sport basketball_nba_summer_league \
  --auto-exit-target-cents 64 \
  --auto-exit-floor-cents 55 \
  --adverse-move-cents 2
```

Real execution additionally requires
`--acknowledge-auto-exit BOUNDED_REDUCE_ONLY_EXIT`. The fallback can pay a taker fee;
its estimated fee, price, response, and remaining position are written to the audit
log. If the process stops after an ambiguous entry submission, run `live-status` before
attempting recovery rather than assuming the entry did not fill.

Manual fair values are supported only when accompanied by an ISO-8601 observation
timestamp no more than 60 seconds old and a timezone-qualified independent event start:

```bash
kalshi-mm live-order \
  --ticker MARKET-TICKER \
  --side bid \
  --price-cents 16 \
  --fair-probability 0.19 \
  --fair-observed-at 2026-07-11T02:00:00Z \
  --external-start-time 2026-07-11T04:00:00Z
```

Asks are reduce-only and require an existing YES position. A risk-reducing exit may
execute without the one-cent entry edge requirement. If a process crashes or an order
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

For tight, active books, `MAKE BID` means the current ask is not cheap enough to take,
but the resting YES bid is below sportsbook fair by the configured edge. This is the
mode intended for high-volume one-cent-spread markets: join or improve the bid
post-only, rather than requiring a wide spread or crossing the ask.

Add `--show-all` to inspect every safely matched market, including markets without an
actionable edge. The scan displays remaining Odds API quota and the cost of the last
request. It is pregame-only unless `--include-live` is provided. Scans use the same
default sharp-book filter, overridable with `--bookmakers`.

The scanner supports same-game head-to-head/moneyline markets and exact full-game
totals. A totals comparison requires the same event and exact numerical line at each
bookmaker; books quoting a different line are ignored. Team totals, first-half/period
totals, player props, spreads, and advancement markets remain excluded rather than
assuming their settlement rules are equivalent.

For a true two-outcome game, the scanner also merges each team's YES book with the
other team's economically equivalent NO book. Displayed bids, asks, spreads, and edges
are therefore the best effective prices across both execution routes. JSON output keeps
the direct prices and identifies the effective bid/ask route. Events with a draw or more
than two outcomes are never synthesized this way.

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
