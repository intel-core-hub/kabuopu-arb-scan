# Phase 9: venue/broker confirmation gate

Phase 8 can show repeated, internally consistent paper BAG fills, but that still does not establish live Osaka Exchange atomicity. Phase 9 is a fully offline evidence gate: it combines the Phase 8 candidate summary with a reviewed JSON record of venue and broker facts.

## Current structural issue

JPX's current securities-option contract specifications state `Strategy Trades: Unavailable`. IBKR's generic combo documentation says a combo routed directly to an exchange is executed as a single transaction, while a SmartRouted combo can execute its legs separately. Those two facts mean that a paper BAG fill cannot be promoted to exchange-native atomic execution without an OSE-specific written broker explanation.

The example evidence file therefore records the JPX venue state as `UNAVAILABLE` and leaves all OSE-specific IBKR fields as `UNKNOWN` until an authoritative response is obtained.

## Inputs

- `data/phase8_paper_candidate_summary.csv`
- `config/phase9_venue_broker_evidence.json` copied from the example and updated only from authoritative evidence

Required broker fields are deliberately coarse:

- `ose_sso_bag_supported`: `YES | NO | UNKNOWN`
- `ose_sso_route_mode`: `DIRECT_EXCHANGE | SMART | BROKER_INTERNAL | UNKNOWN`
- `ose_sso_execution_guarantee`: `ATOMIC | NON_ATOMIC_OR_LEGGING_POSSIBLE | UNKNOWN`

## Decisions

Important outcomes include:

- `NO_GO_EXCHANGE_ATOMIC_COMBO`
- `NON_ATOMIC_EXECUTION_RESEARCH_ONLY`
- `EVIDENCE_CONFLICT_MANUAL_ESCALATION`
- `WAIT_BROKER_CONFIRMATION`
- `WAIT_BROKER_ROUTING_GUARANTEE_CONFIRMATION`
- `STOP_BROKER_COMBO_UNSUPPORTED`
- `MANUAL_LIVE_DESIGN_REVIEW_REQUIRED`

There is intentionally no `GO_LIVE`. Every output keeps:

- `phase9_live_money_allowed=False`
- `phase9_atomicity_established=False`

If the venue says strategy trades are unavailable and the broker claims a direct atomic OSE route, the script treats this as a contradiction requiring written manual resolution rather than trusting either claim automatically.

## Usage

```bash
cp config/phase9_venue_broker_evidence.example.json \
   config/phase9_venue_broker_evidence.json

python scripts/evaluate_broker_confirmation.py \
  data/phase8_paper_candidate_summary.csv \
  --evidence config/phase9_venue_broker_evidence.json \
  --as-of-date 2026-09-14 \
  --output data/phase9_broker_gate.csv \
  --summary-json data/phase9_summary.json
```

With the example evidence as-is, a Phase 8 `BROKER_CONFIRMATION_REQUIRED` candidate is blocked from the exchange-atomic path because the venue record says strategy trades are unavailable. If a broker later confirms BAG acceptance but not exchange-native atomicity, the candidate moves only to `NON_ATOMIC_EXECUTION_RESEARCH_ONLY`.
