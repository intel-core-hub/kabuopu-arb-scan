# Phase 5: reproducibility gate across repeated live-monitor sessions

Phase 4 answers: "did a positive executable-looking edge persist during this one short live monitoring window?"
That is not enough to justify execution engineering. Phase 5 aggregates repeated Phase 4 CSVs and asks whether the
same candidate recurs across independent monitoring sessions with acceptable quote quality.

## What changed

- `ibkr_monitor_findings.py` now records `monitor_started_at_utc` and `monitor_finished_at_utc`.
- `evaluate_persistence.py` accepts multiple Phase 4 CSVs (or glob patterns), deduplicates a candidate within each
  file/session, matches the same candidate across sessions, and writes a ranked reproducibility table.
- The tool remains fully read-only. Phase 5 does not connect to IBKR at all.

## Research decisions

- `PROMOTE_TO_EXECUTION_STUDY`: enough repeated sessions exist and all configured reproducibility/data-quality gates pass.
  This means "worth studying execution mechanics next", **not** "place a trade".
- `KEEP_OBSERVING`: some live-positive evidence exists but one or more repeatability/edge/size/skew gates fail.
- `INSUFFICIENT_EVIDENCE`: not enough non-error sessions have been collected.
- `NO_REPRODUCIBLE_LIVE_EDGE`: enough sessions exist but none produced a live-positive Phase 4 result.

Default promotion gates are deliberately conservative and configurable:

- at least 3 usable sessions
- at least 2 persistent sessions
- persistent-session ratio >= 60%
- live-positive observation ratio >= 80%
- median of per-session minimum live edge >= JPY 1,000 per package after configured fees
- worst displayed executable package size >= 1
- maximum observed cross-leg executable-side timestamp skew <= 1,000 ms
- timestamps available on at least 1 distinct monitoring day

## Example workflow

Run Phase 4 repeatedly, saving a new file each time:

```bash
python scripts/ibkr_monitor_findings.py data/findings_mm50.csv \
  --market-data-type live --exchange OSE.JPN --limit 10 --duration 10 \
  --output data/persistence_20260914_1200.csv
```

After several runs:

```bash
python scripts/evaluate_persistence.py 'data/persistence_*.csv' \
  --min-sessions 3 \
  --min-persistent-sessions 2 \
  --min-edge-jpy 1000 \
  --output data/reproducibility_ranking.csv \
  --summary-json data/reproducibility_summary.json
```

For evidence spanning multiple trading days, raise `--min-distinct-days` (for example to 3).

## Interpretation

A promoted candidate is only a research handoff. Multi-leg fills are not atomic; displayed size may vanish; queue
position, exchange fees/rebates, margin, contract multipliers, corporate actions, exercise/assignment mechanics,
and order-combination support still need explicit validation before any execution-capable code is considered.
