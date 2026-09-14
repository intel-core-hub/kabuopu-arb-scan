from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rec = load("phase18_recorder", "scripts/ibkr_record_package_events.py")
ana = load("phase18_analyzer", "scripts/analyze_event_driven_package_intervals.py")


def event(t, idx, action, qty, kind, value, *, cid="c1", sid="s1"):
    return {
        "candidate_id": cid, "session_id": sid, "monitor_started_at_utc": "2026-09-14T00:00:00+00:00",
        "underlying": "7203", "expiry": "2026-10-09", "floor_pv_per_share": "0",
        "lot_size": "100", "fee_per_contract_leg": "0", "requested_market_data_type": "1",
        "elapsed_sec": str(t), "local_time_utc": "", "req_id": str(100 + idx), "leg_index": str(idx),
        "action": action, "option_type": "C" if idx == 0 else "P", "strike": str(1000 + idx * 100),
        "qty": str(qty), "event_kind": kind, "tick_type": "", "value": str(value),
    }


def marker(t, kind, *, cid="c1", sid="s1"):
    r = event(t, 0, "BUY", 1, kind, "", cid=cid, sid=sid)
    r.update({"req_id": "", "leg_index": "", "action": "", "option_type": "", "strike": "", "qty": "", "value": ""})
    return r


def two_leg_stream(*, start=1.0, end=3.0, invalid_at=None, sid="s1"):
    rows = []
    # Warmup: BUY ask 1.0 and SELL bid 2.0 -> debit=-1 => +100 edge with floor 0.
    for idx, action, price_kind, px, size_kind in [
        (0, "BUY", "ASK_PRICE", 1.0, "ASK_SIZE"),
        (1, "SELL", "BID_PRICE", 2.0, "BID_SIZE"),
    ]:
        rows += [
            event(0.1, idx, action, 1, "MARKET_DATA_TYPE", 1, sid=sid),
            event(0.2, idx, action, 1, price_kind, px, sid=sid),
            event(0.3, idx, action, 1, size_kind, 5, sid=sid),
        ]
    rows.append(marker(start, "ANALYSIS_START", sid=sid))
    if invalid_at is not None:
        rows.append(event(invalid_at, 0, "BUY", 1, "ASK_PRICE", 3.0, sid=sid))
    rows.append(marker(end, "ANALYSIS_END", sid=sid))
    return rows


class RecorderTests(unittest.TestCase):
    def test_parse_legs(self):
        legs = rec.parse_legs_json('[{"action":"BUY","option_type":"C","strike":1000,"qty":2}]')
        self.assertEqual((legs[0].action, legs[0].qty), ("BUY", 2))

    def test_phase17_gate_rejects(self):
        with self.assertRaises(ValueError):
            rec.ensure_phase17_ready({"phase17_candidate_decision": "NO"})

    def test_metadata_selection(self):
        row = rec.select_metadata_row([{"candidate_id":"c1","underlying":"7203","expiry":"x","legs_json":"[]","floor_pv_per_share":"0","lot_size":"100"}], "c1")
        self.assertEqual(row["underlying"], "7203")

    def test_recorder_has_no_order_api(self):
        src = (ROOT / "scripts/ibkr_record_package_events.py").read_text(encoding="utf-8")
        self.assertNotIn("placeOrder(", src)
        self.assertNotIn("cancelOrder(", src)
        self.assertNotIn("reqGlobalCancel(", src)


