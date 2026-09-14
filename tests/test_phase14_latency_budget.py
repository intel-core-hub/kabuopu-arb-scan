from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rttmod = load_module("phase14_rtt", "scripts/ibkr_measure_api_rtt.py")
latmod = load_module("phase14_latency", "scripts/analyze_latency_budget.py")


def p12_row(**overrides):
    row = {
        "candidate_id": "cand1",
        "underlying": "7203",
        "expiry": "2026-10-09",
        "leg_key": "C:3000",
        "option_type": "C",
        "strike": "3000",
        "action": "BUY",
        "qty": "1",
        "complete_triggers": "30",
        "survival_100ms": "0.98",
        "survival_250ms": "0.92",
        "survival_500ms": "0.85",
        "survival_1000ms": "0.75",
        "survival_2000ms": "0.60",
    }
    row.update(overrides)
    return row


def p13_row(**overrides):
    row = {
        "candidate_id": "cand1",
        "leg_key": "C:3000",
        "depletion_events": "8",
        "corroboration_rate": "0.75",
    }
    row.update(overrides)
    return row


class Phase14Tests(unittest.TestCase):
    def test_empirical_quantile_nearest_rank(self):
        self.assertEqual(latmod.empirical_quantile([10, 20, 30, 40], 0.75), 30)
        self.assertEqual(rttmod.empirical_quantile([10, 20, 30, 40], 0.95), 40)

    def test_rtt_summary_ignores_warmup_and_timeout(self):
        rows = [
            {"is_warmup": True, "status": "OK", "rtt_ms": 1},
            {"is_warmup": False, "status": "TIMEOUT", "rtt_ms": ""},
            {"is_warmup": False, "status": "OK", "rtt_ms": 20},
            {"is_warmup": False, "status": "OK", "rtt_ms": 40},
        ]
        s = rttmod.summarize_samples(rows)
        self.assertEqual(s["successful_samples"], 2)
        self.assertEqual(s["rtt_median_ms"], 30)
        self.assertFalse(s["phase14_is_order_arrival_latency"])
        self.assertFalse(s["phase14_one_way_inference_used"])

    def test_choose_conservative_horizon_rounds_up(self):
        self.assertEqual(latmod.choose_conservative_horizon([100, 250, 500], 220), 250)
        self.assertEqual(latmod.choose_conservative_horizon([100, 250, 500], 250), 250)
        self.assertIsNone(latmod.choose_conservative_horizon([100, 250, 500], 501))

    def test_leg_passes_when_budget_survival_and_phase13_pass(self):
        row = latmod.analyze_leg(
            p12_row(), p13_row(), latency_budget_ms=220, min_survival=0.8,
            min_complete_triggers=20, min_depletions=5, min_corroboration=0.5,
        )
        self.assertEqual(row["conservative_survival_horizon_ms"], 250)
        self.assertEqual(row["phase14_leg_decision"], "LATENCY_BUDGET_SURVIVES_FOR_NEXT_RESEARCH")

    def test_leg_fails_survival_threshold(self):
        row = latmod.analyze_leg(
            p12_row(**{"survival_250ms": "0.79"}), p13_row(), latency_budget_ms=220,
            min_survival=0.8, min_complete_triggers=20, min_depletions=5, min_corroboration=0.5,
        )
        self.assertEqual(row["phase14_leg_decision"], "LATENCY_BUDGET_NOT_SURVIVED")

    def test_leg_requires_longer_phase12_horizon(self):
        row = latmod.analyze_leg(
            p12_row(), p13_row(), latency_budget_ms=2500, min_survival=0.8,
            min_complete_triggers=20, min_depletions=5, min_corroboration=0.5,
        )
        self.assertEqual(row["phase14_leg_decision"], "RECOLLECT_LONGER_TOUCH_SURVIVAL_HORIZON")

    def test_leg_requires_phase13_evidence(self):
        row = latmod.analyze_leg(
            p12_row(), p13_row(corroboration_rate="0.2"), latency_budget_ms=220,
            min_survival=0.8, min_complete_triggers=20, min_depletions=5, min_corroboration=0.5,
        )
        self.assertEqual(row["phase14_leg_decision"], "PHASE13_GATE_NOT_MET")

    def test_leg_missing_phase13_is_input_incomplete(self):
        row = latmod.analyze_leg(
            p12_row(), None, latency_budget_ms=220, min_survival=0.8,
            min_complete_triggers=20, min_depletions=5, min_corroboration=0.5,
        )
        self.assertEqual(row["phase14_leg_decision"], "INPUT_INCOMPLETE")

    def test_leg_requires_enough_phase12_triggers(self):
        row = latmod.analyze_leg(
            p12_row(complete_triggers="5"), p13_row(), latency_budget_ms=220,
            min_survival=0.8, min_complete_triggers=20, min_depletions=5, min_corroboration=0.5,
        )
        self.assertEqual(row["phase14_leg_decision"], "COLLECT_MORE_TOUCH_SURVIVAL_DATA")

    def test_analyze_without_rtt_samples_stops(self):
        legs, candidates, summary = latmod.analyze(
            [p12_row()], [p13_row()], [], latency_quantile=0.95, extra_budget_ms=100,
            min_survival=0.8, min_complete_triggers=20, min_depletions=5, min_corroboration=0.5,
        )
        self.assertEqual(legs, [])
        self.assertEqual(candidates, [])
        self.assertEqual(summary["phase14_overall_decision"], "COLLECT_API_RTT_DATA")

    def test_analyze_uses_full_rtt_plus_margin_not_half(self):
        legs, candidates, summary = latmod.analyze(
            [p12_row()], [p13_row()], [80, 100, 120], latency_quantile=0.95,
            extra_budget_ms=100, min_survival=0.8, min_complete_triggers=20,
            min_depletions=5, min_corroboration=0.5,
        )
        self.assertEqual(summary["selected_rtt_quantile_ms"], 120)
        self.assertEqual(summary["latency_budget_ms"], 220)
        self.assertEqual(legs[0]["conservative_survival_horizon_ms"], 250)
        self.assertEqual(candidates[0]["phase14_candidate_decision"], "LATENCY_BUDGET_SURVIVES_FOR_NEXT_RESEARCH")
        self.assertFalse(summary["phase14_one_way_inference_used"])

    def test_candidate_fails_if_any_leg_fails(self):
        r1 = p12_row()
        r2 = p12_row(leg_key="P:3000", option_type="P", action="SELL", **{"survival_250ms": "0.50"})
        q1 = p13_row()
        q2 = p13_row(leg_key="P:3000")
        _, candidates, summary = latmod.analyze(
            [r1, r2], [q1, q2], [120], latency_quantile=0.95, extra_budget_ms=100,
            min_survival=0.8, min_complete_triggers=20, min_depletions=5, min_corroboration=0.5,
        )
        self.assertEqual(candidates[0]["phase14_candidate_decision"], "LATENCY_BUDGET_NOT_SURVIVED")
        self.assertEqual(summary["phase14_overall_decision"], "NO_LATENCY_BUDGET_CANDIDATE")

    def test_load_rtt_filters_rows(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "rtt.csv"
            with p.open("w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["is_warmup", "status", "rtt_ms"])
                w.writeheader()
                w.writerows([
                    {"is_warmup": "True", "status": "OK", "rtt_ms": "1"},
                    {"is_warmup": "False", "status": "TIMEOUT", "rtt_ms": ""},
                    {"is_warmup": "False", "status": "OK", "rtt_ms": "12.5"},
                ])
            self.assertEqual(latmod.load_rtt([p]), [12.5])

    def test_recorder_has_no_order_submission_or_cancel_api(self):
        source = (ROOT / "scripts/ibkr_measure_api_rtt.py").read_text(encoding="utf-8")
        for forbidden in ["placeOrder(", "cancelOrder(", "reqGlobalCancel(", "whatIf"]:
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
