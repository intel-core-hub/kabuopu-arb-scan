# Phase 4: simultaneous streaming persistence check

Phase 3 validates a delayed-board candidate against IBKR executable bid/ask sides, but a one-shot snapshot can still capture a transient or internally stale combination. Phase 4 keeps every leg of one candidate subscribed at the same time and samples the executable package repeatedly.

## What it measures

- exact BUY-side ask / SELL-side bid freshness (timestamps are tracked per executable side)
- cross-leg executable-side timestamp skew
- post-fee edge at each observation
- displayed size after leg ratios (for example 1:-2:1), requiring at least one complete package
- exact-side displayed-size freshness as well as price freshness
- the actual IBKR market-data type delivered on every leg
- longest continuously observed live-positive run

A positive delayed/frozen quote is never called live. A stale executable price/size is excluded rather than carried forward indefinitely. A quote with missing or insufficient displayed size is not counted as a live-positive observation.

## Usage

```bash
python scripts/ibkr_monitor_findings.py data/findings_7203.csv \
  --market-data-type live \
  --exchange OSE.JPN \
  --limit 5 \
  --duration 10 \
  --sample-interval 0.5 \
  --warmup 2 \
  --max-quote-age 3 \
  --output data/persistence_7203.csv
```

The tool is read-only. It uses `reqMktData(..., snapshot=False)` and ends each quote stream with `cancelMktData`; it has no order-submission code.

## Statuses

- `PERSISTENT_LIVE_CANDIDATE`: live-positive observations span at least 80% of the configured monitor duration (using a conservative observed-run lower bound)
- `INTERMITTENT_LIVE_CANDIDATE`: at least one live-positive observation occurred, but persistence was shorter
- `NO_PERSISTENT_LIVE_EDGE`: no live-positive observation occurred
- `ERROR`: contract resolution / API / input failure

These remain research signals, not execution guarantees. Multi-leg fills are not atomic and displayed size can disappear before an order reaches the venue.

## References

- IBKR TWS API market-data request: `snapshot=False` requests a stream rather than a snapshot.
- IBKR market-data lines limit simultaneous subscriptions; monitor only a small number of candidate packages at once.
- IBKR market-data availability distinguishes real-time from delayed/frozen data.
