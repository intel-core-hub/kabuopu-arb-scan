import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ibkr_monitor_findings_phase10", ROOT / "scripts" / "ibkr_monitor_findings.py")
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class Phase4EnrichmentTests(unittest.TestCase):
    def test_live_sample_contains_per_leg_executable_snapshots(self):
        legs = mod.parse_legs_json(json.dumps([
            {"action": "BUY", "option_type": "C", "strike": 100, "qty": 1},
            {"action": "SELL", "option_type": "C", "strike": 200, "qty": 1},
        ]))
        quotes = {
            ("C", 100.0): mod.StreamQuote(
                bid=9, ask=10, bid_size=3, ask_size=4, market_data_type=1,
                bid_update_mono=100.0, ask_update_mono=100.1,
                bid_size_update_mono=100.0, ask_size_update_mono=100.2,
            ),
            ("C", 200.0): mod.StreamQuote(
                bid=4, ask=5, bid_size=6, ask_size=7, market_data_type=1,
                bid_update_mono=100.3, ask_update_mono=100.0,
                bid_size_update_mono=100.4, ask_size_update_mono=100.0,
            ),
        }
        out = mod.sample_candidate(
            legs, quotes, floor_pv_per_share=10, lot_size=100,
            fee_per_contract_leg=0, now_mono=100.5, max_quote_age_sec=3.0,
        )
        self.assertEqual(out["sample_status"], "LIVE_POSITIVE")
        snaps = out["leg_snapshots"]
        self.assertEqual(len(snaps), 2)
        self.assertEqual(snaps[0]["executable_side"], "ASK")
        self.assertEqual(snaps[0]["executable_price"], 10.0)
        self.assertEqual(snaps[0]["executable_size"], 4.0)
        self.assertEqual(snaps[1]["executable_side"], "BID")
        self.assertEqual(snaps[1]["executable_price"], 4.0)
        self.assertEqual(snaps[1]["market_data_type_name"], "live")


if __name__ == "__main__":
    unittest.main()
