import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ibkr_monitor_findings", ROOT / "scripts/ibkr_monitor_findings.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class TickHandlingTests(unittest.TestCase):
    def test_exact_side_timestamps(self):
        q = mod.StreamQuote()
        self.assertTrue(mod.apply_tick_price(q, 1, 10.0, 100.0))
        self.assertTrue(mod.apply_tick_price(q, 2, 11.0, 101.0))
        self.assertEqual(100.0, q.bid_update_mono)
        self.assertEqual(101.0, q.ask_update_mono)
        self.assertTrue(mod.apply_tick_size(q, 69, 7, 102.0))
        self.assertTrue(mod.apply_tick_size(q, 70, 8, 103.0))
        self.assertEqual(7.0, q.bid_size)
        self.assertEqual(8.0, q.ask_size)


class SampleCandidateTests(unittest.TestCase):
    def legs(self):
        return mod.parse_legs_json(json.dumps([
            {"action": "BUY", "option_type": "C", "strike": 100, "qty": 1},
            {"action": "SELL", "option_type": "C", "strike": 200, "qty": 1},
            {"action": "BUY", "option_type": "P", "strike": 200, "qty": 1},
            {"action": "SELL", "option_type": "P", "strike": 100, "qty": 1},
        ]))

    def q(self, bid, ask, t, market_data_type=1, size=10):
        return mod.StreamQuote(
            bid=bid, ask=ask, bid_size=size, ask_size=size,
            market_data_type=market_data_type,
            bid_update_mono=t, ask_update_mono=t,
            bid_size_update_mono=t, ask_size_update_mono=t,
        )

    def quotes(self, t=100.0, market_data_type=1):
        return {
            ("C", 100.0): self.q(99, 100, t, market_data_type),
            ("C", 200.0): self.q(40, 41, t, market_data_type),
            ("P", 200.0): self.q(24, 25, t, market_data_type),
            ("P", 100.0): self.q(5, 6, t, market_data_type),
        }

    def test_live_positive_sample(self):
        r = mod.sample_candidate(
            self.legs(), self.quotes(), floor_pv_per_share=100,
            lot_size=100, fee_per_contract_leg=100,
            now_mono=100.5, max_quote_age_sec=3.0,
        )
        self.assertEqual("LIVE_POSITIVE", r["sample_status"])
        self.assertAlmostEqual(1600.0, r["net_edge_per_contract"])
        self.assertAlmostEqual(10.0, r["min_quote_size"])

    def test_delayed_never_counts_as_live(self):
        r = mod.sample_candidate(
            self.legs(), self.quotes(market_data_type=3), floor_pv_per_share=100,
            lot_size=100, fee_per_contract_leg=100,
            now_mono=100.5, max_quote_age_sec=3.0,
        )
        self.assertEqual("NONLIVE_POSITIVE", r["sample_status"])

    def test_stale_executable_side_rejected(self):
        quotes = self.quotes(t=90.0)
        r = mod.sample_candidate(
            self.legs(), quotes, floor_pv_per_share=100,
            lot_size=100, fee_per_contract_leg=100,
            now_mono=100.0, max_quote_age_sec=3.0,
        )
        self.assertEqual("STALE_QUOTE", r["sample_status"])


    def test_zero_or_insufficient_size_is_not_executable(self):
        quotes = self.quotes()
        quotes[("P", 100.0)].bid_size = 0
        r = mod.sample_candidate(
            self.legs(), quotes, floor_pv_per_share=100,
            lot_size=100, fee_per_contract_leg=100,
            now_mono=100.5, max_quote_age_sec=3.0,
        )
        self.assertEqual("NO_EXECUTABLE_SIZE", r["sample_status"])

    def test_stale_size_is_rejected(self):
        quotes = self.quotes()
        quotes[("P", 100.0)].bid_size_update_mono = 90.0
        r = mod.sample_candidate(
            self.legs(), quotes, floor_pv_per_share=100,
            lot_size=100, fee_per_contract_leg=100,
            now_mono=100.5, max_quote_age_sec=3.0,
        )
        self.assertEqual("STALE_SIZE", r["sample_status"])

    def test_side_skew_is_measured(self):
        quotes = self.quotes(t=100.0)
        quotes[("P", 100.0)].bid_update_mono = 100.7
        r = mod.sample_candidate(
            self.legs(), quotes, floor_pv_per_share=100,
            lot_size=100, fee_per_contract_leg=100,
            now_mono=101.0, max_quote_age_sec=3.0,
        )
        self.assertAlmostEqual(700.0, r["side_skew_ms"])


class SummaryTests(unittest.TestCase):
    def test_longest_positive_run_lower_bound(self):
        samples = [
            {"sample_status": "LIVE_POSITIVE", "net_edge_per_contract": 100, "min_quote_size": 2},
            {"sample_status": "LIVE_POSITIVE", "net_edge_per_contract": 90, "min_quote_size": 2},
            {"sample_status": "LIVE_POSITIVE", "net_edge_per_contract": 80, "min_quote_size": 1},
            {"sample_status": "LIVE_NONPOSITIVE", "net_edge_per_contract": -5, "min_quote_size": 2},
            {"sample_status": "LIVE_POSITIVE", "net_edge_per_contract": 20, "min_quote_size": 1},
        ]
        r = mod.summarize_samples(samples, 0.5)
        self.assertEqual(4, r["samples_live_positive"])
        self.assertAlmostEqual(1.0, r["longest_live_positive_run_sec"])
        self.assertAlmostEqual(20.0, r["min_live_positive_edge_per_contract"])
        self.assertAlmostEqual(1.0, r["min_live_positive_quote_size"])


if __name__ == "__main__":
    unittest.main()
