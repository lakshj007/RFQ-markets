# Kalshi RFQ Market Maker — Agent Handoff

Last updated: 2026-07-30 (America/Los_Angeles)

## Purpose and current decision

This repository implements a low-latency Kalshi RFQ maker for sports parlays. The user
wants to make **NO-side quotes** against customer parlay RFQs, price every leg from
external sportsbook fair values, reject correlated or otherwise unsafe tickets, and run
small, explicitly authorized production canaries.

The latest pricing decision is:

- Use a **1.1% proportional edge** on the fair value of the complete parlay
  outcome.
- Retain the lead-inspired market, timing, independence, data-quality, creator/bot, and
  exposure filters.
- Do not apply 1.1% separately to every leg. Build the parlay probability first, then
  apply the edge once to the side being quoted.
- Continue quoting only the NO side; YES is declined.

The code now defaults to `--edge-percent 1.1`. Because `--lead-style-profile` historically
selected fixed-cent pricing as well as the filters, use
`--lead-style-profile --proportional-pricing --edge-percent 1.1` to retain the filters
while selecting the current proportional-pricing decision.

For an outcome with fair value `F` cents, the pre-grid, pre-fee expected edge is
`F * 0.011` cents per contract. Examples: 40¢ fair -> 0.44¢; 50¢ -> 0.55¢; 60¢ ->
0.66¢; 70¢ -> 0.77¢; 80¢ -> 0.88¢; 90¢ -> 0.99¢. The implementation rounds bids down
to Kalshi's grid and steps down further if necessary to preserve the configured edge
after modeled maker fees.

## Lead's supplied policy

The user's lead described this target policy:

### Markets

- MLB: moneylines, run totals, run lines, pitcher strikeout, home-run, and total-bases
  props.
- WNBA: moneylines, point totals, and spreads; no player props.
- MLS and Liga MX: three-way moneylines including draw, and goal totals; no spreads or
  both-teams-to-score.
- UFC: fight moneylines only.
- Price from Pinnacle through The Odds API using de-vigged fair probabilities.
- Quote as the NO-side maker only; decline YES.

### Ticket filters

- Every leg must be supported and priceable; one unknown leg rejects the entire RFQ.
- No two legs may share a game. The local implementation also rejects repeated/shared
  participants across games.
- Moneyline legs must be YES selections. A NO moneyline leg is treated as programmatic
  requester traffic and rejected.
- Minimum two legs.
- Reject a ticket containing a moneyline favorite steeper than -250. This corresponds
  to a maximum de-vigged fair probability of `5/7` for the selected ML leg.
- Lead maximum size: 750 contracts. Local production canaries intentionally use much
  smaller user-authorized limits.

### Timing and data

- Pregame only; reject any started/live leg.
- Reject legs more than 12 hours from start.
- UFC: stop quoting the entire card 45 minutes before the card's first bout.
- Pause when Pinnacle prices are more than five minutes stale.

### Lead's fixed-cent pricing (reference, not the latest local choice)

- 0.6¢ below de-vigged fair.
- Cap margin at 50% of the outcome's boundary/vig cushion.
- Require at least 0.7¢ cushion.
- Add to both margin and floor: +1.5¢ for a two-leg parlay, +0.5¢ for player-prop legs,
  and +0.5¢ for soccer legs. Premiums stack.
- Snap to the 0.1¢ grid.

The local fixed profile was made more competitive during testing: the two-leg premium
was reduced to 0.9¢, producing a 1.5¢ total fixed edge with the 0.6¢ base. The latest
decision supersedes that for new runs: use proportional 1.1% while retaining the lead
filters.

### Lead's exposure and counterparty policy

- Lead strategy bankroll: $17,000, independent of account balance.
- Per-leg-outcome cap: 5% or $850, reserved at quote time across all parlays.
- Per-combo cumulative cap: $250.
- Re-sync exposure from exchange positions every five minutes.
- Recheck caps at the accepted size before confirming.
- Per creator: no more than three fills or 300 contracts/day.
- Block a creator for the session when it posts more than two RFQs in ten seconds.
- Never rest a remainder on the public book (`rest_remainder=false`).
- Telegram kill switch; lead currently has daily-loss/global-exposure auto-pauses off.

