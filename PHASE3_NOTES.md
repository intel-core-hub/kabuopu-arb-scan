# Phase 3: IBKR live quote re-validation

Phase 2 can find static-arbitrage candidates in the public JPX-linked quote board, but that board is about 15 minutes delayed. Phase 3 therefore re-prices only the strongest delayed candidates against IBKR market-data snapshots.

**This phase is read-only. `ibkr_validate_findings.py` never places, modifies, or cancels an order.**

## Why this is the next step

JPX explicitly warns that the public kabu-opu price page is delayed by about 15 minutes and directs users to their broker for real-time quotes. IBKR's TWS API supports option contract discovery and `reqMktData` snapshots. IBKR currently lists `OSE.JPN` as the IB exchange name for Osaka Exchange market data and lists an Osaka Exchange real-time subscription. Current IBKR documentation also distinguishes the market-data type actually delivered (live, frozen, delayed, delayed-frozen). Phase 3 therefore records the actual callback instead of assuming that a request for live data was fulfilled as live.

References:

- JPX, quote-board explanation (15-minute delay): https://www.jpx.co.jp/ose-toshijuku/column/10.html
- IBKR Campus, defining contracts: https://ibkrcampus.com/campus/trading-lessons/defining-contracts-in-the-tws-api/
- IBKR Campus, market data via Python: https://ibkrcampus.com/campus/trading-lessons/python-receiving-market-data/
- IBKR Japan market-data pricing / exchange codes: https://www.interactivebrokers.co.jp/en/pricing/market-data-pricing.php
- IBKR option commissions: https://www.interactivebrokers.com/en/pricing/commissions-options.php?region=asia-pacific
- IBKR market-data availability codes: https://ibkrcampus.com/docs/web-api/v1/endpoints/market-data/market-data-availability
- IBKR delayed TWS tick IDs: https://interactivebrokers.github.io/tws-api/tick_types.html

## Scanner output change

`quote_arbitrage_scan.py` now adds fields needed for a downstream executable-side recheck:

- `legs_json`: machine-readable actions, option type, strike, and quantity
- `floor_pv_per_share`: present value of the strategy's guaranteed payoff floor
- `lot_size`
- `fee_legs`
- `fee_per_contract_leg`
- `rate`
- `as_of`

The human-readable `legs` column remains unchanged.

The key identity used by the live validator is:

```
live gross edge/share = guaranteed floor PV/share - live executable net debit/share
```

This covers crossed spreads, negative-debit verticals, vertical upper-bound violations, butterflies, long boxes, and reverse boxes with one common re-pricing rule.

## Setup

Install the normal project dependencies, then install the optional IBKR Python API dependency:

```bash
pip install -r requirements.txt
pip install -r requirements-ibkr.txt
```

Alternatively install the official Python API from the TWS API bundle supplied by IBKR.

Enable socket/API clients in TWS or IB Gateway, and make sure the account has the market-data permissions needed for the desired mode. The validator defaults to `127.0.0.1:7497`, but host/port/client ID are configurable. The default request timeout is 12 seconds because IBKR snapshot requests aggregate roughly 11 seconds of market data.

## Flow

First fetch and scan the delayed board as before:

```bash
python scripts/fetch_kabuopu_quotes.py \
  --underlying 7203 \
  --output data/quotes_7203.csv

python scripts/quote_arbitrage_scan.py \
  data/quotes_7203.csv \
  --fee-per-contract-leg 100 \
  --output data/findings_7203.csv
```

Then re-check the strongest candidates in IBKR:

```bash
python scripts/ibkr_validate_findings.py \
  data/findings_7203.csv \
  --market-data-type live \
  --exchange OSE.JPN \
  --limit 20 \
  --output data/live_validation_7203.csv
```

Output status:

- `CONFIRMED_CANDIDATE`: positive after the configured per-contract fee/slippage allowance **and every leg was explicitly reported by IBKR as live market data**
- `NONLIVE_CANDIDATE`: positive edge remains, but at least one leg was frozen/delayed/delayed-frozen; this is research-only and must not be treated as a live confirmation
- `UNVERIFIED_DATA_TYPE`: positive edge remains, but IBKR did not provide a market-data-type callback; the script refuses to silently assume live data
- `NO_LONGER_POSITIVE`: the edge disappeared at the received executable sides
- `ERROR`: contract ambiguity, missing quote side, API/permission issue, etc.

`ibkr_actual_market_data_types` and `ibkr_actual_market_data_type_names` report what IBKR actually delivered. `live_quotes_json` also stores the data type per leg. Delayed TWS ticks (66/67/69/70) are parsed so `--market-data-type delayed` remains useful for diagnostics without ever being mislabeled as a live confirmation.

`live_min_quote_size` accounts for leg quantity (for example, a butterfly body quoted for 12 contracts supports at most 6 complete 1:-2:1 packages).

`snapshot_skew_ms` records the spread in quote-receipt timestamps across the legs. These are still separate market-data snapshots, not atomic fills.

## Important contract-resolution note

IBKR's current public pages use `OSE.JPN` for Osaka Exchange market data, while some commission pages label Japanese stock options with `TSEJ`. The validator therefore makes exchange configurable instead of baking exchange assumptions deeper into the strategy logic. If a precise option returns zero or multiple contract matches, inspect the instrument in TWS (`Contract Info -> Description/Details`) and retry with the exchange shown there.

Adjusted option series after corporate actions can also be ambiguous. The validator intentionally refuses to guess when `reqContractDetails` returns anything other than exactly one contract.

## What counts as a GO signal

A `CONFIRMED_CANDIDATE` is still only a research hit. It now means the edge was positive and IBKR explicitly identified all received leg data as live. Before execution, verify at minimum:

1. all required sizes are simultaneously displayed;
2. the contract multiplier / lot size matches the candidate calculation;
3. fees, taxes, exchange charges, and expected slippage are fully included;
4. short legs are permitted and margin is sufficient;
5. the edge survives another immediate refresh;
6. practical multi-leg execution risk is acceptable.

The script intentionally stops before order placement.
