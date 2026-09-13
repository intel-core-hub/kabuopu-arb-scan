import importlib.util
from pathlib import Path
import unittest

import pandas as pd

SPEC = importlib.util.spec_from_file_location("fetch_jpx_option_data", Path("scripts/fetch_jpx_option_data.py"))
fetcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetcher)


class FetchJpxOptionDataTest(unittest.TestCase):
    def test_read_cp932_csv(self):
        csv = "商品種別,銘柄名\n有価証券オプション,ABC\n".encode("cp932")
        result = fetcher._read_csv(csv, "sample.csv")
        self.assertEqual(result.loc[0, "商品種別"], "有価証券オプション")

    def test_filter_kabuopu_prefers_product_type(self):
        frame = pd.DataFrame({"商品種別": ["有価証券オプション", "指数オプション"], "銘柄名": ["ABC", "日経225"]})
        result = fetcher.filter_kabuopu(frame)
        self.assertEqual(result["銘柄名"].tolist(), ["ABC"])


if __name__ == "__main__":
    unittest.main()