The local system must scale all dollar/contract caps down to the amount the user
explicitly authorizes. Never copy the lead's $17,000 limits into this account.

### Lead's verification/operations policy

- Independently re-audit every fill within ten minutes using Kalshi's canonical combo
  definition and fresh Pinnacle prices.
- Zombie WebSocket watchdog at 15 seconds, price reseed every two minutes, and fixture
  refresh every 30 minutes.
- Telegram controls for tunable risk/pricing parameters.

## What is implemented

- Authenticated Kalshi `communications` WebSocket consumption for `rfq_created`,
  `rfq_deleted`, `quote_accepted`, and `quote_executed`.
- REST quote creation/confirmation/cancellation with guarded live-canary preflight.
- Proportional, fee-aware RFQ pricing and lead-style fixed-cent pricing.
- New `--proportional-pricing` override so lead filters can be combined with 1.1%.
- NO-only quoting.
- Parlay fair value as the product of selected leg probabilities.
- Pinnacle-only selection and de-vigging through The Odds API.
- Fail-closed unsupported-leg behavior.
- Same-event and shared-participant correlation rejection.
- Pregame checks and 12-hour horizon under the lead profile.
- ML YES-leg requirement and -250 favorite ceiling.
- MLB, WNBA, MLS, and Liga MX mapped lanes. WNBA spreads fail closed without an exact
  sportsbook/Kalshi line match.
- Target-cost and fixed-contract RFQs; size is not restricted to exactly one contract.
- Aggregate session cost/contract/execution caps, per-combo cap, and per-leg-outcome
  reservations.
- Acceptance-time re-pricing and risk recheck at Kalshi's accepted size.
- `first_fill_wins`: one valid acceptance owns the single confirmation slot and
  competing quotes are cancelled.
- `rest_remainder=false`.
- Startup and periodic reconciliation, quote TTL cleanup, execution reconciliation,
  and Markdown fill ledger at `RFQ_FILLS.md`.
- Creator burst protection. Kalshi currently leaves `creator_id` empty in
  `rfq_created`, so the maker resolves it through authenticated REST. The first two
  otherwise eligible RFQs inside ten seconds are allowed; the third blocks that creator
  for the session. Missing identity fails closed.
- Coverage, unsupported-reason, lane-readiness, and unique-combo audit summaries.

## Important gaps and caveats

- MLB player props are not fully mapped yet. Exact player/threshold matching is required
  before enabling them.
- UFC is not enabled in the production lane allowlist; trusted card-start metadata and
  the 45-minute card lock still need implementation.
- Daily creator fill/contract limits are not implemented; only session burst blocking
  is present.
- Telegram controls, kill switch, zombie watchdog, periodic independent fill re-audit,
  and full five-minute reconstruction of all strategy exposures are incomplete.
- Creator lookup is performed only after cheap structural prefilters. Unsupported RFQs
  can never receive quotes, but their creators are not added to the block list. Expanding
  identity lookup to every raw WebSocket RFQ could create heavy REST load and needs a
  bounded/cache-aware design.
- The lead allows Pinnacle prices up to five minutes stale, but the locked canary has
  generally used `--max-fair-age 60`. This is safer but reduces coverage. Do not loosen
  it during a live session; test and obtain explicit authorization first.
- The Odds API has no documented featured-odds WebSocket in this integration. Odds are
  refreshed, then read from an in-memory snapshot; Kalshi RFQs arrive over WebSocket.
- Public RFQ quote visibility does not reveal a complete competitor quote book. We can
  observe our quotes and their lifecycle, not reliably inspect every competing maker's
  price.

## Production history and empirical results

