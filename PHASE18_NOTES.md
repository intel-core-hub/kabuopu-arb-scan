# Phase 18: event-driven synchronized package intervals

## Purpose

Phase 17 uses synchronized fixed-cadence Phase 4 samples. That is useful, but a 500 ms sample cadence can miss quote changes between checkpoints. Phase 18 therefore records every relevant IBKR streaming callback for all candidate legs concurrently and reconstructs **locally observed event-driven package-valid intervals** offline.

This deliberately remains a research metric:

- it is based on local callback receipt time, not exchange timestamps;
- it is not fill probability;
- it is not order-arrival probability;
- it is not evidence of atomic execution;
- it never submits, modifies, or cancels orders;
- `cancelMktData` only stops market-data subscriptions.

## Capture

`ibkr_record_package_events.py` requires a candidate that already has:

`SYNCHRONIZED_PACKAGE_TOUCH_ROBUST_ENOUGH_FOR_NEXT_RESEARCH`

from Phase 17. It also uses one enriched Phase 4 row to recover the underlying, expiry, `legs_json`, floor PV, lot size and fee assumption.

Each bid/ask price, bid/ask size and `marketDataType` callback is stored with a local monotonic elapsed timestamp. Warmup callbacks are kept to seed state. The scored window is explicitly delimited by `ANALYSIS_START` and `ANALYSIS_END` markers.

## Offline interval reconstruction

A package is valid only when every leg simultaneously has:

1. actual `marketDataType == 1`;
2. the executable side available (BUY -> ask, SELL -> bid);
3. displayed size at least the leg quantity;
4. price and size update ages no greater than `max_state_age_sec`;
5. positive package edge after the configured fee assumption.

The analyzer splits state intervals at callback receipt times **and at synthetic staleness boundaries**. Therefore a quiet quote does not remain valid indefinitely merely because no later callback arrived.

## Candidate gate

Default research gate:

- at least 3 usable event-driven sessions;
- at least 20 callback rows per session;
- session-bootstrap lower bound of valid package time ratio >= 0.80;
- at least 2/3 of sessions contain one valid package interval lasting >= 1 second.

Passing produces:

`EVENT_DRIVEN_PACKAGE_INTERVALS_ROBUST_ENOUGH_FOR_NEXT_RESEARCH`

This is not a live-trading approval.

## Example

```bash
python scripts/ibkr_record_package_events.py \
  data/phase17_package_candidates.csv \
  --phase4-input data/persistence_7203_new.csv \
  --candidate-id '<candidate-id>' \
  --duration 30 \
  --output data/phase18_events_001.csv \
  --summary-json data/phase18_recording_001.json
```

Repeat at least three sessions, then:

```bash
python scripts/analyze_event_driven_package_intervals.py \
  data/phase17_package_candidates.csv \
  --event-inputs 'data/phase18_events_*.csv' \
  --max-state-age-sec 3 \
  --target-interval-sec 1 \
  --min-valid-time-ratio 0.80 \
  --sessions-output data/phase18_sessions.csv \
  --intervals-output data/phase18_intervals.csv \
  --output data/phase18_candidates.csv \
  --summary-json data/phase18_summary.json
```

## Interpretation

A passing candidate means only that, on the local API callback stream, the entire displayed executable package was observed to remain live, sized, fresh, and positive for sufficiently long event-driven intervals across multiple sessions. It does not prove that the exchange book was continuously unchanged between callbacks, nor that an order would have filled.
