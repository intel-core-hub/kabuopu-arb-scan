import importlib.util
import json
import pathlib
import sys
import types
import unittest

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("phase7", SCRIPTS / "ibkr_paper_combo_test.py")
phase7 = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = phase7
spec.loader.exec_module(phase7)


LEGS = json.dumps(
    [
        {"action": "BUY", "option_type": "C", "strike": 3000, "qty": 1},
        {"action": "SELL", "option_type": "C", "strike": 3100, "qty": 1},
    ],
    separators=(",", ":"),
)


def ready_row(**overrides):
    row = {
        "candidate_id": "abc123",
        "underlying": "7203",
        "expiry": "2026-10-09",
        "legs_json": LEGS,
        "phase6_decision": phase7.PHASE6_READY,
        "phase6_combo_limit_per_share": 12.5,
    }
    row.update(overrides)
    return pd.Series(row)


class FakeOrder:
    pass


class FakeApp:
    def __init__(self, accounts=None):
        self.managed_accounts = list(accounts or ["DU123456"])
        self.calls = []
        self.status_events = {}
        self.execution_events = {}
        self.errors = []

    def placeOrder(self, order_id, bag, order):
        self.calls.append(("place", order_id, bag, order.lmtPrice))

    def cancelOrder(self, order_id, manual_time):
        self.calls.append(("cancel", order_id, manual_time))


