# Phase 10: non-atomic execution quote replay

Phase 9 blocks the exchange-native atomic-combo thesis under the current venue evidence (`Strategy Trades: Unavailable`). Phase 10 does **not** try to bypass that gate. It treats individual-leg execution as a different, non-atomic research track and asks a narrower question:

> If the package looked profitable at one simultaneous top-of-book sample, would the edge still exist if the legs had to be executed one at a time with a delay between them?

## What changes in Phase 4

`scripts/ibkr_monitor_findings.py` now stores `leg_snapshots` inside every successful entry of `monitor_samples_json`. Each leg snapshot records:

- leg index / action / option type / strike / quantity
- executable side (`ASK` for BUY, `BID` for SELL)
- executable price and displayed size
- exact-side price and size age in milliseconds
- actual IBKR market-data type

The monitor row also stores `effective_fee_per_contract_leg`, so a Phase 4 CLI fee override is not lost when the CSV is replayed later.

Old Phase 4 CSVs do not contain these fields. Phase 10 deliberately returns `NEEDS_PHASE4_RECOLLECTION` rather than inventing per-leg price paths from package-level summaries.

## Offline replay

`scripts/simulate_nonatomic_execution.py` takes:

1. a Phase 9 candidate CSV; and
2. one or more enriched Phase 4 persistence CSVs.

For every `LIVE_POSITIVE` Phase 4 sample with enough monitoring time left to observe the full sequential path, it exhaustively enumerates leg-order permutations (up to `--max-permutations`). End-of-window triggers are excluded rather than mislabeled as execution failures. The first leg uses the trigger sample; each later leg uses the first sample at or after `--leg-delay-sec` times its sequence position. A replayed leg is usable only when its stored executable side is live, fresh and large enough for the requested quantity.

For each completed path the script recomputes:

- package debit/credit from the sequential leg prices
- guaranteed-floor gross edge
- per-leg fees
- optional extra slippage stress (`--extra-slippage-per-contract-leg`)
- post-cost edge per package

It then aggregates completion ratio, positive-path ratio, the share of trigger samples for which **all** leg orders remain positive, tail edge, and the median worst-order edge.

## Phase 9 eligibility

Phase 10 is research-only and accepts these Phase 9 outcomes:

- `NON_ATOMIC_EXECUTION_RESEARCH_ONLY`
- `NO_GO_EXCHANGE_ATOMIC_COMBO`
- `STOP_BROKER_COMBO_UNSUPPORTED`

The latter two are allowed because exchange-native/BAG failure does not prevent an *offline* study of individual-leg risk. Any Phase 9 row that claims live-money permission or established atomicity is rejected as inconsistent.

## Decisions

Candidate-level results are intentionally non-live:

- `NONATOMIC_SIGNAL_SURVIVES_REPLAY`
- `KEEP_NONATOMIC_RESEARCH`
- `STOP_NONATOMIC_EDGE_NOT_ROBUST`
- `NEEDS_PHASE4_RECOLLECTION`
- `NO_REPLAYABLE_SESSIONS`
- `NOT_ELIGIBLE_FROM_PHASE9`

Even the strongest result keeps:

- `phase10_live_money_allowed=False`
- `phase10_atomicity_established=False`

## Important limitations

This is **quote-touch replay, not a fill simulator**. It does not model queue position, fill probability, quote cancellation between samples, hidden liquidity, market impact, or the possibility that taking one leg changes the other legs' prices. Sampling can also miss adverse moves between observations. Therefore a positive Phase 10 result is only evidence that the displayed-top-of-book edge was robust to a simple sequential-price stress; it is not proof that the strategy is executable or profitable live.

## Example

Recollect Phase 4 data after applying this patch, then run:

```bash
python scripts/simulate_nonatomic_execution.py \
  data/phase9_broker_gate.csv \
  --phase4-inputs 'data/persistence_*.csv' \
  --leg-delay-sec 0.5 \
  --extra-slippage-per-contract-leg 100 \
  --output data/phase10_nonatomic_candidates.csv \
  --sessions-output data/phase10_nonatomic_sessions.csv \
  --paths-output data/phase10_nonatomic_paths.csv \
  --summary-json data/phase10_summary.json
```

For sensitivity analysis, repeat with larger leg delays (for example 1.0 and 2.0 seconds) and larger extra-slippage assumptions. A result that only survives at zero/very-low delay should not be treated as robust non-atomic evidence.
