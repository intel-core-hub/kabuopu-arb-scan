# Phase 7: paper-account combo mechanics experiment

Phase 6 stops at IBKR `WhatIf=True`. Phase 7 is the first stage that may transmit an
order, but **only to an IBKR paper account** and only to study mechanics. It is not a
live execution engine and it never promotes a result to live trading.

## Why this phase exists

A successful BAG What-If preview says that IBKR can price/check the proposed combo. It
does not tell us how a submitted OSE securities-option combo behaves. Phase 7 records:

- whether the paper simulator acknowledges the BAG order;
- `orderStatus` transitions and duplicate/status timing;
- `execDetails` events, including whether callbacks are reported as `BAG` or individual
  `OPT` contracts;
- whether any partial/leg execution appears before the parent package is filled;
- optional same-order-ID limit-price modification behavior;
- cancellation of the remaining quantity using `cancelOrder` only.

IBKR explicitly documents limitations in paper trading, including top-of-book simulated
fills and **limited combo trading**. Therefore no Phase 7 outcome establishes production
fill quality or atomic OSE execution.

## Safety invariants

The script defaults to `PLAN_ONLY`. `--run-paper` is rejected unless all of the
following are true:

1. `--account` is supplied.
2. The account is returned by IBKR's `managedAccounts` callback.
3. The account identifier begins with `DU` (a conservative local paper-account guard).
4. The exact acknowledgement phrase is supplied:
   `I_UNDERSTAND_THIS_SUBMITS_A_SIMULATED_PAPER_ORDER`.
5. The parent is a `BUY LMT`, quantity is exactly **one package**, `transmit=True`, and
   the order reference starts with `kabuopu-phase7-paper-`.
6. At most one candidate is processed per script invocation.
7. The script never calls `reqGlobalCancel`; it only cancels the order ID it created.
8. The output always keeps `phase7_live_money_allowed=False` and
   `phase7_atomicity_established=False`.

The TWS API itself does not expose a definitive "this socket is paper" flag; IBKR notes
that third-party/TWS API connections do not otherwise distinguish live from paper login.
The managed-account check plus hard `DU` guard is intentionally redundant, but still a
client-side safety check. Use paper credentials and a paper TWS / IB Gateway session.

## Plan-only mode

```bash
python scripts/ibkr_paper_combo_test.py \
  data/execution_study_whatif.csv \
  --output data/paper_combo_plan.csv
```

No IBKR connection or order submission occurs.

## Paper experiment

IBKR's current documentation lists IB Gateway paper port `4002` and TWS paper port
`7947` as defaults. Ports are configurable, so use the value actually configured in
your paper session.

```bash
python scripts/ibkr_paper_combo_test.py \
  data/execution_study_whatif.csv \
  --run-paper \
  --account DU1234567 \
  --paper-ack I_UNDERSTAND_THIS_SUBMITS_A_SIMULATED_PAPER_ORDER \
  --port 4002 \
  --exchange OSE.JPN \
  --currency JPY \
  --observe-seconds 5 \
  --output data/paper_combo_trace.csv
```

`phase6_combo_limit_per_share` is a research reference reconstructed from earlier
observations, not a fresh executable quote. For fill-mechanics testing you can pass a
current, manually observed **paper** combo limit explicitly with
`--paper-limit-price <price>`. The output records whether the price came from
`phase6_reference` or `cli_override`.

The script submits only the first `PAPER_COMBO_TEST_REQUIRED` row. Use `--candidate-id`
to choose a particular candidate.

## Optional modify test

IBKR documents order modification by calling `placeOrder` again with the **same order
ID**, changing only a small set of fields such as price, size, or TIF. Phase 7 therefore
allows one optional price-only modification:

```bash
python scripts/ibkr_paper_combo_test.py \
  data/execution_study_whatif.csv \
  --run-paper \
  --account DU1234567 \
  --paper-ack I_UNDERSTAND_THIS_SUBMITS_A_SIMULATED_PAPER_ORDER \
  --replace-offset 0.1 \
  --output data/paper_combo_replace_trace.csv
```

If any execution callback is already observed, Phase 7 skips the modification and
observes/cancels the residual instead.

## Output interpretation

`phase7_trace_class` can be:

- `PAPER_FULL_FILL_OBSERVED`
- `PAPER_PARTIAL_OR_LEG_EXECUTION_OBSERVED`
- `PAPER_ACCEPTED_THEN_CANCELLED`
- `PAPER_ORDER_REJECTED_OR_INACTIVE`
- `PAPER_ORDER_REJECTED_OR_NO_ACK`
- `PAPER_ACKNOWLEDGED_NO_TERMINAL`
- `PAPER_NO_ACKNOWLEDGEMENT`

The full callback histories are preserved in:

- `phase7_status_events_json`
- `phase7_execution_events_json`
- `phase7_errors_json`
- `phase7_execution_sec_types_json`
- `phase7_execution_exchanges_json`

The only Phase 7 decision after a transmitted paper experiment is
`MANUAL_REVIEW_PAPER_TRACE`. Even a clean simulated full fill is **not** an automatic
GO-live signal.

## Recommended next gate

Repeat the paper experiment for the same candidate at several times and inspect the raw
callbacks. Only after the mechanics are understood should a separate research phase ask
whether a live test is justified. That decision must explicitly account for the fact
that JPX securities options do not currently provide exchange strategy trades and that
IBKR paper combo behavior is simulated and limited.