class Phase7Tests(unittest.TestCase):
    def test_plan_only_for_phase6_ready_candidate(self):
        out = phase7.plan_candidate(ready_row())
        self.assertEqual(out["phase7_status"], phase7.PLAN_ONLY)
        self.assertEqual(out["phase7_order_quantity"], 1)
        self.assertEqual(out["phase7_limit_price"], 12.5)
        self.assertFalse(out["phase7_live_money_allowed"])
        self.assertFalse(out["phase7_atomicity_established"])

    def test_plan_skips_non_promoted_phase6_row(self):
        out = phase7.plan_candidate(ready_row(phase6_decision="STOP_EDGE_AFTER_COMMISSION_NONPOSITIVE"))
        self.assertEqual(out["phase7_decision"], "NO_PAPER_COMBO_TEST")

    def test_plan_rejects_missing_limit(self):
        out = phase7.plan_candidate(ready_row(phase6_combo_limit_per_share=float("nan")))
        self.assertEqual(out["phase7_decision"], "REVIEW_PHASE6_INPUT")

    def test_paper_account_guard_requires_du_and_managed(self):
        self.assertEqual(phase7.assert_paper_account("du123", ["DU123", "U999"]), "DU123")
        with self.assertRaises(RuntimeError):
            phase7.assert_paper_account("U999", ["U999"])
        with self.assertRaises(RuntimeError):
            phase7.assert_paper_account("DU404", ["DU123"])
        with self.assertRaises(ValueError):
            phase7.assert_paper_account(None, ["DU123"])

    def test_build_order_is_fixed_one_package_limit_paper_order(self):
        order = phase7.build_paper_order(
            FakeOrder,
            account="DU123456",
            limit_price=12.5,
            order_ref="kabuopu-phase7-paper-abc",
        )
        self.assertEqual(order.action, "BUY")
        self.assertEqual(order.orderType, "LMT")
        self.assertEqual(order.totalQuantity, 1)
        self.assertEqual(order.lmtPrice, 12.5)
        self.assertIs(order.whatIf, False)
        self.assertIs(order.transmit, True)
        self.assertEqual(order.account, "DU123456")

    def test_build_order_refuses_non_du(self):
        with self.assertRaises(RuntimeError):
            phase7.build_paper_order(
                FakeOrder,
                account="U123456",
                limit_price=12.5,
                order_ref="kabuopu-phase7-paper-abc",
            )

    def test_submit_has_second_safety_gate(self):
        app = FakeApp(["DU123456"])
        order = phase7.build_paper_order(
            FakeOrder,
            account="DU123456",
            limit_price=12.5,
            order_ref="kabuopu-phase7-paper-abc",
        )
        phase7.submit_paper_order(app, order_id=7, bag=object(), order=order, account="DU123456")
        self.assertEqual(len(app.calls), 1)
        order.totalQuantity = 2
        with self.assertRaises(RuntimeError):
            phase7.submit_paper_order(app, order_id=8, bag=object(), order=order, account="DU123456")
        self.assertEqual(len(app.calls), 1)

    def test_submit_refuses_unmanaged_account_even_if_du(self):
        app = FakeApp(["DU111"])
        order = phase7.build_paper_order(
            FakeOrder,
            account="DU222",
            limit_price=12.5,
            order_ref="kabuopu-phase7-paper-abc",
        )
        with self.assertRaises(RuntimeError):
            phase7.submit_paper_order(app, order_id=7, bag=object(), order=order, account="DU222")
        self.assertFalse(app.calls)

    def test_modify_reuses_same_order_id_and_preserves_guards(self):
        app = FakeApp(["DU123456"])
        order = phase7.build_paper_order(
            FakeOrder,
            account="DU123456",
            limit_price=12.5,
            order_ref="kabuopu-phase7-paper-abc",
        )
        phase7.modify_paper_order(
            app,
            order_id=9,
            bag=object(),
            order=order,
            account="DU123456",
            new_limit_price=12.6,
        )
        self.assertEqual(app.calls[0][0:2], ("place", 9))
        self.assertEqual(app.calls[0][3], 12.6)

    def test_cancel_only_targets_own_order_not_global_cancel(self):
        app = FakeApp()
        phase7.cancel_own_order(app, 42)
        self.assertEqual(app.calls, [("cancel", 42, "")])

    def test_classify_full_fill(self):
        statuses = [
            phase7.StatusEvent("t", "Filled", 1.0, 0.0, 12.5, 12.5, 1, "")
        ]
        status, _ = phase7.classify_trace(statuses, [], [])
        self.assertEqual(status, "PAPER_FULL_FILL_OBSERVED")

    def test_classify_partial_or_leg_execution(self):
        statuses = [
            phase7.StatusEvent("t", "Submitted", 0.0, 1.0, 0.0, 0.0, 1, "")
        ]
        executions = [
            phase7.ExecutionEvent("t", "e1", 1, 1, "OPT", 123, "OSE.JPN", "BOT", 1.0, 10.0, 1.0, 10.0, "ref")
        ]
        status, _ = phase7.classify_trace(statuses, executions, [])
        self.assertEqual(status, "PAPER_PARTIAL_OR_LEG_EXECUTION_OBSERVED")

    def test_classify_accepted_then_cancelled(self):
        statuses = [
            phase7.StatusEvent("t1", "Submitted", 0.0, 1.0, 0.0, 0.0, 1, ""),
            phase7.StatusEvent("t2", "Cancelled", 0.0, 1.0, 0.0, 0.0, 1, ""),
        ]
        status, _ = phase7.classify_trace(statuses, [], [])
        self.assertEqual(status, "PAPER_ACCEPTED_THEN_CANCELLED")

    def test_summary_never_promotes_to_live(self):
        base = phase7.plan_candidate(ready_row())
        statuses = [phase7.StatusEvent("t", "Filled", 1.0, 0.0, 12.5, 12.5, 1, "")]
        out = phase7.summarize_trace(
            base,
            order_id=7,
            account="DU123456",
            initial_limit=12.5,
            limit_source="phase6_reference",
            replacement_limit=None,
            statuses=statuses,
            executions=[],
            errors=[],
            started_at="a",
            finished_at="b",
            modify_attempted=False,
            cancel_attempted=False,
            open_order_seen=True,
        )
        self.assertEqual(out["phase7_decision"], "MANUAL_REVIEW_PAPER_TRACE")
        self.assertFalse(out["phase7_live_money_allowed"])
        self.assertFalse(out["phase7_atomicity_established"])

    def test_paper_limit_override_is_explicitly_tagged(self):
        plan = phase7.plan_candidate(ready_row())
        value, source = phase7.select_paper_limit(plan, 13.25)
        self.assertEqual(value, 13.25)
        self.assertEqual(source, "cli_override")
        value, source = phase7.select_paper_limit(plan, None)
        self.assertEqual(value, 12.5)
        self.assertEqual(source, "phase6_reference")

    def test_validate_input(self):
        phase7._validate_input(pd.DataFrame([ready_row().to_dict()]))
        with self.assertRaises(ValueError):
            phase7._validate_input(pd.DataFrame([{"underlying": "7203"}]))


if __name__ == "__main__":
    unittest.main()
