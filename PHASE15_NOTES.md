# Phase 15: paper order-lifecycle timing

## Purpose

Phase 14 deliberately used the full `reqCurrentTime()` round trip plus a safety margin as a conservative control-plane latency budget. It did **not** measure order arrival at OSE.

Phase 15 adds a second, still-limited observation: on an IBKR **paper** account only, submit one already-approved research BAG order and timestamp the local elapsed time until TWS/API callbacks such as `openOrder`, `orderStatus`, `execDetails`, or an order-specific error.

This is useful for checking whether the paper/TWS order-control path is obviously slower than the Phase 14 budget. It still does **not** establish production routing latency, exchange arrival time, OSE atomicity, queue position, or fill probability.

## Safety invariants

`ibkr_measure_paper_order_lifecycle.py`:

- requires a Phase 14 candidate decision of `LATENCY_BUDGET_SURVIVES_FOR_NEXT_RESEARCH`;
- defaults to plan-only mode;
- requires `--run-paper` plus an exact acknowledgement phrase;
- requires a managed `DU...` paper account, reusing Phase 7 guards;
- submits at most one candidate / one package per invocation;
- performs no price replace;
- if the order remains active after the observation window, cancels only that order ID;
- never calls `reqGlobalCancel`;
- always writes `phase15_live_money_allowed=False`;
- always writes `phase15_is_exchange_arrival_latency=False` and `phase15_is_live_latency=False`.

IBKR documents that correctly submitted orders trigger `openOrder` and `orderStatus` activity, and recommends monitoring `error`, `orderStatus`, `openOrder`, and `execDetails`. Paper trading uses a simulator, so execution behavior can differ from live trading.

## Measurement fields

The probe records local monotonic milliseconds from just before `placeOrder` to:

- return from the local `placeOrder` call;
- first callback of any order-lifecycle type;
- `openOrder`;
- first `orderStatus`;
- `PreSubmitted` / `Submitted` when present;
- first `execDetails`;
- first order-specific error;
- terminal order status;
- cancel send and cancel-to-terminal interval when cancellation is required.

The callback sequence is preserved in `phase15_events_json`.

## Offline aggregation

`analyze_paper_order_lifecycle.py` groups repeated runs by `candidate_id` and compares a configured quantile (default p95) of `phase15_first_callback_ms` against the Phase 14 budget carried into each trace.

Possible candidate decisions include:

- `PAPER_ACK_P95_WITHIN_PHASE14_BUDGET`
- `PAPER_ACK_P95_EXCEEDS_PHASE14_BUDGET`
- `COLLECT_MORE_PAPER_LIFECYCLE_DATA`
- `PAPER_LIFECYCLE_REJECTED_OR_UNACKNOWLEDGED`
- `INPUT_INCOMPLETE`

Even the strongest result means only that repeated **paper callback acknowledgement** was observed within the already-conservative research budget.

## Example

Plan only:

```bash
python scripts/ibkr_measure_paper_order_lifecycle.py \
  data/execution_study_whatif.csv \
  data/phase14_latency_candidates.csv \
  --candidate-id '<candidate-id>' \
  --output data/phase15_plan.csv
```

One paper timing run:

```bash
python scripts/ibkr_measure_paper_order_lifecycle.py \
  data/execution_study_whatif.csv \
  data/phase14_latency_candidates.csv \
  --candidate-id '<candidate-id>' \
  --run-paper \
  --account DU1234567 \
  --paper-ack I_UNDERSTAND_THIS_SUBMITS_ONE_SIMULATED_PAPER_ORDER_FOR_TIMING_RESEARCH \
  --port 4002 \
  --output data/phase15_lifecycle_001.csv
```

After at least three runs:

```bash
python scripts/analyze_paper_order_lifecycle.py \
  'data/phase15_lifecycle_*.csv' \
  --min-sessions 3 \
  --timing-quantile 0.95 \
  --output data/phase15_paper_lifecycle_summary.csv \
  --summary-json data/phase15_summary.json
```
