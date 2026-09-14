import importlib.util
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ibkr_execution_study.py"
spec = importlib.util.spec_from_file_location("phase6", SCRIPT)
phase6 = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = phase6
assert spec.loader is not None
spec.loader.exec_module(phase6)


def promoted_row():
    return {
        "candidate_id": "abc123",
        "underlying": "7203",
        "expiry": "2026-10-08",
        "check": "long_box",
        "strikes": "3000,3200",
        "legs_json": (
            '[{"action":"BUY","option_type":"C","strike":3000,"qty":1},'
            '{"action":"SELL","option_type":"C","strike":3200,"qty":1},'
            '{"action":"BUY","option_type":"P","strike":3200,"qty":1},'
            '{"action":"SELL","option_type":"P","strike":3000,"qty":1}]'
        ),
        "research_decision": "PROMOTE_TO_EXECUTION_STUDY",
        "median_min_live_edge_per_contract": 1500.0,
        "floor_pv_per_share": 200.0,
        "lot_size": 100,
        "fee_per_contract_leg": 100.0,
    }


class FakeContract:
    pass


class FakeComboLeg:
    pass


class FakeOrder:
    pass


class FakeWhatIfApp:
    def __init__(self, state=None, errors=None):
        self._oid = 500
        self.whatif_done = {}
        self.whatif_state = {}
        self.errors = list(errors or [])
        self.state = state
        self.last_order = None
        self.last_bag = None

    def next_order_id(self):
        oid = self._oid
        self._oid += 1
        return oid

    def placeOrder(self, order_id, bag, order):
        self.last_order = order
        self.last_bag = bag
        if self.state is not None:
            self.whatif_state[order_id] = self.state
            self.whatif_done[order_id].set()


