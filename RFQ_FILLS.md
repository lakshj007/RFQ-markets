# RFQ Fill Ledger

This file is updated when the maker observes an executed RFQ. Edge is the modeled
pre-fee edge at confirmation when available, otherwise the quote-time edge. Combo/MVE
RFQs are currently rejected, so current fills are single-leg moneylines rather than
parlays. The `Structure` and `Legs` columns are retained for an explicit future parlay
implementation.

| Executed (UTC) | Structure | Event/game | Legs | Side | Contracts | Fair | Quote | Edge | Edge/contract | Total modeled edge | RFQ | Quote | Order | Source |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