The user iteratively authorized small primary-account canaries at proportional edges of
2%, 1.5%, 1%, 0.75%, 1.3%, 1.35%, 1.4%, 1.6%, and 1.75%, followed by lead-style
fixed-cent tests. Never treat an old authorization as permission for a new run.

`RFQ_FILLS.md` currently records four historical NO-side MLB two-leg fills:

- 2026-07-28 20:59 UTC: 5.42 contracts, 0.7745% net edge, about $0.0274 expected net
  edge.
- 2026-07-28 21:39 UTC: 4.86 contracts, 1.1570% net edge, about $0.0345 expected net
  edge.
- 2026-07-29 19:22 UTC: 3.62 contracts, 1.3191% net edge, about $0.0357 expected net
  edge.
- 2026-07-29 19:56 UTC: 6.07 contracts, 1.4234% net edge, about $0.0530 expected net
  edge.

These are expected-value edges at fill time, not guaranteed realized profits. Combo
positions settle only when all legs resolve.

### Previous canary (run 6)

- Audit: `logs/rfq-live-lead-multilane-20260730-run6.jsonl`.
- Ran for 30 minutes from approximately 23:40:58 to 00:10:58 UTC.
- Primary account, all currently mapped MLB/WNBA/MLS/Liga MX lanes.
- Fixed lead-style pricing at 0.6¢ base + 0.9¢ two-leg premium = 1.5¢ total before grid.
- Maximum 10 contracts, maximum $8.69 target/session/notional cost, one execution,
  `first_fill_wins`, 20 active/inflight, 60-second TTL.
- 130 quotes submitted, zero confirmations, zero executions/fills.
- 9,149 otherwise eligible attempts were rejected by creator burst blocking.
- Three remaining quotes were cleanly cancelled at shutdown.
- High raw traffic was dominated by unsupported collections/tickers, repeated combos,
  beyond-horizon events, and blocked creators.

This no-fill result motivated the return to 1.3% proportional pricing. It does not prove
that 1.5¢ is universally unfillable, but proportional pricing is materially tighter on
the common 50-80¢ NO fair-value range.

### Previous canary (run 7)

- Audit: `logs/rfq-live-lead-multilane-20260730-run7.jsonl`.
- Ran for 30 minutes from approximately 02:34 to 03:04 UTC on 2026-07-31.
- Primary account, all currently mapped MLB/WNBA/MLS/Liga MX lanes.
- Proportional pricing at 1.3% with the lead filters and 60-second fair-value freshness.
- Maximum 10 contracts, maximum $8.69 target/session/notional cost, one execution,
  `first_fill_wins`, 20 active/inflight, 60-second TTL.
- Zero quotes submitted, zero confirmations, and zero executions/fills.
- Startup reported no fresh, priceable fixture inside the 12-hour horizon. Subsequent
  traffic was rejected by the market, timing, freshness, and creator-burst filters.
- Clean shutdown and final reconciliation: $8.69 available cash, $0 portfolio value,
  zero positions, zero resting orders, and zero unresolved self RFQ quotes.

After run 7, the user reduced the proportional-pricing decision to 1.2%. This is a
configuration decision only and is not authorization for another production run.

### Previous canary (run 8)

- Audit: `logs/rfq-live-lead-multilane-20260730-run8.jsonl`.
- Ran for 30 minutes and shut down normally at approximately 04:14:49 UTC on
  2026-07-31.
- Primary account, all currently mapped MLB/WNBA/MLS/Liga MX lanes.
- Proportional pricing at 1.2% with the lead filters and 60-second fair-value freshness.
- Maximum 10 contracts, maximum $8.69 target/session/notional cost, one execution,
  `first_fill_wins`, 20 active/inflight, 60-second TTL.
- Zero quotes submitted, zero confirmations, and zero executions/fills.
- Startup again reported no fresh, priceable fixture inside the 12-hour horizon.
- One transient Kalshi reconciliation connection reset and one Odds API refresh failure
  were recorded; both paths failed closed and no quote or exposure was created.
