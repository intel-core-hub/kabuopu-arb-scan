import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


probe = load("phase15_probe", ROOT / "scripts" / "ibkr_measure_paper_order_lifecycle.py")
analyze = load("phase15_analyze", ROOT / "scripts" / "analyze_paper_order_lifecycle.py")


class Phase15Tests(unittest.TestCase):
    def ev(self, kind, ms):
        return probe.CallbackEvent(kind=kind, elapsed_ms=ms, ts_utc="2026-09-14T00:00:00.000+00:00")

    def test_no_ack_classification(self):
        self.assertEqual(probe.classify_lifecycle([])[0], "PAPER_NO_ORDER_ACK")

    def test_error_without_ack_classification(self):
        self.assertEqual(probe.classify_lifecycle([self.ev("error", 10)])[0], "PAPER_ERROR_WITHOUT_ORDER_ACK")

    def test_execution_classification(self):
        self.assertEqual(probe.classify_lifecycle([self.ev("openOrder", 5), self.ev("execDetails", 20)])[0], "PAPER_EXECUTION_ACTIVITY_OBSERVED")

    def test_cancelled_classification(self):
        events = [self.ev("openOrder", 5), self.ev("orderStatus:Submitted", 8), self.ev("orderStatus:Cancelled", 30)]
        self.assertEqual(probe.classify_lifecycle(events)[0], "PAPER_ACK_THEN_CANCELLED")

    def test_summary_first_callback(self):
        out = probe.summarize_lifecycle(
            base={"candidate_id": "c1"},
            events=[self.ev("openOrder", 12), self.ev("orderStatus:Submitted", 9)],
            account="DU1", order_id=7, submit_return_ms=1.0, cancel_sent_ms=None, phase14_budget_ms=100.0,
        )
        self.assertEqual(out["phase15_first_callback_ms"], 9)
        self.assertFalse(out["phase15_is_exchange_arrival_latency"])

    def test_quantile_nearest_rank(self):
        self.assertEqual(analyze.empirical_quantile([1, 2, 3, 100], 0.75), 3)
        self.assertEqual(analyze.empirical_quantile([1, 2, 3, 100], 0.95), 100)

    def test_candidate_collect_more(self):
        rows = [{"candidate_id": "c", "phase15_status": "PAPER_LIFECYCLE_MEASURED", "phase15_first_callback_ms": "20", "phase15_phase14_budget_ms": "100", "phase15_trace_class": "PAPER_ACK_OBSERVED"}]
        out = analyze.analyze_candidate(rows, min_sessions=3, quantile=0.95)
        self.assertEqual(out["phase15_decision"], "COLLECT_MORE_PAPER_LIFECYCLE_DATA")

    def test_candidate_within_budget(self):
        rows = [
            {"candidate_id": "c", "phase15_status": "PAPER_LIFECYCLE_MEASURED", "phase15_first_callback_ms": str(v), "phase15_phase14_budget_ms": "100", "phase15_trace_class": "PAPER_ACK_OBSERVED"}
            for v in [20, 40, 80]
        ]
        out = analyze.analyze_candidate(rows, min_sessions=3, quantile=0.95)
        self.assertEqual(out["phase15_decision"], "PAPER_ACK_P95_WITHIN_PHASE14_BUDGET")
        self.assertFalse(out["phase15_live_money_allowed"])

    def test_candidate_exceeds_budget(self):
        rows = [
            {"candidate_id": "c", "phase15_status": "PAPER_LIFECYCLE_MEASURED", "phase15_first_callback_ms": str(v), "phase15_phase14_budget_ms": "100", "phase15_trace_class": "PAPER_ACK_OBSERVED"}
            for v in [20, 40, 180]
        ]
        out = analyze.analyze_candidate(rows, min_sessions=3, quantile=0.95)
        self.assertEqual(out["phase15_decision"], "PAPER_ACK_P95_EXCEEDS_PHASE14_BUDGET")

    def test_rejection_dominates(self):
        rows = [
            {"candidate_id": "c", "phase15_status": "PAPER_LIFECYCLE_MEASURED", "phase15_first_callback_ms": "20", "phase15_phase14_budget_ms": "100", "phase15_trace_class": "PAPER_ACK_OBSERVED"},
            {"candidate_id": "c", "phase15_status": "PAPER_LIFECYCLE_MEASURED", "phase15_first_callback_ms": "", "phase15_phase14_budget_ms": "100", "phase15_trace_class": "PAPER_REJECTED_OR_INACTIVE"},
        ]
        out = analyze.analyze_candidate(rows, min_sessions=1, quantile=0.95)
        self.assertEqual(out["phase15_decision"], "PAPER_LIFECYCLE_REJECTED_OR_UNACKNOWLEDGED")

    def test_overall_next_research_only(self):
        rows = [
            {"candidate_id": "c", "phase15_status": "PAPER_LIFECYCLE_MEASURED", "phase15_first_callback_ms": str(v), "phase15_phase14_budget_ms": "100", "phase15_trace_class": "PAPER_ACK_OBSERVED"}
            for v in [20, 40, 80]
        ]
        _, summary = analyze.analyze(rows, min_sessions=3, quantile=0.95)
        self.assertEqual(summary["phase15_overall_decision"], "PAPER_CONTROL_PATH_WITHIN_BUDGET_FOR_NEXT_RESEARCH")
        self.assertFalse(summary["phase15_live_money_allowed"])

    def test_source_has_paper_guards_and_no_global_cancel(self):
        src = (ROOT / "scripts" / "ibkr_measure_paper_order_lifecycle.py").read_text(encoding="utf-8")
        self.assertIn("DU paper-account guard", src)
        self.assertIn("PAPER_ACK", src)
        self.assertNotIn("reqGlobalCancel", src)
        self.assertIn("at most one", src)

    def test_expand_inputs_deduplicates(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.csv"
            p.write_text("candidate_id\n", encoding="utf-8")
            out = analyze.expand_inputs([str(p), str(p)])
            self.assertEqual(out, [str(p)])


if __name__ == "__main__":
    unittest.main()
