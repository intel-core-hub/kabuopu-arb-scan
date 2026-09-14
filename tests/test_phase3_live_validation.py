import importlib.util
import json
import sys
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


scanmod = load("quote_arbitrage_scan_phase3", "scripts/quote_arbitrage_scan.py")
livemod = load("ibkr_validate_findings", "scripts/ibkr_validate_findings.py")


class StructuredFindingTests(unittest.TestCase):
    def row(self, typ, strike, bid, ask, size=10):
        return {
            "snapshot_time": "2026-09-11 14:45",
            "underlying": "7203",
            "expiry": "2026-10-08",
            "option_type": typ,
            "strike": strike,
            "bid": bid,
            "ask": ask,
            "bid_size": size,
            "ask_size": size,
            "lot_size": 100,
        }

    def test_scanner_emits_machine_readable_box(self):
        rows = [
            self.row("C", 100, 99, 100),
            self.row("C", 200, 40, 41),
            self.row("P", 100, 5, 6),
            self.row("P", 200, 24, 25),
        ]
        out = scanmod.scan(
            pd.DataFrame(rows), rate=0.0, fee_per_contract_leg=100.0,
            as_of=date(2026, 9, 11),
        )
        box = out[out["check"] == "long_box"].iloc[0]
        legs = json.loads(box["legs_json"])
        self.assertEqual(4, len(legs))
        self.assertEqual("BUY", legs[0]["action"])
        self.assertEqual("C", legs[0]["option_type"])
        self.assertEqual(100.0, legs[0]["strike"])
        self.assertAlmostEqual(100.0, float(box["floor_pv_per_share"]))
        self.assertEqual(4, int(box["fee_legs"]))
        self.assertEqual(100, int(box["lot_size"]))

    def test_crossed_spread_structured_legs(self):
        rows = [self.row("P", 100, 12, 10)]
        out = scanmod.scan(pd.DataFrame(rows), as_of=date(2026, 9, 11))
        hit = out[out["check"] == "crossed_spread"].iloc[0]
        legs = json.loads(hit["legs_json"])
        self.assertEqual(["BUY", "SELL"], [x["action"] for x in legs])
        self.assertEqual(["P", "P"], [x["option_type"] for x in legs])
        self.assertEqual(0.0, float(hit["floor_pv_per_share"]))

    def test_vertical_upper_bound_has_negative_floor(self):
        rows = [
            self.row("C", 100, 30, 31),
            self.row("C", 110, 1, 2),
        ]
        out = scanmod.scan(pd.DataFrame(rows), rate=0.0, as_of=date(2026, 9, 11))
        hit = out[out["check"] == "vertical_upper_bound"].iloc[0]
        self.assertAlmostEqual(-10.0, float(hit["floor_pv_per_share"]))


