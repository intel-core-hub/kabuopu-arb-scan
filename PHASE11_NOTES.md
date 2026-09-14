# Phase 11: OSE real-time/L2 API capability gate

## Why this phase comes before queue/fill modelling

Phase 10 deliberately stops at top-of-book quote-touch replay and lists queue/fill probability as an unmodelled limitation.
Before building a deeper execution model, the project needs to establish that the actual IBKR API session can supply the market data required for that model.

There is a documentation ambiguity worth testing rather than guessing around:

- current IBKR market-data pricing pages list **Osaka Exchange (L1)** and **Osaka Exchange (L2)** real-time subscriptions;
- older TWS API documentation historically stated that OSE API market data was delayed-only.

Phase 11 therefore performs a read-only capability probe against the user's own TWS/IB Gateway session.

## What the probe does

`scripts/ibkr_probe_marketdata_capability.py`:

1. reads one candidate from an enriched Phase 4 CSV;
2. resolves each unique OSE option contract;
3. requests L1 with `reqMktData` and records the authoritative `marketDataType` callback;
4. requests direct market depth with `reqMktDepth(..., isSmartDepth=False)`;
5. records whether two-sided depth callbacks with positive displayed size arrive;
6. cancels only the market-data subscriptions;
7. writes per-leg CSV evidence plus a JSON summary.

It contains no order submission, modification, cancellation, What-If, or account-trading logic.

## Decisions

Per leg:

- `REALTIME_L2_OBSERVED`
- `LIVE_L1_ONLY`
- `L2_ONE_SIDED`
- `L2_NO_POSITIVE_SIZE`
- `LIVE_L1_INCOMPLETE`
- `NONLIVE_L1`
- `L1_UNAVAILABLE_OR_UNVERIFIED`

Overall:

- `READY_FOR_L2_FILL_MODEL`
- `REALTIME_L1_ESTABLISHED_L2_NOT_ESTABLISHED`
- `REALTIME_L1_PARTIAL`
- `OSE_API_REALTIME_NOT_ESTABLISHED`
- `PROBE_INCOMPLETE`

Even `READY_FOR_L2_FILL_MODEL` is only a data-capability result. It does **not** establish queue position, fill probability, atomicity, or profitability.

## Example

```bash
python scripts/ibkr_probe_marketdata_capability.py \
  data/persistence_7203_new.csv \
  --candidate-id '<candidate-id>' \
  --exchange OSE.JPN \
  --duration 8 \
  --depth-rows 5 \
  --output data/phase11_marketdata_probe.csv \
  --summary-json data/phase11_marketdata_probe_summary.json
```

Use the API port appropriate for the currently running TWS/IB Gateway session.

## Next gate

Only if **every leg** returns `REALTIME_L2_OBSERVED` should the next phase build an empirical depth/quote-survival model.
If the probe returns delayed/frozen L1 or no usable depth, stop the IBKR-based fill-model path and choose a different real-time data source rather than inventing queue/fill probabilities from delayed top-of-book data.
