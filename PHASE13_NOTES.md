# Phase 13: quote-trade concordance for displayed touch depletion

## Why Phase 13 still is not fill probability

Phase 12 measures whether displayed executable touch survives for 100-2000 ms.  That is
already the most directly relevant observable for an aggressive BUY-at-ask / SELL-at-bid
leg.  It still cannot tell whether a displayed size reduction was caused by executions,
cancellations, replacement, hidden liquidity, feed aggregation, or callback ordering.

Phase 13 therefore adds a narrower second observable: **quote-trade concordance**.
It records ordinary streaming L1 `Last` / `Last Size` callbacks from `reqMktData` alongside
direct L2 changes, then asks whether adverse touch depletion is accompanied by a nearby
trade callback at the old touch price.

A match means only "trade activity was observed near this displayed depletion".  An
unmatched depletion is intentionally called **uncorroborated**, not "cancelled".  Neither
case establishes the probability that a hypothetical order would fill.

## Why this does not use real-time tick-by-tick option data

The recorder deliberately uses the ordinary streaming market-data request already proven
by Phase 11 rather than assuming that `reqTickByTickData` is available in real time for
OSE options.  The research remains valid if Last/Last Size callbacks are sparse: the
result will simply fail the evidence threshold and request more data.

## Components

### `scripts/ibkr_record_touch_depletion.py`

Read-only recorder.  It requires the Phase 12 overall decision
`TOUCH_SURVIVAL_ROBUST_ENOUGH_FOR_QUEUE_RESEARCH`, resolves the candidate's unique option
legs, and subscribes sequentially to live L1 plus direct L2.  After a warm-up period it
records:

- direct depth changes and reconstructed best bid/ask + size;
- L1 `Last` price callbacks;
- L1 `Last Size` callbacks;
- actual market-data type for every event.

Only market-data subscription cancellation is used.  There is no order submission,
modification, order cancellation, global cancellation, or What-If path.

### `scripts/analyze_touch_depletion.py`

Fully offline.  For the candidate action side it detects:

- visible size decreases at the same touch price;
- a move from the old touch to a worse executable price;
- same-price replenishment.

Each depletion is greedily matched to at most one set of nearby `Last Size` callbacks at
the **old touch price**, within a configurable callback-arrival window (default 250 ms).
This avoids double-using the same print for multiple depth reductions.

The output includes per-leg depletion count, corroborated depletion count, corroboration
rate, observed depleted size and matched Last Size volume.  The volume ratio is capped at
1 because Last Size and depth callbacks are not an exchange-level causal audit trail.

## Decisions

- `TOUCH_DEPLETION_CORROBORATED_ENOUGH_FOR_LATENCY_STUDY`
- `DISPLAYED_DEPLETION_MOSTLY_UNCORROBORATED`
- `COLLECT_MORE_TURNOVER_DATA`
- `NO_ANALYZABLE_LEGS`

The strongest decision only justifies a subsequent latency/arrival study.  It does not
authorize live-money execution and is not a fill-probability estimate.

Every summary keeps:

```text
phase13_live_money_allowed = false
phase13_is_fill_probability = false
phase13_cancellation_inference_allowed = false
```

## Example

```bash
python scripts/ibkr_record_touch_depletion.py \
  data/persistence_7203_new.csv \
  --phase12-summary data/phase12_touch_survival_summary.json \
  --candidate-id '<candidate-id>' \
  --duration-per-leg 120 \
  --depth-rows 5 \
  --output data/phase13_touch_depletion_events.csv \
  --summary-json data/phase13_recording_summary.json

python scripts/analyze_touch_depletion.py \
  data/phase13_touch_depletion_events.csv \
  --match-window-ms 250 \
  --min-depletions 5 \
  --min-corroboration 0.50 \
  --output data/phase13_touch_depletion_summary.csv \
  --details-output data/phase13_touch_depletion_events_matched.csv \
  --summary-json data/phase13_summary.json
```

## Interpretation

A low corroboration rate can mean cancellation-heavy displayed liquidity, sparse or
aggregated Last callbacks, callback timing mismatch, or other feed semantics.  Phase 13
cannot distinguish these causes and therefore must not rename the residual as
"cancellations".  A high rate only says that displayed depletion and nearby trade
activity often coincide; it still does not reveal the user's route latency, queue/arrival
priority, market impact, or actual fill probability.