- Clean shutdown and final reconciliation: $8.69 available cash, $0 portfolio value,
  zero positions, zero resting orders, and zero unresolved self RFQ quotes.

### Previous canary (run 9)

- Audit: `logs/rfq-live-lead-multilane-20260731-run9.jsonl`.
- Ran for 30 minutes and shut down normally at approximately 17:57:22 UTC on
  2026-07-31.
- Primary account, all currently mapped MLB/WNBA/MLS/Liga MX lanes.
- Proportional pricing at 1.2% with the lead filters and 60-second fair-value freshness.
- Maximum 10 contracts, maximum $8.69 target/session/notional cost, one execution,
  `first_fill_wins`, 20 active/inflight, 60-second TTL.
- Startup had materially better coverage: 42 MLB, 9 Liga MX, and 3 MLS fresh fixtures
  inside the 12-hour horizon.
- 300 quotes submitted, zero acceptances, zero confirmations, and zero executions/fills.
- Fourteen remaining quotes were cleanly cancelled at shutdown; no cleanup failures.
- One transient Kalshi reconciliation failure and one fair-value refresh failure were
  recorded; both paths failed closed and no exposure was created.
- Final reconciliation: $8.69 available cash, $0 portfolio value, zero positions, zero
  resting orders, and zero unresolved self RFQ quotes.

After run 9, the user selected 1.1% for the next proportional-pricing test. This is a
configuration decision only and is not authorization for another production run.

### Previous canary (run 10)

- The first startup attempt wrote only
  `logs/rfq-live-lead-multilane-20260731-run10.jsonl` and exited before maker readiness
  because Kalshi returned HTTP 429 while loading positions. It submitted no quotes and
  created no exposure.
- The authorized retry audit is
  `logs/rfq-live-lead-multilane-20260731-run10-retry1.jsonl`.
- The retry ran for 30 minutes and shut down normally at approximately 18:37:02 UTC on
  2026-07-31.
- Primary account, all currently mapped MLB/WNBA/MLS/Liga MX lanes.
- Proportional pricing at 1.1% with the lead filters and 60-second fair-value freshness.
- Maximum 10 contracts, maximum $8.69 target/session/notional cost, one execution,
  `first_fill_wins`, 20 active/inflight, 60-second TTL.
- Startup coverage: 43 MLB, 9 Liga MX, and 3 MLS fresh fixtures inside the 12-hour
  horizon.
- 163 quotes submitted; exactly one quote was accepted, confirmed, and executed.
- Fill: 5.31 NO contracts at $0.8220 on independent Liga MX legs
  `KXLIGAMXGAME-26JUL31ASLTIJ-TIJ` YES and
  `KXLIGAMXGAME-26JUL31PUECDG-CDG` YES. The NO fair was $0.8320171, actual fee was
  $0.00008, net edge was 1.2021%, and expected net edge at fill time was about $0.0531.
- Eleven competing quotes were cancelled after the acceptance. The session limit then
  rejected further executions. There were no cleanup, reconciliation, or fair-refresh
  failures during the retry.
- Final reconciliation: $4.32 available cash, $4.35 portfolio value, one open combo
  position of -5.31 contracts with $4.36482 exposure, zero resting orders, and zero
  unresolved self RFQ quotes.

### Most recent canary (run 11)

- Audit: `logs/rfq-live-lead-multilane-20260731-run11.jsonl`.
- Ran for 30 minutes and shut down normally at approximately 19:40:46 UTC on
  2026-07-31.
- Continued from the run 10 open position using
  `--continue-open-independent-positions`; startup reconstructed both existing Liga MX
  events and all four participants before enabling execution.
- Proportional pricing at 1.1%, maximum 10 new contracts, maximum $4.32
  target/session/notional cost, one new execution, `first_fill_wins`, 20
  active/inflight, and 60-second TTL.
- 175 quotes submitted, zero acceptances, zero confirmations, and zero new
  executions/fills.