class Phase6Tests(unittest.TestCase):
    def test_infers_combo_limit_from_phase5_edge(self):
        row = promoted_row()
        legs = phase6.parse_legs_json(row["legs_json"])
        # fees = 4*100 = 400; debit = 200 - (1500+400)/100 = 181
        self.assertAlmostEqual(phase6.infer_combo_limit_per_share(row, legs), 181.0)

    def test_duplicate_opposite_legs_are_netted(self):
        legs = phase6.parse_legs_json(
            '[{"action":"BUY","option_type":"C","strike":3000,"qty":2},'
            '{"action":"SELL","option_type":"C","strike":3000,"qty":1}]'
        )
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0].action, "BUY")
        self.assertEqual(legs[0].qty, 1)

    def test_plan_only_never_claims_atomicity(self):
        plan = phase6.plan_candidate(promoted_row())
        self.assertEqual(plan["phase6_status"], "PLAN_ONLY")
        self.assertEqual(plan["phase6_decision"], "WHATIF_PREVIEW_REQUIRED")
        self.assertFalse(plan["phase6_atomicity_established"])
        self.assertEqual(plan["phase6_jpx_strategy_trades"], "unavailable")

    def test_nonpromoted_candidate_is_skipped(self):
        row = dict(promoted_row(), research_decision="KEEP_OBSERVING")
        plan = phase6.plan_candidate(row)
        self.assertEqual(plan["phase6_status"], "SKIPPED_NOT_PROMOTED")

    def test_enrichs_phase5_from_phase4_by_stable_signature(self):
        p5 = promoted_row()
        for k in ("floor_pv_per_share", "lot_size", "fee_per_contract_leg"):
            p5.pop(k)
        p5["candidate_id"] = phase6.candidate_id(p5)
        p4 = dict(p5)
        p4.update(floor_pv_per_share=200.0, lot_size=100, fee_per_contract_leg=100.0)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "persistence.csv"
            pd.DataFrame([p4]).to_csv(path, index=False)
            out = phase6.enrich_from_phase4(pd.DataFrame([p5]), [str(path)])
        self.assertEqual(float(out.iloc[0]["lot_size"]), 100.0)
        self.assertEqual(float(out.iloc[0]["floor_pv_per_share"]), 200.0)

    def test_builds_bag_with_exact_leg_actions_and_ratios(self):
        row = promoted_row()
        legs = phase6.parse_legs_json(row["legs_json"])
        contracts = {leg.key: SimpleNamespace(conId=1000 + i) for i, leg in enumerate(legs)}
        bag = phase6.build_bag_contract(
            FakeContract,
            FakeComboLeg,
            underlying="7203",
            legs=legs,
            contracts=contracts,
            exchange="OSE.JPN",
            currency="JPY",
        )
        self.assertEqual(bag.secType, "BAG")
        self.assertEqual(len(bag.comboLegs), 4)
        self.assertEqual([x.action for x in bag.comboLegs], ["BUY", "SELL", "BUY", "SELL"])
        self.assertTrue(all(x.ratio == 1 for x in bag.comboLegs))

    def test_whatif_order_has_hard_safety_flag(self):
        order = phase6.build_whatif_order(
            FakeOrder, limit_price=181.0, account=None, order_ref="test"
        )
        self.assertTrue(order.whatIf)
        self.assertEqual(order.action, "BUY")
        self.assertEqual(order.orderType, "LMT")
        self.assertEqual(order.totalQuantity, 1)

    def test_run_preview_refuses_non_whatif_order(self):
        app = FakeWhatIfApp()
        bad = FakeOrder()
        bad.whatIf = False
        with self.assertRaises(RuntimeError):
            phase6.run_whatif_preview(app, bag=FakeContract(), order=bad, timeout=0)

    def test_accepted_preview_reconciles_commission(self):
        state = SimpleNamespace(
            status="PreSubmitted",
            initMarginBefore="100000",
            initMarginChange="20000",
            initMarginAfter="120000",
            maintMarginBefore="90000",
            maintMarginChange="18000",
            maintMarginAfter="108000",
            equityWithLoanBefore="500000",
            equityWithLoanChange="0",
            equityWithLoanAfter="500000",
            commission=250.0,
            minCommission=250.0,
            maxCommission=250.0,
            commissionCurrency="JPY",
            warningText="",
        )
        app = FakeWhatIfApp(state=state)
        order = phase6.build_whatif_order(FakeOrder, limit_price=181.0, account=None, order_ref="x")
        result = phase6.run_whatif_preview(app, bag=FakeContract(), order=order, timeout=0.01)
        out = phase6.assess_preview(phase6.plan_candidate(promoted_row()), result, currency="JPY")
        # Phase5 edge included assumed 400 fees; replace those with IBKR estimate 250.
        self.assertEqual(out["phase6_status"], "BAG_PREVIEW_ACCEPTED")
        self.assertAlmostEqual(out["phase6_edge_after_whatif_commission"], 1650.0)
        self.assertEqual(out["phase6_decision"], "PAPER_COMBO_TEST_REQUIRED")
        self.assertFalse(out["phase6_atomicity_established"])

    def test_commission_can_kill_edge(self):
        row = promoted_row()
        row["median_min_live_edge_per_contract"] = 100.0
        state = SimpleNamespace(
            status="PreSubmitted",
            initMarginBefore="0",
            initMarginChange="10000",
            initMarginAfter="10000",
            maintMarginBefore="0",
            maintMarginChange="9000",
            maintMarginAfter="9000",
            equityWithLoanBefore="0",
            equityWithLoanChange="0",
            equityWithLoanAfter="0",
            commission=700.0,
            minCommission=700.0,
            maxCommission=700.0,
            commissionCurrency="JPY",
            warningText="",
        )
        result = phase6.WhatIfResult("BAG_PREVIEW_ACCEPTED", state, [])
        out = phase6.assess_preview(phase6.plan_candidate(row), result, currency="JPY")
        self.assertAlmostEqual(out["phase6_edge_after_whatif_commission"], -200.0)
        self.assertEqual(out["phase6_decision"], "STOP_EDGE_AFTER_COMMISSION_NONPOSITIVE")

    def test_rejected_bag_preview_stops_combo_path(self):
        result = phase6.WhatIfResult(
            "BAG_PREVIEW_REJECTED", None, [(500, 200, "No security definition")]
        )
        out = phase6.assess_preview(phase6.plan_candidate(promoted_row()), result, currency="JPY")
        self.assertEqual(out["phase6_decision"], "STOP_COMBO_UNSUPPORTED_OR_REJECTED")
        self.assertIn("200", out["whatif_errors_json"])


if __name__ == "__main__":
    unittest.main()
