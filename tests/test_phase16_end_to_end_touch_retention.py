import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "analyze_end_to_end_touch_retention.py"
spec = importlib.util.spec_from_file_location("phase16_mod", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
assert spec.loader is not None
spec.loader.exec_module(mod)


def leg(key="C100", s100=0.99, s250=0.95, s500=0.90, s1000=0.82, triggers=100):
    return {
        "candidate_id": "cand1", "leg_key": key, "option_type": "CALL", "strike": "100",
        "action": "BUY", "qty": "1", "complete_triggers": str(triggers),
        "survival_100ms": str(s100), "survival_250ms": str(s250),
        "survival_500ms": str(s500), "survival_1000ms": str(s1000),
    }


def summary(decision="PAPER_ACK_P95_WITHIN_PHASE14_BUDGET"):
    return {"candidate_id": "cand1", "phase15_decision": decision}


def trace(ms, cls="PAPER_ACK_OBSERVED"):
    return {
        "candidate_id": "cand1", "phase15_status": "PAPER_LIFECYCLE_MEASURED",
        "phase15_trace_class": cls, "phase15_first_callback_ms": str(ms),
    }


class Phase16Tests(unittest.TestCase):
    def kwargs(self, **overrides):
        base = dict(
            extra_latency_ms=100.0, min_sessions=3, min_complete_triggers=20,
            min_retention=0.80, max_out_of_range_fraction=0.05,
            bootstrap_reps=200, bootstrap_confidence=0.90, seed=16,
        )
        base.update(overrides)
        return base

    def test_survival_curve_parse(self):
        self.assertEqual(mod.survival_curve(leg()), {100: 0.99, 250: 0.95, 500: 0.90, 1000: 0.82})

    def test_ceiling_survival_uses_next_longer_horizon(self):
        curve = {100: .9, 250: .8, 500: .7}
        self.assertEqual(mod.ceiling_survival(curve, 101), (250, .8))
        self.assertEqual(mod.ceiling_survival(curve, 250), (250, .8))

    def test_ceiling_survival_never_extrapolates(self):
        self.assertEqual(mod.ceiling_survival({100: .9}, 101), (None, None))

    def test_phase15_gate_required(self):
        c, _, _ = mod.analyze_candidate("cand1", [leg()], summary("PAPER_ACK_P95_EXCEEDS_PHASE14_BUDGET"), [trace(20)]*3, **self.kwargs())
        self.assertEqual(c["phase16_candidate_decision"], "PHASE15_GATE_NOT_MET")

    def test_min_sessions_required(self):
        c, _, _ = mod.analyze_candidate("cand1", [leg()], summary(), [trace(20), trace(30)], **self.kwargs())
        self.assertEqual(c["phase16_candidate_decision"], "COLLECT_MORE_PAPER_TIMING_DATA")

    def test_rejected_trace_not_used(self):
        rows = [trace(20), trace(30), trace(40, "PAPER_REJECTED_OR_INACTIVE")]
        c, _, _ = mod.analyze_candidate("cand1", [leg()], summary(), rows, **self.kwargs())
        self.assertEqual(c["phase16_candidate_decision"], "COLLECT_MORE_PAPER_TIMING_DATA")

    def test_phase12_trigger_count_required(self):
        c, _, _ = mod.analyze_candidate("cand1", [leg(triggers=10)], summary(), [trace(20), trace(30), trace(40)], **self.kwargs())
        self.assertEqual(c["phase16_candidate_decision"], "COLLECT_MORE_TOUCH_SURVIVAL_DATA")

    def test_out_of_range_requests_longer_horizon(self):
        rows = [trace(950), trace(950), trace(950)]  # +100ms > max 1000ms
        c, _, samples = mod.analyze_candidate("cand1", [leg()], summary(), rows, **self.kwargs())
        self.assertEqual(c["phase16_candidate_decision"], "RECOLLECT_LONGER_TOUCH_SURVIVAL_HORIZON")
        self.assertEqual(len(samples), 3)

    def test_robust_candidate_uses_weakest_leg_not_product(self):
        legs = [leg("A", s250=.92), leg("B", s250=.88)]
        rows = [trace(100), trace(120), trace(140)]  # +100 => <=250
        c, leg_rows, samples = mod.analyze_candidate("cand1", legs, summary(), rows, **self.kwargs(min_retention=.85))
        self.assertEqual(c["phase16_candidate_decision"], "TOUCH_RETAINS_THROUGH_PAPER_CONTROL_PATH_FOR_NEXT_RESEARCH")
        self.assertAlmostEqual(c["mean_weakest_leg_marginal_retention"], .88)
        self.assertTrue(all(abs(r["weakest_leg_survival_at_ceiling_horizon"] - .88) < 1e-12 for r in samples))
        self.assertEqual(len(leg_rows), 2)
        self.assertFalse(c["phase16_independence_assumption_used"])

    def test_not_robust_when_bootstrap_lower_below_threshold(self):
        rows = [trace(100), trace(300), trace(700)]  # effective 200/400/800 => .95/.90/.82
        c, _, _ = mod.analyze_candidate("cand1", [leg()], summary(), rows, **self.kwargs(min_retention=.90, bootstrap_reps=500))
        self.assertEqual(c["phase16_candidate_decision"], "TOUCH_RETENTION_NOT_ROBUST_TO_CONTROL_PATH")

    def test_bootstrap_is_deterministic(self):
        vals = [.9, .8, .7, .6]
        a = mod.bootstrap_mean_interval(vals, reps=500, confidence=.90, seed=7)
        b = mod.bootstrap_mean_interval(vals, reps=500, confidence=.90, seed=7)
        self.assertEqual(a, b)

    def test_analyze_rollup(self):
        candidates, legs, samples, s = mod.analyze(
            [leg()], [summary()], [trace(100), trace(120), trace(140)], **self.kwargs(min_retention=.85)
        )
        self.assertEqual(s["phase16_overall_decision"], "END_TO_END_RESEARCH_CANDIDATE_EXISTS")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(legs), 1)
        self.assertEqual(len(samples), 3)
        self.assertFalse(s["phase16_is_fill_probability"])
        self.assertFalse(s["phase16_is_joint_leg_probability"])

    def test_source_has_no_order_submission_api(self):
        src = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("placeOrder(", src)
        self.assertNotIn("cancelOrder(", src)
        self.assertNotIn("reqGlobalCancel(", src)
        self.assertNotIn("reqMktData(", src)


if __name__ == "__main__":
    unittest.main()
