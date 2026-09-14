# Phase 17: synchronized package-level displayed-touch retention

## Purpose

Phase 16 overlaid the paper/TWS first-callback timing distribution on each leg's **marginal** Phase 12 displayed-touch survival, then conservatively used the weakest leg rather than multiplying marginal survivals.

Phase 17 removes that marginal-leg approximation for the next research gate. It goes back to the enriched Phase 4 `monitor_samples_json`, where all candidate legs were sampled together, and directly measures whether the **whole displayed executable package** remains no worse at synchronized sampled checkpoints.

This is still deliberately narrower than a fill model.

- It is a joint **displayed-touch metric** across legs.
- It is not continuous-time survival between samples.
- It is not exchange-arrival probability.
- It is not fill probability.
- It is not evidence of atomic execution.
- It makes no leg-independence assumption.
- It never submits or cancels orders and has no IBKR dependency.

## Trigger and retention rule

A trigger must be a Phase 4 sample with:

1. `sample_status == LIVE_POSITIVE`;
2. complete `leg_snapshots`;
3. actual `market_data_type == 1` for every leg;
4. displayed executable size at least the leg quantity.

For each configured horizon, Phase 17 uses the first sampled checkpoint at or after the target time. Every sampled checkpoint from the trigger through that target must satisfy all of the following for every leg:

- the package sample is still `LIVE_POSITIVE`;
- market data is still live;
- displayed size still covers the leg quantity;
- BUY ask is no worse than the trigger ask;
- SELL bid is no worse than the trigger bid.

If any leg fails, the whole package fails that trigger/horizon.

The target sample is allowed only a bounded overshoot relative to the observed median Phase 4 cadence. The default is 1.5× the median interval, preventing a sparse future sample from being mislabeled as a precise 500ms/1s observation.

Triggers too close to the end of the monitor window are excluded from the denominator rather than counted as failures.

## Candidate aggregation

Phase 17 requires the Phase 16 decision:

`TOUCH_RETAINS_THROUGH_PAPER_CONTROL_PATH_FOR_NEXT_RESEARCH`

For the selected target horizon (default 1 second), each session must have a minimum number of complete triggers. Candidate confidence is then based on the **distribution of per-session retention ratios**, not on every overlapping trigger as if it were independent.

A bootstrap over sessions produces a lower confidence bound. The strongest decision is:

`SYNCHRONIZED_PACKAGE_TOUCH_ROBUST_ENOUGH_FOR_NEXT_RESEARCH`

It still leaves all live-money/fill-probability claims false.

## Usage

```bash
python scripts/analyze_synchronized_package_survival.py \
  data/phase16_candidates.csv \
  --phase4-inputs 'data/persistence_*_new.csv' \
  --horizons-sec 0.5,1,2 \
  --target-horizon-sec 1 \
  --min-sessions 3 \
  --min-complete-triggers-per-session 5 \
  --min-retention 0.80 \
  --sessions-output data/phase17_package_sessions.csv \
  --triggers-output data/phase17_package_triggers.csv \
  --output data/phase17_package_candidates.csv \
  --summary-json data/phase17_summary.json
```

## Decisions

- `SYNCHRONIZED_PACKAGE_TOUCH_ROBUST_ENOUGH_FOR_NEXT_RESEARCH`
- `SYNCHRONIZED_PACKAGE_TOUCH_NOT_ROBUST`
- `COLLECT_MORE_SYNCHRONIZED_SESSIONS`
- `COLLECT_MORE_SYNCHRONIZED_PACKAGE_DATA`
- `NEEDS_PHASE4_RECOLLECTION`
- `NO_PHASE4_SESSIONS`
- `PHASE16_GATE_NOT_MET`

## Interpretation

A positive Phase 17 result means that in the captured Phase 4 sessions, all displayed executable legs jointly remained no worse at the sampled checkpoints often enough to clear the configured session-level threshold.

It does **not** say that the package was executable continuously between checkpoints, that an order would have reached OSE before the quotes changed, that all legs would fill, or that non-atomic execution is profitable after market impact. Those remain separate research questions.
