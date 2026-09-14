# Phase 12: empirical displayed-touch survival from live OSE L2

## Why this is not called fill probability

Phase 11 only establishes whether the user's IBKR API session actually receives live L1 and direct L2 callbacks. Even with L2, the feed does not reveal the user's future queue position, hidden liquidity, exchange matching priority effects, cancellations ahead of an order, or market impact from submitting the order.

Phase 12 therefore measures a narrower observable quantity: **displayed-touch survival**.

For a BUY leg, a trigger is eligible when the live best ask has at least the required visible quantity. Survival through horizon `h` means the displayed executable ask remains continuously at the trigger price or better, with enough visible size, through `h`. SELL legs use the symmetric rule on the bid.

This is useful as a stress/calibration statistic for Phase 10 legging delays, but it is not a fill-probability estimate.

## Components

### `scripts/ibkr_record_depth_survival.py`

Read-only recorder. It requires a Phase 11 JSON with `READY_FOR_L2_FILL_MODEL`, resolves the candidate's unique OSE option legs, and records direct depth events plus reconstructed top-of-book state.

Depth subscriptions are made **sequentially** per leg. IBKR documents a much tighter limit for Level II subscriptions than ordinary market-data lines; sequential capture avoids assuming the account has enough simultaneous depth lines for a 4-leg candidate.

The recorder contains no order submission/modification/cancellation logic. `cancelMktDepth` and `cancelMktData` only stop market-data subscriptions.

### `scripts/analyze_touch_survival.py`

Pure offline analysis. It forward-fills depth-event states onto a regular grid, identifies executable live triggers, and reports survival at configurable horizons (default 100/250/500/1000/2000 ms).

The last `max(horizon)` portion of each recording is excluded from the denominator because the future observation window is incomplete.

## Decisions

- `TOUCH_SURVIVAL_ROBUST_ENOUGH_FOR_QUEUE_RESEARCH`
- `DISPLAYED_TOUCH_NOT_ROBUST`
- `COLLECT_MORE_DEPTH_DATA`
- `NO_ANALYZABLE_LEGS`

The strongest decision means only that the displayed touch is empirically stable enough to justify further queue/fill research. It never enables live trading.

## Example

```bash
python scripts/ibkr_record_depth_survival.py \
  data/persistence_7203_new.csv \
  --phase11-summary data/phase11_marketdata_probe_summary.json \
  --candidate-id '<candidate-id>' \
  --duration-per-leg 60 \
  --depth-rows 5 \
  --output data/phase12_depth_events.csv \
  --summary-json data/phase12_recording_summary.json

python scripts/analyze_touch_survival.py \
  data/phase12_depth_events.csv \
  --horizons-ms 100,250,500,1000,2000 \
  --grid-step-ms 50 \
  --target-horizon-ms 500 \
  --min-survival 0.80 \
  --min-complete-triggers 20 \
  --output data/phase12_touch_survival.csv \
  --summary-json data/phase12_touch_survival_summary.json
```

## Interpretation

If the 500 ms survival is poor on any leg, Phase 10's 500 ms sequential-leg assumption is already optimistic and the non-atomic path should be treated as weak. If survival remains high at 1-2 seconds, that is evidence about displayed quote persistence only; it still does not establish that a newly submitted order would receive a fill.
