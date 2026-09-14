import importlib.util
import tempfile
import unittest
from pathlib import Path

import pandas as pd

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_persistence.py"
spec = importlib.util.spec_from_file_location("phase5", SCRIPT)
phase5 = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(phase5)


def row(status="PERSISTENT_LIVE_CANDIDATE", edge=1500, size=2, skew=200, live=20, positive=19, ts="2026-09-14T01:00:00+00:00"):
    return {
        "underlying": "7203",
        "expiry": "2026-10-08",
        "check": "long_box",
        "strikes": "3000,3200",
        "legs_json": '[{"action":"BUY","option_type":"C","strike":3000,"qty":1}]',
        "monitor_status": status,
        "samples_live": live,
        "samples_live_positive": positive,
        "longest_live_positive_run_sec": 9.0,
        "min_live_positive_edge_per_contract": edge,
        "min_live_positive_quote_size": size,
        "max_observed_side_skew_ms": skew,
        "monitor_started_at_utc": ts,
    }


class Phase5Tests(unittest.TestCase):
    def test_candidate_id_is_stable(self):
        a = phase5.candidate_id(row())
        b = phase5.candidate_id(dict(row(), monitor_status="ERROR"))
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)

    def test_candidate_id_canonicalizes_integral_underlying(self):
        a = phase5.candidate_id(dict(row(), underlying=7203))
        b = phase5.candidate_id(dict(row(), underlying=7203.0))
        self.assertEqual(a, b)

    def test_duplicate_candidate_in_one_file_counts_once(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s1.csv"
            pd.DataFrame([row(edge=1000), row(edge=2000)]).to_csv(p, index=False)
            sessions = phase5.load_sessions([p])
            self.assertEqual(len(sessions), 1)
            self.assertEqual(float(sessions.iloc[0]["min_live_positive_edge_per_contract"]), 2000.0)

    def test_promotes_reproducible_candidate(self):
        frames = []
        for i in range(3):
            r = row(ts=f"2026-09-{14+i:02d}T01:00:00+00:00")
            r["source_file"] = f"s{i}.csv"
            r["source_session"] = f"s{i}.csv"
            r["candidate_id"] = phase5.candidate_id(r)
            frames.append(r)
        sessions = pd.DataFrame(frames)
        agg = phase5.aggregate_candidates(sessions)
        ranked = phase5.classify_candidates(agg, min_distinct_days=2)
        self.assertEqual(ranked.iloc[0]["research_decision"], "PROMOTE_TO_EXECUTION_STUDY")
        self.assertEqual(int(ranked.iloc[0]["sessions_persistent"]), 3)

    def test_insufficient_evidence(self):
        r = row()
        r.update(source_file="s1.csv", source_session="s1.csv", candidate_id=phase5.candidate_id(r))
        agg = phase5.aggregate_candidates(pd.DataFrame([r]))
        ranked = phase5.classify_candidates(agg)
        self.assertEqual(ranked.iloc[0]["research_decision"], "INSUFFICIENT_EVIDENCE")

    def test_no_reproducible_edge_after_enough_sessions(self):
        frames = []
        for i in range(3):
            r = row(status="NO_PERSISTENT_LIVE_EDGE", edge=None, live=20, positive=0)
            r.update(source_file=f"s{i}.csv", source_session=f"s{i}.csv", candidate_id=phase5.candidate_id(r))
            frames.append(r)
        agg = phase5.aggregate_candidates(pd.DataFrame(frames))
        ranked = phase5.classify_candidates(agg, min_distinct_days=0)
        self.assertEqual(ranked.iloc[0]["research_decision"], "NO_REPRODUCIBLE_LIVE_EDGE")

    def test_keep_observing_when_edge_gate_fails(self):
        frames = []
        for i in range(3):
            r = row(edge=200)
            r.update(source_file=f"s{i}.csv", source_session=f"s{i}.csv", candidate_id=phase5.candidate_id(r))
            frames.append(r)
        agg = phase5.aggregate_candidates(pd.DataFrame(frames))
        ranked = phase5.classify_candidates(agg, min_distinct_days=0, min_edge_jpy=1000)
        self.assertEqual(ranked.iloc[0]["research_decision"], "KEEP_OBSERVING")
        self.assertIn("median_min_edge", ranked.iloc[0]["decision_reason"])

    def test_track_summary_prefers_promoted(self):
        ranked = pd.DataFrame([{
            "rank": 1, "candidate_id": "x", "underlying": "7203", "expiry": "2026-10-08",
            "check": "long_box", "strikes": "3000,3200", "research_decision": "PROMOTE_TO_EXECUTION_STUDY",
            "sessions_persistent": 3, "persistent_session_ratio": 1.0,
            "median_min_live_edge_per_contract": 1500.0, "live_positive_observation_ratio": 0.95,
        }])
        sessions = pd.DataFrame([{"source_file": "a.csv"}, {"source_file": "b.csv"}])
        summary = phase5.build_summary(ranked, sessions)
        self.assertEqual(summary["track_decision"], "PROMOTE_TOP_CANDIDATES_TO_EXECUTION_STUDY")


if __name__ == "__main__":
    unittest.main()
