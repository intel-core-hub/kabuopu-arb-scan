import csv
import importlib.util
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_broker_confirmation.py"
spec = importlib.util.spec_from_file_location("phase9", SCRIPT)
phase9 = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(phase9)


def evidence(strategy="UNAVAILABLE", bag="UNKNOWN", route="UNKNOWN", guarantee="UNKNOWN"):
    return {
        "venue": {
            "strategy_trades": strategy,
            "verified_date": "2026-09-14",
            "source": "jpx",
        },
        "broker": {
            "ose_sso_bag_supported": bag,
            "ose_sso_route_mode": route,
            "ose_sso_execution_guarantee": guarantee,
            "verified_date": "2026-09-14",
            "source": "ibkr",
            "support_case_id": "CASE-1",
        },
    }


def row(decision="BROKER_CONFIRMATION_REQUIRED"):
    return {
        "candidate_id": "c1",
        "phase8_candidate_decision": decision,
        "phase8_live_money_allowed": "False",
        "phase8_atomicity_established": "False",
    }


class Phase9Tests(unittest.TestCase):
    def test_current_venue_unavailable_blocks_atomic_combo(self):
        out = phase9.evaluate_candidate(row(), evidence(), as_of=date(2026, 9, 14))
        self.assertEqual(out["phase9_candidate_decision"], "NO_GO_EXCHANGE_ATOMIC_COMBO")
        self.assertFalse(out["phase9_live_money_allowed"])
        self.assertFalse(out["phase9_atomicity_established"])

    def test_bag_yes_with_unavailable_venue_is_non_atomic_research_only(self):
        out = phase9.evaluate_candidate(
            row(), evidence(bag="YES", route="SMART", guarantee="NON_ATOMIC_OR_LEGGING_POSSIBLE"),
            as_of=date(2026, 9, 14),
        )
        self.assertEqual(out["phase9_candidate_decision"], "NON_ATOMIC_EXECUTION_RESEARCH_ONLY")

    def test_direct_atomic_claim_conflicts_with_unavailable_venue(self):
        out = phase9.evaluate_candidate(
            row(), evidence(bag="YES", route="DIRECT_EXCHANGE", guarantee="ATOMIC"),
            as_of=date(2026, 9, 14),
        )
        self.assertEqual(out["phase9_candidate_decision"], "EVIDENCE_CONFLICT_MANUAL_ESCALATION")

    def test_available_venue_but_unknown_broker_waits(self):
        out = phase9.evaluate_candidate(row(), evidence(strategy="AVAILABLE"), as_of=date(2026, 9, 14))
        self.assertEqual(out["phase9_candidate_decision"], "WAIT_BROKER_CONFIRMATION")

    def test_available_direct_atomic_still_never_goes_live(self):
        out = phase9.evaluate_candidate(
            row(), evidence(strategy="AVAILABLE", bag="YES", route="DIRECT_EXCHANGE", guarantee="ATOMIC"),
            as_of=date(2026, 9, 14),
        )
        self.assertEqual(out["phase9_candidate_decision"], "MANUAL_LIVE_DESIGN_REVIEW_REQUIRED")
        self.assertFalse(out["phase9_live_money_allowed"])
        self.assertFalse(out["phase9_atomicity_established"])

    def test_smart_route_is_non_atomic_research(self):
        out = phase9.evaluate_candidate(
            row(), evidence(strategy="AVAILABLE", bag="YES", route="SMART", guarantee="NON_ATOMIC_OR_LEGGING_POSSIBLE"),
            as_of=date(2026, 9, 14),
        )
        self.assertEqual(out["phase9_candidate_decision"], "NON_ATOMIC_EXECUTION_RESEARCH_ONLY")

    def test_non_promoted_phase8_candidate_is_not_eligible(self):
        out = phase9.evaluate_candidate(row("KEEP_PAPER_OBSERVING"), evidence(), as_of=date(2026, 9, 14))
        self.assertEqual(out["phase9_candidate_decision"], "NOT_ELIGIBLE_FROM_PHASE8")

    def test_phase8_safety_flag_inconsistency_stops(self):
        r = row()
        r["phase8_live_money_allowed"] = "True"
        out = phase9.evaluate_candidate(r, evidence(strategy="AVAILABLE"), as_of=date(2026, 9, 14))
        self.assertEqual(out["phase9_candidate_decision"], "STOP_INCONSISTENT_PHASE8_SAFETY_FLAGS")

    def test_stale_available_evidence_requires_recheck(self):
        ev = evidence(strategy="AVAILABLE", bag="YES", route="DIRECT_EXCHANGE", guarantee="ATOMIC")
        ev["venue"]["verified_date"] = "2025-01-01"
        out = phase9.evaluate_candidate(row(), ev, as_of=date(2026, 9, 14), max_age_days=90)
        self.assertEqual(out["phase9_candidate_decision"], "STALE_EVIDENCE_RECHECK_REQUIRED")

    def test_invalid_enum_fails_closed(self):
        ev = evidence()
        ev["broker"]["ose_sso_route_mode"] = "MAGIC"
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "e.json"
            p.write_text(json.dumps(ev), encoding="utf-8")
            with self.assertRaises(ValueError):
                phase9.load_evidence(p)

    def test_end_to_end_summary_never_authorizes_live(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            csv_path = td / "p8.csv"
            ev_path = td / "e.json"
            with csv_path.open("w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(row().keys()))
                w.writeheader(); w.writerow(row())
            ev_path.write_text(json.dumps(evidence()), encoding="utf-8")
            rows, summary = phase9.evaluate(csv_path, ev_path, as_of=date(2026, 9, 14))
            self.assertEqual(len(rows), 1)
            self.assertEqual(summary["phase9_overall_decision"], "ATOMIC_COMBO_PATH_BLOCKED")
            self.assertFalse(summary["phase9_live_money_allowed"])
            self.assertFalse(summary["phase9_atomicity_established"])


if __name__ == "__main__":
    unittest.main()
