import importlib.util
from pathlib import Path
import unittest

import pandas as pd

SPEC = importlib.util.spec_from_file_location("option_arbitrage_scan", Path("scripts/option_arbitrage_scan.py"))
scanner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scanner)


class ArbitrageScanTest(unittest.TestCase):
    def test_normalize_jpx_style_columns(self):
        raw = pd.DataFrame([{
            "原資産コード": "7203", "満期日": "2026-12-18", "プット・コール": "コール",
            "権利行使価格": "2500", "買気配": "101", "売気配": "102", "プレミアム終値": "101.5",
            "IV": "25", "対象証券終値": "2600",
        }])
        result = scanner.normalize_columns(raw)
        self.assertEqual(result.loc[0, "option_type"], "C")
        self.assertEqual(result.loc[0, "strike"], 2500)

    def test_detects_each_static_check(self):
        rows = [
            # crossed spread and butterfly violation (middle value too high)
            {"underlying":"A", "expiry":"2026-12-18", "option_type":"C", "strike":90, "bid":11, "ask":10, "settlement":12, "iv":20, "spot":100},
            {"underlying":"A", "expiry":"2026-12-18", "option_type":"C", "strike":100, "bid":6, "ask":7, "settlement":11, "iv":20, "spot":100},
            {"underlying":"A", "expiry":"2026-12-18", "option_type":"C", "strike":110, "bid":2, "ask":3, "settlement":2, "iv":20, "spot":100},
            # pair produces parity violation
            {"underlying":"B", "expiry":"2026-12-18", "option_type":"C", "strike":100, "bid":9, "ask":10, "settlement":10, "iv":20, "spot":100},
            {"underlying":"B", "expiry":"2026-12-18", "option_type":"P", "strike":100, "bid":1, "ask":2, "settlement":1, "iv":20, "spot":100},
            # decreasing total variance across maturities
            {"underlying":"C", "expiry":"2026-06-18", "option_type":"P", "strike":100, "bid":1, "ask":2, "settlement":1, "iv":60, "spot":100},
            {"underlying":"C", "expiry":"2026-12-18", "option_type":"P", "strike":100, "bid":1, "ask":2, "settlement":1, "iv":20, "spot":100},
        ]
        findings = scanner.scan(pd.DataFrame(rows), as_of=pd.Timestamp("2026-01-01"))
        self.assertTrue({"crossed_spread", "butterfly_convexity", "put_call_parity", "calendar_total_variance"}.issubset(set(findings.check)))


if __name__ == "__main__":
    unittest.main()
