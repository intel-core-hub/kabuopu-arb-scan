# Phase 14: conservative latency-budget overlay

## Purpose

Phase 12 measures how long displayed executable touch survives. Phase 13 asks whether
adverse touch depletion is often accompanied by nearby Last/Last Size callbacks. Neither
phase measures how long an order takes to reach OSE.

The TWS API does not expose an exchange-arrival timestamp for a hypothetical order, so
Phase 14 deliberately avoids pretending that it can measure one. Instead it adds a much
narrower observable:

1. repeatedly send the read-only `reqCurrentTime()` request;
2. measure local monotonic elapsed time until `currentTime()` returns;
3. treat a high quantile of that **full round-trip time** as a control-plane RTT proxy;
4. add an explicit safety margin;
5. ask whether Phase 12's displayed touch historically survived at least that conservative
   budget, while also requiring the Phase 13 turnover-evidence gate.

The RTT is **not divided by two**. There is no one-way inference.

## Components

### `scripts/ibkr_measure_api_rtt.py`

Read-only IBKR/TWS/IB Gateway probe. Requests are serialized because `reqCurrentTime()` has
no request id. The script records local send/callback timestamps, the server epoch returned
by TWS, and monotonic RTT in milliseconds. Warm-up samples are retained in the CSV but
excluded from quantile summaries.

The metric is named `IBKR_REQ_CURRENT_TIME_CONTROL_PLANE_RTT_PROXY`. It is not order-entry,
exchange, market-data, or one-way latency.

No order API is present.

### `scripts/analyze_latency_budget.py`

Fully offline. Inputs are:

- Phase 12 per-leg touch-survival CSV;
- Phase 13 per-leg depletion/concordance CSV;
- one or more Phase 14 RTT CSVs.

The default budget is:

```text
p95(full reqCurrentTime RTT proxy) + 100 ms safety margin
```

For each leg the analyzer chooses the smallest recorded Phase 12 horizon at or above that
budget. Example: a 220 ms budget uses `survival_250ms`, not interpolation. Because touch
survival should be non-increasing with horizon, rounding upward is deliberately conservative.

If the budget exceeds the longest recorded Phase 12 horizon, the result is not extrapolated;
it requests a longer Phase 12 recollection instead.

## Decisions

Per candidate:

- `LATENCY_BUDGET_SURVIVES_FOR_NEXT_RESEARCH`
- `LATENCY_BUDGET_NOT_SURVIVED`
- `RECOLLECT_LONGER_TOUCH_SURVIVAL_HORIZON`
- `COLLECT_MORE_TOUCH_SURVIVAL_DATA`
- `PHASE13_GATE_NOT_MET`
- `INPUT_INCOMPLETE`
- `NO_ANALYZABLE_LEGS`

Overall:

- `LATENCY_BUDGET_CANDIDATE_EXISTS`
- `NO_LATENCY_BUDGET_CANDIDATE`
- `COLLECT_API_RTT_DATA`
- `NO_ANALYZABLE_CANDIDATES`

The strongest decision means only that a conservative control-plane RTT proxy fits inside
historically observed displayed-touch survival often enough to justify further execution
research. It does not establish route latency, queue priority, fill probability, or atomicity.

Every output keeps:

```text
phase14_is_order_arrival_latency = false
phase14_is_exchange_latency = false
phase14_is_fill_probability = false
phase14_one_way_inference_used = false
phase14_live_money_allowed = false
```

## Example

Collect RTT proxy samples while connected to the same TWS/IB Gateway setup intended for the
market-data research:

```bash
python scripts/ibkr_measure_api_rtt.py \
  --host 127.0.0.1 \
  --port 7497 \
  --samples 50 \
  --warmup 5 \
  --interval-sec 0.5 \
  --output data/phase14_api_rtt.csv \
  --summary-json data/phase14_api_rtt_summary.json
```

Combine the evidence:

```bash
python scripts/analyze_latency_budget.py \
  data/phase12_touch_survival.csv \
  data/phase13_touch_depletion_summary.csv \
  data/phase14_api_rtt.csv \
  --latency-quantile 0.95 \
  --extra-budget-ms 100 \
  --min-survival 0.80 \
  --min-complete-triggers 20 \
  --min-depletions 5 \
  --min-corroboration 0.50 \
  --legs-output data/phase14_latency_legs.csv \
  --output data/phase14_latency_candidates.csv \
  --summary-json data/phase14_summary.json
```

## What Phase 14 still cannot answer

- The RTT proxy is not the time from an order submission to OSE receipt.
- It does not measure TWS order validation, IBKR routing, exchange gateway latency, queue
  position, hidden liquidity, or market impact.
- `reqCurrentTime()` is a control-plane request and can have different service-path behavior
  from market data or order entry.
- A surviving displayed touch does not imply the hypothetical order would have filled.

A positive Phase 14 result therefore justifies a later paper/instrumentation study of actual
order lifecycle timestamps; it does not authorize live-money execution.
