"""Unit checks for forward_curve.py (stdlib unittest; run: python3 -m unittest test_forward_curve)."""
import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

import forward_curve as fc


class Helpers(unittest.TestCase):
    def test_bucket_months(self):
        self.assertIsNone(fc.bucket_months(0))
        self.assertIsNone(fc.bucket_months(""))
        self.assertEqual(fc.bucket_months(1), 3)
        self.assertEqual(fc.bucket_months(3), 3)
        self.assertEqual(fc.bucket_months(6), 6)
        self.assertEqual(fc.bucket_months(9), 12)
        self.assertEqual(fc.bucket_months(12), 12)
        self.assertEqual(fc.bucket_months(18), 18)
        self.assertEqual(fc.bucket_months(24), 24)
        self.assertEqual(fc.bucket_months(36), 36)
        self.assertEqual(fc.bucket_months(48), 60)
        self.assertEqual(fc.bucket_months(60), 60)

    def test_prepay_normalise_raises_price_for_prepaid_quotes(self):
        self.assertEqual(fc.prepay_normalise(5.0, 0), 5.0)
        self.assertAlmostEqual(fc.prepay_normalise(5.0, 100), 5.0 / 0.94)
        self.assertGreater(fc.prepay_normalise(5.0, 30), 5.0)
        self.assertEqual(fc.prepay_normalise(5.0, "bad"), 5.0)

    def test_weighted_median(self):
        self.assertEqual(fc.weighted_median([1, 2, 3], [1, 1, 1]), 2)
        self.assertEqual(fc.weighted_median([1, 2, 3], [1, 1, 10]), 3)


class Build(unittest.TestCase):
    def _write(self, tmp, name, header, rows):
        p = Path(tmp) / name
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        return p

    def test_suppression_and_marks(self):
        with tempfile.TemporaryDirectory() as tmp:
            intel = self._write(tmp, "intel.csv",
                ["message_ts", "message_date", "gpu_model", "price_per_gpu_hour_usd", "term_months",
                 "prepay_pct", "provider_type", "provider_name", "notes"],
                [["a", "2026-09-01", "B300", "4.0", "36", "0", "neocloud", "X", ""],
                 ["b", "2026-08-15", "B300", "4.2", "36", "0", "neocloud", "Y", ""],
                 ["c", "2026-08-01", "B300", "4.4", "36", "0", "hyperscaler", "Z", ""],
                 ["d", "2026-08-01", "H100", "2.0", "12", "0", "hyperscaler", "Z", ""],   # alone -> suppressed
                 ["e", "2026-08-01", "B300", "40.0", "36", "0", "neocloud", "junk", ""]])  # outside band
            reserve = self._write(tmp, "reserve_tenor.csv",
                ["generated_date", "gpu", "tenor_months", "close_month", "deals", "lines", "gpus",
                 "price_lo", "price_med", "price_hi"],
                [["2026-09-15", "B300", "36", "2026-08-01", "2", "2", "512", "4.5", "4.8", "5.0"]])
            history = self._write(tmp, "history.csv",
                ["snapshot_date", "provider", "gpu_model", "consumption_type", "region", "instance_type",
                 "gpu_count", "price_per_gpu_hour_usd", "price_per_hour_usd", "data_source", "source_type",
                 "confidence", "interconnect", "form_factor"],
                [["2026-09-14", "nebius", "B300", "committed_3yr", "", "", "8", "4.55", "", "", "", "", "", ""],
                 ["2026-09-14", "aws", "B300", "reserved_3yr", "", "", "8", "9.0", "", "", "", "", "", ""]])
            res = fc.build(date(2026, 9, 15), intel=intel, reserve=reserve, history=history)
        b300_36 = next(e for e in res["marks"] if e["tier"] == "B300" and e["tenor_months"] == 36)
        self.assertTrue(b300_36["has_mark"])
        self.assertEqual(b300_36["n_obs"], 4)            # junk price excluded by the sanity band
        self.assertEqual(b300_36["n_bid"], 3)
        self.assertEqual(b300_36["n_ask"], 1)
        self.assertEqual(b300_36["ask_deals"], 2)
        self.assertTrue(4.0 <= b300_36["mark"] <= 4.8)
        self.assertEqual(b300_36["list_nebius"], 4.55)
        self.assertEqual(b300_36["list_hyperscaler_min"], 9.0)
        self.assertGreater(b300_36["spread_pct"], 0)
        h100_12 = next(e for e in res["marks"] if e["tier"] == "H100" and e["tenor_months"] == 12)
        self.assertFalse(h100_12["has_mark"])
        self.assertIsNone(h100_12["mark"])
        self.assertIn("insufficient_data", h100_12["reason"])
        # render paths do not blow up and the body converts to well-formed storage XML
        body = fc.render_confluence_body(res, with_images=True)
        self.assertIn("ri:attachment", body)
        from confluence_storage import to_storage, validate_xml
        self.assertIsNone(validate_xml(to_storage(body)))
        svg = fc.render_svg(res)
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("B300", fc.render_view_html(res, svg))

    def test_quarter_effects_shift_old_quotes_up_in_a_rising_market(self):
        obs = []
        for q, (m, p) in {"2026Q1": (2, 3.0), "2026Q3": (8, 4.5)}.items():
            for tier in ("B300", "B200"):
                obs.append({"tier": tier, "tenor": 36, "quarter": q, "weight": 1.0,
                            "logp0": __import__("math").log(p)})
        eff = fc.quarter_effects(obs, date(2026, 9, 15))
        self.assertAlmostEqual(eff["2026Q3"], 0.0)
        self.assertLess(eff["2026Q1"], 0.0)


if __name__ == "__main__":
    unittest.main()