- 2,975 individually audited attempts were rejected for overlap with the existing
  position. Three remaining quotes were cleanly cancelled at shutdown.
- Zero shutdown-cleanup, reconciliation, or fair-refresh failures were recorded in the
  audit.
- Post-run exchange reconciliation could not be performed because the Codex external
  tool escalation was rejected after the account's usage limit was reached. The last
  successful pre-run reconciliation was $4.32 cash, $4.35 portfolio value, one -5.31
  contract combo position, zero resting orders, and zero unresolved self RFQ quotes.
  The local run audit shows no new execution and clean quote cleanup, but a future agent
  must perform a fresh exchange reconciliation before relying on that inferred state.

## Account and credentials

- Production credentials are configured locally through `.env` and a private-key file.
- `.env` is ignored by Git. Never print, copy into this document, commit, or transmit
  the API key ID or private-key contents.
- The key was described by the user as a new full-access Kalshi production key.
- The last successful read-only reconciliation, immediately before run 11, showed
  **$4.32** available cash, $4.35
  portfolio value, one open combo position of -5.31 contracts with $4.36482 exposure,
  zero resting public-book orders, and zero unresolved self RFQ quotes. A new agent must
  still perform a fresh reconciliation before relying on these figures. Any continuation
  must account for the open position and requires fresh, explicit authorization; the
  normal flat-account canary preflight will reject it.

Read-only status command:

```bash
set -a
source .env
set +a
kalshi-mm live-status --json
```

## Safety and authorization requirements

- Do not start, restart, extend, or materially change a production canary without fresh,
  explicit user authorization specifying duration, maximum fills, contracts, total
  cost, and minimum edge.
- Do not infer authorization from previous runs. Old authorizations are exhausted when
  their canary ends.
- Read-only diagnostics and reconciliation are permitted when relevant.
- Before every live run, reconcile balance, positions, resting orders, and unresolved
  self RFQ quotes. Fail closed on unexpected exposure.
- Use a unique audit-log path for every run.
- Keep `--max-session-executions 1`, `--first-fill-wins`, explicit aggregate dollar and
  contract caps, and TTL cleanup for small canaries unless the user expressly changes
  them.
- If shutdown cleanup fails, identify exact unresolved quote IDs. Cancellation is a
  trading mutation and requires the user's explicit instruction unless it is the normal
  in-process cleanup already authorized as part of that live canary.
- Never expose credentials in logs, chat, commits, or handoff files.

## Suggested next workflow

1. Run the full tests and lint after the 1.1% change.
2. Run a read-only shadow using `--lead-style-profile --proportional-pricing
   --edge-percent 1.1` and the same lane/risk settings intended for production.
3. Compare eligible volume, absolute cents of edge, grid-rounding uplift, creator blocks,
   and would-be quote lifecycle against run 6.
4. Reconcile production account state.
5. Ask the user for a fresh, exact live authorization if they want another canary.
6. For any live run, report submitted, accepted, confirmed, executed, balance, positions,
   resting orders, unresolved self quotes, and bot blocks. Reconcile again after shutdown.

Do not paste a production command containing an enable token into this handoff. Generate
the ephemeral acknowledgement only when a newly authorized run is actually being
launched.

## Repository state

The worktree contains many uncommitted changes from the RFQ development sequence and
possibly pre-existing user work. Preserve unrelated changes. The relevant latest edits
are:

- `src/kalshi_mm/rfq.py`: default proportional edge is now `0.011`.
- `src/kalshi_mm/cli.py`: CLI default is `1.1`; `--proportional-pricing` can override the
  fixed pricing bundled with `--lead-style-profile`.
- `tests/test_rfq_cli.py`: covers the new default.
- `README.md`: documents the 1.1% default and combined lead-filter/proportional mode.
- This file: `RFQ_AGENT_HANDOFF.md`.

No commit or push was requested in the latest instruction. Review the dirty worktree
carefully before staging anything.
