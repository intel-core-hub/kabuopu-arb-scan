import importlib.util
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


fetch = load("fetch_kabuopu_quotes", "scripts/fetch_kabuopu_quotes.py")
scanmod = load("quote_arbitrage_scan", "scripts/quote_arbitrage_scan.py")


HTML = r"""
<html><body>
<div>最終更新時刻：2026/09/11 14:45</div>
<div>取引日 2026/09/11　取引最終日 2026/10/08</div>
<table>
<tr><th>銘柄</th><th>現在値</th><th>前日比</th><th>HV</th><th>売買単位</th></tr>
<tr><td>7203 トヨタ自動車</td><td>3,150.0</td><td>+10</td><td>20.1%</td><td>100株</td></tr>
</table>
<table>
<tr><th colspan="8">CALL</th><th>権利行使価格</th><th colspan="8">PUT</th></tr>
<tr>
<td>155.0</td><td>120</td><td>10</td><td>22.1%<br>21.9%</td><td>152.0 (30)<br>150.0 (40)</td><td>22.0%</td><td>+2.0</td><td>151.0</td>
<td>3,000</td>
<td>10.0</td><td>-1.0</td><td>23.0%</td><td>12.0 (50)<br>10.0 (60)</td><td>23.2%<br>22.8%</td><td>5</td><td>90</td><td>11.0</td>
</tr>
<tr>
<td>80.0</td><td>100</td><td>8</td><td>21.8%<br>21.5%</td><td>79.0 (25)<br>77.0 (35)</td><td>21.6%</td><td>+1.0</td><td>78.0</td>
<td>3,100</td>
<td>35.0</td><td>+1.0</td><td>22.4%</td><td>37.0 (45)<br>34.0 (55)</td><td>22.6%<br>22.2%</td><td>4</td><td>80</td><td>36.0</td>
</tr>
</table>
</body></html>
"""


class ParserTests(unittest.TestCase):
    def test_parse_board_to_long_rows(self):
        rows = fetch.parse_quote_board(HTML, "7203", 0, "fixture")
        self.assertEqual(4, len(rows))
        c = rows[0]
        self.assertEqual("C", c["option_type"])
        self.assertEqual(3000.0, c["strike"])
        self.assertEqual(150.0, c["bid"])
        self.assertEqual(152.0, c["ask"])
        self.assertEqual(40, c["bid_size"])
        self.assertEqual(30, c["ask_size"])
        self.assertEqual(21.9, c["bid_iv"])
        self.assertEqual(22.1, c["ask_iv"])
        self.assertEqual("2026-10-08", c["expiry"])
        self.assertEqual("2026-09-11 14:45", c["snapshot_time"])
        self.assertEqual(3150.0, c["spot_ref"])
        self.assertEqual(100, c["lot_size"])
        p = rows[1]
        self.assertEqual("P", p["option_type"])
        self.assertEqual(10.0, p["bid"])
        self.assertEqual(12.0, p["ask"])

    def test_reject_non_board_html(self):
        with self.assertRaises(ValueError):
            fetch.parse_quote_board("<html><body>login</body></html>", "7203", 0)


class ScannerTests(unittest.TestCase):
    def base_row(self, typ, strike, bid, ask, bid_size=10, ask_size=10):
        return {
            "snapshot_time": "2026-09-11 14:45", "underlying": "TEST", "expiry": "2026-10-08",
            "option_type": typ, "strike": strike, "bid": bid, "ask": ask,
            "bid_size": bid_size, "ask_size": ask_size, "lot_size": 100,
        }

    def test_long_box_detected_and_fee_applied(self):
        # Long box at 100/200 costs 80/share and pays 100/share.
        rows = [
            self.base_row("C", 100, 99, 100),
            self.base_row("C", 200, 40, 41),
            self.base_row("P", 100, 5, 6),
            self.base_row("P", 200, 24, 25),
        ]
        out = scanmod.scan(pd.DataFrame(rows), rate=0.0, fee_per_contract_leg=100.0,
                           as_of=date(2026, 9, 11))
        boxes = out[out["check"] == "long_box"]
        self.assertFalse(boxes.empty)
        # Gross = (100 - (100-40+25-5))*100 = 2000 JPY; fees=400.
        self.assertAlmostEqual(1600.0, float(boxes.iloc[0]["net_edge_per_contract"]))

    def test_call_vertical_negative_debit_detected(self):
        rows = [
            self.base_row("C", 100, 8, 10),
            self.base_row("C", 110, 12, 13),
        ]
        out = scanmod.scan(pd.DataFrame(rows), as_of=date(2026, 9, 11))
        hit = out[out["check"] == "vertical_monotonicity"]
        self.assertEqual(1, len(hit))
        self.assertAlmostEqual(200.0, float(hit.iloc[0]["gross_edge_per_contract"]))

    def test_equal_spaced_butterfly_detected(self):
        rows = [
            self.base_row("C", 100, 19, 20, ask_size=9),
            self.base_row("C", 110, 14, 15, bid_size=12),
            self.base_row("C", 120, 4, 5, ask_size=8),
        ]
        out = scanmod.scan(pd.DataFrame(rows), as_of=date(2026, 9, 11))
        hit = out[out["check"] == "butterfly_convexity"]
        self.assertEqual(1, len(hit))
        self.assertAlmostEqual(300.0, float(hit.iloc[0]["gross_edge_per_contract"]))
        self.assertAlmostEqual(6.0, float(hit.iloc[0]["min_quote_size"]))


if __name__ == "__main__":
    unittest.main()
