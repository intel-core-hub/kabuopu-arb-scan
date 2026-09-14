import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("phase10", ROOT / "scripts" / "simulate_nonatomic_execution.py")
phase10 = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = phase10
spec.loader.exec_module(phase10)


LEGS = [
    {"action": "BUY", "option_type": "C", "strike": 100, "qty": 1},
    {"action": "SELL", "option_type": "C", "strike": 110, "qty": 1},
]


def snap(index, action, strike, price, size=10, md=1, age=10):
    return {
        "leg_index": index,
        "action": action,
        "option_type": "C",
        "strike": strike,
        "qty": 1,
        "executable_side": "ASK" if action == "BUY" else "BID",
        "executable_price": price,
        "executable_size": size,
        "price_age_ms": age,
        "size_age_ms": age,
        "market_data_type": md,
        "market_data_type_name": "live" if md == 1 else "delayed",
    }


def sample(t, buy, sell, status="LIVE_POSITIVE", sell_size=10, md=1):
    return {
        "elapsed_sec": t,
        "sample_status": status,
        "leg_snapshots": [
            snap(0, "BUY", 100, buy),
            snap(1, "SELL", 110, sell, size=sell_size, md=md),
        ],
    }


def phase4_row(samples, candidate="c1"):
    return {
        "candidate_id": candidate,
        "underlying": "7203",
        "expiry": "2026-12-01",
        "legs_json": json.dumps(LEGS),
        "floor_pv_per_share": "10",
        "lot_size": "100",
        "fee_per_contract_leg": "100",
        "effective_fee_per_contract_leg": "100",
        "monitor_started_at_utc": "2026-09-14T01:00:00+00:00",
        "monitor_finished_at_utc": "2026-09-14T01:00:10+00:00",
        "monitor_samples_json": json.dumps(samples),
    }


def phase9_row(candidate="c1", decision="NO_GO_EXCHANGE_ATOMIC_COMBO"):
    return {
        "candidate_id": candidate,
        "phase9_candidate_decision": decision,
        "phase9_live_money_allowed": "False",
        "phase9_atomicity_established": "False",
    }


class ReplayPathTests(unittest.TestCase):
    def setUp(self):
        self.legs = phase10.parse_legs_json(json.dumps(LEGS))

    def test_sequential_replay_can_remain_positive(self):
        samples = [sample(0.0, 5, 4), sample(0.5, 5, 4), sample(1.0, 5, 4)]
        out = phase10.replay_path(
            samples, self.legs, trigger_index=0, order=(0, 1),
            floor_pv_per_share=10, lot_size=100, fee_per_contract_leg=100,
            config=phase10.ReplayConfig(leg_delay_sec=0.5),
        )
        # debit = 5 - 4 = 1; gross = 900; fees = 200; net = 700
        self.assertEqual(out["path_status"], "COMPLETE_POSITIVE")
        self.assertAlmostEqual(out["net_edge_per_contract"], 700.0)
        self.assertAlmostEqual(out["fill_window_sec"], 0.5)

    def test_adverse_second_leg_can_destroy_edge(self):
        samples = [sample(0.0, 5, 4), sample(0.5, 5, 0.5), sample(1.0, 5, 0.5)]
        out = phase10.replay_path(
            samples, self.legs, trigger_index=0, order=(0, 1),
            floor_pv_per_share=10, lot_size=100, fee_per_contract_leg=100,
            config=phase10.ReplayConfig(leg_delay_sec=0.5),
        )
        self.assertEqual(out["path_status"], "COMPLETE_POSITIVE")
        self.assertAlmostEqual(out["net_edge_per_contract"], 350.0)
        # Make the deterioration large enough to flip sign.
        samples[1]["leg_snapshots"][1]["executable_price"] = 0.0
        out2 = phase10.replay_path(
            samples, self.legs, trigger_index=0, order=(0, 1),
            floor_pv_per_share=5, lot_size=100, fee_per_contract_leg=100,
            config=phase10.ReplayConfig(leg_delay_sec=0.5),
        )
        self.assertEqual(out2["path_status"], "COMPLETE_NONPOSITIVE")

    def test_insufficient_size_makes_path_incomplete(self):
        samples = [sample(0.0, 5, 4), sample(0.5, 5, 4, sell_size=0)]
        out = phase10.replay_path(
            samples, self.legs, trigger_index=0, order=(0, 1),
            floor_pv_per_share=10, lot_size=100, fee_per_contract_leg=0,
            config=phase10.ReplayConfig(leg_delay_sec=0.5),
        )
        self.assertEqual(out["path_status"], "INCOMPLETE")
        self.assertEqual(out["incomplete_reason"], "NO_EXECUTABLE_SIZE")

    def test_nonlive_later_leg_is_incomplete(self):
        samples = [sample(0.0, 5, 4), sample(0.5, 5, 4, md=3)]
        out = phase10.replay_path(
            samples, self.legs, trigger_index=0, order=(0, 1),
            floor_pv_per_share=10, lot_size=100, fee_per_contract_leg=0,
            config=phase10.ReplayConfig(leg_delay_sec=0.5),
        )
        self.assertEqual(out["incomplete_reason"], "NONLIVE_DATA")

    def test_order_permutations_capture_sequence_risk(self):
        samples = [
            sample(0.0, 5, 4),
            sample(0.5, 9, 1),
        ]
        session, paths = phase10.replay_session(
            phase4_row(samples), config=phase10.ReplayConfig(leg_delay_sec=0.5)
        )
        self.assertEqual(session["session_status"], "REPLAYED")
        self.assertEqual(session["permutations_per_trigger"], 2)
        edges = sorted(p["net_edge_per_contract"] for p in paths if p["path_status"].startswith("COMPLETE_"))
        # BUY-first uses 5 then 1 => debit 4; SELL-first uses 4 then 9 => debit 5.
        self.assertEqual(edges, [300.0, 400.0])


    def test_end_of_window_trigger_is_excluded_not_counted_incomplete(self):
        samples = [
            sample(0.0, 5, 4),
            sample(0.5, 5, 4),
        ]
        session, paths = phase10.replay_session(
            phase4_row(samples), config=phase10.ReplayConfig(leg_delay_sec=0.5)
        )
        self.assertEqual(session["session_status"], "REPLAYED")
        self.assertEqual(session["trigger_samples"], 1)
        self.assertEqual(session["trigger_samples_skipped_end_of_window"], 1)
        self.assertEqual(session["path_completion_ratio"], 1.0)

    def test_old_phase4_schema_requests_recollection(self):
        old = phase4_row([{"elapsed_sec": 0.0, "sample_status": "LIVE_POSITIVE"}])
        session, paths = phase10.replay_session(old, config=phase10.ReplayConfig())
        self.assertEqual(session["session_status"], "NEEDS_ENRICHED_PHASE4_CAPTURE")
        self.assertEqual(paths, [])


