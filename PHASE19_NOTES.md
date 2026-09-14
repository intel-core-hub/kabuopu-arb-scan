# Phase 19: event-driven package failure modes

## Purpose

Phase 18 reconstructs locally observed intervals during which every option leg is simultaneously live, fresh, sufficiently sized, executable, and positive after fees. Phase 19 asks a different question:

> When one of those locally valid package intervals ends, what observable condition broke first?

The analysis is fully offline and replays the original Phase 18 callback logs with the same validity rules. It classifies each **valid -> invalid** boundary into one of these observed modes:

- `PRICE_EDGE_COLLAPSE`
- `DISPLAYED_SIZE_LOSS`
- `MARKET_DATA_TYPE_LOSS`
- `STALE_PRICE`
- `STALE_SIZE`
- `QUOTE_INVALID_OR_MISSING`
- `OTHER`

`ANALYSIS_END` is **right censoring**, not a failure. This matters because otherwise short recording windows mechanically inflate the apparent failure count.

## What the metric is not

Phase 19 deliberately keeps these flags false:

- `phase19_is_exchange_hazard = false`
- `phase19_is_fill_probability = false`
- `phase19_cancellation_inference_allowed = false`
- `phase19_live_money_allowed = false`

The timestamps are local callback receipt times. An uncorroborated size loss is not called a cancellation. A price-driven loss of package edge is not proof that a real order could or could not have filled first.

## Attribution

For failures such as `LEG_2_INSUFFICIENT_SIZE`, `LEG_2_STALE_PRICE`, or `LEG_2_NONLIVE_OR_UNVERIFIED`, the breaker leg is directly encoded by the Phase 18 validity reason.

For `NONPOSITIVE_EDGE`, Phase 19 looks at executable-side price callbacks at the same local boundary:

- BUY legs: `ASK_PRICE`
- SELL legs: `BID_PRICE`

If exactly one leg changed its executable-side price, attribution is `SINGLE_BREAKER_LEG`. If multiple legs changed at the boundary, the result remains `MULTIPLE_POSSIBLE_BREAKER_LEGS` rather than inventing a causal order.

Synthetic staleness boundaries remain explicitly labeled `STALENESS`.

## Candidate decision

The default candidate gate requires:

- Phase 18 decision `EVENT_DRIVEN_PACKAGE_INTERVALS_ROBUST_ENOUGH_FOR_NEXT_RESEARCH`
- at least 3 analyzed event sessions
- at least 5 observed valid->invalid failures

Then:

- `OBSERVATION_STALENESS_DOMINATES` if stale price/size accounts for at least 50% of failures;
- `MARKET_DATA_INSTABILITY_DOMINATES` if market-data-type loss accounts for at least 50%;
- otherwise `FAILURE_MODES_CHARACTERIZED_FOR_NEXT_RESEARCH`.

The staleness/market-data decisions are diagnostic stops, not claims that the underlying market itself is unstable.

## Example

```bash
python scripts/analyze_package_failure_modes.py \
  data/phase18_candidates.csv \
  --event-inputs 'data/phase18_events_*.csv' \
  --max-state-age-sec 3 \
  --min-sessions 3 \
  --min-failures 5 \
  --artifact-dominance-threshold 0.50 \
  --sessions-output data/phase19_sessions.csv \
  --failures-output data/phase19_failures.csv \
  --output data/phase19_candidates.csv \
  --summary-json data/phase19_summary.json
```

## Interpretation

Useful fields include:

- `dominant_failure_mode`
- `dominant_failure_mode_share`
- `local_failure_incidence_per_valid_minute`
- `staleness_failure_share`
- `market_data_type_failure_share`
- `breaker_leg_counts_json`
- `attribution_quality`

The incidence rate is denominated by **locally valid package time** and should be described as a local-observation failure incidence, not an exchange hazard rate.
