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


rec = load("phase13_rec", "scripts/ibkr_record_touch_depletion.py")
ana = load("phase13_ana", "scripts/analyze_touch_depletion.py")


def row(t, *, kind="DEPTH", action="BUY", ask=10.0, ask_size=5.0,
        bid=9.0, bid_size=5.0, last=None, last_size=None, md=1):
    return {
        "candidate_id": "c1",
        "underlying": "7203",
        "expiry": "2026-10-09",
        "leg_key": "C:3000",
        "option_type": "C",
        "strike": "3000",
        "action": action,
        "qty": "1",
        "elapsed_ms": str(t),
        "event_kind": kind,
        "actual_market_data_type": str(md),
        "top_ask": "" if ask is None else str(ask),
        "top_ask_size": "" if ask_size is None else str(ask_size),
        "top_bid": "" if bid is None else str(bid),
        "top_bid_size": "" if bid_size is None else str(bid_size),
        "last_price": "" if last is None else str(last),
        "last_size": "" if last_size is None else str(last_size),
    }


class Phase13Tests(unittest.TestCase):
    def test_phase12_gate_accepts_only_strong_decision(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.json"
            p.write_text(json.dumps({"phase12_overall_decision": "TOUCH_SURVIVAL_ROBUST_ENOUGH_FOR_QUEUE_RESEARCH"}))
            self.assertEqual(rec.require_phase12_ready(p)["phase12_overall_decision"], "TOUCH_SURVIVAL_ROBUST_ENOUGH_FOR_QUEUE_RESEARCH")
            p.write_text(json.dumps({"phase12_overall_decision": "DISPLAYED_TOUCH_NOT_ROBUST"}))
            with self.assertRaises(ValueError):
                rec.require_phase12_ready(p)

    def test_depth_book_operations(self):
        b = rec.DepthBook()
        self.assertTrue(b.apply(position=0, operation=0, side=0, price=10, size=5))
        self.assertEqual(b.top()[2:], (10.0, 5.0))
        self.assertTrue(b.apply(position=0, operation=1, side=0, price=10, size=3))
        self.assertEqual(b.top()[3], 3.0)
        self.assertTrue(b.apply(position=0, operation=2, side=0, price=0, size=0))
        self.assertIsNone(b.top()[2])

    def test_detect_same_price_size_decrease(self):
        deps, repl = ana.detect_depletions([row(0, kind="BASELINE"), row(100, ask_size=3)])
        self.assertEqual(repl, 0)
        self.assertEqual(len(deps), 1)
        self.assertEqual(deps[0]["kind"], "SIZE_DECREASE")
        self.assertEqual(deps[0]["depleted_size"], 2.0)

    def test_detect_price_worsen_buy_and_sell(self):
        deps, _ = ana.detect_depletions([row(0, kind="BASELINE"), row(100, ask=11, ask_size=4)])
        self.assertEqual(deps[0]["kind"], "PRICE_WORSEN")
        sell0 = row(0, kind="BASELINE", action="SELL", bid=10, bid_size=5)
        sell1 = row(100, action="SELL", bid=9, bid_size=4)
        deps2, _ = ana.detect_depletions([sell0, sell1])
        self.assertEqual(deps2[0]["kind"], "PRICE_WORSEN")

    def test_replenishment_counted_not_depletion(self):
        deps, repl = ana.detect_depletions([row(0, kind="BASELINE", ask_size=2), row(100, ask_size=5)])
        self.assertEqual(deps, [])
        self.assertEqual(repl, 1)

    def test_extract_live_last_size_print_only(self):
        rows = [
            row(50, kind="LAST_SIZE", last=10, last_size=2, md=1),
            row(60, kind="LAST_SIZE", last=10, last_size=3, md=3),
            row(70, kind="LAST_PRICE", last=10, last_size=4, md=1),
        ]
        prints = ana.extract_trade_prints(rows)
        self.assertEqual(len(prints), 1)
        self.assertEqual(prints[0]["size"], 2.0)

    def test_matching_corroborates_nearby_same_price(self):
        deps = [{"elapsed_ms": 100, "kind": "SIZE_DECREASE", "old_touch_price": 10, "depleted_size": 2}]
        prints = [{"print_id": 1, "elapsed_ms": 120, "price": 10, "size": 2, "used": False}]
        out = ana.match_prints(deps, prints, window_ms=50, price_tolerance=1e-9)
        self.assertTrue(out[0]["print_corroborated"])
        self.assertEqual(out[0]["matched_volume_to_depletion_ratio_capped"], 1.0)

    def test_matching_rejects_wrong_price_or_time(self):
        deps = [{"elapsed_ms": 100, "kind": "SIZE_DECREASE", "old_touch_price": 10, "depleted_size": 2}]
        prints = [
            {"print_id": 1, "elapsed_ms": 120, "price": 11, "size": 2, "used": False},
            {"print_id": 2, "elapsed_ms": 500, "price": 10, "size": 2, "used": False},
        ]
        out = ana.match_prints(deps, prints, window_ms=50, price_tolerance=1e-9)
        self.assertFalse(out[0]["print_corroborated"])

    def test_print_not_double_used(self):
        deps = [
            {"elapsed_ms": 100, "kind": "SIZE_DECREASE", "old_touch_price": 10, "depleted_size": 1},
            {"elapsed_ms": 120, "kind": "SIZE_DECREASE", "old_touch_price": 10, "depleted_size": 1},
        ]
        prints = [{"print_id": 1, "elapsed_ms": 110, "price": 10, "size": 1, "used": False}]
        out = ana.match_prints(deps, prints, window_ms=50, price_tolerance=1e-9)
        self.assertEqual(sum(bool(x["print_corroborated"]) for x in out), 1)

    def test_classification_thresholds(self):
        base = {"leg_key": "C:3000", "depletion_events": 2, "corroboration_rate": 1.0}
        self.assertEqual(ana.classify_candidate([base], min_depletions=5, min_corroboration=.5)[0], "COLLECT_MORE_TURNOVER_DATA")
        weak = {"leg_key": "C:3000", "depletion_events": 10, "corroboration_rate": .2}
        self.assertEqual(ana.classify_candidate([weak], min_depletions=5, min_corroboration=.5)[0], "DISPLAYED_DEPLETION_MOSTLY_UNCORROBORATED")
        strong = {"leg_key": "C:3000", "depletion_events": 10, "corroboration_rate": .8}
        self.assertEqual(ana.classify_candidate([strong], min_depletions=5, min_corroboration=.5)[0], "TOUCH_DEPLETION_CORROBORATED_ENOUGH_FOR_LATENCY_STUDY")

    def test_analysis_is_not_fill_or_cancel_inference(self):
        rows = [
            row(0, kind="BASELINE", ask_size=5),
            row(100, ask_size=3),
            row(110, kind="LAST_SIZE", ask_size=3, last=10, last_size=2),
        ]
        legs, detail, summary = ana.analyze(rows, window_ms=50, price_tolerance=1e-9, min_depletions=1, min_corroboration=.5)
        self.assertEqual(len(legs), 1)
        self.assertTrue(detail[0]["print_corroborated"])
        self.assertFalse(summary["phase13_is_fill_probability"])
        self.assertFalse(summary["phase13_cancellation_inference_allowed"])
        self.assertFalse(summary["phase13_live_money_allowed"])

    def test_recorder_has_no_order_or_tick_by_tick_api(self):
        src = (ROOT / "scripts/ibkr_record_touch_depletion.py").read_text(encoding="utf-8")
        forbidden = ["placeOrder(", "cancelOrder(", "reqGlobalCancel(", "whatIf", "reqTickByTickData("]
        for token in forbidden:
            self.assertNotIn(token, src)


if __name__ == "__main__":
    unittest.main()