class LiveRepricingTests(unittest.TestCase):
    def q(self, bid, ask, bid_size=10, ask_size=10):
        return livemod.LiveQuote(
            bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size,
            first_update_utc="2026-09-14T00:00:00.000+00:00",
            last_update_utc="2026-09-14T00:00:00.100+00:00",
        )

    def test_long_box_still_positive_live(self):
        legs = livemod.parse_legs_json(json.dumps([
            {"action": "BUY", "option_type": "C", "strike": 100, "qty": 1},
            {"action": "SELL", "option_type": "C", "strike": 200, "qty": 1},
            {"action": "BUY", "option_type": "P", "strike": 200, "qty": 1},
            {"action": "SELL", "option_type": "P", "strike": 100, "qty": 1},
        ]))
        quotes = {
            ("C", 100.0): self.q(99, 100),
            ("C", 200.0): self.q(40, 41),
            ("P", 200.0): self.q(24, 25),
            ("P", 100.0): self.q(5, 6),
        }
        r = livemod.reprice_candidate(
            legs, quotes, floor_pv_per_share=100.0,
            lot_size=100.0, fee_per_contract_leg=100.0,
        )
        self.assertAlmostEqual(80.0, r["live_net_debit_per_share"])
        self.assertAlmostEqual(2000.0, r["live_gross_edge_per_contract"])
        self.assertAlmostEqual(1600.0, r["live_net_edge_per_contract"])
        self.assertAlmostEqual(10.0, r["live_min_quote_size"])

    def test_butterfly_qty_controls_executable_size(self):
        legs = livemod.parse_legs_json(json.dumps([
            {"action": "BUY", "option_type": "C", "strike": 100, "qty": 1},
            {"action": "SELL", "option_type": "C", "strike": 110, "qty": 2},
            {"action": "BUY", "option_type": "C", "strike": 120, "qty": 1},
        ]))
        quotes = {
            ("C", 100.0): self.q(19, 20, ask_size=9),
            ("C", 110.0): self.q(14, 15, bid_size=12),
            ("C", 120.0): self.q(4, 5, ask_size=8),
        }
        r = livemod.reprice_candidate(
            legs, quotes, floor_pv_per_share=0.0,
            lot_size=100.0, fee_per_contract_leg=0.0,
        )
        self.assertAlmostEqual(-3.0, r["live_net_debit_per_share"])
        self.assertAlmostEqual(300.0, r["live_gross_edge_per_contract"])
        self.assertAlmostEqual(6.0, r["live_min_quote_size"])

    def test_missing_executable_side_rejected(self):
        legs = livemod.parse_legs_json(json.dumps([
            {"action": "BUY", "option_type": "P", "strike": 100, "qty": 1},
        ]))
        with self.assertRaisesRegex(ValueError, "missing live ask"):
            livemod.reprice_candidate(
                legs, {("P", 100.0): self.q(5, None)},
                floor_pv_per_share=0.0, lot_size=100.0, fee_per_contract_leg=0.0,
            )


class MarketDataTypeTests(unittest.TestCase):
    def test_delayed_tick_ids_populate_executable_quote(self):
        q = livemod.LiveQuote()
        self.assertTrue(livemod.apply_tick_price(q, 66, 12.5))
        self.assertTrue(livemod.apply_tick_price(q, 67, 13.0))
        self.assertTrue(livemod.apply_tick_size(q, 69, 7))
        self.assertTrue(livemod.apply_tick_size(q, 70, 9))
        self.assertEqual(12.5, q.bid)
        self.assertEqual(13.0, q.ask)
        self.assertEqual(7.0, q.bid_size)
        self.assertEqual(9.0, q.ask_size)

    def test_irrelevant_tick_does_not_update_quote(self):
        q = livemod.LiveQuote()
        self.assertFalse(livemod.apply_tick_price(q, 4, 99.0))  # last price
        self.assertFalse(livemod.apply_tick_size(q, 5, 12))  # last size
        self.assertIsNone(q.bid)
        self.assertIsNone(q.ask)

    def test_positive_edge_requires_actual_live_type_for_confirmation(self):
        live = {("C", 100.0): livemod.LiveQuote(market_data_type=1)}
        delayed = {("C", 100.0): livemod.LiveQuote(market_data_type=3)}
        mixed = {
            ("C", 100.0): livemod.LiveQuote(market_data_type=1),
            ("C", 110.0): livemod.LiveQuote(market_data_type=3),
        }
        unknown = {("C", 100.0): livemod.LiveQuote()}
        self.assertEqual("CONFIRMED_CANDIDATE", livemod.positive_validation_status(live))
        self.assertEqual("NONLIVE_CANDIDATE", livemod.positive_validation_status(delayed))
        self.assertEqual("NONLIVE_CANDIDATE", livemod.positive_validation_status(mixed))
        self.assertEqual("UNVERIFIED_DATA_TYPE", livemod.positive_validation_status(unknown))

    def test_market_data_type_names(self):
        self.assertEqual("live", livemod.market_data_type_name(1))
        self.assertEqual("delayed", livemod.market_data_type_name(3))
        self.assertEqual("unknown", livemod.market_data_type_name(None))


if __name__ == "__main__":
    unittest.main()
