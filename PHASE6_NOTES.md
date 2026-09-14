# Phase 6: execution-feasibility study (IBKR What-If only)

Phase 5 answers whether the same live-positive candidate recurs across independent
monitoring sessions.  Phase 6 deliberately stops short of execution and asks three
narrower questions:

1. Can IBKR resolve every option leg and accept the package as a `BAG` **What-If** preview?
2. What commission and margin impact does IBKR estimate for that preview?
3. After replacing the Phase 4/5 assumed fee with the What-If commission estimate,
   does the observed edge remain positive?

## Why this is not an execution engine

JPX currently lists **Strategy Trades: Unavailable** in the Securities Options
contract specifications.  IBKR's generic combo documentation says a directly-routed
combo can execute as one transaction when the destination exchange supports it, while
SmartRouted combo legs may execute separately.  Therefore an accepted IBKR BAG preview
must **not** be interpreted as proof that an OSE securities-option package is atomic.

The script has no live-order mode.  Its only `placeOrder` call is protected by a hard
runtime invariant requiring `order.whatIf is True`.  IBKR documents a What-If order as
an order preview / credit check that is not sent to a destination.

## Input

Primary input is the Phase 5 ranking:

```bash
python scripts/ibkr_execution_study.py \
  data/reproducibility_ranking.csv \
  --phase4-inputs 'data/persistence_*.csv' \
  --output data/execution_study_plan.csv
```

This default mode is fully offline: it does not connect to IBKR and does not call
`placeOrder`.

Older/current Phase 5 rankings are intentionally compact and may not contain
`floor_pv_per_share`, `lot_size`, or `fee_per_contract_leg`.  Phase 6 therefore accepts
the same Phase 4 CSVs used to create the ranking and joins those fields using Phase 5's
stable candidate signature.

## What-If preview

With TWS or IB Gateway running and API access enabled:

```bash
python scripts/ibkr_execution_study.py \
  data/reproducibility_ranking.csv \
  --phase4-inputs 'data/persistence_*.csv' \
  --run-whatif \
  --exchange OSE.JPN \
  --currency JPY \
  --limit 5 \
  --output data/execution_study_whatif.csv
```

The script resolves each option contract, builds an IBKR `BAG` with the exact BUY/SELL
ratios from `legs_json`, and sends a `LMT` What-If for one package.  The preview price
is reconstructed from the Phase 5 median minimum edge:

```text
net_edge = (floor_pv_per_share - debit_per_share) * lot_size - assumed_fees
```

so

```text
debit_per_share = floor_pv_per_share - (net_edge + assumed_fees) / lot_size
```

This reconstructed price is a research reference, not a fresh executable quote.  Use
`--combo-price-tick` only if you specifically need preview-price rounding for the
broker contract; otherwise the inferred value is preserved.

## Decisions

- `PAPER_COMBO_TEST_REQUIRED`: BAG What-If was accepted, the returned commission is in
  JPY, and replacing the assumed fee with the What-If commission still leaves a
  positive observed edge.  Atomicity is still explicitly `False`.
- `STOP_COMBO_UNSUPPORTED_OR_REJECTED`: IBKR did not return an accepted BAG preview.
- `STOP_EDGE_AFTER_COMMISSION_NONPOSITIVE`: the broker commission estimate removes the
  Phase 5 observed edge.
- `REVIEW_WHATIF_WARNINGS`: preview accepted and edge remains positive, but IBKR
  returned warning text.
- `REVIEW_WHATIF_INCOMPLETE`: preview returned without enough commission/margin data to
  make the fee comparison.
- `REVIEW_COMMISSION_CURRENCY`: commission estimate is not in the expected currency.
- `REVIEW_WHATIF_TIMEOUT`: no What-If order state arrived before the timeout.
- `WHATIF_PREVIEW_REQUIRED`: plan-only output; no broker request was made.

No Phase 6 result means "go live".  The strongest result only means the candidate is
worth a **paper-account / TWS manual combo mechanics test** next.

## Commission and margin context

As of 2026-09, IBKR's published Japan stock-option fixed commission is JPY 90 per
contract, while exchange/regulatory charges are documented separately.  The scanner's
JPY 100/leg assumption is therefore intentionally treated as a research assumption,
not as authoritative final cost.  Phase 6 uses the account-specific What-If commission
when available rather than hard-coding the public schedule.

JSCC calculates listed derivatives margin under the VaR method, and brokers may set
customer requirements at or above the JSCC minimum.  For that reason Phase 6 records
IBKR's account-specific `initMarginChange` / `maintMarginChange`; it does not attempt to
recreate JSCC margin from first principles.

## Next gate

Only if a candidate repeatedly reaches `PAPER_COMBO_TEST_REQUIRED` should the project
move to a Phase 7 paper-account experiment.  That experiment should test actual TWS
order acceptance, legging/partial-fill behavior, cancel/replace semantics and whether
OSE routing is atomic or legged.  Live-money order submission should remain out of the
research code until those mechanics are empirically established.
