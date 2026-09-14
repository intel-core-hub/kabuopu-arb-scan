import importlib.util
import json
import tempfile
import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rec = load("phase12_rec", "scripts/ibkr_record_depth_survival.py")
ana = load("phase12_ana", "scripts/analyze_touch_survival.py")


class DepthBookTests(unittest.TestCase):
    def test_insert_update_delete(self):
        b = rec.DepthBook()
        self.assertTrue(b.apply(position=0, operation=0, side=1, price=100, size=3))
        self.assertTrue(b.apply(position=0, operation=0, side=0, price=102, size=4))
        self.assertEqual(b.top(), (100.0, 3.0, 102.0, 4.0))
        self.assertTrue(b.apply(position=0, operation=1, side=1, price=101, size=2))
        self.assertEqual(b.top()[0], 101.0)
        self.assertTrue(b.apply(position=0, operation=2, side=1, price=0, size=0))
        self.assertIsNone(b.top()[0])

    def test_insert_shifts_rows(self):
        b = rec.DepthBook()
        b.apply(position=0, operation=0, side=0, price=102, size=1)
        b.apply(position=0, operation=0, side=0, price=101, size=2)
        self.assertEqual([x.price for x in b.asks], [101.0, 102.0])

    def test_invalid_position_does_not_corrupt(self):
        b = rec.DepthBook()
        self.assertFalse(b.apply(position=2, operation=1, side=0, price=1, size=1))
        self.assertEqual(b.asks, [])

    def test_phase11_gate(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.json"
            p.write_text(json.dumps({"phase11_overall_decision": "LIVE_L1_ONLY"}))
            with self.assertRaises(ValueError):
                rec.require_phase11_ready(p)
            p.write_text(json.dumps({"phase11_overall_decision": "READY_FOR_L2_FILL_MODEL"}))
            self.assertEqual(rec.require_phase11_ready(p)["phase11_overall_decision"], "READY_FOR_L2_FILL_MODEL")


class SurvivalTests(unittest.TestCase):
    def row(self, t, *, action="BUY", ask=100, ask_size=2, bid=99, bid_size=2):
        return {
            "candidate_id": "c1", "underlying": "7203", "expiry": "2026-10-01",
            "leg_key": "C:3000", "option_type": "C", "strike": 3000,
            "action": action, "qty": 1, "elapsed_ms": t,
            "actual_market_data_type": 1,
            "top_bid": bid, "top_bid_size": bid_size,
            "top_ask": ask, "top_ask_size": ask_size,
        }

    def test_buy_same_or_better(self):
        self.assertTrue(ana.no_worse("BUY", 99, 100))
        self.assertFalse(ana.no_worse("BUY", 101, 100))

    def test_sell_same_or_better(self):
        self.assertTrue(ana.no_worse("SELL", 101, 100))
        self.assertFalse(ana.no_worse("SELL", 99, 100))

    def test_survival_detects_price_worsening(self):
        rows = [self.row(0), self.row(100, ask=100), self.row(200, ask=101), self.row(1000, ask=101)]
        out = ana.analyze_leg(rows, horizons_ms=[100, 500], step_ms=100)
        self.assertGreater(out["complete_triggers"], 0)
        self.assertLess(out["survival_500ms"], 1.0)

    def test_survival_detects_size_loss(self):
        rows = [self.row(0), self.row(100, ask_size=0), self.row(1000, ask_size=0)]
        out = ana.analyze_leg(rows, horizons_ms=[100, 500], step_ms=100)
        self.assertEqual(out["survival_500ms"], 0.0)

    def test_improvement_survives(self):
        rows = [self.row(0), self.row(100, ask=99), self.row(1000, ask=99)]
        out = ana.analyze_leg(rows, horizons_ms=[100, 500], step_ms=100)
        self.assertEqual(out["survival_500ms"], 1.0)

    def test_nonlive_breaks_survival(self):
        rows = [self.row(0), self.row(100), self.row(1000)]
        rows[1]["actual_market_data_type"] = 3
        out = ana.analyze_leg(rows, horizons_ms=[100, 500], step_ms=100)
        self.assertLess(out["survival_500ms"], 1.0)

    def test_candidate_classification(self):
        legs = [
            {"leg_key": "C:1", "complete_triggers": 25, "survival_500ms": 0.9},
            {"leg_key": "P:1", "complete_triggers": 30, "survival_500ms": 0.85},
        ]
        d, _ = ana.classify_candidate(legs, target_horizon_ms=500, min_survival=0.8, min_complete_triggers=20)
        self.assertEqual(d, "TOUCH_SURVIVAL_ROBUST_ENOUGH_FOR_QUEUE_RESEARCH")
        legs[1]["survival_500ms"] = 0.4
        d, _ = ana.classify_candidate(legs, target_horizon_ms=500, min_survival=0.8, min_complete_triggers=20)
        self.assertEqual(d, "DISPLAYED_TOUCH_NOT_ROBUST")

    def test_requires_enough_triggers(self):
        d, _ = ana.classify_candidate(
            [{"leg_key": "C:1", "complete_triggers": 2, "survival_500ms": 1.0}],
            target_horizon_ms=500, min_survival=0.8, min_complete_triggers=20,
        )
        self.assertEqual(d, "COLLECT_MORE_DEPTH_DATA")

    def test_not_called_fill_probability(self):
        src = (ROOT / "scripts/ibkr_record_depth_survival.py").read_text()
        forbidden = ["place" + "Order", "cancel" + "Order", "req" + "GlobalCancel", "what" + "If"]
        for token in forbidden:
            self.assertNotIn(token, src)


if __name__ == "__main__":
    unittest.main()
