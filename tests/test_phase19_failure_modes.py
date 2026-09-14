from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("phase19", SCRIPTS / "analyze_package_failure_modes.py")
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = M
SPEC.loader.exec_module(M)


def row(t, kind, value="", *, leg="", action="", opt="", strike="", qty="", cid="C1", sid="S1"):
    return {
        "candidate_id": cid,
        "session_id": sid,
        "monitor_started_at_utc": "2026-09-14T00:00:00+00:00",
        "underlying": "7203",
        "expiry": "202610",
        "floor_pv_per_share": "10",
        "lot_size": "100",
        "fee_per_contract_leg": "0",
        "requested_market_data_type": "1",
        "elapsed_sec": str(t),
        "local_time_utc": "2026-09-14T00:00:00+00:00",
        "req_id": "",
        "leg_index": str(leg),
        "action": action,
        "option_type": opt,
        "strike": str(strike),
        "qty": str(qty),
        "event_kind": kind,
        "tick_type": "",
        "value": str(value),
    }


def valid_seed(*, sid="S1"):
    # BUY C100 @ ask 2, SELL P90 @ bid 1 => debit=1, floor=10 => positive edge.
    rows = []
    for leg, action, opt, strike, px_kind, px, sz_kind in [
        (0, "BUY", "C", 100, "ASK_PRICE", 2, "ASK_SIZE"),
        (1, "SELL", "P", 90, "BID_PRICE", 1, "BID_SIZE"),
    ]:
        rows += [
            row(0.10 + leg * 0.01, "MARKET_DATA_TYPE", 1, leg=leg, action=action, opt=opt, strike=strike, qty=1, sid=sid),
            row(0.20 + leg * 0.01, px_kind, px, leg=leg, action=action, opt=opt, strike=strike, qty=1, sid=sid),
            row(0.30 + leg * 0.01, sz_kind, 5, leg=leg, action=action, opt=opt, strike=strike, qty=1, sid=sid),
        ]
    rows.append(row(1.0, "ANALYSIS_START", sid=sid))
    return rows


def session_with(event=None, *, end=2.5, sid="S1"):
    rows = valid_seed(sid=sid)
    if event:
        rows.append(event)
    rows.append(row(end, "ANALYSIS_END", sid=sid))
    return rows


