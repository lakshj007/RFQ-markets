# RFQ Fill Ledger

This file is updated when the maker observes an executed RFQ. Net edge includes the
actual fill fee when the Fills API returns it, otherwise the conservative modeled fee.
Parlay fair value is the product of independent selected-leg moneyline probabilities;
the configured proportional edge is applied once to that complete parlay fair value.

| Executed (UTC) | Structure | Event/game | Legs | Side | Contracts | Fair | Quote | Gross edge | Fee | Fee source | Net edge | Gross edge $ | Net edge $ | RFQ | Quote | Order | Source |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- | --- | --- | --- |
