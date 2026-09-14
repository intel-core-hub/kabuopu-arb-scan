import importlib.util
import json
import tempfile
import unittest
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ibkr_probe_marketdata_capability.py"
spec = importlib.util.spec_from_file_location("phase11_probe", SCRIPT)
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
assert spec.loader is not None
spec.loader.exec_module(probe)


class Phase11ProbeTests(unittest.TestCase):
    def test_parse_legs(self):
        legs = probe.parse_legs_json(json.dumps([
            {"action": "BUY", "option_type": "C", "strike": 3000, "qty": 1},
            {"action": "SELL", "option_type": "P", "strike": 2900, "qty": 2},
        ]))
        self.assertEqual(len(legs), 2)
        self.assertEqual(legs[0].key, ("C", 3000.0))
        self.assertEqual(legs[1].qty, 2)

    def test_parse_legs_rejects_bad_action(self):
        with self.assertRaises(ValueError):
            probe.parse_legs_json('[{"action":"HOLD","option_type":"C","strike":3000,"qty":1}]')

    def test_l1_live_and_two_sided_depth_is_ready(self):
        l1 = probe.L1State(bid=10, ask=11, market_data_type=1)
        depth = probe.DepthStats(events=4, bid_events=2, ask_events=2, positive_size_events=4)
        status, _ = probe.classify_leg(l1=l1, depth=depth, errors=[])
        self.assertEqual(status, "REALTIME_L2_OBSERVED")

    def test_delayed_l1_is_not_live(self):
        l1 = probe.L1State(bid=10, ask=11, market_data_type=3)
        depth = probe.DepthStats(events=4, bid_events=2, ask_events=2, positive_size_events=4)
        status, reason = probe.classify_leg(l1=l1, depth=depth, errors=[])
        self.assertEqual(status, "NONLIVE_L1")
        self.assertIn("delayed", reason)

    def test_live_l1_without_depth_stops_before_fill_model(self):
        l1 = probe.L1State(bid=10, ask=11, market_data_type=1)
        status, _ = probe.classify_leg(l1=l1, depth=probe.DepthStats(), errors=[])
        self.assertEqual(status, "LIVE_L1_ONLY")

    def test_depth_event_counts_sides(self):
        stats = probe.DepthStats()
        probe.apply_depth_event(stats, position=0, side=1, size=3, is_l2=False)
        probe.apply_depth_event(stats, position=1, side=0, size=2, is_l2=True)
        self.assertEqual(stats.events, 2)
        self.assertEqual(stats.bid_events, 1)
        self.assertEqual(stats.ask_events, 1)
        self.assertEqual(stats.positive_size_events, 2)
        self.assertEqual(stats.direct_callbacks, 1)
        self.assertEqual(stats.l2_callbacks, 1)
        self.assertEqual(stats.max_position_seen, 1)

    def test_summary_ready_only_if_every_leg_ready(self):
        ready = {"probe_status": "REALTIME_L2_OBSERVED"}
        self.assertEqual(
            probe.summarize_probe([ready, ready])["phase11_overall_decision"],
            "READY_FOR_L2_FILL_MODEL",
        )
        mixed = [ready, {"probe_status": "LIVE_L1_ONLY"}]
        self.assertEqual(
            probe.summarize_probe(mixed)["phase11_overall_decision"],
            "REALTIME_L1_ESTABLISHED_L2_NOT_ESTABLISHED",
        )

    def test_summary_delayed_is_realtime_not_established(self):
        summary = probe.summarize_probe([{"probe_status": "NONLIVE_L1"}])
        self.assertEqual(summary["phase11_overall_decision"], "OSE_API_REALTIME_NOT_ESTABLISHED")
        self.assertFalse(summary["phase11_live_money_allowed"])
        self.assertFalse(summary["phase11_atomicity_established"])

    def test_load_candidate_by_id(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.csv"
            p.write_text(
                "candidate_id,underlying,expiry,legs_json\n"
                'a,7203,2026-10-09,"[{""action"":""BUY"",""option_type"":""C"",""strike"":3000,""qty"":1}]"\n'
                'b,6758,2026-10-09,"[{""action"":""BUY"",""option_type"":""P"",""strike"":2500,""qty"":1}]"\n',
                encoding="utf-8",
            )
            row = probe.load_candidate(p, "b")
            self.assertEqual(row["underlying"], "6758")

    def test_source_has_no_order_submission_api(self):
        source = SCRIPT.read_text(encoding="utf-8")
        # Market-data cancellation is expected; order APIs are intentionally absent.
        self.assertNotIn("place" + "Order(", source)
        self.assertNotIn("cancel" + "Order(", source)
        self.assertNotIn("reqGlobal" + "Cancel(", source)


if __name__ == "__main__":
    unittest.main()