class CandidateGateTests(unittest.TestCase):
    def test_phase9_safety_inconsistency_is_not_eligible(self):
        p9 = phase9_row()
        p9["phase9_live_money_allowed"] = "True"
        out = phase10.aggregate_candidate("c1", p9, [], thresholds=phase10.DecisionThresholds())
        self.assertEqual(out["phase10_candidate_decision"], "NOT_ELIGIBLE_FROM_PHASE9")
        self.assertFalse(out["phase10_live_money_allowed"])

    def test_strong_replay_can_only_reach_research_survives(self):
        sessions = []
        for i in range(3):
            sessions.append({
                "session_status": "REPLAYED",
                "paths_total": 10,
                "paths_complete": 10,
                "paths_positive": 10,
                "trigger_samples": 5,
                "robust_trigger_count": 5,
                "worst_completed_path_edge_jpy": 1200,
                "p10_completed_path_edge_jpy": 1300,
                "median_trigger_worst_edge_jpy": 1500,
            })
        out = phase10.aggregate_candidate(
            "c1", phase9_row(), sessions, thresholds=phase10.DecisionThresholds()
        )
        self.assertEqual(out["phase10_candidate_decision"], "NONATOMIC_SIGNAL_SURVIVES_REPLAY")
        self.assertFalse(out["phase10_live_money_allowed"])
        self.assertFalse(out["phase10_atomicity_established"])

    def test_negative_tail_stops_nonatomic_path(self):
        sessions = [{
            "session_status": "REPLAYED",
            "paths_total": 10,
            "paths_complete": 10,
            "paths_positive": 4,
            "trigger_samples": 5,
            "robust_trigger_count": 0,
            "worst_completed_path_edge_jpy": -5000,
            "p10_completed_path_edge_jpy": -1000,
            "median_trigger_worst_edge_jpy": -500,
        }] * 3
        out = phase10.aggregate_candidate(
            "c1", phase9_row(), sessions, thresholds=phase10.DecisionThresholds()
        )
        self.assertEqual(out["phase10_candidate_decision"], "STOP_NONATOMIC_EDGE_NOT_ROBUST")


class EndToEndTests(unittest.TestCase):
    def test_evaluate_detects_old_phase4_data_and_never_authorizes_live(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            p9 = td / "phase9.csv"
            p4 = td / "phase4.csv"
            with p9.open("w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(phase9_row().keys()))
                w.writeheader(); w.writerow(phase9_row())
            old = phase4_row([{"elapsed_sec": 0.0, "sample_status": "LIVE_POSITIVE"}])
            with p4.open("w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(old.keys()))
                w.writeheader(); w.writerow(old)
            candidates, sessions, paths, summary = phase10.evaluate(p9, [str(p4)])
            self.assertEqual(candidates[0]["phase10_candidate_decision"], "NEEDS_PHASE4_RECOLLECTION")
            self.assertEqual(summary["phase10_overall_decision"], "RECOLLECT_ENRICHED_PHASE4_DATA")
            self.assertFalse(summary["phase10_live_money_allowed"])
            self.assertEqual(paths, [])


if __name__ == "__main__":
    unittest.main()
