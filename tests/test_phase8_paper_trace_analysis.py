import importlib.util
import json
import pathlib
import tempfile
import unittest

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
spec = importlib.util.spec_from_file_location("phase8", SCRIPTS / "analyze_paper_combo_traces.py")
phase8 = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(phase8)


def js(x):
    return json.dumps(x, separators=(",", ":"))


def base_row(**kw):
    row = {
        "candidate_id": "c1",
        "underlying": "7203",
        "expiry": "2026-10-09",
        "legs_json": js([
            {"action": "BUY", "option_type": "C", "strike": 3000, "qty": 1},
            {"action": "SELL", "option_type": "C", "strike": 3100, "qty": 1},
        ]),
        "phase7_status": "PAPER_TEST_COMPLETE",
        "phase7_trace_class": "PAPER_FULL_FILL_OBSERVED",
        "phase7_order_id": 7,
        "phase7_started_at_utc": "2026-09-14T01:00:00.000+00:00",
        "phase7_leg_count": 2,
        "phase7_open_order_seen": True,
        "phase7_status_events_json": js([
            {"ts_utc": "2026-09-14T01:00:01.000+00:00", "status": "Submitted", "filled": 0, "remaining": 1},
            {"ts_utc": "2026-09-14T01:00:02.000+00:00", "status": "Filled", "filled": 1, "remaining": 0},
        ]),
        "phase7_execution_events_json": js([
            {"ts_utc": "2026-09-14T01:00:01.950+00:00", "exec_id": "e1", "sec_type": "BAG", "con_id": 100, "side": "BOT", "shares": 1, "price": 12.5},
        ]),
        "phase7_errors_json": "[]",
    }
    row.update(kw)
    return row


class Phase8Tests(unittest.TestCase):
    def test_bag_only_full_fill(self):
        out = phase8.analyze_session(base_row())
        self.assertEqual(out["phase8_session_class"], "BAG_ONLY_FULL_FILL_OBSERVED")
        self.assertEqual(out["phase8_reporting_mode"], "BAG_ONLY")
        self.assertFalse(out["phase8_live_money_allowed"])
        self.assertFalse(out["phase8_atomicity_established"])

    def test_opt_callback_materially_before_parent_full_flags_sequence(self):
        row = base_row(
            phase7_execution_events_json=js([
                {"ts_utc": "2026-09-14T01:00:01.000+00:00", "exec_id": "leg1", "sec_type": "OPT", "con_id": 1, "side": "BOT", "shares": 1, "price": 20},
                {"ts_utc": "2026-09-14T01:00:01.100+00:00", "exec_id": "leg2", "sec_type": "OPT", "con_id": 2, "side": "SLD", "shares": 1, "price": 7.5},
            ])
        )
        out = phase8.analyze_session(row, sequence_gap_ms=250)
        self.assertEqual(out["phase8_session_class"], "LEG_CALLBACK_SEQUENCE_RISK")
        self.assertEqual(out["phase8_opt_before_parent_full_count"], 2)

    def test_opt_callbacks_close_to_parent_fill_are_reporting_not_legging_proof(self):
        row = base_row(
            phase7_execution_events_json=js([
                {"ts_utc": "2026-09-14T01:00:01.900+00:00", "exec_id": "leg1", "sec_type": "OPT", "con_id": 1},
                {"ts_utc": "2026-09-14T01:00:01.950+00:00", "exec_id": "leg2", "sec_type": "OPT", "con_id": 2},
            ])
        )
        out = phase8.analyze_session(row, sequence_gap_ms=250)
        self.assertEqual(out["phase8_session_class"], "LEG_LEVEL_REPORTING_OBSERVED")
        self.assertEqual(out["phase8_opt_before_parent_full_count"], 0)

    def test_exact_duplicate_exec_id_is_deduplicated(self):
        e = {"ts_utc": "2026-09-14T01:00:01.950+00:00", "exec_id": "e1", "sec_type": "BAG", "con_id": 100}
        out = phase8.analyze_session(base_row(phase7_execution_events_json=js([e, dict(e)])))
        self.assertEqual(out["phase8_execution_event_count_raw"], 2)
        self.assertEqual(out["phase8_execution_event_count_dedup"], 1)
        self.assertEqual(out["phase8_duplicate_execution_callbacks"], 1)

    def test_structural_combo_error_has_priority(self):
        row = base_row(phase7_errors_json=js([{"req_id": 7, "code": 312, "message": "combo invalid"}]))
        out = phase8.analyze_session(row)
        self.assertEqual(out["phase8_session_class"], "STRUCTURAL_COMBO_REJECT")

    def test_filled_without_exec_is_inconsistent(self):
        out = phase8.analyze_session(base_row(phase7_execution_events_json="[]"))
        self.assertEqual(out["phase8_session_class"], "REVIEW_TRACE_INCONSISTENT")

    def test_malformed_json_is_not_usable(self):
        out = phase8.analyze_session(base_row(phase7_execution_events_json="{"))
        self.assertEqual(out["phase8_session_class"], "REVIEW_MALFORMED_TRACE")
        self.assertFalse(out["phase8_usable_session"])

    def test_repeated_bag_only_fills_require_broker_confirmation_not_live_go(self):
        rows = [phase8.analyze_session(base_row(phase7_order_id=i, phase7_started_at_utc=f"2026-09-{14+i:02d}T01:00:00+00:00")) for i in range(1, 4)]
        summary = phase8.summarize_candidate(pd.DataFrame(rows), min_sessions=3, min_bag_full_fills=2)
        self.assertEqual(summary["phase8_candidate_decision"], "BROKER_CONFIRMATION_REQUIRED")
        self.assertFalse(summary["phase8_live_money_allowed"])
        self.assertFalse(summary["phase8_atomicity_established"])

    def test_any_leg_sequence_risk_blocks_promotion(self):
        good = phase8.analyze_session(base_row(phase7_order_id=1))
        risky = phase8.analyze_session(base_row(
            phase7_order_id=2,
            phase7_execution_events_json=js([
                {"ts_utc": "2026-09-14T01:00:01.000+00:00", "exec_id": "l1", "sec_type": "OPT", "con_id": 1}
            ]),
        ))
        summary = phase8.summarize_candidate(pd.DataFrame([good, risky]), min_sessions=2, min_bag_full_fills=1)
        self.assertEqual(summary["phase8_candidate_decision"], "INVESTIGATE_LEG_CALLBACK_SEQUENCE")

    def test_evaluate_deduplicates_same_session_across_overlapping_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "trace.csv"
            pd.DataFrame([base_row()]).to_csv(p, index=False)
            sessions, ranking, summary = phase8.evaluate([str(p), str(pathlib.Path(td) / "*.csv")], min_sessions=1, min_bag_full_fills=1)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(len(ranking), 1)
            self.assertEqual(summary["unique_sessions"], 1)

    def test_overall_never_emits_live_go(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "trace.csv"
            rows = [base_row(phase7_order_id=i, phase7_started_at_utc=f"2026-09-{14+i:02d}T01:00:00+00:00") for i in range(1, 4)]
            pd.DataFrame(rows).to_csv(p, index=False)
            _, _, summary = phase8.evaluate([str(p)], min_sessions=3, min_bag_full_fills=2)
            self.assertEqual(summary["phase8_overall_decision"], "NO_LIVE_AUTOMATION_BROKER_CONFIRMATION_REQUIRED")
            self.assertFalse(summary["phase8_live_money_allowed"])


if __name__ == "__main__":
    unittest.main()
