# Phase 8: repeated paper-trace mechanics analysis

Phase 8 is a **fully offline** analysis layer for the CSV traces produced by
Phase 7 (`ibkr_paper_combo_test.py`). It does not import `ibapi`, connect to
TWS/IB Gateway, or submit/modify/cancel any order.

## Why this phase exists

A single `PAPER_FULL_FILL_OBSERVED` event is not a production execution result.
IBKR paper trading is simulated and has known limitations, especially for combo
trading. In addition, IBKR documents that `orderStatus` callbacks may be duplicated
or omitted for some transitions and recommends monitoring `execDetails` as well.

Phase 8 therefore studies the *trace mechanics* across repeated paper sessions:

- whether `execDetails` is reported as parent `BAG`, individual `OPT` legs, or both;
- whether an `OPT` callback is observed materially before the parent full-fill
  callback (a callback-sequence risk flag, **not proof of real legging**);
- whether the trace contains structural combo errors (IBKR codes 312/313/314);
- whether exact duplicate `execDetails` callbacks are present;
- whether a `Filled` parent status exists without captured execution details;
- whether the same candidate behaves consistently across repeated sessions.

## Outputs

`analyze_paper_combo_traces.py` writes:

1. `phase8_paper_sessions.csv`: one annotated row per unique Phase 7 paper run.
2. `phase8_paper_candidate_summary.csv`: repeated-session aggregation by candidate.
3. `phase8_summary.json`: top-level research gate.

Session classes include:

- `BAG_ONLY_FULL_FILL_OBSERVED`
- `LEG_LEVEL_REPORTING_OBSERVED`
- `LEG_CALLBACK_SEQUENCE_RISK`
- `STRUCTURAL_COMBO_REJECT`
- `PAPER_ORDER_REJECTED`
- `ACCEPTED_NO_FILL_EVIDENCE`
- `NO_ACKNOWLEDGEMENT`
- `REVIEW_TRACE_INCONSISTENT`
- `REVIEW_MALFORMED_TRACE`

Candidate decisions include:

- `BROKER_CONFIRMATION_REQUIRED`
- `INVESTIGATE_LEG_CALLBACK_SEQUENCE`
- `INVESTIGATE_LEG_REPORTING`
- `STOP_COMBO_PATH_PENDING_FIX`
- `STOP_COMBO_PATH_IN_PAPER`
- `PAPER_ACCEPTS_BUT_NO_FILL_EVIDENCE`
- `KEEP_PAPER_OBSERVING`

There is deliberately **no GO_LIVE result**. Every output keeps:

- `phase8_live_money_allowed=False`
- `phase8_atomicity_established=False`

Even repeated BAG-only fills can at most produce `BROKER_CONFIRMATION_REQUIRED`.
Before any live experiment is designed, the OSE/IBKR routing and guarantee semantics
must be confirmed outside the paper simulator.

## Usage

Collect separate Phase 7 CSVs across repeated paper sessions, then run:

```bash
python scripts/analyze_paper_combo_traces.py \
  'data/paper_combo_trace_*.csv' \
  --min-sessions 3 \
  --min-bag-full-fills 2 \
  --sequence-gap-ms 250 \
  --sessions-output data/phase8_paper_sessions.csv \
  --output data/phase8_paper_candidate_summary.csv \
  --summary-json data/phase8_summary.json
```

`--sequence-gap-ms` only controls when an earlier leg-level callback is considered
materially separated from the parent fill callback. Callback receipt ordering is
not exchange execution ordering, so this flag is intentionally described as a
risk/investigation signal rather than evidence of non-atomic exchange execution.

## Known limitation carried from Phase 7

Phase 7 currently records `execDetails` but not `commissionReport`. Phase 6 already
uses What-If commission estimates, so Phase 8 does not invent fill commissions.
If paper fills become informative enough to justify further mechanics work, capture
of `commissionReport` can be added separately without changing this trace gate.
