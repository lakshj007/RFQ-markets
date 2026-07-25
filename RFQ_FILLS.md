# RFQ Fill Ledger

This file is updated when the maker observes an executed RFQ. Edge is the modeled
pre-fee edge at confirmation when available, otherwise the quote-time edge. Parlay
fair value is the product of independent selected-leg moneyline probabilities; the
configured proportional edge is applied once to that complete parlay fair value.

| Executed (UTC) | Structure | Event/game | Legs | Side | Contracts | Fair | Quote | Edge | Edge/contract | Total modeled edge | RFQ | Quote | Order | Source |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