class AnalyzerTests(unittest.TestCase):
    def test_evaluate_positive_package(self):
        states = {0: ana.LegState("BUY",1,ask=1,ask_size=2,market_data_type=1,ask_time=1,ask_size_time=1),
                  1: ana.LegState("SELL",1,bid=2,bid_size=2,market_data_type=1,bid_time=1,bid_size_time=1)}
        ok, reason, edge = ana.evaluate_package(states, now=1.5, floor_pv_per_share=0, lot_size=100, fee_per_contract_leg=0, max_state_age_sec=3)
        self.assertTrue(ok); self.assertEqual(reason, "LIVE_POSITIVE_EXECUTABLE_PACKAGE"); self.assertEqual(edge, 100)

    def test_nonlive_rejected(self):
        states = {0: ana.LegState("BUY",1,ask=1,ask_size=2,market_data_type=3,ask_time=1,ask_size_time=1)}
        ok, reason, _ = ana.evaluate_package(states, now=1.1, floor_pv_per_share=2, lot_size=100, fee_per_contract_leg=0, max_state_age_sec=3)
        self.assertFalse(ok); self.assertIn("NONLIVE", reason)

    def test_insufficient_size_rejected(self):
        states = {0: ana.LegState("BUY",2,ask=1,ask_size=1,market_data_type=1,ask_time=1,ask_size_time=1)}
        ok, reason, _ = ana.evaluate_package(states, now=1.1, floor_pv_per_share=2, lot_size=100, fee_per_contract_leg=0, max_state_age_sec=3)
        self.assertFalse(ok); self.assertIn("INSUFFICIENT_SIZE", reason)

    def test_stale_boundary(self):
        s = ana.LegState("BUY",1,ask=1,ask_size=2,market_data_type=1,ask_time=1,ask_size_time=1)
        self.assertAlmostEqual(ana.next_expiry_time({0:s}, 2, 3), 4)
        ok, reason, _ = ana.evaluate_package({0:s}, now=4.0001, floor_pv_per_share=2, lot_size=100, fee_per_contract_leg=0, max_state_age_sec=3)
        self.assertFalse(ok); self.assertIn("STALE", reason)

    def test_interval_closes_on_price_worsening(self):
        session, intervals = ana.analyze_session(two_leg_stream(invalid_at=2.2), max_state_age_sec=10)
        self.assertEqual(session["phase18_session_status"], "ANALYZED")
        self.assertEqual(len(intervals), 1)
        self.assertAlmostEqual(intervals[0]["interval_start_sec"], 1.0)
        self.assertAlmostEqual(intervals[0]["interval_end_sec"], 2.2)
        self.assertAlmostEqual(session["valid_package_time_ratio"], 1.2/2.0)

    def test_interval_expires_without_callbacks(self):
        rows = two_leg_stream(start=1.0, end=5.0)
        session, intervals = ana.analyze_session(rows, max_state_age_sec=2.0)
        # Earliest executable-side component (price at 0.2) expires at 2.2.
        self.assertEqual(len(intervals), 1)
        self.assertAlmostEqual(intervals[0]["interval_end_sec"], 2.2)
        self.assertAlmostEqual(session["valid_package_time_sec"], 1.2)

    def test_missing_markers(self):
        session, intervals = ana.analyze_session([event(0.1,0,"BUY",1,"MARKET_DATA_TYPE",1)], max_state_age_sec=3)
        self.assertEqual(session["phase18_session_status"], "MISSING_ANALYSIS_MARKERS")
        self.assertEqual(intervals, [])

    def test_bootstrap_single(self):
        self.assertEqual(ana.bootstrap_mean_interval([0.9], reps=100, confidence=.9, seed=1), (0.9,0.9,0.9))

    def test_phase17_gate_not_met(self):
        out = ana.aggregate_candidate("c1", {"phase17_candidate_decision":"NO"}, [], min_sessions=1, min_events_per_session=1, min_valid_time_ratio=.8, target_interval_sec=1, min_target_session_ratio=.5, bootstrap_reps=0, bootstrap_confidence=.9, seed=1)
        self.assertEqual(out["phase18_candidate_decision"], "PHASE17_GATE_NOT_MET")

    def test_aggregate_robust(self):
        gate = {"phase17_candidate_decision": ana.PHASE17_READY}
        sessions = [{"phase18_session_status":"ANALYZED","event_rows":30,"valid_package_time_ratio":.9,"longest_valid_interval_sec":2} for _ in range(3)]
        out = ana.aggregate_candidate("c1", gate, sessions, min_sessions=3, min_events_per_session=20, min_valid_time_ratio=.8, target_interval_sec=1, min_target_session_ratio=2/3, bootstrap_reps=100, bootstrap_confidence=.9, seed=1)
        self.assertEqual(out["phase18_candidate_decision"], ana.PHASE18_READY)

    def test_aggregate_not_robust(self):
        gate = {"phase17_candidate_decision": ana.PHASE17_READY}
        sessions = [{"phase18_session_status":"ANALYZED","event_rows":30,"valid_package_time_ratio":.4,"longest_valid_interval_sec":.5} for _ in range(3)]
        out = ana.aggregate_candidate("c1", gate, sessions, min_sessions=3, min_events_per_session=20, min_valid_time_ratio=.8, target_interval_sec=1, min_target_session_ratio=2/3, bootstrap_reps=100, bootstrap_confidence=.9, seed=1)
        self.assertEqual(out["phase18_candidate_decision"], "EVENT_DRIVEN_PACKAGE_INTERVALS_NOT_ROBUST")

    def test_analyze_end_to_end(self):
        gate = [{"candidate_id":"c1","phase17_candidate_decision":ana.PHASE17_READY}]
        groups = {("c1",f"s{i}"): two_leg_stream(sid=f"s{i}") for i in range(3)}
        # max age is long enough to keep warmup state fresh throughout each 2s analysis.
        cands, sess, intervals, summary = ana.analyze(gate, groups, max_state_age_sec=10, min_sessions=3, min_events_per_session=8, min_valid_time_ratio=.8, target_interval_sec=1, min_target_session_ratio=2/3, bootstrap_reps=100, bootstrap_confidence=.9, seed=1)
        self.assertEqual(cands[0]["phase18_candidate_decision"], ana.PHASE18_READY)
        self.assertEqual(len(sess), 3); self.assertEqual(len(intervals), 3)
        self.assertEqual(summary["phase18_overall_decision"], "EVENT_DRIVEN_PACKAGE_RESEARCH_CANDIDATE_EXISTS")


if __name__ == "__main__":
    unittest.main()
