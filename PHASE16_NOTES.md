# Phase 16: conservative touch-retention × paper-control-path overlay

## Purpose

Phase 12 measured **displayed-touch survival** from direct L2 observations. Phase 15 measured **paper/TWS first-callback timing** after one guarded paper BAG submission. Neither measurement is exchange-arrival latency or fill probability.

Phase 16 combines those two empirical observations without upgrading either claim. For each measured Phase 15 first-callback time, it adds an explicit safety margin and maps that delay to the **next-longer** Phase 12 survival horizon. It never interpolates between horizons and never extrapolates beyond the longest observed horizon.

For a multi-leg candidate, the sample score is the **minimum marginal displayed-touch survival across the legs**. The script deliberately does **not** multiply leg survivals, because that would impose an unjustified independence assumption and could be mistaken for a joint fill probability.

## Inputs

- `data/phase12_touch_survival.csv` from `analyze_touch_survival.py`
- `data/phase15_paper_lifecycle_summary.csv` from `analyze_paper_order_lifecycle.py`
- the raw Phase 15 lifecycle CSVs, so the empirical `phase15_first_callback_ms` distribution can be used rather than only a p95 summary

A candidate must already have a Phase 15 decision ending in `WITHIN_PHASE14_BUDGET`.

## Conservative mapping

If the paper callback sample is 137ms and `--extra-latency-ms 100`, the effective research delay is 237ms. With Phase 12 horizons `100,250,500,1000,2000`, Phase 16 uses **250ms** survival. It does not interpolate 237ms between 100ms and 250ms.

If a timing sample plus safety margin exceeds the longest available Phase 12 horizon, that sample is out of range. If the out-of-range fraction exceeds `--max-out-of-range-fraction`, the decision is `RECOLLECT_LONGER_TOUCH_SURVIVAL_HORIZON` rather than extrapolating.

## Decisions

- `TOUCH_RETAINS_THROUGH_PAPER_CONTROL_PATH_FOR_NEXT_RESEARCH`
- `TOUCH_RETENTION_NOT_ROBUST_TO_CONTROL_PATH`
- `RECOLLECT_LONGER_TOUCH_SURVIVAL_HORIZON`
- `COLLECT_MORE_PAPER_TIMING_DATA`
- `COLLECT_MORE_TOUCH_SURVIVAL_DATA`
- `PHASE15_GATE_NOT_MET`
- `INPUT_INCOMPLETE`

The strongest decision only means that the **bootstrap lower bound of the weakest-leg marginal displayed-touch retention score** cleared the configured threshold under the observed paper callback timing distribution plus the explicit safety margin.

## Non-claims / safety invariants

Phase 16 is fully offline and contains no IBKR API calls. It always preserves these interpretations:

- `phase16_is_fill_probability=False`
- `phase16_is_order_arrival_probability=False`
- `phase16_is_joint_leg_probability=False`
- `phase16_independence_assumption_used=False`
- `phase16_live_money_allowed=False`

It does not establish OSE arrival latency, live routing latency, queue priority, hidden liquidity, joint leg survival, execution probability, or expected P&L.

## Example

```bash
python scripts/analyze_end_to_end_touch_retention.py \
  data/phase12_touch_survival.csv \
  data/phase15_paper_lifecycle_summary.csv \
  --phase15-traces 'data/phase15_lifecycle_*.csv' \
  --extra-latency-ms 100 \
  --min-sessions 3 \
  --min-retention 0.80 \
  --bootstrap-reps 2000 \
  --legs-output data/phase16_touch_latency_legs.csv \
  --samples-output data/phase16_touch_latency_samples.csv \
  --output data/phase16_candidates.csv \
  --summary-json data/phase16_summary.json
```

## Next gate

Only if the strongest Phase 16 result survives across multiple market sessions should the next phase study **cross-leg dependence / joint touch survival from synchronized L2 observations**. That is the missing piece before any probability-like package metric can be discussed responsibly. Live-money automation remains out of scope.