class Phase19Tests(unittest.TestCase):
    def test_classify_reason(self):
        self.assertEqual(M.classify_reason("NONPOSITIVE_EDGE"), (M.CAUSE_PRICE, None))
        self.assertEqual(M.classify_reason("LEG_2_INSUFFICIENT_SIZE"), (M.CAUSE_SIZE, 2))
        self.assertEqual(M.classify_reason("LEG_1_STALE_PRICE"), (M.CAUSE_STALE_PRICE, 1))

    def test_price_edge_collapse_single_breaker(self):
        ev = row(1.5, "ASK_PRICE", 12, leg=0, action="BUY", opt="C", strike=100, qty=1)
        session, failures = M.analyze_failure_session(session_with(ev), max_state_age_sec=3.0)
        self.assertEqual(session["failure_count"], 1)
        f = failures[0]
        self.assertEqual(f["failure_mode"], M.CAUSE_PRICE)
        self.assertEqual(f["breaker_leg_index"], 0)
        self.assertEqual(f["attribution_quality"], "SINGLE_BREAKER_LEG")
        self.assertEqual(f["boundary_source"], "CALLBACK")

    def test_size_loss(self):
        ev = row(1.5, "ASK_SIZE", 0, leg=0, action="BUY", opt="C", strike=100, qty=1)
        _, failures = M.analyze_failure_session(session_with(ev), max_state_age_sec=3.0)
        self.assertEqual(failures[0]["failure_mode"], M.CAUSE_SIZE)
        self.assertEqual(failures[0]["breaker_leg_index"], 0)

    def test_market_data_type_loss(self):
        ev = row(1.5, "MARKET_DATA_TYPE", 3, leg=1, action="SELL", opt="P", strike=90, qty=1)
        _, failures = M.analyze_failure_session(session_with(ev), max_state_age_sec=3.0)
        self.assertEqual(failures[0]["failure_mode"], M.CAUSE_MDT)
        self.assertEqual(failures[0]["breaker_leg_index"], 1)

    def test_stale_price_boundary(self):
        # ASK price leg 0 was last updated at 0.20, so with age=1 it expires first at 1.20.
        session, failures = M.analyze_failure_session(session_with(None, end=2.5), max_state_age_sec=1.0)
        self.assertEqual(session["failure_count"], 1)
        self.assertEqual(failures[0]["failure_mode"], M.CAUSE_STALE_PRICE)
        self.assertEqual(failures[0]["boundary_source"], "STALENESS")
        self.assertEqual(failures[0]["attribution_quality"], "STALENESS_BOUNDARY_EXACT")

    def test_analysis_end_is_right_censor_not_failure(self):
        session, failures = M.analyze_failure_session(session_with(None, end=2.0), max_state_age_sec=5.0)
        self.assertEqual(failures, [])
        self.assertEqual(session["failure_count"], 0)
        self.assertEqual(session["right_censored_valid_interval_count"], 1)
        self.assertGreater(session["valid_package_time_sec"], 0)

    def test_failure_incidence_uses_valid_time(self):
        ev = row(2.0, "ASK_SIZE", 0, leg=0, action="BUY", opt="C", strike=100, qty=1)
        session, _ = M.analyze_failure_session(session_with(ev, end=3.0), max_state_age_sec=5.0)
        self.assertAlmostEqual(session["valid_package_time_sec"], 1.0, places=6)
        self.assertAlmostEqual(session["local_failure_incidence_per_valid_minute"], 60.0, places=6)

    def test_gate_not_met(self):
        out = M.aggregate_candidate("C1", {"candidate_id": "C1", "phase18_candidate_decision": "NO"}, [], [], min_sessions=1, min_failures=1, artifact_dominance_threshold=0.5)
        self.assertEqual(out["phase19_candidate_decision"], "PHASE18_GATE_NOT_MET")

    def test_collect_more_sessions(self):
        gate = {"candidate_id": "C1", "phase18_candidate_decision": M.PHASE18_READY}
        out = M.aggregate_candidate("C1", gate, [], [], min_sessions=2, min_failures=1, artifact_dominance_threshold=0.5)
        self.assertEqual(out["phase19_candidate_decision"], "COLLECT_MORE_FAILURE_SESSIONS")

    def test_no_failures_observed(self):
        gate = {"candidate_id": "C1", "phase18_candidate_decision": M.PHASE18_READY}
        s = {"phase19_session_status": "ANALYZED", "valid_package_time_sec": 10, "right_censored_valid_interval_count": 1}
        out = M.aggregate_candidate("C1", gate, [s], [], min_sessions=1, min_failures=1, artifact_dominance_threshold=0.5)
        self.assertEqual(out["phase19_candidate_decision"], "NO_FAILURES_OBSERVED")

    def test_staleness_dominates(self):
        gate = {"candidate_id": "C1", "phase18_candidate_decision": M.PHASE18_READY}
        s = {"phase19_session_status": "ANALYZED", "valid_package_time_sec": 30, "right_censored_valid_interval_count": 0}
        failures = [{"failure_mode": M.CAUSE_STALE_PRICE, "breaker_leg_index": 0}] * 3 + [{"failure_mode": M.CAUSE_SIZE, "breaker_leg_index": 1}]
        out = M.aggregate_candidate("C1", gate, [s], failures, min_sessions=1, min_failures=4, artifact_dominance_threshold=0.5)
        self.assertEqual(out["phase19_candidate_decision"], "OBSERVATION_STALENESS_DOMINATES")

    def test_market_data_instability_dominates(self):
        gate = {"candidate_id": "C1", "phase18_candidate_decision": M.PHASE18_READY}
        s = {"phase19_session_status": "ANALYZED", "valid_package_time_sec": 30, "right_censored_valid_interval_count": 0}
        failures = [{"failure_mode": M.CAUSE_MDT, "breaker_leg_index": 0}] * 3 + [{"failure_mode": M.CAUSE_PRICE, "breaker_leg_index": 1}]
        out = M.aggregate_candidate("C1", gate, [s], failures, min_sessions=1, min_failures=4, artifact_dominance_threshold=0.5)
        self.assertEqual(out["phase19_candidate_decision"], "MARKET_DATA_INSTABILITY_DOMINATES")

    def test_characterized(self):
        gate = {"candidate_id": "C1", "phase18_candidate_decision": M.PHASE18_READY}
        s = {"phase19_session_status": "ANALYZED", "valid_package_time_sec": 60, "right_censored_valid_interval_count": 0}
        failures = [
            {"failure_mode": M.CAUSE_PRICE, "breaker_leg_index": 0},
            {"failure_mode": M.CAUSE_PRICE, "breaker_leg_index": 0},
            {"failure_mode": M.CAUSE_SIZE, "breaker_leg_index": 1},
            {"failure_mode": M.CAUSE_SIZE, "breaker_leg_index": 1},
            {"failure_mode": M.CAUSE_STALE_PRICE, "breaker_leg_index": 0},
        ]
        out = M.aggregate_candidate("C1", gate, [s], failures, min_sessions=1, min_failures=5, artifact_dominance_threshold=0.5)
        self.assertEqual(out["phase19_candidate_decision"], "FAILURE_MODES_CHARACTERIZED_FOR_NEXT_RESEARCH")
        self.assertAlmostEqual(out["local_failure_incidence_per_valid_minute"], 5.0)

    def test_analyze_end_to_end(self):
        phase18 = [{"candidate_id": "C1", "phase18_candidate_decision": M.PHASE18_READY}]
        groups = {}
        for i in range(3):
            sid = f"S{i}"
            ev = row(1.5, "ASK_SIZE", 0, leg=0, action="BUY", opt="C", strike=100, qty=1, sid=sid)
            groups[("C1", sid)] = session_with(ev, sid=sid)
        candidates, sessions, failures, summary = M.analyze(
            phase18, groups, max_state_age_sec=3.0, min_sessions=3,
            min_failures=3, artifact_dominance_threshold=0.8,
        )
        self.assertEqual(len(sessions), 3)
        self.assertEqual(len(failures), 3)
        self.assertEqual(candidates[0]["dominant_failure_mode"], M.CAUSE_SIZE)
        self.assertEqual(candidates[0]["phase19_candidate_decision"], "FAILURE_MODES_CHARACTERIZED_FOR_NEXT_RESEARCH")
        self.assertEqual(summary["phase19_overall_decision"], "FAILURE_MODE_RESEARCH_CANDIDATE_EXISTS")
        self.assertFalse(summary["phase19_live_money_allowed"])

    def test_safety_flags_present_on_failure(self):
        ev = row(1.5, "ASK_SIZE", 0, leg=0, action="BUY", opt="C", strike=100, qty=1)
        _, failures = M.analyze_failure_session(session_with(ev), max_state_age_sec=3.0)
        self.assertFalse(failures[0]["phase19_is_exchange_hazard"])
        self.assertFalse(failures[0]["phase19_is_fill_probability"])
        self.assertFalse(failures[0]["phase19_cancellation_inference_allowed"])
        self.assertFalse(failures[0]["phase19_live_money_allowed"])


if __name__ == "__main__":
    unittest.main()
