import importlib.util
import json
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "analyze_synchronized_package_survival.py"
spec = importlib.util.spec_from_file_location("phase17_mod", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
assert spec.loader is not None
spec.loader.exec_module(mod)


def leg(idx, action, price, size=2, qty=1, mdt=1):
    return {
        "leg_index": idx,
        "action": action,
        "option_type": "C" if idx % 2 == 0 else "P",
        "strike": 100 + idx * 10,
        "qty": qty,
        "executable_side": "ASK" if action == "BUY" else "BID",
        "executable_price": price,
        "executable_size": size,
        "market_data_type": mdt,
        "market_data_type_name": "live" if mdt == 1 else "delayed",
    }


def sample(t, buy=10.0, sell=8.0, *, status="LIVE_POSITIVE", buy_size=2, sell_size=2, snapshots=True):
    row = {"elapsed_sec": t, "sample_status": status}
    if snapshots:
        row["leg_snapshots"] = [
            leg(0, "BUY", buy, buy_size),
            leg(1, "SELL", sell, sell_size),
        ]
    return row


def phase4_row(samples, started="2026-09-14T00:00:00+00:00"):
    return {
        "candidate_id": "cand1",
        "monitor_started_at_utc": started,
        "monitor_finished_at_utc": "2026-09-14T00:00:10+00:00",
        "monitor_samples_json": json.dumps(samples),
    }


def phase16(decision=mod.PHASE16_READY):
    return {"candidate_id": "cand1", "phase16_candidate_decision": decision}


class Phase17Tests(unittest.TestCase):
    def test_parse_horizons_sorted_unique(self):
        self.assertEqual(mod.parse_horizons("1,0.5,1,2"), [0.5, 1.0, 2.0])

    def test_trigger_requires_live_complete_snapshots(self):
        snaps, reason = mod.trigger_snapshots(sample(0))
        self.assertEqual(set(snaps), {0, 1})
        self.assertEqual(reason, "")
        snaps, reason = mod.trigger_snapshots(sample(0, status="NONLIVE_POSITIVE"))
        self.assertIsNone(snaps)
        self.assertEqual(reason, "NOT_LIVE_POSITIVE")

    def test_buy_and_sell_no_worse_prices_retain(self):
        trig, _ = mod.trigger_snapshots(sample(0, buy=10, sell=8))
        ok, _ = mod.package_checkpoint_retained(trig, sample(.5, buy=9.5, sell=8.5))
        self.assertTrue(ok)

    def test_worse_buy_price_breaks_package(self):
        trig, _ = mod.trigger_snapshots(sample(0, buy=10, sell=8))
        ok, reason = mod.package_checkpoint_retained(trig, sample(.5, buy=10.1, sell=8.5))
        self.assertFalse(ok)
        self.assertIn("BUY_PRICE_WORSE", reason)

    def test_missing_size_breaks_package(self):
        trig, _ = mod.trigger_snapshots(sample(0))
        ok, reason = mod.package_checkpoint_retained(trig, sample(.5, buy_size=0))
        self.assertFalse(ok)
        self.assertIn("NO_EXECUTABLE_SIZE", reason)

    def test_session_retains_half_second(self):
        samples = [sample(0), sample(.5, buy=9.9, sell=8.1), sample(1.0, buy=9.8, sell=8.2)]
        session, details = mod.analyze_session(phase4_row(samples), horizons_sec=[.5], max_target_overshoot_factor=1.5)
        self.assertEqual(session["phase17_session_status"], "ANALYZED")
        self.assertEqual(session["complete_triggers_500ms"], 2)
        self.assertEqual(session["retained_triggers_500ms"], 2)
        self.assertAlmostEqual(session["package_retention_500ms"], 1.0)
        self.assertEqual(len(details), 3)  # final trigger is end-of-window/incomplete

    def test_intermediate_failure_breaks_longer_horizon(self):
        samples = [sample(0), sample(.5, buy=10.2), sample(1.0, buy=9.8)]
        session, details = mod.analyze_session(phase4_row(samples), horizons_sec=[1.0], max_target_overshoot_factor=1.5)
        self.assertEqual(session["complete_triggers_1000ms"], 1)
        self.assertEqual(session["retained_triggers_1000ms"], 0)
        complete = [d for d in details if d["complete_window"]]
        self.assertIn("BUY_PRICE_WORSE", complete[0]["failure_reason"])

    def test_end_window_trigger_excluded_not_failed(self):
        samples = [sample(0), sample(.5), sample(1.0)]
        session, details = mod.analyze_session(phase4_row(samples), horizons_sec=[1.0], max_target_overshoot_factor=1.5)
        self.assertEqual(session["complete_triggers_1000ms"], 1)
        incomplete = [d for d in details if not d["complete_window"]]
        self.assertGreaterEqual(len(incomplete), 1)
        self.assertTrue(all(d["package_retained"] is None for d in incomplete))

    def test_large_target_gap_is_not_complete(self):
        samples = [sample(0), sample(.5), sample(3.0)]
        session, details = mod.analyze_session(phase4_row(samples), horizons_sec=[1.0], max_target_overshoot_factor=.5)
        complete_at_zero = [d for d in details if d["trigger_sample_index"] == 0][0]
        self.assertFalse(complete_at_zero["complete_window"])
        self.assertEqual(complete_at_zero["failure_reason"], "TARGET_SAMPLE_TOO_FAR")

    def test_old_phase4_schema_requests_recollection(self):
        old = phase4_row([sample(0, snapshots=False), sample(.5, snapshots=False)])
        session, _ = mod.analyze_session(old, horizons_sec=[.5], max_target_overshoot_factor=1.5)
        self.assertEqual(session["phase17_session_status"], "NEEDS_ENRICHED_PHASE4_CAPTURE")

    def test_phase16_gate_required(self):
        out = mod.aggregate_candidate(
            "cand1", phase16("TOUCH_RETENTION_NOT_ROBUST_TO_CONTROL_PATH"), [],
            target_horizon_sec=1.0, min_sessions=3, min_complete_triggers_per_session=1,
            min_retention=.8, bootstrap_reps=100, bootstrap_confidence=.9, seed=17,
        )
        self.assertEqual(out["phase17_candidate_decision"], "PHASE16_GATE_NOT_MET")

    def test_candidate_robust_uses_session_level_bootstrap(self):
        sessions = []
        for ratio in [.9, .9, 1.0]:
            sessions.append({
                "phase17_session_status": "ANALYZED",
                "complete_triggers_1000ms": 10,
                "package_retention_1000ms": ratio,
            })
        out = mod.aggregate_candidate(
            "cand1", phase16(), sessions,
            target_horizon_sec=1.0, min_sessions=3, min_complete_triggers_per_session=5,
            min_retention=.85, bootstrap_reps=500, bootstrap_confidence=.9, seed=17,
        )
        self.assertEqual(out["phase17_candidate_decision"], "SYNCHRONIZED_PACKAGE_TOUCH_ROBUST_ENOUGH_FOR_NEXT_RESEARCH")
        self.assertFalse(out["phase17_independence_assumption_used"])
        self.assertFalse(out["phase17_is_fill_probability"])

    def test_candidate_not_robust(self):
        sessions = [
            {"phase17_session_status": "ANALYZED", "complete_triggers_1000ms": 10, "package_retention_1000ms": x}
            for x in [.5, .6, .7]
        ]
        out = mod.aggregate_candidate(
            "cand1", phase16(), sessions,
            target_horizon_sec=1.0, min_sessions=3, min_complete_triggers_per_session=5,
            min_retention=.8, bootstrap_reps=500, bootstrap_confidence=.9, seed=17,
        )
        self.assertEqual(out["phase17_candidate_decision"], "SYNCHRONIZED_PACKAGE_TOUCH_NOT_ROBUST")

    def test_analyze_rollup(self):
        p4 = []
        for n in range(3):
            samples = [sample(i * .5, buy=10 - i * .01, sell=8 + i * .01) for i in range(8)]
            row = phase4_row(samples, f"2026-09-14T00:0{n}:00+00:00")
            p4.append(row)
        candidates, sessions, triggers, summary = mod.analyze(
            [phase16()], p4,
            horizons_sec=[.5, 1.0], target_horizon_sec=1.0,
            max_target_overshoot_factor=1.5, min_sessions=3,
            min_complete_triggers_per_session=5, min_retention=.8,
            bootstrap_reps=200, bootstrap_confidence=.9, seed=17,
        )
        self.assertEqual(candidates[0]["phase17_candidate_decision"], "SYNCHRONIZED_PACKAGE_TOUCH_ROBUST_ENOUGH_FOR_NEXT_RESEARCH")
        self.assertEqual(summary["phase17_overall_decision"], "SYNCHRONIZED_PACKAGE_RESEARCH_CANDIDATE_EXISTS")
        self.assertEqual(len(sessions), 3)
        self.assertGreater(len(triggers), 0)
        self.assertTrue(summary["phase17_is_joint_displayed_touch_metric"])
        self.assertFalse(summary["phase17_is_fill_probability"])

    def test_source_has_no_broker_or_order_api(self):
        src = MODULE_PATH.read_text(encoding="utf-8")
        for forbidden in ["ibapi", "placeOrder(", "cancelOrder(", "reqGlobalCancel(", "reqMktData(", "reqMktDepth("]:
            self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
